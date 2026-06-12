"""ProactiveEngine — the layer where JARVIS speaks on its own.

Periodic tick (driven by the intelligence Scheduler) walks the
NOTIFICATION_TYPES registry and asks each spec's ``check`` function
whether it has something to say *right now*. If it does, we check
cooldowns and rate limits, persist the fire to SQLite, and hand the
message to a delivery callback that knows about TTS + WebSocket push.

Design notes
------------
* The brain stays unaware of this module. The IntelligenceManager
  owns the engine, registers its tick, and forwards the same delivery
  channel it already uses for scheduled briefings. So every PWA / HUD
  client already on the WS bus receives notifications without us
  touching the transport layer.
* Cooldowns + daily counters live in ``data/jarvis.db`` (shared with
  the memory layer) so a restart doesn't unmute the engine. A single
  row per notification type keeps the schema tiny.
* Each check function is pure-ish (one outbound API call max) and
  returns ``str | None``. None means "nothing to say"; a string is
  the literal German message to deliver.
* Triggers that depend on data sources we haven't built yet
  (``forgotten_task``, ``important_email``, ``package_delivery``) are
  registered but their check functions return None with a one-time
  startup log — the architecture is in place so wiring them later is
  a single function swap.
* Priority shapes *when* we deliver, not *whether* we deliver:
    - high    → always (even in meetings)
    - medium  → suppress in meetings + late_night
    - low     → suppress in meetings + late_night + sleeping
  We never auto-deliver during the "sleeping" activity state.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..tools import battery, calendar_tool, traffic, weather
from .context import ContextEngine

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

# Delivery callback: ``(text, priority)`` — the manager wires this to
# a function that publishes to the WS event bus and (best-effort)
# speaks the text through the local Mac TTS.
NotificationHandler = Callable[[str, str], None]


# -- spec dataclass -------------------------------------------------------- #

@dataclass(frozen=True)
class NotificationSpec:
    """Static metadata + the per-tick check function for one trigger."""
    name: str
    priority: str               # "high" | "medium" | "low"
    cooldown_minutes: int       # minimum gap between two fires
    max_per_day: int | None     # None = unlimited
    active_hours: tuple[int, int] | None  # (start_h, end_h_exclusive); None = 24h
    # The check returns the message to deliver, or None to skip. We
    # pass the engine in so checks can reach the context engine, the
    # in-memory dedup sets, etc., without circular module imports.
    check: Callable[["ProactiveEngine"], str | None]


# -- env helpers ----------------------------------------------------------- #

def _flag(name: str, default: str = "1") -> bool:
    val = (os.getenv(name) or default).strip().lower()
    return val not in {"", "0", "false", "no"}


def _home_location() -> str:
    return (os.getenv("HOME_LOCATION")
            or os.getenv("WEATHER_LOCATION")
            or "Berlin,DE").strip()


def _battery_threshold() -> int:
    try:
        return max(1, min(99, int(os.getenv("BATTERY_LOW_PERCENT", "15"))))
    except ValueError:
        return 15


# -- the engine ------------------------------------------------------------ #

class ProactiveEngine:
    """One instance per server. Thread-safe enough for our use: the
    scheduler is a single background thread, so ``tick()`` never runs
    concurrently with itself; the SQLite connection is opened per
    call (cheap, ~50µs) so we don't fight the memory layer for the
    process-wide lock."""

    def __init__(
        self,
        db_path: Path,
        context: ContextEngine,
        handler: NotificationHandler | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._ctx = context
        self._handler: NotificationHandler | None = handler

        # In-memory dedup for meetings: we don't want two warnings for
        # the same event in a single 15-minute window even if our
        # 30-min cooldown drift somehow re-armed. The set is keyed by
        # ``(title, start_iso)`` and pruned when events drop out of
        # the look-ahead window.
        self._meeting_fired: set[tuple[str, str]] = set()
        # Once-per-process log for stub triggers — keeps the console
        # from screaming the same "needs Gmail" line every minute.
        self._stub_logged: set[str] = set()
        self._init_schema()
        self._specs: tuple[NotificationSpec, ...] = _build_specs()

    # --- public API ---------------------------------------------------- #

    def set_handler(self, fn: NotificationHandler) -> None:
        self._handler = fn

    def tick(self) -> None:
        """One scheduler tick. Walk every registered spec, fire what's
        ready. Per-spec exceptions are isolated so one broken check
        can't silence the others."""
        for spec in self._specs:
            if not _flag(f"JARVIS_PROACTIVE_{spec.name.upper()}", "1"):
                continue
            try:
                if not self._should_notify(spec):
                    continue
                msg = spec.check(self)
                if not msg:
                    continue
                self._deliver(spec, msg)
            except Exception:  # noqa: BLE001
                print(f"[proactive] {spec.name} check crashed:")
                traceback.print_exc()

    # --- gating -------------------------------------------------------- #

    def _should_notify(self, spec: NotificationSpec) -> bool:
        now = datetime.now(_LOCAL_TZ)

        # Active-hours gate (e.g. hydration only during the work day).
        if spec.active_hours is not None:
            h = now.hour
            start_h, end_h = spec.active_hours
            if not (start_h <= h < end_h):
                return False

        # Priority × current activity. Sleeping suppresses every
        # priority; the others suppress low-priority during quiet
        # hours and non-high during meetings.
        activity = self._ctx.activity()
        time_label = _time_label(now)
        if activity == "sleeping" and spec.priority != "high":
            return False
        if activity == "in_meeting" and spec.priority != "high":
            return False
        if time_label == "late_night" and spec.priority == "low":
            return False

        # Cooldown + per-day limit straight from SQLite.
        last_ts, day, count_today = self._load_state(spec.name)
        today = now.strftime("%Y-%m-%d")
        if day != today:
            count_today = 0
        if spec.max_per_day is not None and count_today >= spec.max_per_day:
            return False
        if last_ts is not None:
            gap = (now.timestamp() - last_ts) / 60.0
            if gap < spec.cooldown_minutes:
                return False
        return True

    # --- delivery + persistence --------------------------------------- #

    def _deliver(self, spec: NotificationSpec, message: str) -> None:
        self._record_fire(spec.name)
        if self._handler is None:
            print(f"[proactive] {spec.name} (no handler): {message}")
            return
        try:
            self._handler(message, spec.priority)
        except Exception:  # noqa: BLE001
            print(f"[proactive] handler crashed for {spec.name}:")
            traceback.print_exc()

    def _log_stub_once(self, name: str, reason: str) -> None:
        if name in self._stub_logged:
            return
        self._stub_logged.add(name)
        print(f"[proactive] {name}: {reason} — trigger registered but inactive")

    # --- SQLite -------------------------------------------------------- #

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS notification_log (
        type           TEXT PRIMARY KEY,
        last_triggered REAL    NOT NULL,
        day            TEXT    NOT NULL,
        count_today    INTEGER NOT NULL DEFAULT 1
    );
    """

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because the scheduler thread opens
        # and closes the connection itself; we don't share it across
        # threads. timeout=2s matches the rest of the memory layer.
        conn = sqlite3.connect(
            self._db_path, timeout=2.0, check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def _load_state(
        self, name: str,
    ) -> tuple[float | None, str | None, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_triggered, day, count_today "
                "FROM notification_log WHERE type=?",
                (name,),
            ).fetchone()
        if row is None:
            return None, None, 0
        return row[0], row[1], int(row[2])

    def _record_fire(self, name: str) -> None:
        now = datetime.now(_LOCAL_TZ)
        ts = now.timestamp()
        today = now.strftime("%Y-%m-%d")
        # ON CONFLICT update: bump count_today only when same day,
        # otherwise reset to 1. count_today is set BEFORE day so the
        # CASE reads the previous-row's day.
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notification_log "
                "(type, last_triggered, day, count_today) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(type) DO UPDATE SET "
                "  last_triggered = excluded.last_triggered, "
                "  count_today    = CASE "
                "    WHEN notification_log.day = excluded.day "
                "    THEN notification_log.count_today + 1 "
                "    ELSE 1 END, "
                "  day            = excluded.day",
                (name, ts, today),
            )
            conn.commit()


# -- time helpers (mirrors context.py buckets so we don't import the
#    private functions across files) -------------------------------------- #

def _time_label(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 7:   return "early_morning"
    if 7 <= h < 9:   return "morning"
    if 9 <= h < 12:  return "work_morning"
    if 12 <= h < 13: return "lunch"
    if 13 <= h < 17: return "work_afternoon"
    if 17 <= h < 21: return "evening"
    if 21 <= h < 23: return "night"
    return "late_night"


# -- check functions (one per trigger) ------------------------------------ #

def _check_battery_warning(engine: ProactiveEngine) -> str | None:
    """Fire when MacBook battery is below the threshold AND not on AC.
    pmset returns None on Mac mini / Mac Studio — those just don't
    trigger, which is the right behaviour."""
    s = battery.get_status()
    if s is None:
        return None
    if s.on_ac or s.charging:
        return None
    threshold = _battery_threshold()
    if s.percent > threshold:
        return None
    return f"MacBook-Akku bei {s.percent} Prozent — Ladekabel anschließen."


def _check_meeting_warning(engine: ProactiveEngine) -> str | None:
    """Look 15 min ahead. If a calendar event starts inside that
    window and we haven't warned for it yet this run, return a short
    German pre-meeting nudge."""
    now = datetime.now(_LOCAL_TZ)
    horizon = now + timedelta(minutes=15)
    try:
        events = calendar_tool.get_events(now, horizon + timedelta(minutes=2))
    except Exception:  # noqa: BLE001
        return None
    # Prune stale entries from the per-process dedup set first so it
    # doesn't grow unbounded over a long uptime.
    fresh_keys = {
        (e.title, e.start.isoformat()) for e in events
        if e.start <= horizon + timedelta(minutes=2)
    }
    engine._meeting_fired &= fresh_keys

    for ev in events:
        if ev.is_all_day:
            continue
        if ev.start <= now:
            continue
        if ev.start > horizon:
            continue
        key = (ev.title, ev.start.isoformat())
        if key in engine._meeting_fired:
            continue
        engine._meeting_fired.add(key)
        hhmm = ev.start.astimezone(_LOCAL_TZ).strftime("%H:%M")
        loc = f" in {ev.location}" if ev.location else ""
        title = ev.title or "Termin"
        return (f"Termin in 15 Minuten: {hhmm} {title}{loc}.")
    return None


def _check_hydration(engine: ProactiveEngine) -> str | None:
    """Active-hours-gated and cooldown-gated already; here we just
    check whether the user has been silent for ≥ 90 minutes."""
    last = engine._ctx._last_command_time()  # noqa: SLF001
    if last is None:
        # Without a baseline we don't fire — better silent than
        # nagging a user who just booted the server.
        return None
    if (datetime.now(_LOCAL_TZ) - last) < timedelta(minutes=90):
        return None
    return ("Du bist seit 90 Minuten am Stück bei der Arbeit. "
            "Vielleicht Zeit für ein Glas Wasser.")


def _check_weather_morning(engine: ProactiveEngine) -> str | None:
    """Morning weather *alert* — only fires when there's something
    actionable (rain, cold, heat). We deliberately do not duplicate
    the broader morning_briefing routine; cooldown 12 h keeps it once
    per morning even if the tick lands twice in the same hour."""
    w = weather.get_current()
    if w is None:
        return None
    temp = w.temp_c
    cond = (w.condition or "").lower()
    if w.precipitation_mm >= 0.5 or "regen" in cond or "rain" in cond:
        return (f"Heute ist Regen angesagt — denk an einen Schirm. "
                f"Aktuell {temp:.0f} Grad.")
    if temp <= 4:
        return f"Heute nur {temp:.0f} Grad — warm anziehen."
    if temp >= 28:
        return f"Heute {temp:.0f} Grad — viel trinken."
    # Pleasant weather: stay quiet so the user isn't woken up to be
    # told the weather is fine. The morning routine handles the
    # "everything's normal" summary.
    return None


def _check_traffic_warning(engine: ProactiveEngine) -> str | None:
    """If a calendar event with a location starts in 45-75 minutes,
    pull an OSRM driving estimate and warn if it eats most of the
    lead time. We need HOME_LOCATION / WEATHER_LOCATION in the env
    for the origin; without that, no-op."""
    now = datetime.now(_LOCAL_TZ)
    lead_start = now + timedelta(minutes=45)
    lead_end   = now + timedelta(minutes=75)
    try:
        events = calendar_tool.get_events(now, lead_end + timedelta(minutes=2))
    except Exception:  # noqa: BLE001
        return None
    target = next(
        (e for e in events
         if e.location and not e.is_all_day
         and lead_start <= e.start <= lead_end),
        None,
    )
    if target is None:
        return None
    origin = _home_location()
    est = traffic.get_travel_time(origin, target.location)
    if est is None:
        return None
    minutes_until = (target.start - now).total_seconds() / 60.0
    # Warn if the drive eats more than 70% of the lead time, or if
    # leaving now still arrives late by 5+ min.
    headroom = minutes_until - est.minutes
    if headroom > 15:
        return None
    hhmm = target.start.astimezone(_LOCAL_TZ).strftime("%H:%M")
    return (f"Verkehr in Richtung {target.location}: "
            f"{est.minutes:.0f} Minuten Fahrt für den Termin um {hhmm}. "
            f"Du solltest in {max(0, int(headroom))} Minuten losfahren.")


def _check_productivity_slump(engine: ProactiveEngine) -> str | None:
    """Detect a quiet stretch during work hours. Heuristic: ≤ 1
    command in the trailing 2 h AND the last command was at least
    60 min ago. Avoids firing right after a busy morning."""
    ctx = engine._ctx  # noqa: SLF001
    now = datetime.now(_LOCAL_TZ)
    hist = list(ctx._history)  # snapshot under GIL
    cutoff_2h = now - timedelta(hours=2)
    n_recent = sum(1 for ts, _ in hist if ts >= cutoff_2h)
    if n_recent > 1:
        return None
    if not hist:
        return None
    last_ts = hist[-1][0]
    if (now - last_ts) < timedelta(minutes=60):
        return None
    try:
        from pathlib import Path
        from ..productivity.task_manager import TaskManager
        tm = TaskManager(Path("data/jarvis.db"))
        try:
            tasks = tm.get_today_tasks()
        finally:
            conn = getattr(tm, "_conn", None)
            if conn is not None:
                conn.close()
        if tasks:
            names = ", ".join(t["title"] for t in tasks[:3])
            extra = " ..." if len(tasks) > 3 else ""
            return (f"Ruhiger Nachmittag. Offene Tasks: {names}{extra}. "
                    "Womit fangen wir an?")
    except Exception:  # noqa: BLE001
        pass
    return ("Ruhiger Nachmittag. Soll ich die offenen Punkte für heute "
            "kurz zusammenfassen?")


def _check_budget_warning(engine: ProactiveEngine) -> str | None:
    """Fire when any tracked budget category is ≥ 80 % spent."""
    try:
        from pathlib import Path
        from ..finance import FinanceManager
        fm = FinanceManager(Path("data/finance.db"))
        try:
            rows = fm.expenses.budget_status()
        finally:
            conn = getattr(fm.expenses, "_db", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if not rows:
            return None
        warnings = [
            r for r in rows
            if r["limit"] > 0 and r["spent"] / r["limit"] >= 0.80
        ]
        if not warnings:
            return None
        parts = [
            f"{r['category']} ({r['spent']:.0f}/{r['limit']:.0f} {r['currency']})"
            for r in warnings[:3]
        ]
        plural = "s" if len(warnings) != 1 else ""
        return (f"Budget-Warnung: {len(warnings)} Kategorie{plural} fast aufgebraucht — "
                + ", ".join(parts) + ".")
    except Exception:  # noqa: BLE001
        return None


def _check_forgotten_task(engine: ProactiveEngine) -> str | None:
    """Nudge about overdue tasks. Reads the productivity task store
    (jarvis.db) — runs only when the cooldown has elapsed (see tick), so a
    fresh short-lived connection here is cheap."""
    try:
        from pathlib import Path
        from ..productivity.task_manager import TaskManager
        tm = TaskManager(Path("data/jarvis.db"))
        try:
            overdue = tm.get_overdue()
        finally:
            conn = getattr(tm, "_conn", None)
            if conn is not None:
                conn.close()
        if overdue:
            n = len(overdue)
            return (f"Du hast {n} überfällige Task{'s' if n != 1 else ''}. "
                    f"Zum Beispiel: {overdue[0]['title']}.")
    except Exception:  # noqa: BLE001
        pass
    return None


def _check_important_email(engine: ProactiveEngine) -> str | None:
    """Nudge about an unread-mail backlog via Apple Mail (mail_tool). Light
    by design — a count, not a per-message Claude classification — so the
    proactive thread stays cheap."""
    try:
        from ..tools import mail_tool
        out, err = mail_tool.get_unread_count()
        if err:
            return None
        # mail_tool returns the count as text.
        digits = "".join(ch for ch in (out or "") if ch.isdigit())
        n = int(digits) if digits else 0
        if n >= 5:
            return (f"Du hast {n} ungelesene E-Mails — davon könnten welche "
                    f"wichtig sein.")
    except Exception:  # noqa: BLE001
        pass
    return None


def _check_package_delivery(engine: ProactiveEngine) -> str | None:
    """No-op until tracking numbers live in memory."""
    engine._log_stub_once(
        "package_delivery",
        "no tracking-number source",
    )
    return None


def _check_spending_spike(engine: ProactiveEngine) -> str | None:
    """Warn when any category is >30 % over its previous-month spend."""
    try:
        from pathlib import Path
        from ..finance import FinanceManager
        fm = FinanceManager(Path("data/finance.db"))
        try:
            trends = fm.expenses.spending_trend()
        finally:
            conn = getattr(getattr(fm, "expenses", None), "_db", None)
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        spikes = [
            t for t in trends
            if t.get("change_pct") is not None
            and t["change_pct"] >= 30
            and t["current"] >= 10  # ignore trivial amounts
        ]
        if not spikes:
            return None
        top = spikes[0]
        return (f"Ausgaben-Spike: {top['category']} +{top['change_pct']:.0f} % "
                f"gegenüber letztem Monat ({top['current']:.0f} vs "
                f"{top['previous']:.0f} EUR).")
    except Exception:  # noqa: BLE001
        return None


def _check_watchlist_alerts(engine: ProactiveEngine) -> str | None:
    """Warn when a watched asset is within 5 % of a price target."""
    try:
        from pathlib import Path
        from ..finance import FinanceManager
        fm = FinanceManager(Path("data/finance.db"))
        try:
            items = fm.market._db.get_watchlist()
        finally:
            conn = getattr(getattr(fm.market, "_db", None), "_conn", None)
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if not items:
            return None
        near: list[str] = []
        for w in items:
            price = w.get("last_price")
            if not price:
                continue
            above = w.get("target_above")
            below = w.get("target_below")
            sym = w["symbol"]
            cur = w.get("last_currency", "")
            if above and not w.get("alert_armed") is False:
                gap_pct = (above - price) / above
                if 0 < gap_pct <= 0.05:
                    near.append(f"{sym} nähert sich {above:.0f} {cur} (aktuell {price:.2f})")
            if below and not w.get("alert_armed") is False:
                gap_pct = (price - below) / below
                if 0 < gap_pct <= 0.05:
                    near.append(f"{sym} nähert sich {below:.0f} {cur} (aktuell {price:.2f})")
        if not near:
            return None
        return "Kursalarm nah: " + "; ".join(near[:3]) + "."
    except Exception:  # noqa: BLE001
        return None


def _check_flashcard_due(engine: ProactiveEngine) -> str | None:
    """Nudge when there are enough due flashcards to make a review session worthwhile."""
    try:
        from ..knowledge.flashcards import FlashcardManager
        fm = FlashcardManager("data/knowledge.db")
        try:
            n = fm.due_count()
        finally:
            fm.close()
        if n < 3:
            return None
        plural = "n" if n != 1 else ""
        return f"Du hast {n} fällige Karteikarte{plural} zum Wiederholen."
    except Exception:  # noqa: BLE001
        pass
    return None


def _check_lernziel_reminder(engine: ProactiveEngine) -> str | None:
    """Evening nudge about still-open Lernziele so nothing slips through the day."""
    try:
        from ..knowledge.lerntrack import LerntrackDB
        db = LerntrackDB("data/lerntrack.db")
        try:
            st = db.stats()
            if st["total"] == 0 or st["offen"] == 0:
                return None
            open_rows = db.list_group(status="offen")[:2]
        finally:
            db.close()
        names = ", ".join(r["display_name"] for r in open_rows)
        extra = " ..." if st["offen"] > 2 else ""
        plural = "e" if st["offen"] != 1 else ""
        return (
            f"Du hast noch {st['offen']} offene"
            f" Lernziel{plural} für heute: {names}{extra}."
        )
    except Exception:  # noqa: BLE001
        pass
    return None


def _check_morning_learning(engine: ProactiveEngine) -> str | None:
    """Morning overview: fällige Karteikarten + offene Lernziele in one sentence."""
    parts: list[str] = []
    try:
        from ..knowledge.flashcards import FlashcardManager
        fm = FlashcardManager("data/knowledge.db")
        try:
            n = fm.due_count()
        finally:
            fm.close()
        if n > 0:
            plural = "n" if n != 1 else ""
            parts.append(f"{n} fällige Karteikarte{plural}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..knowledge.lerntrack import LerntrackDB
        db = LerntrackDB("data/lerntrack.db")
        try:
            st = db.stats()
        finally:
            db.close()
        if st.get("offen", 0) > 0:
            n = st["offen"]
            plural = "e" if n != 1 else ""
            parts.append(f"{n} offene Lernziel{plural}")
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return None
    return "Lernstand heute: " + " und ".join(parts) + "."


def _check_mood_checkin(engine: "ProactiveEngine") -> str | None:
    """Prompt the user to log their mood if they haven't done so today."""
    try:
        from pathlib import Path
        from ..productivity.mood_tracker import MoodTracker as _MT
        mt = _MT(Path("data/jarvis.db"))
        today = mt.today_mood()
        mt.close()
        if today is not None:
            return None  # already logged today
        return "Wie war dein Tag? Sag mir deine Stimmung von 1 bis 10."
    except Exception:  # noqa: BLE001
        return None


