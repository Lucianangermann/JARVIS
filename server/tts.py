"""Text-to-speech with a pull-based playback buffer (for AEC barge-in).

Architecture
------------
On macOS we render JARVIS's reply to an in-memory 16 kHz mono int16 PCM
buffer using ``say --data-format=LEI16@16000 -o file.wav …``, then feed
those samples to the speakers via the voice loop's full-duplex
``sd.Stream`` — *not* directly via the ``say`` process. Doing it this
way means we know exactly what bytes are heading to the speakers each
10 ms, which is the reference signal Speex needs to subtract the echo
back out of the mic input. Net effect: "Stop" works mid-sentence over
laptop speakers.

* ``speak(text)``  — non-blocking; queues a render job. The render
  worker shells out to ``say -o`` and pushes the resulting samples into
  the shared playback buffer.
* ``pull(n)``      — called from the audio callback every ~10 ms; pops
  up to ``n`` int16 samples from the buffer (zeros if empty).
* ``stop()``       — drops all queued renders + clears the playback
  buffer. Barge-in interrupt — speakers fall silent within one tick.
* ``wait(timeout)`` — block until everything queued has finished
  playing. Set by the audio callback when it drains the buffer.
* ``is_idle()``    — True iff render queue empty AND buffer empty.

On other OSes (Windows / Linux) we keep the pyttsx3 path — it's not
echo-cancellable but at least the text-to-speech part still works.
"""
from __future__ import annotations

import collections
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import wave

import numpy as np

_DARWIN = sys.platform == "darwin"
SAMPLE_RATE = 16_000

# ---- Markdown / emoji stripper ------------------------------------------ #
# Claude tends to format replies with **bold**, *italic*, `code`, bullet
# points, etc. The synthesiser reads those literally otherwise.

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_BOLD_ITAL = re.compile(r"(\*+|_+)(.+?)\1")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")          # [text](url) -> text
_BARE_URL = re.compile(r"https?://\S+")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.MULTILINE)
_HEADING = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_STRIPPABLE = re.compile(
    r"[^\w\s\.,;:!\?\-'\"()äöüÄÖÜßéèêàâîïôûç%€$°]+",
    flags=re.UNICODE,
)


# Pronunciation corrections — applied after markdown stripping.
# German TTS reads "Lucian" as "Lutsian" (hard c). Replace with "Lusian"
# so the soft-s sound comes out correctly.
_PRONUNCIATION = [
    (re.compile(r"\bLucian\b", re.IGNORECASE), "Lusian"),
]


