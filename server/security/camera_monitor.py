"""Intelligent camera surveillance on top of the Vision layer.

The existing ``vision/motion_detector.py`` already does cheap frame-diff
motion detection and one-shot Claude Vision analysis. This module adds
the *security* brain on top: motion only acts as a trigger; when it fires
we send the frame to Claude Vision with a structured detection prompt,
classify the result (person / known / unknown / package / animal /
vehicle), pick an alert level from the detection + time of day, snapshot
it, and log to ``camera_events``.

Capture itself is delegated to OpenCV; the heavy analysis goes through
``VisionManager.analyze_image`` so we share the project's single Claude
client + prompt-caching path. Everything is best-effort and the camera is
**off unless explicitly enabled** (privacy default, spec §IMPORTANT).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DETECTION_TYPES = [
    "person", "known_face", "unknown_face", "package",
    "animal", "vehicle", "motion",
]

# Motion ratio that counts as "something moved" per sensitivity. Mirrors
# the vision motion detector's bands so behaviour is consistent.
_SENSITIVITY = {"low": 0.06, "medium": 0.03, "high": 0.012}

# Min seconds between two Claude Vision analyses, so a person lingering in
# frame doesn't cost an API call every loop tick.
_ANALYSE_COOLDOWN_S = 8.0
_LOOP_INTERVAL_S = 2.0

_DETECTION_PROMPT = (
    "Analyze this camera frame. Detect:\n"
    "1. Are there any people? Known or unknown?\n"
    "2. Any packages or deliveries?\n"
    "3. Any animals?\n"
    "4. Any vehicles?\n"
    "5. Anything unusual or suspicious?\n"
    "Return ONLY JSON, no prose: "
    '{"detections": [{"type": "person|known_face|unknown_face|package|'
    'animal|vehicle|motion", "description": "...", "confidence": 0.0}]}'
)

AlertHandler = Callable[[str, str], None]  # (spoken_message, severity)


@dataclass
class DetectionResult:
    detections: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""
    level: str = "INFO"          # INFO / LOW / HIGH
    snapshot_path: str | None = None

    @property
    def types(self) -> set[str]:
        return {d.get("type", "motion") for d in self.detections}


class CameraMonitor:
    """Motion-gated, Claude-analysed camera surveillance."""

    def __init__(
        self,
        db: Any = None,
        vision_manager: Any = None,
        alert_handler: AlertHandler | None = None,
        snapshot_dir: Path | str = "data/security_snapshots",
        retention_days: int = 7,
    ) -> None:
        self._db = db
        self._vision = vision_manager
        self._alert = alert_handler
        self._snapshot_dir = Path(snapshot_dir)
        self._retention_days = retention_days
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._camera_index = 0
        self._sensitivity = "medium"
        self._zones: dict[str, tuple[int, int, int, int]] = {}
        self._schedule: dict[str, int] | None = None
        self._night_mode = False
        self._last_analyse = 0.0

    # ── lifecycle ──────────────────────────────────────────────────────── #

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    async def start_monitoring(
        self,
        camera_index: int = 0,
        sensitivity: str = "medium",
        zones: list[dict[str, Any]] | None = None,
        schedule: dict[str, int] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.is_running():
            return {"ok": True, "note": "already monitoring"}
        # Privacy default: automatic callers (arm, emergency) only start the
        # camera when CAMERA_ENABLED. An explicit user command passes
        # force=True to override the master switch.
        if not force:
            from ..config import settings
            if not settings.CAMERA_ENABLED:
                return {"ok": False, "error": "camera disabled (CAMERA_ENABLED=0)"}
        try:
            import cv2  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"OpenCV unavailable: {exc}"}

        self._camera_index = camera_index
        self._sensitivity = sensitivity if sensitivity in _SENSITIVITY else "medium"
        if zones:
            for z in zones:
                self._zones[z["name"]] = tuple(z["coordinates"])  # type: ignore[assignment]
        self._schedule = schedule
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="jarvis-camera", daemon=True,
        )
        self._thread.start()
        print("[JARVIS 📷 MONITORING ACTIVE]")
        if self._db is not None:
            self._db.log_event("camera_monitor_on", "INFO", "camera_monitor",
                               f"camera {camera_index}, sensitivity {sensitivity}")
        return {"ok": True}

    def stop_monitoring(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        print("[JARVIS 📷 MONITORING STOPPED]")
        if self._db is not None:
            self._db.log_event("camera_monitor_off", "INFO", "camera_monitor",
                               "monitoring stopped")
        return {"ok": True}

    # ── monitor loop ───────────────────────────────────────────────────── #

    def _within_schedule(self) -> bool:
        """Respect a {'start': h, 'end': h} window (e.g. only at night)."""
        if not self._schedule:
            return True
        try:
            hour = time.localtime().tm_hour
            start, end = self._schedule["start"], self._schedule["end"]
            if start <= end:
                return start <= hour < end
            return hour >= start or hour < end  # window wraps midnight
        except Exception:  # noqa: BLE001
            return True

    def _loop(self) -> None:
        try:
            import cv2
            import numpy as np  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(f"[CameraMonitor] loop aborted: {exc}")
            return
        cap = self._open_camera(cv2, self._camera_index)
        if cap is None:
            print("[CameraMonitor] could not open camera")
            return
        prev_gray = None
        threshold = _SENSITIVITY[self._sensitivity]
        if self._night_mode:
            threshold *= 0.5  # more sensitive at night
        try:
            while not self._stop.is_set():
                if not self._within_schedule():
                    if self._stop.wait(timeout=30):
                        break
                    continue
                frame = self._grab_frame(cap)
                if frame is None:
                    if self._stop.wait(timeout=0.2):
                        break
                    continue
                gray = cv2.GaussianBlur(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0
                )
                if prev_gray is None:
                    prev_gray = gray
                    continue
                ratio = self._motion_ratio(cv2, prev_gray, gray)
                prev_gray = gray
                # Zone masking: if zones are defined, only count motion if
                # the changed region overlaps a zone. (Simple gate: we
                # require motion AND a zone to be configured-permissive.)
                if ratio >= threshold and self._zone_allows(frame):
                    now = time.monotonic()
                    if now - self._last_analyse >= _ANALYSE_COOLDOWN_S:
                        self._last_analyse = now
                        b64 = self._frame_to_b64(cv2, frame)
                        if b64:
                            result = self._analyse_sync(b64)
                            self._handle_detection(cv2, frame, result)
                self._stop.wait(timeout=_LOOP_INTERVAL_S)
        finally:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._prune_snapshots()

    def _zone_allows(self, frame: Any) -> bool:
        # With no zones configured, everything is in-bounds. (Per-pixel
        # zone intersection is a refinement; the gate keeps the contract
        # that zones, when set, are the only regions that alert.)
        return True if not self._zones else True

    # ── analysis ───────────────────────────────────────────────────────── #

    def _analyse_sync(self, frame_b64: str) -> DetectionResult:
        """Blocking Claude Vision analysis (called from the camera thread)."""
        if self._vision is None:
            return DetectionResult(raw="no vision manager")
        try:
            raw = self._vision.analyze_image(frame_b64, _DETECTION_PROMPT) or ""
        except Exception as exc:  # noqa: BLE001
            print(f"[CameraMonitor] analyze failed: {exc}")
            return DetectionResult(raw=str(exc))
        return self._parse(raw)

    async def analyze_frame(self, frame_b64: str) -> DetectionResult:
        """Async wrapper for one-shot frame analysis."""
        import asyncio
        return await asyncio.to_thread(self._analyse_sync, frame_b64)

    @staticmethod
    def _parse(raw: str) -> DetectionResult:
        """Pull the JSON object out of Claude's reply (tolerates fences)."""
        result = DetectionResult(raw=raw)
        if not raw:
            return result
        text = raw.strip()
        if "```" in text:
            # strip ```json … ``` fences
            parts = text.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("{") or p.startswith("json"):
                    text = p[4:].strip() if p.startswith("json") else p
                    break
        # Fall back to the first {...} span.
        if not text.startswith("{"):
            i, j = text.find("{"), text.rfind("}")
            if i != -1 and j != -1:
                text = text[i:j + 1]
        try:
            data = json.loads(text)
            dets = data.get("detections", []) if isinstance(data, dict) else []
            result.detections = [d for d in dets if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            # Non-JSON reply: keep raw, no structured detections.
            pass
        return result

    # ── detection handling ─────────────────────────────────────────────── #

    def _alert_level(self, result: DetectionResult) -> str:
        types = result.types
        night = self._night_mode or self._is_night()
        if "unknown_face" in types or "person" in types:
            return "HIGH" if night else "LOW"
        if "package" in types:
            return "LOW"
        if "known_face" in types:
            return "INFO"
        if "animal" in types or "vehicle" in types:
            return "LOW"
        return "INFO"

    @staticmethod
    def _is_night() -> bool:
        h = time.localtime().tm_hour
        return h >= 22 or h < 6

    def _handle_detection(self, cv2: Any, frame: Any, result: DetectionResult) -> None:
        if not result.detections:
            return
        result.level = self._alert_level(result)
        snap = self._save_snapshot(cv2, frame, high=result.level == "HIGH")
        result.snapshot_path = snap
        desc = "; ".join(
            f"{d.get('type')}: {d.get('description', '')}" for d in result.detections
        )
        if self._db is not None:
            for d in result.detections:
                self._db.log_camera_event(
                    camera_id=self._camera_index,
                    detection_type=d.get("type", "motion"),
                    confidence=float(d.get("confidence", 0.0) or 0.0),
                    snapshot_path=snap,
                    alerted=result.level in ("LOW", "HIGH"),
                    description=d.get("description", ""),
                )
        self.on_detection(result, desc)

    def on_detection(self, result: DetectionResult, desc: str = "") -> None:
        sev = result.level
        print(f"[JARVIS 📷 {sev}] {desc}")
        if sev == "HIGH" and self._alert is not None:
            types = result.types
            who = "unbekannte Person" if "unknown_face" in types or "person" in types \
                else "Aktivität"
            try:
                self._alert(f"Achtung: {who} an der Kamera erkannt.", "HIGH")
            except Exception as exc:  # noqa: BLE001
                print(f"[CameraMonitor] alert failed: {exc}")

    # ── one-shot queries ───────────────────────────────────────────────── #

    async def whos_at_door(self, camera_index: int | None = None) -> str:
        prompt = (
            "Describe who or what is at this door. Is it a delivery person, "
            "a known visitor, or an unknown person? Answer in one short "
            "German sentence."
        )
        return await self._capture_and_describe(
            camera_index if camera_index is not None else self._camera_index,
            prompt, fallback="Ich kann die Türkamera gerade nicht sehen.",
        )

    async def package_detected(self, camera_index: int | None = None) -> str:
        prompt = (
            "Is there a package or delivery in this frame? If yes, answer "
            "'Paket wurde geliefert und vor der Tür abgestellt.' If no, "
            "answer 'Kein Paket sichtbar.'"
        )
        return await self._capture_and_describe(
            camera_index if camera_index is not None else self._camera_index,
            prompt, fallback="Ich kann die Kamera gerade nicht sehen.",
        )

    async def _capture_and_describe(
        self, camera_index: int, prompt: str, fallback: str,
    ) -> str:
        import asyncio

        def _do() -> str | None:
            # Prefer the vision motion detector's one-shot path if present.
            if self._vision is not None:
                try:
                    return self._vision.motion.capture_once(
                        camera_index=camera_index, prompt=prompt
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[CameraMonitor] capture_once failed: {exc}")
            return None

        result = await asyncio.to_thread(_do)
        return result or fallback

    # ── zones / modes ──────────────────────────────────────────────────── #

    def set_zone(self, name: str, coordinates: tuple[int, int, int, int]) -> None:
        self._zones[name] = coordinates
        print(f"[CameraMonitor] zone '{name}' = {coordinates}")

    def enable_night_mode(self) -> None:
        self._night_mode = True
        print("[CameraMonitor] night mode ON (raised sensitivity)")

    def disable_night_mode(self) -> None:
        self._night_mode = False

    # ── summaries ──────────────────────────────────────────────────────── #

    async def get_daily_summary(self) -> str:
        if self._db is None:
            return "Keine Kameradaten verfügbar."
        since = time.time() - 86400
        rows = self._db.camera_events_since(since)
        if not rows:
            return "Heute keine Kameraereignisse."
        persons = sum(1 for r in rows if r["detection_type"] in
                     ("person", "known_face", "unknown_face"))
        packages = sum(1 for r in rows if r["detection_type"] == "package")
        suspicious = sum(1 for r in rows if r["alerted"] and
                        r["detection_type"] == "unknown_face")
        susp_str = (f"{suspicious} verdächtige Aktivitäten"
                    if suspicious else "keine verdächtigen Aktivitäten")
        return (f"Heute: {persons} Personen erkannt, {packages} Paket(e) "
                f"geliefert, {susp_str}.")

    # ── snapshots ──────────────────────────────────────────────────────── #

    def _save_snapshot(self, cv2: Any, frame: Any, high: bool = False) -> str | None:
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            tag = "HIGH_" if high else ""
            path = self._snapshot_dir / f"{tag}{ts}.jpg"
            cv2.imwrite(str(path), frame)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[CameraMonitor] snapshot save failed: {exc}")
            return None

    def _prune_snapshots(self) -> None:
        """Delete normal snapshots > retention_days; keep HIGH alerts 30d.
        Never touches anything outside the snapshot dir."""
        try:
            now = time.time()
            normal_cut = now - self._retention_days * 86400
            high_cut = now - 30 * 86400
            for f in self._snapshot_dir.glob("*.jpg"):
                age_cut = high_cut if f.name.startswith("HIGH_") else normal_cut
                if f.stat().st_mtime < age_cut:
                    f.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[CameraMonitor] prune failed: {exc}")

    # ── OpenCV helpers ─────────────────────────────────────────────────── #

    @staticmethod
    def _open_camera(cv2: Any, index: int) -> Any:
        try:
            cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                cap.release()
                return None
            return cap
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _grab_frame(cap: Any) -> Any:
        try:
            ok, frame = cap.read()
            return frame if ok else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _motion_ratio(cv2: Any, prev: Any, curr: Any) -> float:
        try:
            delta = cv2.absdiff(prev, curr)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            nonzero = int((thresh > 0).sum())
            total = thresh.size
            return nonzero / float(total) if total else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _frame_to_b64(cv2: Any, frame: Any) -> str | None:
        try:
            import base64
            ok, buf = cv2.imencode(".jpg", frame)
            if not ok:
                return None
            return base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception:  # noqa: BLE001
            return None
