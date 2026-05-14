"""Text-to-speech via pyttsx3.

pyttsx3 is fully synchronous and not thread-safe on every platform, so we
serialise all speech through a single worker thread + queue. Callers just
call ``speak(text)``; the function returns immediately.

Swap-ability:
    Replace this module with another backend (Piper, OpenAI TTS, system
    `say`/`espeak`) — the rest of the server only imports ``speak``.
"""
from __future__ import annotations

import queue
import sys
import threading

_jobs: "queue.Queue[str | None]" = queue.Queue()
_worker: threading.Thread | None = None
_engine = None
_lock = threading.Lock()


def _ensure_engine():
    global _engine
    if _engine is None:
        import pyttsx3  # imported lazily so a TTS-less deploy doesn't pay for it

        _engine = pyttsx3.init()
        _engine.setProperty("rate", 185)
    return _engine


def _run() -> None:
    engine = _ensure_engine()
    while True:
        text = _jobs.get()
        if text is None:  # shutdown sentinel
            return
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:  # noqa: BLE001 — TTS errors shouldn't crash the server
            print(f"[JARVIS] tts error: {exc}", file=sys.stderr)


def speak(text: str) -> None:
    """Enqueue ``text`` for the server's local speakers. Non-blocking."""
    if not text or not text.strip():
        return
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="jarvis-tts", daemon=True)
            _worker.start()
    _jobs.put(text)


def shutdown() -> None:
    """Stop the worker thread (called from the FastAPI lifespan)."""
    if _worker is not None and _worker.is_alive():
        _jobs.put(None)