def _speakable(text: str) -> str:
    """Strip markdown/emoji/URLs so the synthesiser doesn't read them literally."""
    if not text:
        return ""
    text = _CODE_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD_ITAL.sub(r"\2", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("link", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _LIST_MARKER.sub("", text)
    text = _STRIPPABLE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    for pattern, replacement in _PRONUNCIATION:
        text = pattern.sub(replacement, text)
    return text.strip()


# ============================== macOS path ============================== #
# Render → samples in playback buffer → voice_loop's audio callback consumes
# them and feeds the speakers (and AEC) in the same tick.

# Render queue: text strings waiting to be turned into PCM.
_render_q: "queue.Queue[str | None]" = queue.Queue()
_render_worker: threading.Thread | None = None
_render_lock = threading.Lock()

# Playback buffer: a deque of int16 numpy arrays. The audio callback
# pulls samples from the front; the render worker appends to the back.
# A separate offset is used because samples are usually consumed in
# smaller chunks than they were pushed.
_playback_buf: "collections.deque[np.ndarray]" = collections.deque()
_playback_offset = 0
_playback_lock = threading.Lock()

# Combined-idle event: set when no render is in flight AND playback
# buffer is empty. The voice loop uses wait() to know when JARVIS has
# finished speaking; the busy check uses is_idle().
#
# We track "in flight" with an explicit counter (incremented by speak,
# decremented by the render worker) instead of inferring it from the
# render_q + playback_buf pair. The pair-inference had a nasty race:
# right after the worker pops a job from the queue but before it
# finishes rendering and pushes samples, both queue *and* buffer are
# briefly empty — pull() would then set _idle and the voice loop's
# busy-skip-VAD would drop, allowing the normal VAD to fire on the TTS
# audio about to start playing.
_idle = threading.Event()
_idle.set()
_inflight_count = 0
_inflight_lock = threading.Lock()

# Cached voice resolution for the macOS `say` command.
_resolved_macos_voice: str | None = None


def _list_say_voices() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] tts: could not list say voices: {exc}")
        return {}
    voices: dict[str, str] = {}
    for line in out.splitlines():
        toks = line.rstrip().split()
        if not toks:
            continue
        name_parts: list[str] = []
        locale = ""
        for t in toks:
            if re.fullmatch(r"[a-z]{2,3}_[A-Z0-9]{2,5}", t) or re.fullmatch(
                r"[a-z]{2,3}_[a-z]{2,5}", t
            ):
                locale = t
                break
            name_parts.append(t)
        if name_parts:
            voices[" ".join(name_parts)] = locale
    return voices


def _resolve_say_voice(preferred: str, language: str) -> str:
    voices = _list_say_voices()
    if not voices:
        return ""
    if preferred:
        pref = preferred.strip()
        if pref in voices:
            return pref
        for name in voices:
            if pref.lower() in name.lower():
                return name
        print(f"[JARVIS] tts: preferred voice {preferred!r} not installed; trying fallbacks.")
    fallback_priority = [
        "Markus", "Yannick",
        "Reed (Deutsch (Deutschland))", "Reed (German (Germany))",
        "Grandpa (Deutsch (Deutschland))", "Grandpa (German (Germany))",
        "Rocko (Deutsch (Deutschland))", "Rocko (German (Germany))",
        "Eddy (Deutsch (Deutschland))", "Eddy (German (Germany))",
        "Anna",
    ]
    for cand in fallback_priority:
        if cand in voices:
            return cand
    lang = (language or "").strip().lower()
    if lang:
        for name, loc in voices.items():
            if loc.lower().startswith(lang):
                return name
    return ""


def _ensure_voice() -> None:
    """Resolve the macOS `say` voice once and cache it."""
    global _resolved_macos_voice
    if _resolved_macos_voice is None:
        from .config import settings
        _resolved_macos_voice = _resolve_say_voice(settings.TTS_VOICE, settings.TTS_LANGUAGE)
        if _resolved_macos_voice:
            print(f"[JARVIS] tts: say voice={_resolved_macos_voice!r}")
        else:
            print("[JARVIS] tts: using system default voice")


def _render_to_pcm(text: str) -> np.ndarray:
    """Turn ``text`` into 16 kHz mono int16 PCM samples via macOS `say`."""
    from .config import settings

    _ensure_voice()

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis-tts-")
    os.close(fd)
    try:
        cmd = ["say"]
        if _resolved_macos_voice:
            cmd += ["-v", _resolved_macos_voice]
        if settings.TTS_RATE:
            cmd += ["-r", str(settings.TTS_RATE)]
        # LEI16 = little-endian int16 linear PCM; @16000 = sample rate.
        # This skips any need for resampling later.
        cmd += ["--data-format=LEI16@16000", "-o", path, "--", text]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)

        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
        if sw != 2:
            raise RuntimeError(f"unexpected sample width {sw}")
        samples = np.frombuffer(frames, dtype=np.int16)
        if ch == 2:
            samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
        if sr != SAMPLE_RATE:
            # Should not happen — we asked say for 16 kHz — but guard anyway.
            raise RuntimeError(f"unexpected sample rate {sr}")
        return samples
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _render_loop() -> None:
    global _inflight_count
    while True:
        text = _render_q.get()
        if text is None:
            # Shutdown sentinel.
            return
        try:
            samples = _render_to_pcm(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] tts render error: {exc}", file=sys.stderr)
            samples = None
        if samples is not None and len(samples) > 0:
            with _playback_lock:
                _playback_buf.append(samples)
        # This job is done; the buffer (if not empty) keeps us non-idle.
        with _inflight_lock:
            _inflight_count -= 1
        _maybe_set_idle()


def _maybe_set_idle() -> None:
    """Set ``_idle`` iff no render in flight AND playback buffer empty."""
    with _inflight_lock:
        if _inflight_count > 0:
            return
    with _playback_lock:
        if not _playback_buf:
            _idle.set()


