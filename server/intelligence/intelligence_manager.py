"""Coordinator for the intelligence layer.

Slice 1 responsibilities:
  • own the background Scheduler
  • register the morning routine to fire at WAKE_TIME on weekdays
  • expose ``briefing_now()`` for manual triggers (chat, API)
  • provide a small ``get_context_for_brain()`` injection string

The brain holds an optional reference to this manager and short-
circuits to ``briefing_now()`` when the user asks for a briefing —
so an unhealthy manager never blocks normal chat. The server's
lifespan owns ``start()``/``stop()``.

Later slices grow ``get_context_for_brain()`` (activity, stress,
predictions), wire notifications/triggers, and add API routes for
custom routines. The shape below is intentionally narrow so those
additions don't ripple into brain.py.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import routines
from .scheduler import Scheduler

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

# Default weekly schedule: morning briefing fires Monday through
# Friday. Weekends keep quiet unless the user later overrides it.
_DEFAULT_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon..Fri


def _flag(name: str, default: str = "1") -> bool:
    """Truthy env flag — empty / 0 / false / no all mean off."""
    val = (os.getenv(name) or default).strip().lower()
    return val not in {"", "0", "false", "no"}


class IntelligenceManager:
    def __init__(self) -> None:
        self.scheduler = Scheduler()
        self._enabled = _flag("INTELLIGENCE_ENABLED", "1")
        self._briefing_enabled = _flag("MORNING_BRIEFING_ENABLED", "1")
        self._wake_hhmm = (os.getenv("WAKE_TIME") or "08:00").strip()
        # Optional callback: text → side-effect (TTS, WS push). The
        # server registers one in main.py; without it, scheduled
        # briefings just print to stdout (useful in dev).
        self._briefing_handler: Callable[[str], None] | None = None

    # --- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        if not self._enabled:
            print("[INTEL] disabled (INTELLIGENCE_ENABLED=0)")
            return
        if self._briefing_enabled:
            self.scheduler.daily(
                self._wake_hhmm,
                self._fire_morning_briefing,
                weekdays=_DEFAULT_WEEKDAYS,
                name="morning-briefing",
            )
            print(f"[INTEL] morning briefing scheduled for {self._wake_hhmm} "
                  f"(Mon–Fri)")
        self.scheduler.start()
        print("[INTEL] scheduler active")

    def stop(self) -> None:
        self.scheduler.stop()

    # --- briefing ------------------------------------------------------- #

    def set_briefing_handler(self, fn: Callable[[str], None]) -> None:
        """Register the function the scheduler will call with the
        assembled briefing text. The server wires this to TTS + WS
        push so every connected client hears the morning briefing
        without us re-implementing those channels here."""
        self._briefing_handler = fn

    def briefing_now(self) -> str:
        """Build the morning briefing on demand. Used by the brain
        when the user types/says a briefing trigger phrase, and by
        the scheduled daily fire below."""
        try:
            return routines.morning_briefing()
        except Exception as exc:  # noqa: BLE001 — never crash
            print(f"[INTEL] briefing assembly failed: {exc}")
            return "Briefing konnte nicht erstellt werden."

    def _fire_morning_briefing(self) -> None:
        text = self.briefing_now()
        if self._briefing_handler is None:
            print(f"[INTEL] morning briefing (no handler): {text}")
            return
        try:
            self._briefing_handler(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[INTEL] briefing handler crashed: {exc}")

    # --- context for brain --------------------------------------------- #

    def get_context_for_brain(self) -> str:
        """Short context string suitable for injection into Claude's
        system prompt. Slice 1 only emits local time + next-event;
        future slices append activity, stress level, recent
        notifications. Always best-effort: returns "" when nothing
        useful was found, never raises."""
        if not self._enabled:
            return ""
        parts: list[str] = []
        try:
            now = datetime.now(_LOCAL_TZ)
            wd = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                  "Freitag", "Samstag", "Sonntag"][now.weekday()]
            parts.append(f"Lokale Zeit: {wd} {now.strftime('%H:%M')}.")
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..tools import calendar_tool
            nxt = calendar_tool.get_next_event()
            if nxt is not None:
                hhmm = nxt.start.astimezone(_LOCAL_TZ).strftime("%H:%M")
                title = nxt.title or "ohne Titel"
                parts.append(f"Nächster Termin: {hhmm} {title}.")
        except Exception:  # noqa: BLE001
            pass
        return " ".join(parts)
