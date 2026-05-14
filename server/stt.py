"""Speech-to-text with wake-word gating.

Whisper is loaded lazily because the model file is hundreds of MB and not
every deployment of JARVIS will use audio (text-input fallback is always
available).

Privacy:
    The microphone is opened *only* after the wake word fires. We print
    ``[MIC ON]`` / ``[MIC OFF]`` to stdout so the user can see exactly
    when audio is being captured.

Swap-ability:
    All STT goes through the ``transcribe(audio_bytes)`` function — swap
    in another backend (e.g. Deepgram) by editing this single module.
"""
from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

from .config import settings

_model = None  # lazy whisper model


def _load_model():  # -> "whisper.Whisper"
    global _model
    if _model is None:
        import whisper  # imported lazily — heavy dep

        print(f"[JARVIS] loading whisper model={settings.WHISPER_MODEL!r}…")
        _model = whisper.load_model(settings.WHISPER_MODEL)
    return _model


# ---------- public API ---------------------------------------------------- #

def transcribe(audio_bytes: bytes, *, sample_rate: int = 16_000) -> str:
    """Transcribe a 16-bit PCM mono WAV (or raw PCM) blob to text.

    Accepts a full WAV file (RIFF header) or raw PCM samples — we sniff
    the leading bytes. Returns lowercase, whitespace-collapsed text.
    """
    model = _load_model()

    # Whisper's Python API wants a file path; write to a tempfile.
    if audio_bytes[:4] != b"RIFF":
        audio_bytes = _pcm_to_wav(audio_bytes, sample_rate=sample_rate)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
        fp.write(audio_bytes)
        tmp = Path(fp.name)
    try:
        result = model.transcribe(str(tmp), fp16=False)
        return " ".join(result.get("text", "").lower().split())
    finally:
        tmp.unlink(missing_ok=True)


def has_wake_word(text: str) -> bool:
    """Return True if the configured wake word appears in ``text``."""
    return settings.WAKE_WORD in text.lower()


def strip_wake_word(text: str) -> str:
    """Drop the wake word and any leading punctuation from a transcript."""
    lowered = text.lower()
    idx = lowered.find(settings.WAKE_WORD)
    if idx == -1:
        return text
    tail = text[idx + len(settings.WAKE_WORD):]
    return tail.lstrip(" ,.!?:;-").strip()


def listen_once(duration_s: float = 5.0, sample_rate: int = 16_000) -> str:
    """Record ``duration_s`` of audio from the default mic and transcribe.

    Used by the optional local mic loop (``python -m server.stt --listen``);
    network clients send their own audio over the WebSocket instead.
    """
    import numpy as np
    import sounddevice as sd

    print("[MIC ON] listening…", flush=True)
    try:
        recording = sd.rec(
            int(duration_s * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
    finally:
        print("[MIC OFF]", flush=True)

    pcm = recording.astype(np.int16).tobytes()
    wav = _pcm_to_wav(pcm, sample_rate=sample_rate)
    return transcribe(wav, sample_rate=sample_rate)


# ---------- helpers ------------------------------------------------------- #

def _pcm_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM in a minimal WAV container so Whisper can read it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


if __name__ == "__main__":  # pragma: no cover — manual mic test
    import sys

    if "--listen" in sys.argv:
        print(listen_once())
