"""Client for a fish-speech API server (tools/api_server.py), the second engine next to OmniVoice."""

from __future__ import annotations

import base64
import io
import re
import threading
import time
from typing import Any

FISH_PARAMS: list[dict[str, Any]] = [
    {"key": "temperature", "label": "Temperature", "type": "float", "default": 0.7, "min": 0.1, "max": 1.0, "step": 0.05,
     "group": "Sampling", "help": "Randomness of the delivery. Lower is steadier, higher is livelier."},
    {"key": "top_p", "label": "Top-p", "type": "float", "default": 0.7, "min": 0.1, "max": 1.0, "step": 0.05,
     "group": "Sampling", "help": "Nucleus sampling cut-off. Lower keeps to the most likely sounds."},
    {"key": "chunk_length", "label": "Piece length", "type": "int", "default": 300, "min": 100, "max": 1000, "step": 10,
     "group": "Long-form", "help": "Long text is split at sentence ends into pieces of about this many characters, "
                                   "spoken one by one and joined."},
    {"key": "max_new_tokens", "label": "Max frames per piece", "type": "int", "default": 1024, "min": 0, "max": 4096, "step": 64,
     "group": "Long-form", "help": "Upper bound on audio per piece; 1024 frames is about 47 s. 0 uses the model's own cap."},
]
FISH_PARAM_DEFAULTS = {p["key"]: p["default"] for p in FISH_PARAMS}


def check_params(params: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults and reject values outside FISH_PARAMS' ranges (they arrive from the public API)."""
    out = dict(FISH_PARAM_DEFAULTS)
    for p in FISH_PARAMS:
        if p["key"] not in params:
            continue
        value = params[p["key"]]
        cast = int if p["type"] == "int" else float
        if isinstance(value, bool) or not isinstance(value, (int, float)) or cast(value) != value:
            raise ValueError(f"{p['label']} must be {'a whole number' if cast is int else 'a number'}.")
        if not p["min"] <= value <= p["max"]:
            raise ValueError(f"{p['label']} must be between {p['min']} and {p['max']}.")
        out[p["key"]] = cast(value)
    return out

# Fish takes free-form inline instructions; these are the ones its docs use.
FISH_TAGS = [
    ("[pause]", "Pause"), ("[laughing]", "Laugh"), ("[whisper]", "Whisper"), ("[excited]", "Excited"),
    ("[angry]", "Angry"), ("[emphasis]", "Emphasis"),
]

PIECE_GAP_SECONDS = 0.15
_SENTENCE_END = re.compile(r"(?<=[.!?。！？…])\s+|\s*\n\s*")
_CLAUSE_END = re.compile(r"(?<=[,;:，；：])\s+")


class FishError(Exception):
    """The fish-speech server is unreachable or rejected the request."""


def split_text(text: str, limit: int) -> list[str]:
    """Split text at sentence ends into pieces of at most about `limit` characters.

    fish-speech only splits text at <|speaker:N|> tags, and a single pass stops at max_new_tokens
    (about 47 s by default), so long plain text has to be cut up here.
    """
    limit = max(int(limit), 1)
    parts: list[str] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if len(sentence) <= limit:
            parts.append(sentence)
            continue
        for clause in _CLAUSE_END.split(sentence):
            while len(clause) > limit:
                cut = clause.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                parts.append(clause[:cut])
                clause = clause[cut:].lstrip()
            parts.append(clause)
    pieces: list[str] = []
    for part in filter(None, parts):
        if pieces and len(pieces[-1]) + 1 + len(part) <= limit:
            pieces[-1] = f"{pieces[-1]} {part}"
        else:
            pieces.append(part)
    return pieces


class FishSpeech:
    def __init__(self, url: str, api_key: str | None = None, model: str = "S2-Pro"):
        import httpx

        self.url = url.rstrip("/")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # Long text can take minutes to synthesise; only connecting should fail fast.
        self.http = httpx.Client(base_url=self.url, headers=headers, timeout=httpx.Timeout(900, connect=5))
        self._status: dict | None = None
        self._checked = 0.0
        self._status_lock = threading.Lock()
        self._busy = 0  # requests in flight

    def status(self) -> dict:
        """Health of the server, cached for a few seconds because the UI polls it."""
        base = {"model": self.model, "url": self.url}
        if self._busy:
            # fish-speech generates on its event loop, so /v1/health can't answer mid-request; it is clearly up.
            return {**base, "state": "ready", "message": f"Fish Speech {self.model} ready."}
        with self._status_lock:  # one health check at a time; the rest get its result
            cached = self._status
            if cached and time.time() - self._checked < 3:
                return cached
            cached = {**base, **self._probe()}
            self._status, self._checked = cached, time.time()
            return cached

    def _probe(self) -> dict:
        import httpx

        try:
            code = self.http.get("/v1/health", timeout=3).status_code
        except httpx.HTTPError:
            # The server only starts listening once its weights are loaded.
            return {"state": "loading", "message": f"Waiting for Fish Speech at {self.url} (it may still be loading)…"}
        if code == 200:
            return {"state": "ready", "message": f"Fish Speech {self.model} ready."}
        if code in (401, 403):
            return {"state": "error", "message": "Fish Speech rejected the API key (check SONORUS_FISH_API_KEY)."}
        return {"state": "error", "message": f"Fish Speech answered its health check with HTTP {code}."}

    def tts(self, text: str, references: list[tuple[bytes, str]], seed: int, params: dict[str, Any]):
        """Synthesise speech, piece by piece for long text; returns (float samples, sample rate)."""
        import numpy as np

        refs = [{"audio": base64.b64encode(audio).decode(), "text": ref_text} for audio, ref_text in references]
        audio, sr = [], None
        self._busy += 1
        try:
            for piece in split_text(text, int(params["chunk_length"])):
                samples, sr = self._tts_once(piece, refs, seed, params)
                if audio:
                    audio.append(np.zeros(int(PIECE_GAP_SECONDS * sr), dtype=np.float32))
                audio.append(samples)
        finally:
            self._busy -= 1
        return np.concatenate(audio), sr

    def _tts_once(self, text: str, refs: list[dict], seed: int, params: dict[str, Any]):
        import httpx
        import soundfile as sf

        body = {
            "text": text,
            "references": refs,
            "format": "wav",
            "seed": seed,
            # Reuse the encoded reference across pieces and requests for the same voice.
            "use_memory_cache": "on",
            "temperature": float(params["temperature"]),
            "top_p": float(params["top_p"]),
            "chunk_length": int(params["chunk_length"]),
            "max_new_tokens": int(params["max_new_tokens"]),
        }
        try:
            resp = self.http.post("/v1/tts", json=body)
        except httpx.HTTPError as exc:
            raise FishError(f"Fish Speech isn't reachable at {self.url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail") or data.get("message") or resp.text
            except Exception:
                detail = resp.text
            raise FishError(f"Fish Speech refused the request ({resp.status_code}): {detail}")
        samples, sr = sf.read(io.BytesIO(resp.content), dtype="float32")
        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        return samples, sr
