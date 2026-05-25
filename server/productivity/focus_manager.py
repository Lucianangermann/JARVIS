"""Pomodoro timer and time tracking backed by the time_entries table."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any


class FocusManager:
    """Pomodoro timer and work-session tracker."""

    def __init__(
        self,
        db_path: Path | str,
        smarthome: Any = None,
    ) -> None:
        self._db_path = str(db_path)
        self.smarthome = smarthome
        self._active_timer_id: int | None = None
        self._pomodoro_thread: threading.Thread | None = None
        self._pomodoro_cancel = threading.Event()

        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
        except Exception as exc:
            print(f"[FocusManager] DB init failed: {exc}")

    # ── Pomodoro ──────────────────────────────────────────────────────────── #

    def start_pomodoro(self, task_name: str = "", minutes: int = 25) -> str:
        try:
            if self._pomodoro_thread and self._pomodoro_thread.is_alive():
                return "Ein Pomodoro läuft bereits. Stoppe ihn zuerst."

            self._pomodoro_cancel.clear()

            def _run() -> None:
                deadline = time.time() + minutes * 60
                while time.time() < deadline:
                    if self._pomodoro_cancel.is_set():
                        return
                    time.sleep(1)
                if not self._pomodoro_cancel.is_set():
                    self._on_pomodoro_done(task_name, minutes)

            self._pomodoro_thread = threading.Thread(
                target=_run, daemon=True, name="jarvis-pomodoro",
            )
            self._pomodoro_thread.start()

            label = f" für '{task_name}'" if task_name else ""
            return f"Pomodoro{label} gestartet. {minutes} Minuten Fokuszeit."
        except Exception as exc:
            print(f"[FocusManager] start_pomodoro failed: {exc}")
            return "Pomodoro konnte nicht gestartet werden."

    def stop_pomodoro(self) -> str:
        try:
            if not (self._pomodoro_thread and self._pomodoro_thread.is_alive()):
                return "Kein aktiver Pomodoro."
            self._pomodoro_cancel.set()
            self._pomodoro_thread.join(timeout=2)
            return "Pomodoro gestoppt."
        except Exception as exc:
            print(f"[FocusManager] stop_pomodoro failed: {exc}")
            return "Pomodoro konnte nicht gestoppt werden."

    def is_running(self) -> bool:
        return bool(
            self._pomodoro_thread
            and self._pomodoro_thread.is_alive()
            and not self._pomodoro_cancel.is_set()
        )

    def _on_pomodoro_done(self, task_name: str, minutes: int) -> None:
        print(f"[FOCUS] Pomodoro done — {minutes} min on '{task_name}'")
        try:
            now = time.time()
            self._conn.execute(
                """INSERT INTO time_entries
                   (project, task_title, start_time, end_time,
                    duration_minutes, category, notes)
                   VALUES (?, ?, ?, ?, ?, 'focus', 'pomodoro')""",
                (
                    task_name or "Pomodoro",
                    task_name,
                    now - minutes * 60,
                    now,
                    float(minutes),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[FocusManager] _on_pomodoro_done DB write failed: {exc}")

    # ── Manual timer ──────────────────────────────────────────────────────── #

    def start_timer(self, project: str, task_title: str = "") -> str:
        try:
            if self._active_timer_id is not None:
                return "Ein Timer läuft bereits. Stoppe ihn zuerst."
            cur = self._conn.execute(
                """INSERT INTO time_entries
                   (project, task_title, start_time, category)
                   VALUES (?, ?, ?, 'work')""",
                (project, task_title, time.time()),
            )
            self._conn.commit()
            self._active_timer_id = cur.lastrowid
            label = f" – {task_title}" if task_title else ""
            return f"Timer gestartet: {project}{label}."
        except Exception as exc:
            print(f"[FocusManager] start_timer failed: {exc}")
            return "Timer konnte nicht gestartet werden."

    def stop_timer(self) -> str:
        try:
            if self._active_timer_id is None:
                return "Kein aktiver Timer."
            end = time.time()
            cur = self._conn.execute(
                "SELECT * FROM time_entries WHERE id=?",
                (self._active_timer_id,),
            )
            row = cur.fetchone()
            if not row:
                self._active_timer_id = None
                return "Timer-Eintrag nicht gefunden."
            duration = (end - row["start_time"]) / 60.0
            self._conn.execute(
                """UPDATE time_entries
                   SET end_time=?, duration_minutes=?
                   WHERE id=?""",
                (end, round(duration, 2), self._active_timer_id),
            )
            self._conn.commit()
            self._active_timer_id = None
            mins = int(duration)
            return f"{mins} Minuten auf {row['project']} gestoppt."
        except Exception as exc:
            print(f"[FocusManager] stop_timer failed: {exc}")
            return "Timer konnte nicht gestoppt werden."

    def get_active_session(self) -> dict[str, Any] | None:
        try:
            if self._active_timer_id is None:
                return None
            cur = self._conn.execute(
                "SELECT * FROM time_entries WHERE id=?",
                (self._active_timer_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            print(f"[FocusManager] get_active_session failed: {exc}")
            return None

    # ── Summary ───────────────────────────────────────────────────────────── #

    def get_time_today(self) -> str:
        try:
            today_start = _today_epoch()
            cur = self._conn.execute(
                """SELECT project, SUM(duration_minutes) as mins
                   FROM time_entries
                   WHERE start_time >= ? AND duration_minutes IS NOT NULL
                   GROUP BY project
                   ORDER BY mins DESC""",
                (today_start,),
            )
            rows = cur.fetchall()
            if not rows:
                return "Heute noch keine Zeit erfasst."
            parts = []
            for r in rows:
                h = int(r["mins"] // 60)
                m = int(r["mins"] % 60)
                label = f"{h}h {m}min" if h else f"{m}min"
                parts.append(f"{label} {r['project']}")
            return "Heute: " + ", ".join(parts) + "."
        except Exception as exc:
            print(f"[FocusManager] get_time_today failed: {exc}")
            return "Zeiterfassung momentan nicht verfügbar."


def _today_epoch() -> float:
    """Unix timestamp for midnight today (local time)."""
    d = date.today()
    import datetime as _dt
    return float(_dt.datetime(d.year, d.month, d.day).timestamp())
