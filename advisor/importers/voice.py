"""Transcribe a spoken process description locally with faster-whisper (no API key).

faster-whisper is optional: `is_available()` says whether it is installed and
`transcribe()` raises VoiceUnavailable with an install hint when it is not.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

MAX_AUDIO_BYTES = 200 * 1024 * 1024
INSTALL_HINT = (
    "Voice input needs the optional local speech-to-text model. Install it with "
    "`pip install faster-whisper`; the first transcription downloads the Whisper model "
    "(about 150 MB for 'base'), after which it works offline."
)
_MODEL_SIZE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")  # a model name, never a path
_MODELS: dict[tuple[int, str], object] = {}


class VoiceUnavailable(RuntimeError):
    """faster-whisper is not installed."""


def is_available() -> bool:
    """True when faster-whisper can be imported (checked without importing it)."""
    if "faster_whisper" in sys.modules:
        return sys.modules["faster_whisper"] is not None
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def transcribe(audio: bytes, filename: str = "audio.wav", model_size: str = "base") -> str:
    """Speech in `audio` (wav, mp3, m4a, ...) as plain text, using a local Whisper model on CPU."""
    if not audio:
        raise ValueError("The audio file is empty.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError(f"The audio file is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB.")
    if not _MODEL_SIZE.match(model_size or "") or ".." in model_size:
        raise ValueError(f"Unknown Whisper model size: {model_size!r}")
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise VoiceUnavailable(INSTALL_HINT) from e

    key = (id(WhisperModel), model_size)
    model = _MODELS.get(key)
    if model is None:
        model = _MODELS[key] = WhisperModel(model_size, device="cpu", compute_type="int8")

    # Only the extension of the uploaded name is used (it tells ffmpeg the format).
    suffix = Path(filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        suffix = ".wav"
    fd, path = tempfile.mkstemp(prefix="advisor-voice-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        segments, _info = model.transcribe(path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return re.sub(r"\s+", " ", text).strip()
