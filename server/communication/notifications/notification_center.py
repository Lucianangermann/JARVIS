"""Unified notification hub for ALL JARVIS systems.

Every system *can* route a notification through here instead of speaking
directly, so priority filtering, Do-Not-Disturb, quiet hours, and
delivery-channel selection happen in one place. The migration is phased
(see tasks/todo.md): new communication + security/intelligence
notifications go through the center; legacy direct ``tts.speak`` calls
are left untouched for now.

The center is decoupled from the actual transports: callers inject a
voice handler (TTS), a UI publisher (event bus), a Telegram sender, and a
macOS-toast handler. Any missing handler is simply skipped — a comms
failure must never crash JARVIS.

Priority → channels (spec §7):
    critical : voice + ui + telegram + macos   (always, even in DND)
    high     : voice + ui + telegram
    medium   : ui + telegram
    low      : ui only, batched
    info     : logged only
DND / quiet hours suppress everything except ``critical``; suppressed
items are queued and surfaced by :meth:`batch_summary`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

PRIORITY_LEVELS: dict[str, int] = {
    "critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4,
}

_CHANNELS_BY_PRIORITY: dict[str, list[str]] = {
    "critical": ["voice", "ui", "telegram", "macos"],
    "high":     ["voice", "ui", "telegram"],
    "medium":   ["ui", "telegram"],
    "low":      ["ui"],
    "info":     [],
}

VoiceHandler = Callable[[str], None]
UIHandler = Callable[[dict[str, Any]], None]
TelegramHandler = Callable[[str, str, str], None]   # title, body, priority
MacosHandler = Callable[[str, str], None]           # title, body
MeetingProbe = Callable[[], bool]


class NotificationCenter:
    """Priority-filtered, multi-channel notification delivery."""

    def __init__(
        self,
        db: Any = None,
        voice_handler: VoiceHandler | None = None,
        ui_handler: UIHandler | None = None,
        telegram_handler: TelegramHandler | None = None,
        macos_handler: MacosHandler | None = None,
        meeting_probe: MeetingProbe | None = None,
        quiet_start: str = "",
        quiet_end: str = "",
    ) -> None:
        self._db = db
        self._voice = voice_handler
        self._ui = ui_handler
        self._telegram = telegram_handler
        self._macos = macos_handler
        self._meeting_probe = meeting_probe

        self._quiet = self._parse_quiet(quiet_start, quiet_end)
        self._dnd_until: float | None = None  # None = off; inf = until cleared
        self._dnd_allow_critical = True

        self._lock = threading.Lock()
        # Suppressed / batched low-priority items awaiting a summary.
        self._batch: list[dict[str, Any]] = []
        # Everything not yet delivered (for get_pending()).
        self._pending: list[dict[str, Any]] = []

    # ── channel handler wiring (settable post-construction) ────────────── #

    def set_handlers(
        self, *, voice: VoiceHandler | None = None, ui: UIHandler | None = None,
        telegram: TelegramHandler | None = None, macos: MacosHandler | None = None,
        meeting_probe: MeetingProbe | None = None,
    ) -> None:
        if voice is not None: self._voice = voice
        if ui is not None: self._ui = ui
        if telegram is not None: self._telegram = telegram
        if macos is not None: self._macos = macos
        if meeting_probe is not None: self._meeting_probe = meeting_probe

    # ── core send ──────────────────────────────────────────────────────── #

    def send(
        self,
        title: str,
        body: str,
        priority: str = "medium",
        source: str = "jarvis",
        channels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Deliver a notification. Returns {delivered_via, suppressed}.
        Synchronous + best-effort; safe to call from any thread."""
        if priority not in PRIORITY_LEVELS:
            priority = "medium"
        is_critical = priority == "critical"

        # Resolve target channels.
        targets = channels if channels is not None else \
            list(_CHANNELS_BY_PRIORITY.get(priority, []))

        # Suppression: DND / quiet hours / meeting. Critical always passes.
        suppressed_reason = None
        if not is_critical:
            if self._in_dnd():
                suppressed_reason = "dnd"
            elif self._in_quiet_hours():
                suppressed_reason = "quiet_hours"
            elif priority in ("medium", "low") and self._in_meeting():
                suppressed_reason = "meeting"
            elif priority == "low":
                # Low priority is always batched, never spoken live.
                suppressed_reason = "batched"

        record = {
            "title": title, "body": body, "priority": priority,
            "source": source, "ts": time.time(),
        }

        if suppressed_reason is not None:
            with self._lock:
                self._pending.append(record)
                if priority in ("low", "medium"):
                    self._batch.append(record)
            self._persist(title, body, priority, source, [])
            return {"delivered_via": [], "suppressed": suppressed_reason}

        delivered = self._dispatch(title, body, priority, targets)
        self._persist(title, body, priority, source, delivered)
        return {"delivered_via": delivered, "suppressed": None}

    def _dispatch(self, title: str, body: str, priority: str,
                  targets: list[str]) -> list[str]:
        delivered: list[str] = []
        spoken = f"{title}. {body}" if title else body
        if "voice" in targets and self._voice is not None:
            if self._safe(lambda: self._voice(spoken), "voice"):
                delivered.append("voice")
        if "ui" in targets and self._ui is not None:
            event = {"type": "jarvis_notification", "priority": priority,
                     "text": spoken, "title": title}
            if self._safe(lambda: self._ui(event), "ui"):
                delivered.append("ui")
        if "telegram" in targets and self._telegram is not None:
            if self._safe(lambda: self._telegram(title, body, priority), "telegram"):
                delivered.append("telegram")
        if "macos" in targets and self._macos is not None:
            if self._safe(lambda: self._macos(title, body), "macos"):
                delivered.append("macos")
        return delivered

    @staticmethod
    def _safe(fn: Callable[[], Any], name: str) -> bool:
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[NotificationCenter] {name} channel failed: {exc}")
            return False

    def _persist(self, title: str, body: str, priority: str, source: str,
                 delivered: list[str]) -> None:
        if self._db is not None:
            self._db.log_notification(title, body, priority, source, delivered)

    # ── pending / batch ────────────────────────────────────────────────── #

    def get_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._pending,
                          key=lambda r: PRIORITY_LEVELS.get(r["priority"], 4))

    def batch_summary(self, drain: bool = True) -> str:
        """Spoken summary of batched (low/medium suppressed) items. Called
        hourly by the scheduler, or on demand ('benachrichtigungen
        zusammenfassen')."""
        with self._lock:
            items = list(self._batch)
            if drain:
                self._batch.clear()
                # Batched items have now been surfaced — clear them from
                # pending too.
                self._pending = [
                    p for p in self._pending if p not in items
                ]
        if not items:
            return "Keine ausstehenden Benachrichtigungen."
        # Group by source for a compact summary.
        by_source: dict[str, int] = {}
        for it in items:
            by_source[it["source"]] = by_source.get(it["source"], 0) + 1
        parts = [f"{n} von {src}" for src, n in by_source.items()]
        return (f"Du hast {len(items)} Benachrichtigungen: "
                + ", ".join(parts) + ".")

    # ── DND / quiet hours ──────────────────────────────────────────────── #

    def set_dnd(self, enabled: bool, until: str | None = None,
                allow_critical: bool = True) -> dict[str, Any]:
        self._dnd_allow_critical = allow_critical
        if not enabled:
            self._dnd_until = None
            return {"dnd": False}
        if until:
            self._dnd_until = self._next_time_today(until)
        else:
            self._dnd_until = float("inf")  # until explicitly cleared
        return {"dnd": True, "until": until or "manual"}

    def is_dnd(self) -> bool:
        return self._in_dnd()

    def _in_dnd(self) -> bool:
        if self._dnd_until is None:
            return False
        if self._dnd_until == float("inf"):
            return True
        if time.time() >= self._dnd_until:
            self._dnd_until = None  # auto-expire
            return False
        return True

    def set_quiet_hours(self, start: str, end: str) -> dict[str, Any]:
        self._quiet = self._parse_quiet(start, end)
        return {"quiet_hours": {"start": start, "end": end}}

    def _in_quiet_hours(self) -> bool:
        if self._quiet is None:
            return False
        start, end = self._quiet
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end  # window wraps midnight

    def _in_meeting(self) -> bool:
        if self._meeting_probe is None:
            return False
        try:
            return bool(self._meeting_probe())
        except Exception:  # noqa: BLE001
            return False

    # ── time helpers ───────────────────────────────────────────────────── #

    @staticmethod
    def _parse_quiet(start: str, end: str) -> tuple[int, int] | None:
        try:
            if not start or not end:
                return None
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
            return (sh * 60 + sm, eh * 60 + em)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _next_time_today(hhmm: str) -> float:
        """Epoch for the next occurrence of HH:MM (today, or tomorrow if
        already past)."""
        try:
            h, m = (int(x) for x in hhmm.split(":"))
            now = time.localtime()
            target = time.struct_time((
                now.tm_year, now.tm_mon, now.tm_mday, h, m, 0,
                now.tm_wday, now.tm_yday, now.tm_isdst,
            ))
            ts = time.mktime(target)
            if ts <= time.time():
                ts += 86400
            return ts
        except Exception:  # noqa: BLE001
            return float("inf")
