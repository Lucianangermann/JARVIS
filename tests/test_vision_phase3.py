"""Tests for the Phase 3 vision modules: motion detector + translator.

OpenCV is real (the dep is installed) so the motion-ratio math is
exercised directly with synthetic numpy frames. The camera is never
opened — ``_open_camera`` / ``_grab_frame`` are stubbed via
monkeypatch so the loop sees a controlled stream of frames.

The translator's rate-limit timing is tested by monkeypatching
``time.monotonic`` inside the translator module so we can fast-
forward through the 2-second window without sleeping.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from server.vision import (
    MotionDetector,
    MotionEvent,
    Translator,
    VisionManager,
)
from server.vision.ocr import TranslationResult


class _StubClient:
    """Same shape as the Phase 1/2 test stub — records calls,
    returns canned replies."""

    def __init__(self, replies) -> None:
        self.messages = SimpleNamespace(create=self._create)
        self._queue = list(replies) if isinstance(replies, list) else [replies]
        self.calls: list[dict] = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._queue.pop(0) if self._queue else ""
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])


def _mgr(replies="ok") -> VisionManager:
    return VisionManager(client=_StubClient(replies))


# ============================================================ #
# MotionDetector                                               #
# ============================================================ #

def _solid_frame(value: int, size=(120, 160, 3)) -> np.ndarray:
    """BGR uint8 frame filled with one grey level."""
    return np.full(size, value, dtype=np.uint8)


def test_motion_ratio_zero_for_identical_frames():
    import cv2
    a = _solid_frame(80)
    g = MotionDetector._to_diff_gray(cv2, a)
    assert MotionDetector._motion_ratio(cv2, g, g) == 0.0


def test_motion_ratio_high_for_very_different_frames():
    import cv2
    dark = MotionDetector._to_diff_gray(cv2, _solid_frame(20))
    bright = MotionDetector._to_diff_gray(cv2, _solid_frame(200))
    ratio = MotionDetector._motion_ratio(cv2, dark, bright)
    # Whole-frame swap should put ratio well above any sensitivity
    # threshold (high = 0.008, medium = 0.02, low = 0.05).
    assert ratio > 0.5


def test_motion_frame_to_base64_round_trip():
    import cv2
    frame = _solid_frame(120)
    b64 = MotionDetector._frame_to_base64(cv2, frame)
    assert b64 is not None
    # JPEG SOI marker after decoding.
    import base64
    assert base64.b64decode(b64).startswith(b"\xff\xd8\xff")


def test_motion_start_refuses_if_opencv_missing(monkeypatch):
    mgr = _mgr()
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("simulated no opencv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert mgr.motion.start() is False
    assert mgr.motion.is_running() is False


def test_motion_start_refuses_when_already_running(monkeypatch):
    mgr = _mgr()
    # Fake an alive thread so the start guard refuses a second one.
    fake_thread = SimpleNamespace(is_alive=lambda: True)
    mgr.motion._thread = fake_thread  # type: ignore[attr-defined]
    assert mgr.motion.start() is False


def test_motion_unknown_sensitivity_falls_back_to_medium(monkeypatch):
    mgr = _mgr()
    # We don't actually want the thread to run — stub _run to no-op
    # and just verify the sensitivity attribute ends up at "medium".
    monkeypatch.setattr(mgr.motion, "_run", lambda: None)
    assert mgr.motion.start(sensitivity="bogus") is True
    assert mgr.motion._sensitivity == "medium"  # type: ignore[attr-defined]
    mgr.motion.stop()


def test_motion_capture_once_returns_analysis(monkeypatch):
    mgr = _mgr("Ein Mensch im Türrahmen.")
    import cv2

    # Stub the camera helpers so we never touch a real device.
    fake_frame = _solid_frame(150)
    monkeypatch.setattr(
        MotionDetector, "_open_camera",
        staticmethod(lambda _cv2, _idx: SimpleNamespace(
            release=lambda: None, read=lambda: (True, fake_frame),
        )),
    )
    monkeypatch.setattr(
        MotionDetector, "_grab_frame",
        staticmethod(lambda _cap: fake_frame),
    )
    result = mgr.motion.capture_once()
    assert result == "Ein Mensch im Türrahmen."


def test_motion_capture_once_handles_open_failure(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(
        MotionDetector, "_open_camera",
        staticmethod(lambda _cv2, _idx: None),
    )
    assert mgr.motion.capture_once() is None


def test_motion_event_handler_receives_event(monkeypatch):
    """Run one synthetic loop iteration that crosses the threshold
    and confirm the handler is invoked with a MotionEvent."""
    mgr = _mgr("Person erkannt.")
    received: list[MotionEvent] = []

    def handler(ev: MotionEvent) -> None:
        received.append(ev)

    # Two distinct frames to force a motion ratio > 0.
    frames = [_solid_frame(20), _solid_frame(200)]
    state = {"i": 0}

    def fake_grab(_cap):
        i = state["i"]
        state["i"] = i + 1
        if i >= len(frames):
            return None
        return frames[i]

    def fake_open(_cv2, _idx):
        return SimpleNamespace(release=lambda: None, read=lambda: (True, None))

    monkeypatch.setattr(MotionDetector, "_open_camera", staticmethod(fake_open))
    monkeypatch.setattr(MotionDetector, "_grab_frame", staticmethod(fake_grab))

    # Drive the loop body directly with a stop event set after one
    # event — avoids the daemon-thread timing flakiness.
    mgr.motion._handler = handler  # type: ignore[attr-defined]
    mgr.motion._sensitivity = "high"  # type: ignore[attr-defined]
    mgr.motion._camera_index = 0  # type: ignore[attr-defined]
    mgr.motion._started_at = time.monotonic()  # type: ignore[attr-defined]
    mgr.motion._stop.clear()  # type: ignore[attr-defined]

    # Run the loop until frames run out — fake_grab returns None
    # on the 3rd call and the loop's transient-failure handler
    # backs off, which eventually flips the stop event we'll set
    # from the test thread.
    import threading
    t = threading.Thread(target=mgr.motion._run, daemon=True)  # type: ignore[attr-defined]
    t.start()
    # Wait a tiny bit for the event, then stop the loop.
    deadline = time.monotonic() + 2.0
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)
    mgr.motion._stop.set()  # type: ignore[attr-defined]
    t.join(timeout=2.0)

    assert len(received) >= 1
    assert received[0].analysis == "Person erkannt."
    assert received[0].frame_b64 is not None


# ============================================================ #
# Translator                                                   #
# ============================================================ #

def test_translator_single_shot_passes_through(monkeypatch):
    mgr = _mgr()
    expected = TranslationResult(
        original="Hello", translated="Hallo", target_language="de",
    )

    monkeypatch.setattr(
        mgr.ocr, "extract_and_translate",
        lambda *a, **kw: expected,
    )
    result = mgr.translator.translate_image("ZmFrZQ==")
    assert result is expected  # exact pass-through


def test_translator_live_first_call_hits_ocr(monkeypatch):
    mgr = _mgr()
    calls = []
    expected = TranslationResult(
        original="x", translated="y", target_language="de",
    )

    def fake_ocr(image, *, target_language="de"):
        calls.append((image, target_language))
        return expected

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", fake_ocr)
    live = mgr.translator.live_translation("img1", session_id="s1")
    assert live is not None
    assert live.original == "x"
    assert live.translated == "y"
    assert live.stale is False
    assert calls == [("img1", "de")]


def test_translator_live_inside_window_returns_cached(monkeypatch):
    mgr = _mgr()
    base = [
        TranslationResult(original="o1", translated="t1", target_language="de"),
    ]

    def fake_ocr(_img, *, target_language="de"):
        return base[0]

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", fake_ocr)

    # First call → makes the OCR call.
    live1 = mgr.translator.live_translation("a", session_id="s")
    assert live1 is not None and live1.stale is False

    # Replace the OCR fn so a second call would raise if invoked —
    # it shouldn't be, because the rate limit hasn't elapsed.
    def boom(*args, **kwargs):
        raise AssertionError("rate limit should have blocked this call")

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", boom)
    live2 = mgr.translator.live_translation("b", session_id="s")
    assert live2 is not None
    assert live2.stale is True
    assert live2.original == "o1"
    assert live2.translated == "t1"
    assert live2.cached_age >= 0.0


def test_translator_live_after_window_makes_new_call(monkeypatch):
    mgr = _mgr()
    seq = [
        TranslationResult(original="o1", translated="t1", target_language="de"),
        TranslationResult(original="o2", translated="t2", target_language="de"),
    ]
    state = {"i": 0}

    def fake_ocr(_img, *, target_language="de"):
        out = seq[state["i"]]
        state["i"] += 1
        return out

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", fake_ocr)
    live1 = mgr.translator.live_translation("a", session_id="s2")
    assert live1.stale is False

    # Fast-forward time inside the translator module.
    import server.vision.translator as t_mod
    real_monotonic = t_mod.time.monotonic
    monkeypatch.setattr(
        t_mod.time, "monotonic", lambda: real_monotonic() + 5.0,
    )
    live2 = mgr.translator.live_translation("b", session_id="s2")
    assert live2.stale is False
    assert live2.original == "o2"
    assert live2.translated == "t2"


def test_translator_live_sessions_are_isolated(monkeypatch):
    mgr = _mgr()
    seq = [
        TranslationResult(original="A", translated="a", target_language="de"),
        TranslationResult(original="B", translated="b", target_language="de"),
    ]
    state = {"i": 0}

    def fake_ocr(_img, *, target_language="de"):
        out = seq[state["i"]]
        state["i"] += 1
        return out

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", fake_ocr)
    live_alice = mgr.translator.live_translation("img-A", session_id="alice")
    live_bob   = mgr.translator.live_translation("img-B", session_id="bob")
    assert live_alice.original == "A"
    assert live_bob.original == "B"
    assert live_alice.stale is False
    assert live_bob.stale is False


def test_translator_empty_base64_returns_none(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(
        mgr.ocr, "extract_and_translate",
        lambda *a, **kw: pytest.fail("should not be called"),
    )
    assert mgr.translator.live_translation("", session_id="x") is None


def test_translator_reset_session_clears_cache(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(
        mgr.ocr, "extract_and_translate",
        lambda *a, **kw: TranslationResult(
            original="o", translated="t", target_language="de",
        ),
    )
    mgr.translator.live_translation("a", session_id="zap")
    assert "zap" in mgr.translator._sessions  # type: ignore[attr-defined]
    mgr.translator.reset_session("zap")
    assert "zap" not in mgr.translator._sessions  # type: ignore[attr-defined]


def test_translator_live_falls_back_to_cache_on_api_failure(monkeypatch):
    mgr = _mgr()
    good = TranslationResult(original="o", translated="t", target_language="de")
    state = {"call": 0}

    def fake_ocr(*a, **kw):
        state["call"] += 1
        return good if state["call"] == 1 else None

    monkeypatch.setattr(mgr.ocr, "extract_and_translate", fake_ocr)
    # First call seeds the cache.
    first = mgr.translator.live_translation("x", session_id="fb")
    assert first is not None and first.stale is False

    # Fast-forward past the rate-limit window so the second call is
    # actually issued — and have it fail. The translator should hand
    # back the stale cache instead of None.
    import server.vision.translator as t_mod
    real_monotonic = t_mod.time.monotonic
    monkeypatch.setattr(
        t_mod.time, "monotonic", lambda: real_monotonic() + 5.0,
    )
    second = mgr.translator.live_translation("y", session_id="fb")
    assert second is not None
    assert second.stale is True
    assert second.original == "o"
