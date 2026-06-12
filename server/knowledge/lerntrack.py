"""Subject-level learning progress tracker.

Tracks which topics/Lernziele the user has worked on across days so JARVIS
can answer "von 8 Lernzielen hast du 5 schon bearbeitet" and proactively
remind about unfinished subjects.

Schema
------
  subjects table  — one row per subject / Lernziel
    id            INTEGER PK
    name          TEXT UNIQUE      — normalised name (lowercase, stripped)
    display_name  TEXT             — original casing as entered
    subject_group TEXT             — grouping e.g. "Mechatronik M4"
    status        TEXT             — 'offen' | 'bearbeitet' | 'abgeschlossen'
    notes         TEXT             — optional free-text notes
    created_at    REAL             — unix timestamp
    updated_at    REAL
    last_worked   REAL             — when it was last marked worked on

All mutations are thread-safe (ThreadSafeDB base class).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..common.sqlite_store import ThreadSafeDB


class LerntrackDB(ThreadSafeDB):
    def __init__(self, db_path: str | Path = "data/lerntrack.db") -> None:
        super().__init__(db_path, label="Lerntrack")

    def _init_schema(self, conn: object) -> None:
        import sqlite3 as _sql
        assert isinstance(conn, _sql.Connection)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subjects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                subject_group TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'offen',
                notes        TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                last_worked  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sub_group
                ON subjects(subject_group);
            CREATE INDEX IF NOT EXISTS idx_sub_status
                ON subjects(status);
        """)

    # ── write ──────────────────────────────────────────────────────────── #

    def add(self, display_name: str, group: str = "",
            notes: str = "") -> int | None:
        """Add a new subject. Returns the id, or None if already exists."""
        name = _norm(display_name)
        now = time.time()
        return self._execute(
            "INSERT OR IGNORE INTO subjects "
            "(name, display_name, subject_group, status, notes, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, 'offen', ?, ?, ?)",
            (name, display_name.strip(), group.strip(), notes, now, now),
        )

    def mark(self, name_or_id: str | int, status: str,
             notes: str | None = None) -> bool:
        """Update a subject's status. ``name_or_id`` is the display name
        (partial match) or row id. Returns True if a row was updated."""
        now = time.time()
        if isinstance(name_or_id, int):
            where, args = "id = ?", (name_or_id,)
        else:
            where, args = "name LIKE ?", (f"%{_norm(name_or_id)}%",)
        if notes is not None:
            return self._execute(
                f"UPDATE subjects SET status=?, notes=?, updated_at=?, "
                f"last_worked=? WHERE {where}",
                (status, notes, now, now, *args),
            ) is not None
        return self._execute(
            f"UPDATE subjects SET status=?, updated_at=?, last_worked=? "
            f"WHERE {where}",
            (status, now, now, *args),
        ) is not None

    def delete(self, name_or_id: str | int) -> bool:
        if isinstance(name_or_id, int):
            where, args = "id = ?", (name_or_id,)
        else:
            where, args = "name LIKE ?", (f"%{_norm(name_or_id)}%",)
        return self._execute(
            f"DELETE FROM subjects WHERE {where}", args) is not None

    # ── read ───────────────────────────────────────────────────────────── #

    def list_group(self, group: str = "",
                   status: str | None = None) -> list[dict[str, Any]]:
        if group and status:
            q = ("SELECT * FROM subjects WHERE subject_group LIKE ? "
                 "AND status = ? ORDER BY id")
            return self.query(q, (f"%{group}%", status))
        elif group:
            return self.query(
                "SELECT * FROM subjects WHERE subject_group LIKE ? ORDER BY id",
                (f"%{group}%",))
        elif status:
            return self.query(
                "SELECT * FROM subjects WHERE status = ? ORDER BY id",
                (status,))
        return self.query("SELECT * FROM subjects ORDER BY subject_group, id")

    def stats(self, group: str = "") -> dict[str, int]:
        rows = self.list_group(group)
        counts: dict[str, int] = {"offen": 0, "bearbeitet": 0,
                                   "abgeschlossen": 0, "total": len(rows)}
        for r in rows:
            s = r.get("status", "offen")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def groups(self) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT subject_group FROM subjects "
            "WHERE subject_group != '' ORDER BY subject_group")
        return [r["subject_group"] for r in rows]

    # ── spoken output ─────────────────────────────────────────────────── #

    def spoken_status(self, group: str = "") -> str:
        st = self.stats(group)
        if st["total"] == 0:
            label = f"Keine Themen in '{group}'" if group else "Keine Themen"
            return f"{label} gespeichert."
        done = st["bearbeitet"] + st["abgeschlossen"]
        label = f"'{group}'" if group else "gesamt"
        msg = (f"{label}: {done} von {st['total']} Themen erledigt "
               f"({st['offen']} offen, {st['bearbeitet']} bearbeitet, "
               f"{st['abgeschlossen']} abgeschlossen).")
        if st["offen"] > 0:
            open_rows = self.list_group(group, status="offen")[:3]
            names = ", ".join(r["display_name"] for r in open_rows)
            if len(open_rows) < st["offen"]:
                names += " ..."
            msg += f" Noch offen: {names}."
        return msg


def _norm(s: str) -> str:
    return s.strip().lower()