def _ensure_worker() -> None:
    global _render_worker
    with _render_lock:
        if _render_worker is None or not _render_worker.is_alive():
            _render_worker = threading.Thread(
                target=_render_loop, name="jarvis-tts-render", daemon=True,
            )
            _render_worker.start()


# ---------------- public API used by the rest of the server ---------------- #

def speak(text: str) -> None:
    """Non-blocking: enqueue ``text`` for synthesis + playback."""
    cleaned = _speakable(text)
    if not cleaned:
        return
    print(f"[JARVIS·tts] {cleaned}")
    if _DARWIN:
        # Order matters: bump the in-flight counter and clear idle BEFORE
        # we hand the job to the worker, otherwise a callback can race
        # in between and mark us idle.
        global _inflight_count
        with _inflight_lock:
            _inflight_count += 1
        _idle.clear()
        _ensure_worker()
        _render_q.put(cleaned)
    else:
        _pyttsx3_speak(cleaned)


def pull(n: int) -> np.ndarray:
    """Return up to ``n`` int16 samples for playback; zero-padded if short.

    Called from the voice loop's audio callback every audio tick. Marks
    the TTS as idle the first time the buffer empties out completely.
    """
    global _playback_offset
    out = np.zeros(n, dtype=np.int16)
    if not _DARWIN:
        return out  # pyttsx3 path plays directly; nothing for us to feed

    pos = 0
    became_empty = False
    with _playback_lock:
        while pos < n and _playback_buf:
            chunk = _playback_buf[0]
            avail = len(chunk) - _playback_offset
            take = min(n - pos, avail)
            out[pos:pos + take] = chunk[_playback_offset:_playback_offset + take]
            _playback_offset += take
            pos += take
            if _playback_offset >= len(chunk):
                _playback_buf.popleft()
                _playback_offset = 0
        if not _playback_buf:
            became_empty = True
    if became_empty:
        # Defer to the canonical check — only marks idle if no in-flight
        # render and the buffer is *really* empty.
        _maybe_set_idle()
    return out


def stop() -> None:
    """Drop everything in flight — instant interrupt for barge-in."""
    # Drain the render queue (without removing the shutdown sentinel).
    global _inflight_count
    drained = 0
    pending = []
    while True:
        try:
            item = _render_q.get_nowait()
        except queue.Empty:
            break
        if item is None:
            pending.append(item)
        else:
            drained += 1
    for item in pending:
        _render_q.put(item)
    if drained:
        print(f"[JARVIS·tts] dropped {drained} pending render job(s)")
        # Each dropped job had bumped the in-flight counter — decrement now.
        with _inflight_lock:
            _inflight_count = max(0, _inflight_count - drained)

    # Clear the playback buffer — next audio tick will emit silence.
    global _playback_offset
    with _playback_lock:
        had = bool(_playback_buf)
        _playback_buf.clear()
        _playback_offset = 0
    if had:
        print("[JARVIS·tts] playback buffer cleared")

    # pyttsx3 fallback path
    if not _DARWIN and _engine is not None:
        try:
            _engine.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS·tts] could not stop pyttsx3 engine: {exc}")

    _idle.set()


def is_idle() -> bool:
    return _idle.is_set()


def wait(timeout: float | None = None) -> bool:
    return _idle.wait(timeout)


def shutdown() -> None:
    """Stop the render worker (called from the FastAPI lifespan)."""
    if _DARWIN and _render_worker is not None and _render_worker.is_alive():
        _render_q.put(None)


# ============================ pyttsx3 fallback ============================ #
# Linux / Windows path. No AEC available — TTS just plays through the
# system default audio output; the voice loop will use its non-AEC mic
# path on those platforms.

_engine = None


def _ensure_pyttsx3():
    global _engine
    if _engine is None:
        import pyttsx3
        from .config import settings
        _engine = pyttsx3.init()
        _engine.setProperty("rate", settings.TTS_RATE)
    return _engine


def _pyttsx3_speak(text: str) -> None:
    _idle.clear()
    try:
        engine = _ensure_pyttsx3()
        engine.say(text)
        engine.runAndWait()
    except Exception as exc:  # noqa: BLE001
        print(f"[JARVIS] tts error: {exc}", file=sys.stderr)
    finally:
        _idle.set()
