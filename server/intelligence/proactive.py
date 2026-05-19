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
    return ("Ruhiger Nachmittag. Soll ich die offenen Punkte für heute "
            "kurz zusammenfassen?")


def _check_forgotten_task(engine: ProactiveEngine) -> str | None:
    """No-op until we have a task tracker. The brain's memory layer
    can store notes but doesn't tag anything as 'todo' yet."""
    engine._log_stub_once(
        "forgotten_task",
        "no task-tracking source",
    )
    return None


def _check_important_email(engine: ProactiveEngine) -> str | None:
    """No-op until a Gmail tool exists. The MCP Gmail surface is for
    Claude.ai, not reachable from the server runtime."""
    engine._log_stub_once(
        "important_email",
        "Gmail tool not wired into server runtime",
    )
    return None


def _check_package_delivery(engine: ProactiveEngine) -> str | None:
    """No-op until tracking numbers live in memory."""
    engine._log_stub_once(
        "package_delivery",
        "no tracking-number source",
    )
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
    )
