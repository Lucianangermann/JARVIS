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


def morning_briefing(*, client: object = None) -> str:
    """Greeting, weekday, weather, today's calendar — one paragraph.

    Each sub-section degrades independently: a failed weather lookup
    just drops that sentence; the rest of the briefing still goes out.
    When ``client`` is provided, appends an LLM-generated daily plan.
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
        try:
            addon = _pm.morning_brief_addon()
            if addon:
                lines.append(addon)
        finally:
            _pm.stop()  # close the 3 jarvis.db connections it opened
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

    # Gestrige Gespräche — pull yesterday's session summaries from ChromaDB
    # so JARVIS can reference what was discussed the day before.
    try:
        from ..memory.long_term import LongTermMemory as _LTM
        _lt = _LTM(_DATA_DIR / "chromadb")
        if _lt.available:
            _sessions = _lt.get_recent_sessions(days=1)
            n_sess = len(_sessions)
            if n_sess:
                plural = "e" if n_sess != 1 else ""
                lines.append(
                    f"Gestern {n_sess} Gespräch{plural} mit JARVIS — "
                    "Details auf Anfrage verfügbar."
                )
    except Exception:
        pass

    # ── LLM-generated daily plan ────────────────────────────────────
    if client is not None:
        try:
            _plan_ctx: list[str] = []
            # Open tasks with deadlines
            from ..productivity.task_manager import TaskManager as _TM2
            _tm2 = _TM2(_DATA_DIR / "jarvis.db")
            try:
                _tasks = _tm2._conn.execute(
                    "SELECT title, due_date, priority FROM tasks "
                    "WHERE status IN ('todo','in_progress') "
                    "ORDER BY CASE WHEN due_date IS NOT NULL THEN due_date ELSE '9999' END, "
                    "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END "
                    "LIMIT 6"
                ).fetchall()
            finally:
                _tm2._conn.close()
            if _tasks:
                task_lines = []
                for t in _tasks:
                    entry = t["title"] or "?"
                    if t["due_date"]:
                        entry += f" (fällig {t['due_date']})"
                    if t["priority"] == "high":
                        entry += " [HOCH]"
                    task_lines.append(entry)
                _plan_ctx.append("Offene Tasks: " + "; ".join(task_lines))
            # Yesterday's productivity score
            from ..productivity.analytics import ProductivityAnalytics as _PA2
            _pa2 = _PA2(_DATA_DIR / "jarvis.db")
            try:
                import datetime as _dt2
                _yesterday = _dt2.date.today() - _dt2.timedelta(days=1)
                _yd_start = _dt2.datetime(
                    _yesterday.year, _yesterday.month, _yesterday.day
                ).timestamp()
                _yd_end = _yd_start + 86400
                _yd_done = _pa2._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='done' "
                    "AND completed_at >= ? AND completed_at < ?",
                    (_yd_start, _yd_end),
                ).fetchone()[0]
                _yd_focus = _pa2._conn.execute(
                    "SELECT COALESCE(SUM(duration_minutes),0) FROM time_entries "
                    "WHERE start_time >= ? AND start_time < ?",
                    (_yd_start, _yd_end),
                ).fetchone()[0]
                if _yd_done or _yd_focus:
                    _plan_ctx.append(
                        f"Gestern: {_yd_done} Tasks erledigt, "
                        f"{int(_yd_focus)}min fokussiert gearbeitet."
                    )
            finally:
                _pa2._conn.close()
            if _plan_ctx:
                _prompt_body = "\n".join(_plan_ctx)
                _resp = client.messages.create(  # type: ignore[union-attr]
                    model="claude-haiku-4-5-20251001",
                    max_tokens=120,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Erstelle einen kurzen Tagesplan in 2-3 Sätzen auf Deutsch. "
                            "Nenne konkrete Prioritäten — was zuerst, warum. "
                            "Kein Markdown, keine Listen. Nur Fließtext.\n\n"
                            + _prompt_body
                        ),
                    }],
                )
                for _b in _resp.content:
                    if getattr(_b, "type", None) == "text" and _b.text:
                        lines.append("Empfehlung für heute: " + _b.text.strip())
                        break
        except Exception:  # noqa: BLE001
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
        try:
            addon = _pm.evening_brief_addon()
            if addon:
                lines.append(addon)
        finally:
            _pm.stop()
    except Exception:
        pass

    return " ".join(lines)


def weekly_summary() -> str:
    """Friday-evening recap — what happened this week across all modules.

    Pulls completed tasks, finished Lernziele, budget burn, and flashcard
    activity. Each section degrades independently on errors.
    """
    now = datetime.now(_LOCAL_TZ)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    lines: list[str] = ["Wochenrückblick."]

    # ── completed tasks ────────────────────────────────────────────────
    try:
        from ..productivity.task_manager import TaskManager as _TM
        tm = _TM(_DATA_DIR / "jarvis.db")
        try:
            done = tm._conn.execute(
                "SELECT title FROM tasks WHERE status='done' AND completed_at >= ?",
                (week_start.timestamp(),),
            ).fetchall()
        finally:
            tm._conn.close()
        n = len(done)
        if n:
            lines.append(f"{n} Task{'s' if n != 1 else ''} abgeschlossen.")
    except Exception:  # noqa: BLE001
        pass

    # ── finished Lernziele ─────────────────────────────────────────────
    try:
        from ..knowledge.lerntrack import LerntrackDB as _LT
        lt = _LT(_DATA_DIR / "lerntrack.db")
        try:
            finished = lt.list_group(status="abgeschlossen")
            this_week = [
                r for r in finished
                if r.get("updated_at", 0) >= week_start.timestamp()
            ]
        finally:
            lt.close()
        n = len(this_week)
        if n:
            names = ", ".join(r["display_name"] for r in this_week[:3])
            extra = " ..." if n > 3 else ""
            plural = "e" if n != 1 else ""
            lines.append(f"{n} Lernziel{plural} abgeschlossen: {names}{extra}.")
    except Exception:  # noqa: BLE001
        pass

    # ── budget burn ────────────────────────────────────────────────────
    try:
        from ..finance import FinanceManager as _FM
        fm = _FM(_DATA_DIR / "finance.db")
        try:
            rows = fm.expenses.budget_status()
        finally:
            conn = getattr(getattr(fm, "expenses", None), "_db", None)
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if rows:
            total_spent = sum(r["spent"] for r in rows)
            total_limit = sum(r["limit"] for r in rows)
            cur = rows[0].get("currency", "EUR")
            over = [r for r in rows if r.get("over")]
            budget_line = (f"Ausgaben: {total_spent:.0f} von {total_limit:.0f} {cur}.")
            if over:
                cats = ", ".join(r["category"] for r in over[:2])
                budget_line += f" Überzogen: {cats}."
            lines.append(budget_line)
    except Exception:  # noqa: BLE001
        pass

    # ── flashcard activity ─────────────────────────────────────────────
    try:
        from ..knowledge.flashcards import FlashcardManager as _FC
        fc = _FC(_DATA_DIR / "knowledge.db")
        try:
            due = fc.due_count()
        finally:
            fc.close()
        if due:
            plural = "n" if due != 1 else ""
            lines.append(f"Noch {due} Karteikarte{plural} fällig.")
    except Exception:  # noqa: BLE001
        pass

    # ── spending trend ─────────────────────────────────────────────────
    try:
        from ..finance import FinanceManager as _FM2
        fm2 = _FM2(_DATA_DIR / "finance.db")
        try:
            trend = fm2.expenses.spoken_trend()
        finally:
            conn2 = getattr(getattr(fm2, "expenses", None), "_db", None)
            if conn2:
                try:
                    conn2.close()
                except Exception:  # noqa: BLE001
                    pass
        if trend and "stabil" not in trend and "keine" not in trend.lower():
            lines.append(trend)
    except Exception:  # noqa: BLE001
        pass

    # ── mood / wellbeing trend ─────────────────────────────────────────
    try:
        from ..productivity.mood_tracker import MoodTracker as _Mood
        _mood = _Mood(_DATA_DIR / "jarvis.db")
        try:
            _mood_text = _mood.spoken_weekly()
        finally:
            _mood.close()
        if _mood_text:
            lines.append(_mood_text)
    except Exception:  # noqa: BLE001
        pass

    # ── deep work + deadline risk ──────────────────────────────────────
    try:
        from ..productivity.analytics import ProductivityAnalytics as _PA
        pa = _PA(_DATA_DIR / "jarvis.db")
        try:
            dw_blocks = pa.deep_work_blocks(since_ts=week_start.timestamp())
            risk = pa.deadline_risk_score()
            proj_dist = pa.project_time_distribution(since_ts=week_start.timestamp())
        finally:
            try:
                pa._conn.close()
            except Exception:  # noqa: BLE001
                pass
        if dw_blocks:
            total_dw = sum(b["minutes"] for b in dw_blocks)
            h, m = int(total_dw // 60), int(total_dw % 60)
            dw_label = f"{h}h {m}min" if h else f"{m}min"
            n_dw = len(dw_blocks)
            lines.append(f"{n_dw} Deep-Work-Block{'s' if n_dw != 1 else ''}, gesamt {dw_label}.")
        if risk["at_risk"]:
            n_risk = len(risk["at_risk"])
            titles = ", ".join(t["title"] for t in risk["at_risk"][:2])
            extra = " ..." if n_risk > 2 else ""
            lines.append(f"Achtung: {n_risk} Task{'s' if n_risk != 1 else ''} bald fällig — {titles}{extra}.")
        if proj_dist:
            top = proj_dist[0]
            h2, m2 = int(top["minutes"] // 60), int(top["minutes"] % 60)
            lbl = f"{h2}h {m2}min" if h2 else f"{m2}min"
            lines.append(f"Meiste Zeit in '{top['label']}' ({lbl}).")
    except Exception:  # noqa: BLE001
        pass

    # ── active long-term goals ─────────────────────────────────────────
    try:
        from ..productivity.goals import GoalDB as _GoalDB
        gdb = _GoalDB(_DATA_DIR / "jarvis.db")
        try:
            goal_text = gdb.weekly_spoken()
        finally:
            gdb.close()
        if goal_text:
            lines.append(goal_text)
    except Exception:  # noqa: BLE001
        pass

    if len(lines) == 1:
        lines.append("Keine Aktivitäten diese Woche erfasst.")

    lines.append("Schönes Wochenende!")
    return " ".join(lines)


def session_greeting(*, client: object = None) -> str:
    """1-2 sentence warm opening for a new JARVIS session.

    Synthesises open tasks, lerntrack, flashcard due count, and last
    mood score into a concise spoken context. When a Claude client is
    provided, Haiku writes a natural version; otherwise a template is
    used. Always best-effort: each sub-section degrades independently.
    """
    now = datetime.now(_LOCAL_TZ)
    context_parts: list[str] = []

    # ── open tasks ────────────────────────────────────────────────────
    try:
        from ..productivity.task_manager import TaskManager as _TM
        tm = _TM(_DATA_DIR / "jarvis.db")
        try:
            row_ov = tm._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','in_progress') "
                "AND due_date < date('now')"
            ).fetchone()
            row_td = tm._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','in_progress') "
                "AND due_date = date('now')"
            ).fetchone()
            n_ov = row_ov["n"] if row_ov else 0
            n_td = row_td["n"] if row_td else 0
        finally:
            tm._conn.close()
        if n_ov:
            s = "s" if n_ov != 1 else ""
            context_parts.append(f"{n_ov} überfällige{s} Task{s}")
        if n_td:
            s = "s" if n_td != 1 else ""
            context_parts.append(f"{n_td} Task{s} für heute fällig")
    except Exception:
        pass

    # ── open lernziele ────────────────────────────────────────────────
    try:
        from ..knowledge.lerntrack import LerntrackDB as _LT
        lt = _LT(_DATA_DIR / "lerntrack.db")
        try:
            open_subs = lt.list_group(status="offen")
        finally:
            lt.close()
        if open_subs:
            s = "e" if len(open_subs) != 1 else ""
            context_parts.append(f"{len(open_subs)} offene{s} Lernziel{s}")
    except Exception:
        pass

    # ── flashcards due ────────────────────────────────────────────────
    try:
        from ..knowledge.flashcards import FlashcardManager as _FC
        fc = _FC(_DATA_DIR / "knowledge.db")
        try:
            due = fc.due_count()
        finally:
            fc.close()
        if due:
            s = "n" if due != 1 else ""
            context_parts.append(f"{due} Karteikarte{s} fällig")
    except Exception:
        pass

    # ── last mood score ───────────────────────────────────────────────
    try:
        from ..productivity.mood_tracker import MoodTracker as _Mood
        mood = _Mood(_DATA_DIR / "jarvis.db")
        try:
            today_mood = mood.today_mood()
        finally:
            mood.close()
        if today_mood:
            score = today_mood.get("score")
            if score is not None and score <= 4:
                context_parts.append(f"Stimmung heute {score}/10")
    except Exception:
        pass

    context = "; ".join(context_parts) if context_parts else ""

    if client is None or not context:
        greet = _greeting(now)
        return f"{greet}. {context}." if context else f"{greet}."

    prompt = (
        f"Es ist {now.strftime('%H:%M')} Uhr.\n"
        f"Offene Punkte: {context}\n\n"
        "Begrüße den Nutzer kurz auf Deutsch in 1-2 Sätzen. "
        "Erwähne, was heute ansteht. Direkt zum Punkt, kein Opener wie 'Guten Morgen'. "
        "Kein Markdown."
    )
    try:
        from anthropic import Anthropic as _A
        _client = _A() if client is True else client  # type: ignore[arg-type]
        resp = _client.messages.create(  # type: ignore[union-attr]
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text" and block.text:
                return block.text.strip()
    except Exception as exc:
        print(f"[SessionGreeting] LLM failed: {exc}")

    return f"{_greeting(now)}. {context}." if context else f"{_greeting(now)}."
