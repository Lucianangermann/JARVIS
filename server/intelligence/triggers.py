"""Deferred / conditional actions JARVIS fires itself.

Apple Reminders are passive; this is the active layer — a small SQLite
store of time-scheduled actions plus a background checker that delivers
each due action through the NotificationCenter (so it reaches voice + UI +
Telegram). Lets JARVIS honour "erinnere mich in 2 Stunden an X" / "um 18
Uhr Y" by actually speaking up at that time.

Time math is done by Claude at the tool boundary (delay_minutes / at), so
this module just stores a fire-at timestamp and a message — no fragile
natural-language date parsing here. Best-effort throughout.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..common.sqlite_store import ThreadSafeDB

_CREATE = """
CREATE TABLE IF NOT EXISTS triggers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        REAL NOT NULL,
    fire_at           REAL NOT NULL,
    message           TEXT NOT NULL,
    priority          TEXT NOT NULL DEFAULT 'high',
    fired             INTEGER NOT NULL DEFAULT 0,
    recurrence        TEXT NOT NULL DEFAULT 'none',
    recurrence_weekday INTEGER
)
"""

_RECURRENCE_VALUES = ("none", "daily", "weekly", "weekdays")

# (message, priority) -> None
DeliverFn = Callable[[str, str], None]


class TriggerStore(ThreadSafeDB):
    """Time-scheduled actions + a checker that fires them when due."""

    def __init__(self, db_path: Path | str = "data/triggers.db",
                 deliver: DeliverFn | None = None,
                 interval_s: int = 30) -> None:
        self._deliver = deliver
        self._interval = max(5, int(interval_s))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        super().__init__(db_path, label="Triggers")

    def _init_schema(self, conn) -> None:
        conn.execute(_CREATE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trg_due ON triggers(fired, fire_at)")
        # Migration: add recurrence columns to existing DBs (ignored if present)
        for stmt in (
            "ALTER TABLE triggers ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'",
            "ALTER TABLE triggers ADD COLUMN recurrence_weekday INTEGER",
        ):
            try:
                conn.execute(stmt)
            except Exception:  # noqa: BLE001 — column already exists
                pass

    # ── authoring ──────────────────────────────────────────────────────── #

    def add(self, fire_at: float, message: str, priority: str = "high",
            recurrence: str = "none",
            recurrence_weekday: int | None = None) -> int | None:
        if not message.strip() or fire_at <= 0:
            return None
        rec = recurrence if recurrence in _RECURRENCE_VALUES else "none"
        return self._execute(
            "INSERT INTO triggers "
            "(created_at, fire_at, message, priority, recurrence, recurrence_weekday) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), fire_at, message.strip(), priority, rec, recurrence_weekday))

    def pending(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM triggers WHERE fired=0 ORDER BY fire_at ASC")

    def cancel(self, trigger_id: int) -> bool:
        return self._execute("UPDATE triggers SET fired=1 WHERE id=?",
                             (trigger_id,)) is not None

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        return self.query(
            "SELECT * FROM triggers WHERE fired=0 AND fire_at <= ? "
            "ORDER BY fire_at", (now,))

    def _mark_fired(self, trigger_id: int) -> None:
        self._execute("UPDATE triggers SET fired=1 WHERE id=?", (trigger_id,))

    def _next_fire_at(self, trigger: dict) -> float:
        """Compute the next fire_at timestamp for a recurring trigger."""
        import datetime as _dt
        rec = trigger.get("recurrence", "none")
        current = trigger["fire_at"]
        now = time.time()
        base = max(current, now)
        if rec == "daily":
            # Advance by one day, keep same time-of-day
            return base + 86400
        if rec == "weekdays":
            # Advance 1 day at a time until we hit Mon–Fri
            next_ts = base + 86400
            for _ in range(7):
                wd = _dt.datetime.fromtimestamp(next_ts).weekday()
                if wd < 5:
                    return next_ts
                next_ts += 86400
            return base + 86400  # fallback
        if rec == "weekly":
            # Advance by 7 days
            return base + 7 * 86400
        return current  # 'none' — should not be called

    def spoken_pending(self) -> str:
        p = self.pending()
        if not p:
            return "Keine geplanten Erinnerungen."
        parts = []
        for t in p[:5]:
            when = time.strftime("%H:%M", time.localtime(t["fire_at"]))
            rec = t.get("recurrence", "none")
            rec_label = {"daily": " (täglich)", "weekly": " (wöchentlich)",
                         "weekdays": " (Mo–Fr)"}.get(rec, "")
            parts.append(f"{when}: {t['message']}{rec_label}")
        return f"{len(p)} geplante Erinnerung(en): " + ", ".join(parts) + "."

    # ── checker loop ───────────────────────────────────────────────────── #

    def fire_due(self, now: float | None = None) -> int:
        """Deliver every due trigger; reschedule recurring ones instead of
        marking them fired. Returns the count delivered."""
        fired = 0
        for t in self.due(now):
            msg = f"Erinnerung: {t['message']}"
            if self._deliver is not None:
                try:
                    self._deliver(msg, t.get("priority", "high"))
                except Exception as exc:  # noqa: BLE001
                    print(f"[Triggers] deliver failed: {exc}")
            else:
                print(f"[Triggers] (no sink) {msg}")
            rec = t.get("recurrence", "none")
            if rec != "none":
                next_at = self._next_fire_at(t)
                self._execute(
                    "UPDATE triggers SET fire_at=? WHERE id=?",
                    (next_at, t["id"]),
                )
            else:
                self._mark_fired(t["id"])
            fired += 1
        return fired

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-triggers",
                                        daemon=True)
        self._thread.start()
        print(f"[Triggers] checker active (every {self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.fire_due()
            except Exception as exc:  # noqa: BLE001
                print(f"[Triggers] tick failed: {exc}")
            self._stop.wait(self._interval)
