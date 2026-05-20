"""Live visual translation — rate-limited wrapper around OCR.

The iPhone PWA's "point camera at text" view streams frames at the
phone's frame rate. We can't afford one Claude Vision call per
frame, and the user can't read translations that flicker every
50 ms anyway, so the translator clamps each session to one
extract-and-translate call every ``_MIN_INTERVAL_S`` seconds. The
last successful translation is cached so callers between the
intervals still get a usable answer (just slightly stale).

Sessions
--------
Sessions are keyed by an opaque token from the caller (typically the
PWA's auth token). One stale-cache + last-call slot per session
keeps unrelated phones / browsers from starving each other; if you
don't need that isolation, reuse the same key everywhere.

Single-shot translation
-----------------------
``extract_and_translate(image)`` on this class is just a thin pass-
through to :py:class:`OCR.extract_and_translate` — kept here so the
caller has one obvious entry point ("anything image+language goes
through Translator") instead of two.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vision_manager import VisionManager
    from .ocr import TranslationResult


# Minimum gap between two Claude calls per session. 2 s is the
# spec's rate; tighter than that mostly burns tokens because the
# camera view doesn't change that fast at reading distance.
_MIN_INTERVAL_S = float(__import__("os").getenv(
    "JARVIS_VISION_TRANSLATE_INTERVAL_S", "2.0",
))


@dataclass
class LiveTranslation:
    """Outcome of a single live_translate call.

    ``stale=True`` means we returned the last cached translation
    because the rate-limit window hasn't elapsed yet — the caller
    can choose to render it slightly dimmed instead of flashing
    new text every frame. ``cached_age`` is the seconds since the
    cached translation was actually produced; clients use this to
    decide whether to keep showing it at all.
    """
    original: str
    translated: str
    target_language: str
    stale: bool
    cached_age: float


@dataclass
class _Session:
    last_call_at: float = 0.0
    last_result: "TranslationResult | None" = None
    last_result_at: float = 0.0


class Translator:
    """Rate-limited live-translation surface.

    Stateful: holds one ``_Session`` per session-id. Thread-safe
    enough for our needs (single lock around the dict + per-session
    state mutation); the live-translate path is called serially per
    WebSocket so contention is low.
    """

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # --- single-shot (no rate-limit) ---------------------------------- #

    def translate_image(
        self,
        image_base64: str,
        *,
        target_language: str = "de",
    ) -> "TranslationResult | None":
        """One-off translation — bypasses the rate limit. Use this
        for the "übersetze das" trigger on a still photo (PWA upload,
        screen region). The :py:meth:`live_translation` method is the
        one to call from a streaming camera surface."""
        return self._mgr.ocr.extract_and_translate(
            image_base64, target_language=target_language,
        )

    # --- live (rate-limited per session) ------------------------------ #

    def live_translation(
        self,
        image_base64: str,
        *,
        target_language: str = "de",
        session_id: str = "default",
    ) -> LiveTranslation | None:
        """Streaming-camera variant. Caps Claude calls to one per
        ``_MIN_INTERVAL_S`` per session; calls inside the window
        return the cached translation (with ``stale=True``).

        Returns ``None`` only if there has NEVER been a successful
        translation for this session AND the current call hit a
        Claude failure — once anything succeeds, subsequent rate-
        limited calls always have a cached result to return.
        """
        if not image_base64:
            return None

        now = time.monotonic()
        with self._lock:
            session = self._sessions.setdefault(session_id, _Session())
            since_last_call = now - session.last_call_at

            if session.last_result is not None \
                    and since_last_call < _MIN_INTERVAL_S:
                # Inside the window — return cached result.
                cached = session.last_result
                age = now - session.last_result_at
                return LiveTranslation(
                    original=cached.original,
                    translated=cached.translated,
                    target_language=cached.target_language,
                    stale=True,
                    cached_age=age,
                )

            # Mark the call time BEFORE the network round-trip so
            # concurrent callers don't both spend a Claude call.
            session.last_call_at = now

        # Network call happens OUTSIDE the lock so a slow Claude
        # response can't block another session's lookups.
        result = self._mgr.ocr.extract_and_translate(
            image_base64, target_language=target_language,
        )

        with self._lock:
            session = self._sessions.setdefault(session_id, _Session())
            if result is None:
                # API failure — fall back to the cached result if
                # we have one, otherwise surface the failure to the
                # caller as None.
                if session.last_result is None:
                    return None
                age = time.monotonic() - session.last_result_at
                cached = session.last_result
                return LiveTranslation(
                    original=cached.original,
                    translated=cached.translated,
                    target_language=cached.target_language,
                    stale=True,
                    cached_age=age,
                )

            session.last_result = result
            session.last_result_at = time.monotonic()

        return LiveTranslation(
            original=result.original,
            translated=result.translated,
            target_language=result.target_language,
            stale=False,
            cached_age=0.0,
        )

    # --- session lifecycle -------------------------------------------- #

    def reset_session(self, session_id: str) -> None:
        """Forget a session's rate-limit state + cached translation.
        Called when the user explicitly ends a live-translate view
        (PWA Stop button)."""
        with self._lock:
            self._sessions.pop(session_id, None)
