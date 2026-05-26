"""Gaming session tracker with optional smart home integration."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS gaming_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_name TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL,
    duration_minutes REAL,
    platform TEXT DEFAULT 'Mac'
)
"""


class GamingMode:
    """Track gaming sessions and optionally adjust smart home lighting."""

    def __init__(self, db_path: Path, smarthome=None) -> None:
        try:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_SQL)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[GAMING] DB init failed: {exc}")
            self._conn = None  # type: ignore[assignment]

        self._active_session_id: int | None = None
        self._start_time: float | None = None
        self._current_game: str = ""
        self.smarthome = smarthome

    def _run_smarthome_command(self, command: str) -> None:
        """Fire-and-forget smart home command in a daemon thread."""
        if self.smarthome is None:
            return

        def _target() -> None:
            import asyncio as _aio
            try:
                coro = self.smarthome.process_command(command)
                _aio.run(coro)
            except Exception as exc:  # noqa: BLE001
                print(f"[GAMING] smarthome command failed: {exc}")

        t = threading.Thread(target=_target, daemon=True, name="gaming-smarthome")
        t.start()

    def start(self, game_name: str = "Unbekanntes Spiel") -> tuple[str, bool]:
        """Start a gaming session."""
        try:
            now = time.time()
            if self._conn is not None:
                cur = self._conn.execute(
                    "INSERT INTO gaming_sessions (game_name, start_time) VALUES (?, ?)",
                    (game_name, now),
                )
                self._conn.commit()
                self._active_session_id = cur.lastrowid
            self._start_time = now
            self._current_game = game_name
            self._run_smarthome_command("lila licht")
            return f"Gaming Modus gestartet: {game_name}. Viel Spass!", False
        except Exception as exc:  # noqa: BLE001
            return f"Gaming-Start-Fehler: {exc}", True

    def stop(self) -> tuple[str, bool]:
        """End the current gaming session."""
        if self._active_session_id is None or self._start_time is None:
            return "Kein aktiver Gaming-Modus.", True
        try:
            now = time.time()
            duration = (now - self._start_time) / 60.0
            if self._conn is not None:
                self._conn.execute(
                    "UPDATE gaming_sessions SET end_time=?, duration_minutes=? WHERE id=?",
                    (now, duration, self._active_session_id),
                )
                self._conn.commit()
            game_name = self._current_game
            self._active_session_id = None
            self._start_time = None
            self._current_game = ""
            self._run_smarthome_command("normales licht")
            return (
                f"Gaming Session beendet: {game_name}, {duration:.0f} Minuten gespielt.",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Gaming-Stop-Fehler: {exc}", True

    def get_stats(self) -> tuple[str, bool]:
        """Return spoken gaming stats for today, this week, and all-time."""
        if self._conn is None:
            return "Gaming-Stats nicht verfügbar.", True
        try:
            import datetime as _dt
            now = time.time()
            today_start = _dt.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            week_start = today_start - (6 * 86400)

            cur = self._conn.execute(
                "SELECT COALESCE(SUM(duration_minutes), 0) FROM gaming_sessions "
                "WHERE start_time >= ?",
                (today_start,),
            )
            today_mins = cur.fetchone()[0] or 0.0

            cur = self._conn.execute(
                "SELECT COALESCE(SUM(duration_minutes), 0) FROM gaming_sessions "
                "WHERE start_time >= ?",
                (week_start,),
            )
            week_mins = cur.fetchone()[0] or 0.0

            cur = self._conn.execute(
                "SELECT game_name, COUNT(*) as cnt FROM gaming_sessions "
                "GROUP BY game_name ORDER BY cnt DESC LIMIT 1"
            )
            fav_row = cur.fetchone()
            fav = fav_row[0] if fav_row else "keines"

            return (
                f"Gaming-Stats: Heute {today_mins:.0f} Minuten, "
                f"diese Woche {week_mins:.0f} Minuten. "
                f"Lieblingsspiel: {fav}.",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Stats-Fehler: {exc}", True

    def is_active(self) -> bool:
        return self._active_session_id is not None
