"""Daily mood / wellbeing tracker backed by jarvis.db."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


class MoodTracker:
    _schema = """
    CREATE TABLE IF NOT EXISTS mood_logs (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        ts    REAL    NOT NULL,
        score INTEGER NOT NULL,
        note  TEXT    DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS ix_mood_ts ON mood_logs (ts);
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self._schema)
            self._conn.commit()
            self.available = True
        except Exception as exc:
            print(f"[MoodTracker] init failed: {exc}")
            self.available = False

    def log(self, score: int, note: str = "") -> int | None:
        score = max(1, min(10, int(score)))
        try:
            cur = self._conn.execute(
                "INSERT INTO mood_logs (ts, score, note) VALUES (?, ?, ?)",
                (time.time(), score, (note or "").strip()[:200]),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[MoodTracker] log failed: {exc}")
            return None

    def today_mood(self) -> dict[str, Any] | None:
        try:
            d = date.today()
            start = datetime(d.year, d.month, d.day).timestamp()
            row = self._conn.execute(
                "SELECT score, note, ts FROM mood_logs WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
                (start,),
            ).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            print(f"[MoodTracker] today_mood failed: {exc}")
            return None

    def weekly_moods(self, days: int = 7) -> list[dict[str, Any]]:
        try:
            since = time.time() - days * 86400
            rows = self._conn.execute(
                "SELECT ts, score, note FROM mood_logs WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[MoodTracker] weekly_moods failed: {exc}")
            return []

    def weekly_average(self, days: int = 7) -> float | None:
        rows = self.weekly_moods(days)
        if not rows:
            return None
        return round(sum(r["score"] for r in rows) / len(rows), 1)

    def spoken_weekly(self) -> str:
        rows = self.weekly_moods()
        if not rows:
            return ""
        avg = round(sum(r["score"] for r in rows) / len(rows), 1)
        low = min(r["score"] for r in rows)
        high = max(r["score"] for r in rows)
        return (f"Stimmung diese Woche: Ø {avg}/10 "
                f"(Min {low}, Max {high}, {len(rows)} Einträge).")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