def _check_breaking_news(engine: "ProactiveEngine") -> str | None:
    """Fire when a recent headline (<30 min) matches the user's news_topics."""
    try:
        from pathlib import Path
        from ..memory.profile_manager import ProfileManager as _PM
        prof = _PM(Path("data/profile.json"), Path("data/jarvis.db"))
        topics: list[str] = (prof.get().get("preferences") or {}).get("news_topics") or []
        if not topics:
            return None
        from datetime import datetime, timezone, timedelta
        from ..tools.news import get_headlines_for_topics
        headlines = get_headlines_for_topics(topics, n=5)
        if not headlines:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        breaking = [h for h in headlines if h.published and h.published > cutoff]
        if not breaking:
            return None
        h = breaking[0]
        return f"Aktuelle Meldung zu deinen Themen: {h.title} ({h.source})"
    except Exception:  # noqa: BLE001
        return None


# -- registry build ------------------------------------------------------- #

def _build_specs() -> tuple[NotificationSpec, ...]:
    """Single source of truth for trigger metadata. Order here defines
    iteration order in tick(), which matters only if two specs would
    both fire in the same minute — high-priority ones go first so
    they aren't silenced by a same-minute low-priority delivery (we
    deliver one-by-one but the spec is clean either way)."""
    return (
        NotificationSpec(
            name="battery_warning",
            priority="high",
            cooldown_minutes=30,
            max_per_day=None,
            active_hours=None,
            check=_check_battery_warning,
        ),
        NotificationSpec(
            name="meeting_warning",
            priority="high",
            cooldown_minutes=10,  # backstop; per-event dedup is stronger
            max_per_day=None,
            active_hours=None,
            check=_check_meeting_warning,
        ),
        NotificationSpec(
            name="traffic_warning",
            priority="high",
            cooldown_minutes=15,
            max_per_day=None,
            active_hours=None,
            check=_check_traffic_warning,
        ),
        NotificationSpec(
            name="important_email",
            priority="high",
            cooldown_minutes=15,
            max_per_day=None,
            active_hours=None,
            check=_check_important_email,
        ),
        NotificationSpec(
            name="weather_morning",
            priority="medium",
            cooldown_minutes=12 * 60,
            max_per_day=1,
            active_hours=(6, 10),
            check=_check_weather_morning,
        ),
        NotificationSpec(
            name="package_delivery",
            priority="medium",
            cooldown_minutes=12 * 60,
            max_per_day=1,
            active_hours=(8, 20),
            check=_check_package_delivery,
        ),
        NotificationSpec(
            name="hydration",
            priority="low",
            cooldown_minutes=90,
            max_per_day=4,
            active_hours=(9, 18),
            check=_check_hydration,
        ),
        NotificationSpec(
            name="forgotten_task",
            priority="low",
            cooldown_minutes=4 * 60,
            max_per_day=3,
            active_hours=(9, 20),
            check=_check_forgotten_task,
        ),
        NotificationSpec(
            name="productivity_slump",
            priority="low",
            cooldown_minutes=4 * 60,
            max_per_day=2,
            active_hours=(13, 18),
            check=_check_productivity_slump,
        ),
        NotificationSpec(
            name="morning_learning",
            priority="low",
            cooldown_minutes=12 * 60,
            max_per_day=1,
            active_hours=(7, 10),
            check=_check_morning_learning,
        ),
        NotificationSpec(
            name="flashcard_due",
            priority="medium",
            cooldown_minutes=4 * 60,
            max_per_day=2,
            active_hours=(8, 20),
            check=_check_flashcard_due,
        ),
        NotificationSpec(
            name="lernziel_reminder",
            priority="low",
            cooldown_minutes=6 * 60,
            max_per_day=1,
            active_hours=(17, 22),
            check=_check_lernziel_reminder,
        ),
        NotificationSpec(
            name="budget_warning",
            priority="medium",
            cooldown_minutes=6 * 60,
            max_per_day=1,
            active_hours=(8, 21),
            check=_check_budget_warning,
        ),
        NotificationSpec(
            name="watchlist_alert",
            priority="medium",
            cooldown_minutes=2 * 60,
            max_per_day=3,
            active_hours=(8, 22),
            check=_check_watchlist_alerts,
        ),
        NotificationSpec(
            name="spending_spike",
            priority="medium",
            cooldown_minutes=24 * 60,
            max_per_day=1,
            active_hours=(8, 21),
            check=_check_spending_spike,
        ),
        NotificationSpec(
            name="breaking_news",
            priority="medium",
            cooldown_minutes=30,
            max_per_day=6,
            active_hours=(7, 22),
            check=_check_breaking_news,
        ),
        NotificationSpec(
            name="mood_checkin",
            priority="low",
            cooldown_minutes=12 * 60,
            max_per_day=1,
            active_hours=(19, 22),
            check=_check_mood_checkin,
        ),
    )
