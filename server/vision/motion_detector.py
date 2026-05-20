"""Camera-based motion detector with Claude-Vision callbacks.

The detector opens an OpenCV ``VideoCapture`` in a background thread,
computes a frame-to-frame difference, and when the diff exceeds a
sensitivity threshold it captures the current frame, hands it to
``VisionManager.analyze_image`` with a "what / who is this" prompt,
and delivers the model's reply through a caller-supplied callback.

Why this is small
-----------------
Motion detection itself stays local — the OpenCV diff costs nothing
and runs at the camera frame rate. We only spend a Claude call when
the diff trips the threshold AND we're past the per-event cooldown.
That keeps the API bill at "one call per actual event" rather than
"one call per frame".

Privacy
-------
A ``[JARVIS 📷 CAMERA MONITORING ACTIVE]`` line prints when the loop
starts and ``CAMERA OFF`` when it stops, mirroring the screen
indicator pattern in ``screen_reader.py``. The default monitor has
a hard 5-minute auto-shutoff so an accidental "start watching" can't
leave the camera light burning all afternoon.

Failure model
-------------
Every entry point is wrapped in best-effort try/except. ``start()``
returns ``False`` if OpenCV is missing, the camera can't be opened,
or another monitor is already running. Frame-grab failures inside
the loop don't tear down the thread — they just skip that tick.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .vision_manager import VisionManager


# Per-spec sensitivity presets. The numeric value is the minimum
# ratio of "differing pixels" (after threshold) over total pixels
# that we treat as motion. Tuned on a 640×480 webcam stream — too
# small and a passing fly trips it; too large and a slow door swing
# slips through.
_SENSITIVITY: dict[str, float] = {
    "low":    0.05,   # ~5 % of pixels must change
    "medium": 0.02,
    "high":   0.008,
}

# Pixel-level threshold used by cv2.threshold() to binarise the
# frame difference. Higher = ignore more lighting noise.
_DIFF_THRESHOLD = 25
# How long to wait between two Claude calls for the same monitoring
# session. Prevents one continuously-moving object (e.g. a fan) from
# burning a steady stream of vision tokens.
_EVENT_COOLDOWN_S = 8.0
# Hard auto-shutoff. Belt-and-braces in case the user forgets and
# walks away with the camera running.
_AUTO_SHUTOFF_S = 5 * 60.0


@dataclass
class MotionEvent:
    """One detected motion event. ``analysis`` is whatever Claude
    returned for the captured frame; ``None`` means the API call
    failed and the event is informational only (the local detector
    still fired)."""
    timestamp: float
    analysis: str | None
    frame_b64: str | None


# Callback type — the brain wires this to a function that publishes
# the event over the event bus and (optionally) speaks the analysis
# through TTS.
MotionHandler = Callable[[MotionEvent], None]


class MotionDetector:
    """One running monitor at a time. Tracks its own thread + stop
    event so the caller doesn't have to.

    Stateful: holds the OpenCV ``VideoCapture`` reference and the
    most recent frame for the diff comparison. Tests can swap the
    ``_open_camera`` and ``_grab_frame`` methods to feed synthetic
    frames without touching the real camera.
    """

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._handler: MotionHandler | None = None
        self._sensitivity: str = "medium"
        self._camera_index: int = 0
        self._last_event_at: float = 0.0
        self._started_at: float = 0.0

    # --- lifecycle ---------------------------------------------------- #

    def start(
        self,
        *,
        sensitivity: str = "medium",
        camera_index: int = 0,
        handler: MotionHandler | None = None,
    ) -> bool:
        """Start the monitoring loop. Returns ``False`` if it didn't
        come up (deps missing, camera busy, already running). The
        loop runs in a daemon thread — quitting the server terminates
        it cleanly; explicit ``stop()`` is preferred to release the
        camera quickly."""
        if self._thread is not None and self._thread.is_alive():
            print("[VISION] motion monitor already running — ignoring start")
            return False
        if sensitivity not in _SENSITIVITY:
            print(f"[VISION] unknown sensitivity {sensitivity!r}, "
                  f"falling back to 'medium'")
            sensitivity = "medium"

        # Defer the OpenCV import to start-time so a vision package
        # that didn't install opencv-python still imports cleanly.
        try:
            import cv2  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] OpenCV not available — `pip install "
                  f"opencv-python<4.11`: {exc}")
            return False

        self._sensitivity = sensitivity
        self._camera_index = camera_index
        self._handler = handler
        self._stop.clear()
        self._last_event_at = 0.0
        self._started_at = time.monotonic()

        # Daemon=True is OK here because the loop closes its own
        # VideoCapture inside the finally block. macOS's audio-device
        # double-free concern (see voice_loop) doesn't apply: OpenCV's
        # camera handle is process-owned and the OS reclaims it on
        # process exit even without explicit release.
        self._thread = threading.Thread(
            target=self._run, name="jarvis-vision-motion", daemon=True,
        )
        self._thread.start()
        print(f"[JARVIS 📷 CAMERA MONITORING ACTIVE] "
              f"(sensitivity={sensitivity}, camera={camera_index})")
        return True

    def stop(self) -> None:
        """Signal the loop to exit and wait briefly for it to finish.
        Safe to call repeatedly. Always prints the camera-off
        indicator even if nothing was running."""
        if self._thread is not None and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=2.0)
        self._thread = None
        print("[JARVIS 📷 CAMERA OFF]")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- one-shot capture (no background loop) ----------------------- #

    def capture_once(
        self,
        *,
        camera_index: int = 0,
        prompt: str = "Beschreibe was du auf diesem Kamerabild siehst.",
    ) -> str | None:
        """Take a single frame and ask Claude about it. Used for the
        "ist jemand da?" trigger that doesn't want a continuous
        monitor running."""
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] OpenCV not available: {exc}")
            return None

        print("[JARVIS 📷 CAMERA SNAPSHOT]")
        cap = None
        try:
            cap = self._open_camera(cv2, camera_index)
            if cap is None:
                return None
            frame = self._grab_frame(cap)
            if frame is None:
                print("[VISION] camera returned no frame on snapshot")
                return None
            b64 = self._frame_to_base64(cv2, frame)
            if b64 is None:
                return None
            return self._mgr.analyze_image(b64, prompt)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
            print("[JARVIS 📷 CAMERA OFF]")

    # --- loop --------------------------------------------------------- #

    def _run(self) -> None:
        """Background thread body. Runs until ``stop()`` or the
        auto-shutoff timer fires."""
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] motion loop aborted: {exc}")
            return

        cap = self._open_camera(cv2, self._camera_index)
        if cap is None:
            return

        threshold_ratio = _SENSITIVITY[self._sensitivity]
        prev_gray: Any = None
        try:
            while not self._stop.is_set():
                # Auto-shutoff after the global cap.
                if time.monotonic() - self._started_at > _AUTO_SHUTOFF_S:
                    print(f"[VISION] motion monitor auto-shutoff after "
                          f"{_AUTO_SHUTOFF_S/60:.0f} min")
                    break

                frame = self._grab_frame(cap)
                if frame is None:
                    # Transient camera hiccup — back off briefly and
                    # try again instead of tearing the loop down.
                    if self._stop.wait(timeout=0.1):
                        break
                    continue

                gray = self._to_diff_gray(cv2, frame)
                if prev_gray is None:
                    prev_gray = gray
                    continue

                ratio = self._motion_ratio(cv2, prev_gray, gray)
                prev_gray = gray
                if ratio < threshold_ratio:
                    # No event — sleep a touch so we don't peg a CPU
                    # on cheap frame-rate cameras. Stop-event-aware.
                    if self._stop.wait(timeout=0.05):
                        break
                    continue

                # Trip the event subject to cooldown.
                now = time.monotonic()
                if now - self._last_event_at < _EVENT_COOLDOWN_S:
                    continue
                self._last_event_at = now
                self._handle_event(cv2, frame)
        finally:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _handle_event(self, cv2: Any, frame: Any) -> None:
        """Convert the trigger frame to base64, ask Claude what's in
        it, and forward the event to the user-supplied handler."""
        b64 = self._frame_to_base64(cv2, frame)
        analysis: str | None = None
        if b64 is not None:
            analysis = self._mgr.analyze_image(
                b64,
                "Auf der Kamera wurde Bewegung erkannt. Beschreibe "
                "was du siehst. Ist es eine Person, ein Tier oder ein "
                "Objekt? Sollte der Nutzer benachrichtigt werden? "
                "Antworte kurz auf Deutsch.",
            )
        event = MotionEvent(
            timestamp=time.time(),
            analysis=analysis,
            frame_b64=b64,
        )
        if self._handler is not None:
            try:
                self._handler(event)
            except Exception as exc:  # noqa: BLE001
                print(f"[VISION] motion handler crashed: {exc}")
        else:
            print(f"[VISION] motion event (no handler): "
                  f"{(analysis or 'no analysis')[:120]}")

    # --- OpenCV plumbing (extracted for test mocking) ---------------- #

    @staticmethod
    def _open_camera(cv2: Any, index: int) -> Any:
        """Open the camera at ``index``. Returns ``None`` and prints
        a diagnostic if it can't be opened (in use, no permission,
        bad index)."""
        try:
            cap = cv2.VideoCapture(index)
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] cv2.VideoCapture({index}) raised: {exc}")
            return None
        if not cap.isOpened():
            print(f"[VISION] camera {index} not available — likely in "
                  f"use by another app or missing macOS Camera "
                  f"permission for the JARVIS Electron bundle.")
            return None
        # Modest resolution keeps frame-diff cheap; the user can
        # bump this later via env if motion-on-distant-objects needs
        # more pixels.
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        except Exception:  # noqa: BLE001
            pass
        return cap

    @staticmethod
    def _grab_frame(cap: Any) -> Any:
        try:
            ok, frame = cap.read()
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] camera read raised: {exc}")
            return None
        if not ok:
            return None
        return frame

    @staticmethod
    def _to_diff_gray(cv2: Any, frame: Any) -> Any:
        """Pre-process a frame for the diff: convert to grayscale and
        blur slightly to suppress single-pixel noise."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (21, 21), 0)

    @staticmethod
    def _motion_ratio(cv2: Any, prev: Any, curr: Any) -> float:
        """Return the fraction of pixels that changed past the
        ``_DIFF_THRESHOLD`` between two prepared gray frames."""
        diff = cv2.absdiff(prev, curr)
        _, thresh = cv2.threshold(diff, _DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
        nonzero = int(cv2.countNonZero(thresh))
        total = max(1, thresh.shape[0] * thresh.shape[1])
        return nonzero / float(total)

    @staticmethod
    def _frame_to_base64(cv2: Any, frame: Any) -> str | None:
        """JPEG-encode a BGR frame for the Claude Vision payload."""
        try:
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] frame JPEG encode failed: {exc}")
            return None
        if not ok:
            return None
        import base64 as _b64
        return _b64.b64encode(buf.tobytes()).decode("ascii")
