"""Client for a fish-speech API server (tools/api_server.py), the second engine next to OmniVoice."""

from __future__ import annotations

import base64
import io
import time
from typing import Any

FISH_PARAMS: list[dict[str, Any]] = [
    {"key": "temperature", "label": "Temperature", "type": "float", "default": 0.7, "min": 0.1, "max": 1.0, "step": 0.05,
     "group": "Sampling", "help": "Randomness of the delivery. Lower is steadier, higher is livelier."},
    {"key": "top_p", "label": "Top-p", "type": "float", "default": 0.7, "min": 0.1, "max": 1.0, "step": 0.05,
     "group": "Sampling", "help": "Nucleus sampling cut-off. Lower keeps to the most likely sounds."},
    {"key": "chunk_length", "label": "Chunk length", "type": "int", "default": 200, "min": 100, "max": 1000, "step": 10,
     "group": "Long-form", "help": "Long text is split into chunks of about this many characters (S2-Pro only)."},
    {"key": "max_new_tokens", "label": "Max frames per chunk", "type": "int", "default": 1024, "min": 0, "max": 4096, "step": 64,
     "group": "Long-form", "help": "Upper bound on audio length per chunk. 0 uses the model's own cap."},
]
FISH_PARAM_DEFAULTS = {p["key"]: p["default"] for p in FISH_PARAMS}

# Fish takes free-form inline instructions; these are the ones its docs use.
FISH_TAGS = [
    ("[pause]", "Pause"), ("[laughing]", "Laugh"), ("[whisper]", "Whisper"), ("[excited]", "Excited"),
    ("[angry]", "Angry"), ("[emphasis]", "Emphasis"),
]


class FishError(Exception):
    """The fish-speech server is unreachable or rejected the request."""


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

    def status(self) -> dict:
        """Health of the server, cached for a couple of seconds because the UI polls it."""
        import httpx

        if self._status and time.time() - self._checked < 2:
            return self._status
        base = {"model": self.model, "url": self.url}
        try:
            ok = self.http.get("/v1/health", timeout=2).status_code == 200
            st = {"state": "ready", "message": f"Fish Speech {self.model} ready."} if ok else {
                "state": "error", "message": "Fish Speech answered, but reports it is unhealthy."}
        except httpx.HTTPError:
            # The server only starts listening once its weights are loaded.
            st = {"state": "loading", "message": f"Waiting for Fish Speech at {self.url} (it may still be loading)…"}
        self._status, self._checked = {**base, **st}, time.time()
        return self._status

    def tts(self, text: str, references: list[tuple[bytes, str]], seed: int, normalize: bool,
            params: dict[str, Any]):
        """Synthesise speech; returns (float samples, sample rate)."""
        import httpx
        import soundfile as sf

        body = {
            "text": text,
            "references": [{"audio": base64.b64encode(audio).decode(), "text": ref_text} for audio, ref_text in references],
            "format": "wav",
            "seed": seed,
            "normalize": normalize,
            # Reuse the encoded reference across requests for the same voice.
            "use_memory_cache": "on",
            "temperature": float(params["temperature"]),
            "top_p": float(params["top_p"]),
            "chunk_length": int(params["chunk_length"]),
            "max_new_tokens": int(params["max_new_tokens"]),
        }
        try:
            resp = self.http.post("/v1/tts", json=body)
        except httpx.HTTPError as exc:
            self._status = None
            raise FishError(f"Fish Speech isn't reachable at {self.url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail") or resp.json().get("message") or resp.text
            except Exception:
                detail = resp.text
            raise FishError(f"Fish Speech refused the request ({resp.status_code}): {detail}")
        audio, sr = sf.read(io.BytesIO(resp.content), dtype="float32")
        return audio, sr
