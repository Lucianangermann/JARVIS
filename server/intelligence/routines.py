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
from pathlib import Path
from zoneinfo import ZoneInfo

from datetime import timedelta

from ..tools import calendar_tool, news, weather

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

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

    try:
        from ..productivity.productivity_manager import ProductivityManager as _PM
        _pm = _PM(_DATA_DIR / "jarvis.db")
        addon = _pm.morning_brief_addon()
        if addon:
            lines.append(addon)
    except Exception:
        pass

    try:
        from ..entertainment.birthdays import check_todays_birthdays as _birthdays
        bd = _birthdays()
        if bd:
            lines.append(f"Geburtstag heute: {bd}!")
    except Exception:
        pass

    # Security overnight summary. Read the events table directly (a
    # concurrent WAL reader) instead of building a second SecurityManager,
    # which would spawn a duplicate monitor thread.
    try:
        import time as _t
        from ..security.db import SecurityDB
        _sdb = SecurityDB(_DATA_DIR / "security.db")
        _rows = _sdb.query(
            "SELECT COUNT(*) AS n FROM security_events "
            "WHERE timestamp >= ? AND severity IN ('HIGH','CRITICAL')",
            (_t.time() - 43200,),
        )
        _sdb.close()
        _n = _rows[0]["n"] if _rows else 0
        lines.append(
            f"Sicherheit: {_n} relevante Ereignisse über Nacht."
            if _n else "Sicherheit: keine Vorkommnisse über Nacht."
        )
    except Exception:
        pass

    # Communication signal — overnight Telegram messages + missed calls from
    # communication.db (concurrent WAL read; iMessage unread is surfaced live
    # via the "neue nachrichten" command, not here).
    try:
        import time as _t
        from ..communication.db import CommunicationDB
        _cdb = CommunicationDB(_DATA_DIR / "communication.db")
        _since = _t.time() - 43200
        _tg = _cdb.query(
            "SELECT COUNT(*) AS n FROM messages WHERE direction='in' "
            "AND platform='telegram' AND timestamp >= ?", (_since,))
        _miss = _cdb.query(
            "SELECT COUNT(*) AS n FROM calls WHERE outcome='missed' "
            "AND timestamp >= ?", (_since,))
        _cdb.close()
        _tn = _tg[0]["n"] if _tg else 0
        _mn = _miss[0]["n"] if _miss else 0
        if _tn or _mn:
            bits = []
            if _tn:
                bits.append(f"{_tn} Telegram-Nachricht{'en' if _tn != 1 else ''}")
            if _mn:
                bits.append(f"{_mn} verpasste Anrufe")
            lines.append("Kommunikation: " + ", ".join(bits) + ".")
    except Exception:
        pass

    # Second Brain — flashcards due for review today (the heart of spaced
    # repetition: without the daily nudge you never review).
    try:
        from ..knowledge.flashcards import FlashcardManager
        _fm = FlashcardManager(_DATA_DIR / "knowledge.db")
        _due = _fm.due_count()
        _fm.close()
        if _due:
            lines.append(f"Lernen: {_due} Karteikarte"
                         f"{'n' if _due != 1 else ''} stehen zur Wiederholung an.")
    except Exception:
        pass

    # Finance — over-budget categories (construct WITHOUT start() so no market
    # poll thread is spawned for a one-shot read).
    try:
        from ..finance import FinanceManager
        _fin = FinanceManager(_DATA_DIR / "finance.db")
        _fb = _fin.morning_brief()
        _fin.stop()
        if _fb:
            lines.append("Finanzen: " + _fb)
    except Exception:
        pass

    return " ".join(lines)


