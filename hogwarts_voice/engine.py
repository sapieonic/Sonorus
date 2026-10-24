"""Speech engines (OmniVoice in-process, Fish Speech over HTTP), the voice vault and the Pensieve (history)."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .fish import FISH_PARAM_DEFAULTS, FishError, FishSpeech

log = logging.getLogger("sonorus")

# Less fragmentation on small GPUs; must be set before CUDA initialises.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
# Keep CLI output clean: no "Fetching files" / "Loading weights" progress bars.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
SMALL_GPU_BYTES = 8 * 1024**3
# The model loads on a background thread while web requests (e.g. /api/options) also import omnivoice;
# two threads importing the package for the first time at once can hit Python's _DeadlockError.
_OMNIVOICE_IMPORT_LOCK = threading.Lock()

SAMPLE_RATE = 24000
AUDIO_FORMATS = ("wav", "flac", "ogg")
ENGINES = ("omnivoice", "fish")

# Every OmniVoiceGenerationConfig knob, with UI metadata. Defaults mirror OmniVoice's.
GENERATION_PARAMS: list[dict[str, Any]] = [
    {"key": "num_step", "label": "Diffusion steps", "type": "int", "default": 32, "min": 4, "max": 64, "step": 1,
     "group": "Decoding", "help": "Iterative unmasking steps. More steps, better quality, slower. 16 is a good fast setting."},
    {"key": "guidance_scale", "label": "Guidance scale", "type": "float", "default": 2.0, "min": 0.0, "max": 6.0, "step": 0.1,
     "group": "Decoding", "help": "Classifier-free guidance. Higher sticks closer to the text and voice prompt."},
    {"key": "t_shift", "label": "Time-step shift", "type": "float", "default": 0.1, "min": 0.01, "max": 1.0, "step": 0.01,
     "group": "Decoding", "help": "Noise-schedule shift. Smaller values emphasise the earlier decoding steps."},
    {"key": "denoise", "label": "Denoise", "type": "bool", "default": True,
     "group": "Decoding", "help": "Ask the model for cleaner speech (prepends the <|denoise|> token)."},
    {"key": "position_temperature", "label": "Position temperature", "type": "float", "default": 5.0, "min": 0.0, "max": 10.0, "step": 0.1,
     "group": "Sampling", "help": "Randomness when picking which positions to unmask. 0 = deterministic."},
    {"key": "class_temperature", "label": "Token temperature", "type": "float", "default": 0.0, "min": 0.0, "max": 2.0, "step": 0.05,
     "group": "Sampling", "help": "Randomness when sampling tokens. 0 = greedy."},
    {"key": "layer_penalty_factor", "label": "Layer penalty", "type": "float", "default": 5.0, "min": 0.0, "max": 10.0, "step": 0.1,
     "group": "Sampling", "help": "Pushes lower codebook layers to unmask first."},
    {"key": "preprocess_prompt", "label": "Clean reference audio", "type": "bool", "default": True,
     "group": "Processing", "help": "Strip long silences from the reference clip and punctuate its transcript."},
    {"key": "postprocess_output", "label": "Trim output silences", "type": "bool", "default": True,
     "group": "Processing", "help": "Remove long silences from the generated audio. Turn off if you need an exact duration."},
    {"key": "pad_duration", "label": "Edge padding (s)", "type": "float", "default": 0.1, "min": 0.0, "max": 1.0, "step": 0.05,
     "group": "Processing", "help": "Silence added to each end of the output. 0 disables."},
    {"key": "fade_duration", "label": "Fade in/out (s)", "type": "float", "default": 0.1, "min": 0.0, "max": 1.0, "step": 0.05,
     "group": "Processing", "help": "Fade curve at each end of the output. 0 disables."},
    {"key": "audio_chunk_duration", "label": "Chunk length (s)", "type": "float", "default": 15.0, "min": 5.0, "max": 60.0, "step": 1,
     "group": "Long-form", "help": "For long text, each chunk produces roughly this many seconds of audio."},
    {"key": "audio_chunk_threshold", "label": "Chunking threshold (s)", "type": "float", "default": 30.0, "min": 10.0, "max": 120.0, "step": 1,
     "group": "Long-form", "help": "Text expected to run longer than this is split into chunks."},
]
PARAM_DEFAULTS = {p["key"]: p["default"] for p in GENERATION_PARAMS}

NONVERBAL_TAGS = [
    ("[laughter]", "Laugh"), ("[sigh]", "Sigh"), ("[confirmation-en]", "Mm-hm"), ("[question-en]", "Huh?"),
    ("[question-ah]", "Ah?"), ("[question-oh]", "Oh?"), ("[question-ei]", "Ei?"), ("[question-yi]", "Yi?"),
    ("[surprise-ah]", "Ah!"), ("[surprise-oh]", "Oh!"), ("[surprise-wa]", "Wa!"), ("[surprise-yo]", "Yo!"),
    ("[dissatisfaction-hnn]", "Hnn"),
]

DESIGN_ATTRIBUTES = {
    "Gender": ["male", "female"],
    "Age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
    "Pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
    "Style": ["whisper"],
    "Accent (English text)": ["american accent", "british accent", "australian accent", "canadian accent",
                              "indian accent", "chinese accent", "korean accent", "japanese accent",
                              "portuguese accent", "russian accent"],
    "Dialect (Chinese text)": ["河南话", "陕西话", "四川话", "贵州话", "云南话", "桂林话", "济南话",
                               "石家庄话", "甘肃话", "宁夏话", "青岛话", "东北话"],
}


class SonorusError(Exception):
    """An error worth showing to the user as-is."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "voice"


