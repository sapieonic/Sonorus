"""Sonorus — a Hogwarts-themed voice cloning and speech service built on OmniVoice."""

import warnings

# pydub (an OmniVoice dependency) warns on import when ffmpeg is absent; we never need it for WAV/FLAC/OGG.
warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv")

__version__ = "0.1.0"
