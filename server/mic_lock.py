"""Process-wide microphone coordination.

The MacBook mic is a single CoreAudio capture device. Several components
can try to open it: the always-on wake-word loop (voice_loop, continuous
InputStream), meeting recording, voice-auth enrollment/verify, and
one-shot STT — all via sounddevice. Two simultaneous capture clients on
the built-in mic typically error or starve each other.

This module is a tiny coordinator: a non-reentrant lock plus the current
owner's name. Components that grab the mic should ``try_acquire(name)``
and ``release(name)``; a busy mic returns False with the owner exposed via
``owner()`` so the caller can refuse with a clear message instead of
racing the audio device.

voice_loop is intentionally NOT modified to take this lock (it's a
sensitive file); instead, meeting/voice-auth check ``JARVIS_LOCAL_VOICE``
and decline while the wake-word loop is the presumed owner.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_owner: str | None = None
_state_lock = threading.Lock()


def try_acquire(name: str) -> bool:
    """Acquire the mic non-blocking. Returns True if granted."""
    global _owner
    if _lock.acquire(blocking=False):
        with _state_lock:
            _owner = name
        return True
    return False


def release(name: str) -> None:
    """Release the mic if held by ``name`` (idempotent / safe)."""
    global _owner
    with _state_lock:
        if _owner != name:
            return
        _owner = None
    try:
        _lock.release()
    except RuntimeError:
        pass  # not locked — already released


def owner() -> str | None:
    with _state_lock:
        return _owner
