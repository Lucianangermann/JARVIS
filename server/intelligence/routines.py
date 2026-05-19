"""Built-in routines: pure assembly of spoken briefings from tools.

A routine pulls data from the tools/ package and returns a single
German text string. The IntelligenceManager forwards that string to
TTS + WebSocket so it reaches every connected client. Routines are
side-effect-free apart from logging — schedulers, cooldowns, and
delivery channels live one layer up.

Adding a new routine here means: import the tools you need, write a
function returning ``str``, and register it on the manager.
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from ..tools import calendar_tool, weather

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

_WEEKDAY_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
               "Freitag", "Samstag", "Sonntag"]
_MONTH_DE = ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
             "Juli", "August", "September", "Oktober", "November", "Dezember"]


def _greeting(now: datetime) -> str:
    h = now.hour
    if h < 5:   return "Späten Abend"
    if h < 12:  return "Guten Morgen"
    if h < 18:  return "Guten Tag"
    if h < 22:  return "Guten Abend"
    return "Späten Abend"


def _fmt_event(ev: calendar_tool.CalendarEvent) -> str:
    title = ev.title or "ohne Titel"
    if ev.is_all_day:
        return f"{title} (ganztägig)"
    hhmm = ev.start.astimezone(_LOCAL_TZ).strftime("%H:%M")
    suffix = f" in {ev.location}" if ev.location else ""
    return f"{hhmm} {title}{suffix}"


def morning_briefing() -> str:
    """Greeting, weekday, weather, today's calendar — one paragraph.

    Each sub-section degrades independently: a failed weather lookup
    just drops that sentence; the rest of the briefing still goes out.
    """
    now = datetime.now(_LOCAL_TZ)
    lines: list[str] = [
        # strftime('%B') gives "May" under the C locale — we keep a
        # fixed German month table instead of touching process-global
        # locale state, which can race with other libs.
        f"{_greeting(now)}. Heute ist {_WEEKDAY_DE[now.weekday()]}, "
        f"der {now.day}. {_MONTH_DE[now.month]}."
    ]

    # ── weather ────────────────────────────────────────────────────
    try:
        w = weather.get_current()
    except Exception as exc:  # noqa: BLE001
        w = None
        print(f"[routine] weather lookup failed: {exc}")
    if w is not None:
        sentence = (
            f"Das Wetter in {w.location_label}: {w.temp_c:.0f} Grad, "
            f"{w.condition}"
        )
        if w.precipitation_mm >= 0.3:
            sentence += (f", aktuell {w.precipitation_mm:.1f} "
                         f"Millimeter Niederschlag")
        lines.append(sentence + ".")

    # ── today's calendar ───────────────────────────────────────────
    try:
        events = calendar_tool.get_today_events()
    except Exception as exc:  # noqa: BLE001
        events = []
        print(f"[routine] calendar lookup failed: {exc}")
    # Drop events that already ended; the user cares about what's
    # ahead, not the breakfast meeting at 7 if it's already 10am.
    upcoming = [e for e in events if e.end >= now]

    if not upcoming:
        lines.append("Heute stehen keine Termine im Kalender.")
    elif len(upcoming) == 1:
        lines.append(f"Ein Termin heute: {_fmt_event(upcoming[0])}.")
    else:
        lines.append(
            f"Du hast heute {len(upcoming)} Termine. Als nächstes: "
            f"{_fmt_event(upcoming[0])}."
        )
        # Optional follow-up: list the next two so the user knows the
        # shape of the day without us reading the whole calendar aloud.
        rest = upcoming[1:3]
        if rest:
            lines.append(
                "Danach: " + ", ".join(_fmt_event(e) for e in rest) + "."
            )

    return " ".join(lines)