def work_start_briefing() -> str:
    """Short morning kick-off: today's meetings + a single headline.

    Lighter than morning_briefing — meant to fire when the user
    actually sits down to work, not when the alarm goes off. Skips
    weather (already heard at breakfast) and skips the calendar
    overview if there's nothing on today.
    """
    now = datetime.now(_LOCAL_TZ)
    lines: list[str] = ["Arbeitsstart."]

    try:
        events = calendar_tool.get_today_events()
    except Exception as exc:  # noqa: BLE001
        events = []
        print(f"[routine] calendar lookup failed: {exc}")
    upcoming = [e for e in events if e.end >= now]
    if upcoming:
        lines.append(f"Heute {len(upcoming)} Termin"
                     f"{'e' if len(upcoming) != 1 else ''}. "
                     f"Erster: {_fmt_event(upcoming[0])}.")
    else:
        lines.append("Heute keine Termine im Kalender.")

    try:
        top = news.get_headlines(n=1)
    except Exception as exc:  # noqa: BLE001
        top = []
        print(f"[routine] news lookup failed: {exc}")
    if top:
        lines.append(f"Schlagzeile: {top[0].title}.")

    return " ".join(lines)


def lunch_briefing() -> str:
    """Mittagspause: what's still on the calendar after lunch + the
    afternoon weather hint. Skips past meetings."""
    now = datetime.now(_LOCAL_TZ)
    lines: list[str] = ["Mittagspause."]

    try:
        events = calendar_tool.get_today_events()
    except Exception as exc:  # noqa: BLE001
        events = []
        print(f"[routine] calendar lookup failed: {exc}")
    afternoon = [e for e in events if e.start >= now and not e.is_all_day]
    if afternoon:
        lines.append(
            f"Nachmittag: {len(afternoon)} Termin"
            f"{'e' if len(afternoon) != 1 else ''}. "
            f"Als nächstes: {_fmt_event(afternoon[0])}."
        )
    else:
        lines.append("Der Nachmittag ist termintechnisch frei.")

    # Re-pull current weather. Open-Meteo's "current" snapshot is
    # close enough to "afternoon" for a useful one-line cue.
    try:
        w = weather.get_current()
    except Exception:  # noqa: BLE001
        w = None
    if w is not None:
        lines.append(f"Draußen {w.temp_c:.0f} Grad, {w.condition}.")

    return " ".join(lines)


def evening_briefing() -> str:
    """Ausblick auf morgen: tomorrow's first meeting + tomorrow's
    weather. Closes out the work day."""
    now = datetime.now(_LOCAL_TZ)
    tomorrow_start = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    tomorrow_end = tomorrow_start + timedelta(days=1)

    lines: list[str] = ["Feierabend."]

    # ── tomorrow's first calendar event ────────────────────────────
    try:
        events = calendar_tool.get_events(tomorrow_start, tomorrow_end)
    except Exception as exc:  # noqa: BLE001
        events = []
        print(f"[routine] calendar lookup failed: {exc}")
    if events:
        first = events[0]
        lines.append(f"Morgen, erster Termin: {_fmt_event(first)}.")
        if len(events) > 1:
            lines.append(f"Insgesamt {len(events)} Termine morgen.")
    else:
        lines.append("Morgen keine Termine im Kalender — freier Tag.")

    # ── tomorrow's weather ─────────────────────────────────────────
    try:
        forecast = weather.get_forecast(days=2)
    except Exception:  # noqa: BLE001
        forecast = []
    # forecast[0] = today, forecast[1] = tomorrow. Defensive index.
    if len(forecast) >= 2:
        tmrw = forecast[1]
        rain = ""
        if tmrw.precipitation_mm >= 1.0:
            rain = f", {tmrw.precipitation_mm:.0f} mm Niederschlag"
        lines.append(
            f"Wetter morgen: {tmrw.temp_min_c:.0f} bis "
            f"{tmrw.temp_max_c:.0f} Grad, {tmrw.condition}{rain}."
        )

    try:
        from ..productivity.productivity_manager import ProductivityManager as _PM
        _pm = _PM(_DATA_DIR / "jarvis.db")
        addon = _pm.evening_brief_addon()
        if addon:
            lines.append(addon)
    except Exception:
        pass

    return " ".join(lines)
