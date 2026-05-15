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

# Whisper's German training data is heavy on TV / YouTube subtitles, so
# when given silence or pure noise it confidently emits one of these
# canned phrases. Drop transcripts that match — they're never real user
# speech, and dispatching them to Claude as commands wastes a turn.
_WHISPER_HALLUCINATIONS: tuple[str, ...] = (
    "untertitel",
    "vielen dank fürs zuschauen",
    "vielen dank fürs zusehen",
    "vielen dank für ihre aufmerksamkeit",
    "untertitelung des zdf",
    "untertitel im auftrag des zdf",
    "untertitel der amara.org-community",
    "amara.org",
    "thanks for watching",
    "thank you for watching",
    "subtitles by",
    "transcription by",
    ". .",
    "..",
    "...",
    "♪",
)


def looks_like_hallucination(text: str) -> bool:
    """Return True if ``text`` matches one of Whisper's canned phantom outputs."""
    if not text or not text.strip():
        return True
    lowered = text.lower().strip(" .,!?")
    if len(lowered) < 2:
        return True
    for needle in _WHISPER_HALLUCINATIONS:
        if needle in lowered:
            return True
    return False

# Whisper-`base` mishears "jarvis" pretty consistently as one of these,
# especially with a non-native accent. Treat all of them as the wake word.
_WAKE_WORD_ALTERNATES: tuple[str, ...] = (
    "jarvis", "jaravis", "travis", "charvis", "chavis", "javis", "yarvis",
    "djarvis", "dschawis", "jarwis", "jervis",
    "ciao",  # whisper often hears the German "Jarvis" as Italian "ciao"
)

# "End conversation" phrases — exit follow-up mode entirely AND interrupt
# whatever JARVIS is doing right now. Matched against the normalised
# transcript (lowercased, punctuation stripped, single-spaced).
_END_PHRASES: frozenset[str] = frozenset({
    "okay das wars", "okay das war es", "okay das war's",
    "ok das wars", "ok das war es", "ok das war's",
    "das wars", "das war es", "das war's",
    "tschüss", "tschüss jarvis",
    "ende", "ende jarvis",
    "danke jarvis", "danke das wars",
})

# "Stop / barge-in" phrases — cancel the current reply or search and go
# back to listening, but DO NOT exit follow-up mode. Use these to cut
# JARVIS off mid-sentence without ending the session.
_STOP_PHRASES: frozenset[str] = frozenset({
    "stop", "stopp", "stop jarvis", "stopp jarvis",
    "halt", "halt mal", "halt stop", "halt jarvis",
    "warte", "warte mal", "warte jarvis",
    "schluss", "schluss jarvis",
    "okay stop", "ok stop", "okay halt", "ok halt",
    "okay warte", "ok warte",
    "psst", "pst", "pscht",
    "ruhe",
})

# 16-bit PCM RMS below this counts as "silence" — skip the chunk entirely
# instead of letting Whisper hallucinate ghost transcripts on quiet rooms.
SILENCE_RMS_THRESHOLD = 350

# Hard kill-switch phrases — these don't just barge in, they DISARM the
# entire mac_control surface (Tier 2+ refuses until a resume phrase).
# Must explicitly include the wake word so a casual "Stop" while music
# plays doesn't lock down the system.
_KILL_SWITCH_PHRASES: frozenset[str] = frozenset({
    "jarvis halt", "jarvis stopp", "jarvis stop",
    "jarvis aus", "jarvis notaus", "notaus", "notaus jarvis",
    "jarvis halt alles", "jarvis stop alles",
})