def _import_omnivoice() -> None:
    """Finish the first import of omnivoice on one thread before any other thread touches it."""
    with _OMNIVOICE_IMPORT_LOCK:
        import omnivoice  # noqa: F401


def list_languages() -> list[dict[str, str]]:
    _import_omnivoice()
    from omnivoice.utils.lang_map import LANG_NAME_TO_ID

    langs = [{"id": code, "name": name.title()} for name, code in LANG_NAME_TO_ID.items()]
    return sorted(langs, key=lambda x: x["name"])


def normalization_available() -> bool:
    try:
        import tn  # noqa: F401  (WeTextProcessing)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Requests & results
# ---------------------------------------------------------------------------


@dataclass
class SpeakRequest:
    text: str
    voice: str | None = None  # vault voice id or name
    ref_audio: str | None = None  # path to a reference clip (one-off clone)
    ref_text: str | None = None
    instruct: str | None = None
    language: str | None = None
    speed: float | None = None
    duration: float | None = None
    seed: int | None = None
    normalize_text: bool = False
    format: str = "wav"
    params: dict[str, Any] = field(default_factory=dict)
    engine: str = "omnivoice"

    @property
    def mode(self) -> str:
        if self.voice or self.ref_audio:
            return "clone"
        if self.instruct:
            return "design"
        return "auto"


# ---------------------------------------------------------------------------
# Vault (saved voices) and Pensieve (history)
# ---------------------------------------------------------------------------


