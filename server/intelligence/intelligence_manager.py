"""Coordinator for the intelligence layer.

Slice 1 + 2 responsibilities:
  • own the background Scheduler
  • register the morning / work-start / lunch / evening routines on
    Mon–Fri at configurable times
  • expose ``run_routine(name)`` and the legacy ``briefing_now()``
    for manual triggers (brain trigger phrases, future API routes)
  • provide a small ``get_context_for_brain()`` injection string

The brain holds an optional reference to this manager and short-
circuits to ``run_routine()`` when the user asks for a briefing —
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
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import routines
from .context import ContextEngine
from .proactive import ProactiveEngine
from .scheduler import Scheduler

# Shared DB file with the memory layer. Hard-coded relative to the
# repo root so dev runs and the .app bundle launch agree on the path.
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "jarvis.db"
_DB_PATH = Path(os.getenv("JARVIS_DB_PATH") or _DEFAULT_DB)

# Proactive engine ticks. 60 s matches the scheduler's minute
# resolution; anything finer just wastes a check we'd skip on cooldown.
_PROACTIVE_TICK_SECONDS = 60

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

# Default weekly schedule: briefings fire Monday through Friday.
# Weekends stay quiet unless the user later overrides it via custom
# routines (slice 2c, not yet implemented).
_DEFAULT_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon..Fri

# Routine registry — name → assembler. Adding a new built-in routine
# means: add the function to routines.py, register it here, optionally
# add a default schedule below.
_ROUTINE_REGISTRY: dict[str, Callable[[], str]] = {
    "morning":    routines.morning_briefing,
    "work_start": routines.work_start_briefing,
    "lunch":      routines.lunch_briefing,
    "evening":    routines.evening_briefing,
    "weekly":     routines.weekly_summary,
}


def _flag(name: str, default: str = "1") -> bool:
    """Truthy env flag — empty / 0 / false / no all mean off."""
    val = (os.getenv(name) or default).strip().lower()
    return val not in {"", "0", "false", "no"}


def _hhmm(env_name: str, default: str) -> str:
    return (os.getenv(env_name) or default).strip()


class IntelligenceManager:
    def __init__(self) -> None:
        self.scheduler = Scheduler()
        self.context = ContextEngine()
        # Proactive engine is created upfront so brain.py / API can
        # reach it for debug, but only started if INTELLIGENCE_ENABLED
        # and JARVIS_PROACTIVE_ENABLED are both on.
        self.proactive = ProactiveEngine(
            db_path=_DB_PATH,
            context=self.context,
        )
        self._proactive_enabled = _flag("JARVIS_PROACTIVE_ENABLED", "1")
        self._enabled = _flag("INTELLIGENCE_ENABLED", "1")
        # One master switch for all scheduled briefings. Manual triggers
        # via the brain (typed/spoken phrases) work regardless — this
        # only gates the time-of-day auto-fires.
        self._briefings_enabled = _flag("MORNING_BRIEFING_ENABLED", "1")
        # Each routine's daily clock time is independently overridable
        # — useful for early risers or different work hours.
        self._schedule: dict[str, str] = {
            "morning":    _hhmm("WAKE_TIME",          "08:00"),
            "work_start": _hhmm("WORK_START_TIME",    "09:00"),
            "lunch":      _hhmm("LUNCH_TIME",         "12:30"),
            "evening":    _hhmm("EVENING_TIME",       "18:00"),
            "weekly":     _hhmm("WEEKLY_SUMMARY_TIME", "18:00"),
        }
        # Optional callback: text → side-effect (TTS, WS push). The
        # server registers one in main.py; without it, scheduled
        # briefings just print to stdout (useful in dev).
        self._briefing_handler: Callable[[str], None] | None = None

    # --- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        if not self._enabled:
            print("[INTEL] disabled (INTELLIGENCE_ENABLED=0)")
            return
        if self._briefings_enabled:
            for name, hhmm in self._schedule.items():
                # Default-arg trick on the lambda freezes `name` per
                # iteration — without it every job would close over
                # the same `name` reference and all four would run
                # the last routine.
                weekdays = {6} if name == "weekly" else _DEFAULT_WEEKDAYS
                self.scheduler.daily(
                    hhmm,
                    lambda n=name: self._fire_routine(n),
                    weekdays=weekdays,
                    name=f"{name}-briefing",
                )
            print("[INTEL] briefings scheduled Mon–Fri: " +
                  ", ".join(f"{n}@{t}" for n, t in self._schedule.items()))
        if self._proactive_enabled:
            # One tick per minute is plenty — every check is either
            # cheap (battery, context) or already TTL-cached
            # (calendar). The scheduler isolates check exceptions per
            # spec, so a misbehaving tool can't take the loop down.
            self.scheduler.every(
                _PROACTIVE_TICK_SECONDS,
                self.proactive.tick,
                name="proactive-tick",
            )
            print(f"[INTEL] proactive engine enabled "
                  f"(tick every {_PROACTIVE_TICK_SECONDS}s)")
        self.scheduler.start()
        print("[INTEL] scheduler active")

    def stop(self) -> None:
        self.scheduler.stop()

    # --- routines ------------------------------------------------------- #

    def set_briefing_handler(self, fn: Callable[[str], None]) -> None:
        """Register the function the scheduler will call with the
        assembled briefing text. The server wires this to TTS + WS
        push so every connected client hears scheduled briefings
        without us re-implementing those channels here."""
        self._briefing_handler = fn

    def set_notification_handler(
        self, fn: Callable[[str, str], None],
    ) -> None:
        """Register the delivery sink for proactive notifications.

        Signature is ``(text, priority)`` — priority is one of
        ``"high" | "medium" | "low"``. The server wires this to the
        same TTS + WS path as scheduled briefings, with a small priority
        adjustment so low-priority items can stay silent on the speakers
        if the user is in a meeting."""
        self.proactive.set_handler(fn)

    def set_client(self, client: object) -> None:
        """Wire the Anthropic client so LLM-enhanced routines can use it."""
        self._client = client

    def run_routine(self, name: str) -> str | None:
        """Assemble a routine on demand by name. Returns the text
        ready for TTS / display, or ``None`` if no routine of that
        name is registered. Assembly errors are caught so callers
        always get either a usable string or None."""
        fn = _ROUTINE_REGISTRY.get(name)
        if fn is None:
            return None
        try:
            client = getattr(self, "_client", None)
            if name == "morning" and client is not None:
                return fn(client=client)  # type: ignore[call-arg]
            return fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[INTEL] routine {name!r} failed: {exc}")
            return None

    def briefing_now(self) -> str:
        """Back-compat shim — the slice-1 brain trigger called this
        for the morning briefing. Kept so old call-sites don't break;
        new code should use ``run_routine("morning")`` directly."""
        return self.run_routine("morning") or "Briefing konnte nicht erstellt werden."

    def _fire_routine(self, name: str) -> None:
        text = self.run_routine(name)
        if not text:
            return
        if self._briefing_handler is None:
            print(f"[INTEL] {name} briefing (no handler): {text}")
            return
        try:
            self._briefing_handler(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[INTEL] briefing handler crashed: {exc}")

    # --- per-turn ingress for the context engine ----------------------- #

    def record_command(self, text: str) -> None:
        """Brain calls this on every user turn so the ContextEngine
        can track command frequency, recency, and intent keywords.
        No-op when intelligence is disabled."""
        if not self._enabled:
            return
        try:
            self.context.record_command(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[INTEL] record_command failed: {exc}")

    # --- context for brain --------------------------------------------- #

    def get_context_for_brain(self) -> str:
        """Short context string suitable for injection into Claude's
        system prompt. Slice 1 emitted local time + next event;
        slice 3 layers ContextEngine's prose summary on top (time
        bucket, activity, stress, location, response-style hint).
        Always best-effort: returns "" on total failure."""
        if not self._enabled:
            return ""
        parts: list[str] = []
        # 1) Local time + next calendar event (slice 1)
        try:
            now = datetime.now(_LOCAL_TZ)
            wd = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                  "Freitag", "Samstag", "Sonntag"][now.weekday()]
            parts.append(f"Lokale Zeit: {wd} {now.strftime('%H:%M')}.")
        except Exception:  # noqa: BLE001
            pass
        calendar_busy = False
        try:
            from datetime import timedelta
            from ..tools import calendar_tool
            # ONE calendar query per turn for both signals — the
            # AppleScript bridge is slow (200-800 ms healthy, 3-15 s
            # when something pins Calendar.app) and we don't want
            # context-building to dominate reply latency. A 48-hour
            # window covers both "next event" and "is anything
            # happening right now"; the calendar_tool layer caches
            # results across turns.
            now_local = datetime.now(_LOCAL_TZ)
            events = calendar_tool.get_events(
                now_local - timedelta(hours=1),
                now_local + timedelta(days=2),
            )
            nxt = next((e for e in events if e.start > now_local), None)
            if nxt is not None:
                hhmm = nxt.start.astimezone(_LOCAL_TZ).strftime("%H:%M")
                title = nxt.title or "ohne Titel"
                parts.append(f"Nächster Termin: {hhmm} {title}.")
            for ev in events:
                if ev.start <= now_local < ev.end and not ev.is_all_day:
                    calendar_busy = True
                    break
        except Exception:  # noqa: BLE001
            pass

        # 2) ContextEngine block (slice 3) — activity, stress, style
        try:
            parts.append(self.context.prompt_block(calendar_busy=calendar_busy))
        except Exception as exc:  # noqa: BLE001
            print(f"[INTEL] context prompt_block failed: {exc}")

        return " ".join(parts)