# Phrase the user must say to re-arm after a kill switch.
_RESUME_PHRASES: frozenset[str] = frozenset({
    "jarvis weiter", "jarvis resume", "jarvis fortsetzen",
    "jarvis los", "jarvis weitermachen",
})


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

    Drops very-low-energy clips on the floor: feeding Whisper near-silent
    audio reliably produces hallucinated phrases ("Untertitel im Auftrag
    des ZDF", random English, …). Cheaper to bail than to filter that
    out later.
    """
    import numpy as _np

    model = _load_model()

    # RMS sanity check on raw bytes (before any WAV wrapping). Skip the
    # 44-byte WAV header if present.
    raw = audio_bytes[44:] if audio_bytes[:4] == b"RIFF" else audio_bytes
    if len(raw) >= 2:
        samples = _np.frombuffer(raw, dtype=_np.int16)
        if samples.size:
            rms = float(_np.sqrt(_np.mean(samples.astype(_np.int32) ** 2)))
            if rms < 100:
                return ""

    # Whisper's Python API wants a file path; write to a tempfile.
    if audio_bytes[:4] != b"RIFF":
        audio_bytes = _pcm_to_wav(audio_bytes, sample_rate=sample_rate)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
        fp.write(audio_bytes)
        tmp = Path(fp.name)
    try:
        kwargs = {"fp16": False}
        if settings.WHISPER_LANGUAGE:
            # Pin the language. Without this, short German utterances
            # like "stopp" often auto-detect as English ("up").
            kwargs["language"] = settings.WHISPER_LANGUAGE
        result = model.transcribe(str(tmp), **kwargs)
        return " ".join(result.get("text", "").lower().split())
    finally:
        tmp.unlink(missing_ok=True)


def _wake_match(lowered: str) -> tuple[int, int] | None:
    """Return (start, end) of the first matched wake-word variant in ``lowered``."""
    # Try the configured wake word first, then known mishearings.
    candidates = (settings.WAKE_WORD, *_WAKE_WORD_ALTERNATES)
    for word in candidates:
        idx = lowered.find(word)
        if idx != -1:
            return idx, idx + len(word)
    return None


def has_wake_word(text: str) -> bool:
    """Return True if the wake word (or a known mishearing) appears in ``text``."""
    return _wake_match(text.lower()) is not None


def starts_with_wake_word(text: str) -> bool:
    """Return True if ``text`` *starts* with the wake word (after trimming
    leading punctuation and whitespace). Used as a barge-in signal during
    TTS playback: JARVIS rarely starts his own sentences with "Jarvis",
    so a wake word at the front of a transcript reliably means the user
    is talking over him."""
    lowered = text.lower().lstrip(" ,.!?:;-\t\n")
    for word in _WAKE_WORD_ALTERNATES:
        if lowered.startswith(word):
            return True
    return False


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    import re as _re

    text = text.lower()
    text = _re.sub(r"[^\w\säöüß']", " ", text, flags=_re.UNICODE)
    text = _re.sub(r"\s+", " ", text)
    return text.strip()


def is_end_phrase(text: str) -> bool:
    """Return True if ``text`` is one of the conversation-ending phrases."""
    return _normalise(text) in _END_PHRASES


def is_stop_phrase(text: str) -> bool:
    """Return True if ``text`` is a stop / end phrase. Either one cancels
    the in-flight reply; end phrases additionally exit follow-up mode."""
    norm = _normalise(text)
    return norm in _STOP_PHRASES or norm in _END_PHRASES


def is_kill_switch_phrase(text: str) -> bool:
    """Return True if ``text`` triggers the mac_control kill switch.

    Stricter than is_stop_phrase — these explicitly include the wake word
    so an offhand "Stop" mid-conversation doesn't disarm the whole
    automation surface. See ``_KILL_SWITCH_PHRASES``."""
    return _normalise(text) in _KILL_SWITCH_PHRASES


def is_resume_phrase(text: str) -> bool:
    """Return True if ``text`` is a kill-switch resume phrase."""
    return _normalise(text) in _RESUME_PHRASES


# First-word stop matchers. Catches Whisper variants like "stoppt",
# "stoppen", "halt mal", "okay stop ich meine …", etc. — anything that
# *begins* with one of these is treated as an interrupt. False positives
# are way less annoying than false negatives here (the user can always
# re-issue; a JARVIS that won't shut up is the worse failure mode).
_STOP_FIRST_WORDS: frozenset[str] = frozenset({
    "stop", "stopp", "stops", "stopps", "stoppt", "stoppen",
    "halt", "schluss", "ende",
    "warte", "wartet", "wartemal",
    "pst", "psst", "pscht",
    "ruhe",
})

# When the first word is one of these "okay/ok"-style prefixes, look at
# the SECOND word for a stop match — handles "okay stop", "ok halt", etc.
_STOP_LEAD_WORDS: frozenset[str] = frozenset({"okay", "ok", "oke", "k"})


def starts_with_stop_phrase(text: str) -> bool:
    """Lenient stop-phrase detector — matches any utterance whose first
    (or second-after-okay) word is a stop variant. Catches Whisper
    mis-transcriptions and short-burst command starts."""
    norm = _normalise(text)
    if not norm:
        return False
    words = norm.split()
    if not words:
        return False
    if words[0] in _STOP_FIRST_WORDS:
        return True
    if len(words) >= 2 and words[0] in _STOP_LEAD_WORDS and words[1] in _STOP_FIRST_WORDS:
        return True
    return False


def strip_wake_word(text: str) -> str:
    """Drop the wake word and any leading punctuation from a transcript."""
    match = _wake_match(text.lower())
    if match is None:
        return text
    tail = text[match[1]:]
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

    # Energy gate: silent windows go to Whisper and come back with
    # hallucinated English ("I'll see you next time", etc.). Skip them.
    samples = recording.astype(np.int32).reshape(-1)
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    if rms < SILENCE_RMS_THRESHOLD:
        return ""  # treated as "nothing heard" by the caller

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