class Vault:
    """Saved cloned voices. Each voice is a folder: meta.json, prompt.pt, reference.wav."""

    def __init__(self, root: Path):
        self.root = root

    def _dir(self, voice_id: str) -> Path:
        return self.root / voice_id

    def list(self) -> list[dict]:
        voices = []
        for meta in self.root.glob("*/meta.json"):
            try:
                voices.append(json.loads(meta.read_text()))
            except Exception:
                log.warning("Skipping unreadable voice %s", meta.parent.name)
        return sorted(voices, key=lambda v: v.get("created", ""), reverse=True)

    def get(self, key: str) -> dict:
        """Look up by id, or by name (case-insensitive)."""
        d = self._dir(_slug(key))
        if (d / "meta.json").exists():
            return json.loads((d / "meta.json").read_text())
        for v in self.list():
            if v["name"].lower() == key.lower():
                return v
        names = ", ".join(v["name"] for v in self.list()) or "none yet"
        raise SonorusError(f"No voice called '{key}' in the vault. Known voices: {names}")

    def prompt_path(self, voice_id: str) -> Path:
        return self._dir(voice_id) / "prompt.pt"

    def reference_path(self, voice_id: str) -> Path:
        return self._dir(voice_id) / "reference.wav"

    def new_id(self, name: str) -> str:
        base = _slug(name)
        voice_id, n = base, 2
        while self._dir(voice_id).exists():
            voice_id, n = f"{base}-{n}", n + 1
        return voice_id

    def save(self, meta: dict) -> None:
        d = self._dir(meta["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    def delete(self, key: str) -> dict:
        meta = self.get(key)
        shutil.rmtree(self._dir(meta["id"]))
        return meta


class Pensieve:
    """Generation history: <id>.json + <id>.<ext> per memory."""

    def __init__(self, root: Path, limit: int):
        self.root = root
        self.limit = limit

    def list(self) -> list[dict]:
        items = []
        for meta in self.root.glob("*.json"):
            try:
                items.append(json.loads(meta.read_text()))
            except Exception:
                continue
        return sorted(items, key=lambda m: m.get("created", ""), reverse=True)

    def get(self, item_id: str) -> dict:
        path = self.root / f"{item_id}.json"
        if not path.exists():
            raise SonorusError(f"No memory with id '{item_id}' in the Pensieve.")
        return json.loads(path.read_text())

    def audio_path(self, item: dict) -> Path:
        return self.root / item["file"]

    def add(self, item: dict) -> None:
        (self.root / f"{item['id']}.json").write_text(json.dumps(item, indent=2, ensure_ascii=False))
        for old in self.list()[self.limit:]:
            self.delete(old["id"])

    def delete(self, item_id: str) -> None:
        item = self.get(item_id)
        self.audio_path(item).unlink(missing_ok=True)
        (self.root / f"{item_id}.json").unlink(missing_ok=True)

    def clear(self) -> int:
        items = self.list()
        for item in items:
            self.delete(item["id"])
        return len(items)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self.vault = Vault(self.settings.voices_dir)
        self.pensieve = Pensieve(self.settings.history_dir, self.settings.history_limit)
        self.model = None
        self.state = "idle"  # idle | loading | ready | error
        self.message = "The model has not been summoned yet."
        self.device: str | None = None
        self.dtype: str | None = None
        self.busy = False
        self._load_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        s = self.settings
        self.fish = FishSpeech(s.fish_url, s.fish_api_key, s.fish_model) if s.fish_url else None

    # -- loading ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "device": self.device,
            "dtype": self.dtype,
            "model": self.settings.model,
            "busy": self.busy,
            "sample_rate": SAMPLE_RATE,
            "engines": {
                "omnivoice": {"state": self.state, "message": self.message, "model": self.settings.model},
                "fish": self.fish.status() if self.fish else {
                    "state": "disabled", "message": "Fish Speech is not set up (set SONORUS_FISH_URL)."},
            },
        }

    def _pick_device_dtype(self):
        import torch

        device = self.settings.device
        if device == "auto":
            device = "cpu"
            if torch.cuda.is_available():
                major, minor = torch.cuda.get_device_capability(0)
                arches = torch.cuda.get_arch_list()
                # sm_XY binaries run on any sm_XZ device with Z >= Y (e.g. sm_60 covers a Pascal sm_61).
                if any(f"sm_{major}{m}" in arches for m in range(minor + 1)) or any(a.startswith("compute_") for a in arches):
                    device = "cuda:0"
                else:
                    log.warning("GPU sm_%s%s is not supported by this PyTorch build; using CPU.", major, minor)
            elif hasattr(torch, "xpu") and torch.xpu.is_available():
                device = "xpu"
            elif torch.backends.mps.is_available():
                device = "mps"

        dtype_name = self.settings.dtype
        if dtype_name == "auto":
            dtype_name = "float32" if device == "cpu" else "float16"
        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}[dtype_name]

        small_gpu = device.startswith("cuda") and torch.cuda.get_device_properties(device).total_memory < SMALL_GPU_BYTES
        # On small GPUs keep Whisper (~1.6 GB) and the fp32 audio codec (~0.8 GB + activations) on the CPU.
        asr_device = self.settings.asr_device
        if asr_device == "auto":
            asr_device = "cpu" if small_gpu else device
        codec_device = self.settings.codec_device
        if codec_device == "auto":
            codec_device = "cpu" if small_gpu else device
        return device, dtype, dtype_name, asr_device, codec_device

    def load(self) -> None:
        """Load the model (blocking). Safe to call repeatedly."""
        with self._load_lock:
            if self.state == "ready":
                return
            self.state = "loading"
            self.message = "Summoning OmniVoice into memory…"
            if not self._is_cached():
                self.message = "Fetching OmniVoice from Hugging Face (one-time download of about 3.3 GB)…"
            try:
                import torch
                import transformers
                _import_omnivoice()
                from omnivoice import OmniVoice

                transformers.utils.logging.disable_progress_bar()
                # Whisper auto-transcription spams deprecation warnings that need no action from the user.
                transformers.utils.logging.set_verbosity_error()
                device, dtype, dtype_name, asr_device, codec_device = self._pick_device_dtype()
                self.device, self.dtype = device, dtype_name
                if device == "mps":
                    # transformers copies weights to the device from worker threads; on MPS that hangs forever.
                    os.environ.setdefault("HF_DEACTIVATE_ASYNC_LOAD", "1")
                log.info("Loading %s on %s (%s), codec on %s, ASR on %s",
                         self.settings.model, device, dtype_name, codec_device, asr_device)
                started = time.time()
                kwargs: dict[str, Any] = {"device_map": device, "dtype": dtype, "asr_device": asr_device}
                if self.settings.asr_model:
                    kwargs["asr_model_name"] = self.settings.asr_model
                self.model = OmniVoice.from_pretrained(self.settings.model, **kwargs)
                if codec_device != device:
                    self.model.audio_tokenizer.to(codec_device)
                    self._free_gpu()
                if device.startswith("cuda") and torch.cuda.get_device_capability(device)[0] < 7:
                    # On pre-Volta GPUs PyTorch's memory-efficient SDPA kernel is very slow for this model
                    # (~70% of GPU time); the plain matmul path is ~2.5x faster for the same memory.
                    torch.backends.cuda.enable_mem_efficient_sdp(False)
                    log.info("Pre-Volta GPU: using math attention for speed.")
                self.state = "ready"
                self.message = f"Wand ready on {device} ({dtype_name}), loaded in {time.time() - started:.0f}s."
                log.info(self.message)
            except Exception as exc:  # surface any load failure in the UI
                log.exception("Model load failed")
                self.state = "error"
                self.message = f"The summoning failed: {exc}"
                raise

    def _is_cached(self) -> bool:
        if Path(self.settings.model).is_dir():
            return True
        try:
            from huggingface_hub import try_to_load_from_cache

            return isinstance(try_to_load_from_cache(self.settings.model, "model.safetensors"), str)
        except Exception:
            return False

    def load_in_background(self) -> None:
        threading.Thread(target=self._safe_load, daemon=True, name="sonorus-loader").start()

    def _safe_load(self) -> None:
        try:
            self.load()
        except Exception:
            pass

    @staticmethod
    def _free_gpu() -> None:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _oom_error(self) -> "SonorusError":
        self._free_gpu()
        return SonorusError(
            "The GPU ran out of memory. Try shorter text, a shorter reference clip, a smaller "
            "'Chunk length' in the Restricted Section, or restart with SONORUS_DEVICE=cpu."
        )

    def _require_model(self):
        if self.state != "ready":
            self.load()
        return self.model

    # -- voices -------------------------------------------------------------

    def create_voice(self, name: str, ref_audio: str, ref_text: str | None = None,
                     description: str = "", preprocess: bool = True) -> dict:
        name = name.strip()
        if not name:
            raise SonorusError("A voice needs a name.")
        if not Path(ref_audio).exists():
            raise SonorusError(f"Reference audio not found: {ref_audio}")
        model = self._require_model()
        voice_id = self.vault.new_id(name)
        with self._gen_lock:
            self.busy = True
            try:
                prompt = model.create_voice_clone_prompt(
                    ref_audio=ref_audio, ref_text=(ref_text or "").strip() or None, preprocess_prompt=preprocess
                )
            except ValueError as exc:
                raise SonorusError(str(exc)) from exc
            except Exception as exc:
                if type(exc).__name__ == "OutOfMemoryError":
                    raise self._oom_error() from exc
                raise
            finally:
                self.busy = False
        d = self.vault.root / voice_id
        d.mkdir(parents=True, exist_ok=True)
        prompt.save(str(d / "prompt.pt"))
        self._copy_reference(ref_audio, d / "reference.wav")
        meta = {
            "id": voice_id,
            "name": name,
            "description": description.strip(),
            "ref_text": prompt.ref_text,
            "auto_transcribed": not (ref_text or "").strip(),
            "created": _now(),
        }
        self.vault.save(meta)
        return meta

    def _copy_reference(self, src: str, dst: Path) -> None:
        """Keep a 24 kHz mono WAV of the reference so the UI can play it back."""
        try:
            import soundfile as sf
            _import_omnivoice()
            from omnivoice.utils.audio import load_audio

            sf.write(str(dst), load_audio(src, SAMPLE_RATE)[0], SAMPLE_RATE)
        except Exception:
            log.warning("Could not store a playable copy of %s", src)

    # -- generation ---------------------------------------------------------

    def _transcribe(self, ref_audio: str) -> str:
        """Transcribe a reference clip with OmniVoice's Whisper (Fish Speech needs a transcript to clone)."""
        _import_omnivoice()
        from omnivoice.utils.audio import load_audio

        model = self._require_model()
        with self._gen_lock:
            self.busy = True
            try:
                if model._asr_pipe is None:
                    model.load_asr_model()
                return model.transcribe((load_audio(ref_audio, SAMPLE_RATE), SAMPLE_RATE))
            finally:
                self.busy = False

    def speak(self, req: SpeakRequest) -> dict:
        text = req.text.strip()
        if not text:
            raise SonorusError("Nothing to say: the incantation is empty.")
        fmt = req.format.lower()
        if fmt not in AUDIO_FORMATS:
            raise SonorusError(f"Unsupported format '{req.format}'. Choose one of: {', '.join(AUDIO_FORMATS)}")
        if req.engine not in ENGINES:
            raise SonorusError(f"Unknown engine '{req.engine}'. Choose one of: {', '.join(ENGINES)}")
        if req.ref_audio and not Path(req.ref_audio).exists():
            raise SonorusError(f"Reference audio not found: {req.ref_audio}")
        voice_meta = self.vault.get(req.voice) if req.voice else None
        seed = req.seed if req.seed is not None else random.randint(0, 2**31 - 1)

        generate = self._generate_fish if req.engine == "fish" else self._generate_omnivoice
        audio, sr, elapsed, details = generate(req, text, voice_meta, seed)

        import soundfile as sf

        item_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        filename = f"{item_id}.{fmt}"
        sf.write(str(self.pensieve.root / filename), audio, sr)
        seconds = len(audio) / sr
        item = {
            "id": item_id,
            "file": filename,
            "created": _now(),
            "text": text,
            "engine": req.engine,
            "mode": req.mode,
            "voice": voice_meta["id"] if voice_meta else None,
            "voice_name": voice_meta["name"] if voice_meta else None,
            **details,
            "seed": seed,
            "format": fmt,
            "seconds": round(seconds, 2),
            "elapsed": round(elapsed, 2),
            "rtf": round(elapsed / seconds, 3) if seconds else None,
        }
        self.pensieve.add(item)
        return item

    def _generate_omnivoice(self, req: SpeakRequest, text: str, voice_meta: dict | None, seed: int):
        unknown = set(req.params) - set(PARAM_DEFAULTS)
        if unknown:
            raise SonorusError(f"Unknown OmniVoice option(s): {', '.join(sorted(unknown))}")
        if req.normalize_text and not normalization_available():
            raise SonorusError("Number normalisation needs the extra package: pip install 'omnivoice[tn]'")

        _import_omnivoice()
        from omnivoice import OmniVoiceGenerationConfig, VoiceClonePrompt
        from omnivoice.utils.common import fix_random_seed

        model = self._require_model()
        params = {**PARAM_DEFAULTS, **req.params}

        kwargs: dict[str, Any] = {
            "text": text,
            "language": req.language or None,
            "instruct": (req.instruct or "").strip() or None,
            "speed": req.speed,
            "duration": req.duration,
            "normalize_text": req.normalize_text,
            "generation_config": OmniVoiceGenerationConfig.from_dict(params),
        }
        if voice_meta:
            kwargs["voice_clone_prompt"] = VoiceClonePrompt.load(str(self.vault.prompt_path(voice_meta["id"])))
        elif req.ref_audio:
            kwargs["ref_audio"] = req.ref_audio
            kwargs["ref_text"] = (req.ref_text or "").strip() or None

        with self._gen_lock:
            self.busy = True
            try:
                fix_random_seed(seed)
                started = time.time()
                audio = model.generate(**kwargs)[0]
                elapsed = time.time() - started
            except ValueError as exc:
                raise SonorusError(str(exc)) from exc
            except Exception as exc:
                if type(exc).__name__ == "OutOfMemoryError":
                    raise self._oom_error() from exc
                raise
            finally:
                self.busy = False
                self._free_gpu()

        details = {
            "ref_text": kwargs.get("ref_text"),
            "instruct": kwargs["instruct"],
            "language": req.language,
            "speed": req.speed,
            "duration_target": req.duration,
            "normalize_text": req.normalize_text,
            "params": {k: v for k, v in params.items() if v != PARAM_DEFAULTS[k]},
            "device": self.device,
        }
        return audio, model.sampling_rate or SAMPLE_RATE, elapsed, details

    def _generate_fish(self, req: SpeakRequest, text: str, voice_meta: dict | None, seed: int):
        if not self.fish:
            raise SonorusError("Fish Speech is not set up on this server (set SONORUS_FISH_URL).")
        unsupported = [name for name, value in (
            ("voice design / style hints", (req.instruct or "").strip()), ("language", req.language),
            ("speed", req.speed), ("exact duration", req.duration)) if value]
        if unsupported:
            raise SonorusError(f"Fish Speech doesn't support {', '.join(unsupported)}. "
                               "Steer it with inline tags like [whisper] in the text, or switch to OmniVoice.")
        unknown = set(req.params) - set(FISH_PARAM_DEFAULTS)
        if unknown:
            raise SonorusError(f"Unknown Fish Speech option(s): {', '.join(sorted(unknown))}")
        params = {**FISH_PARAM_DEFAULTS, **req.params}

        references, ref_text = [], None
        if voice_meta:
            clip = self.vault.reference_path(voice_meta["id"])
            if not clip.exists():
                raise SonorusError(f"'{voice_meta['name']}' has no stored reference clip, so Fish Speech can't use it.")
            references.append((clip.read_bytes(), voice_meta.get("ref_text") or ""))
        elif req.ref_audio:
            ref_text = (req.ref_text or "").strip() or self._transcribe(req.ref_audio)
            references.append((Path(req.ref_audio).read_bytes(), ref_text))

        with self._gen_lock:
            self.busy = True
            try:
                started = time.time()
                audio, sr = self.fish.tts(text, references, seed, req.normalize_text, params)
                elapsed = time.time() - started
            except FishError as exc:
                raise SonorusError(str(exc)) from exc
            finally:
                self.busy = False

        details = {
            "ref_text": ref_text,
            "instruct": None,
            "language": None,
            "speed": None,
            "duration_target": None,
            "normalize_text": req.normalize_text,
            "params": {k: v for k, v in params.items() if v != FISH_PARAM_DEFAULTS[k]},
            "device": f"fish-speech {self.fish.model}",
        }
        return audio, sr, elapsed, details


def request_from_dict(data: dict) -> SpeakRequest:
    known = {f for f in SpeakRequest.__dataclass_fields__}
    return SpeakRequest(**{k: v for k, v in data.items() if k in known})


__all__ = [
    "AUDIO_FORMATS", "DESIGN_ATTRIBUTES", "ENGINES", "Engine", "GENERATION_PARAMS", "NONVERBAL_TAGS", "PARAM_DEFAULTS",
    "SonorusError", "SpeakRequest", "list_languages", "normalization_available", "request_from_dict",
]
