"""Runtime settings, read from environment variables (all optional)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    # HF repo id or local path of the OmniVoice checkpoint.
    model: str = field(default_factory=lambda: _env("SONORUS_MODEL", "k2-fsa/OmniVoice"))
    # "auto", "cuda", "cuda:0", "cpu", "mps", "xpu"
    device: str = field(default_factory=lambda: _env("SONORUS_DEVICE", "auto"))
    # "auto", "float16", "bfloat16", "float32"
    dtype: str = field(default_factory=lambda: _env("SONORUS_DTYPE", "auto"))
    # Device for Whisper auto-transcription of reference audio ("auto" = cpu on small GPUs).
    asr_device: str = field(default_factory=lambda: _env("SONORUS_ASR_DEVICE", "auto"))
    asr_model: str | None = field(default_factory=lambda: _env("SONORUS_ASR_MODEL"))
    # Device for the audio codec (encodes reference clips, decodes output). "auto" = cpu on small GPUs.
    codec_device: str = field(default_factory=lambda: _env("SONORUS_CODEC_DEVICE", "auto"))
    # Where voices, history and generated audio live.
    data_dir: Path = field(
        default_factory=lambda: Path(_env("SONORUS_HOME", str(Path.home() / ".sonorus"))).expanduser()
    )
    host: str = field(default_factory=lambda: _env("SONORUS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("SONORUS_PORT", "7860")))
    # Keep at most this many items in the Pensieve (history).
    history_limit: int = field(default_factory=lambda: int(_env("SONORUS_HISTORY_LIMIT", "200")))

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "pensieve"

    def ensure_dirs(self) -> None:
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)


def server_url() -> str:
    """URL the CLI uses to reach a running Sonorus server."""
    url = _env("SONORUS_URL")
    if url:
        return url.rstrip("/")
    s = Settings()
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    return f"http://{host}:{s.port}"
