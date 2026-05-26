"""SQLite-backed watchlist for movies and shows."""
from __future__ import annotations

import random
import sqlite3
from datetime import date
from pathlib import Path


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT DEFAULT 'unknown',
    platform TEXT DEFAULT '',
    status TEXT DEFAULT 'want_to_watch',
    added_date TEXT NOT NULL,
    watched_date TEXT,
    rating INTEGER,
    notes TEXT DEFAULT '',
    genre TEXT DEFAULT ''
)
"""


class Watchlist:
    """Manage a personal movie/show watchlist in SQLite."""

    def __init__(self, db_path: Path) -> None:
        try:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_SQL)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[WATCHLIST] DB init failed: {exc}")
            self._conn = None  # type: ignore[assignment]

    def add(
        self,
        title: str,
        type: str = "unknown",
        platform: str = "",
        genre: str = "",
    ) -> tuple[str, bool]:
        """Add a title to the watchlist."""
        if not title:
            return "Kein Titel angegeben.", True
        try:
            today = date.today().isoformat()
            self._conn.execute(
                "INSERT INTO watchlist (title, type, platform, status, added_date, genre) "
                "VALUES (?, ?, ?, 'want_to_watch', ?, ?)",
                (title, type, platform, today, genre),
            )
            self._conn.commit()
            return f"'{title}' zur Watchlist hinzugefügt.", False
        except Exception as exc:  # noqa: BLE001
            return f"Fehler beim Hinzufügen: {exc}", True

    def mark_watched(
        self, title: str, rating: int | None = None
    ) -> tuple[str, bool]:
        """Mark a title as watched (fuzzy title match)."""
        try:
            today = date.today().isoformat()
            cur = self._conn.execute(
                "SELECT id, title FROM watchlist WHERE LOWER(title) LIKE LOWER(?)",
                (f"%{title}%",),
            )
            row = cur.fetchone()
            if row is None:
                return f"'{title}' nicht in der Watchlist gefunden.", True
            real_title = row["title"]
            rid = row["id"]
            if rating is not None:
                self._conn.execute(
                    "UPDATE watchlist SET status='watched', watched_date=?, rating=? WHERE id=?",
                    (today, rating, rid),
                )
            else:
                self._conn.execute(
                    "UPDATE watchlist SET status='watched', watched_date=? WHERE id=?",
                    (today, rid),
                )
            self._conn.commit()
            rating_str = f" ({rating}/10)" if rating is not None else ""
            return f"'{real_title}' als gesehen markiert{rating_str}.", False
        except Exception as exc:  # noqa: BLE001
            return f"Fehler beim Aktualisieren: {exc}", True

    def get_list(self, status_filter: str = "want_to_watch") -> list[dict]:
        """Return watchlist items as list of dicts."""
        try:
            cur = self._conn.execute(
                "SELECT * FROM watchlist WHERE status=? ORDER BY added_date DESC",
                (status_filter,),
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"[WATCHLIST] get_list failed: {exc}")
            return []

    def what_to_watch(self) -> tuple[str, bool]:
        """Pick a random unwatched item and return a suggestion."""
        try:
            cur = self._conn.execute(
                "SELECT title, type FROM watchlist WHERE status='want_to_watch'"
            )
            rows = cur.fetchall()
            if not rows:
                return "Deine Watchlist ist leer! Füge etwas hinzu.", False
            item = random.choice(rows)  # noqa: S311
            title = item["title"]
            media_type = item["type"]
            return f"Heute empfehle ich: {title} ({media_type}). Viel Spaß!", False
        except Exception as exc:  # noqa: BLE001
            return f"Fehler beim Laden der Watchlist: {exc}", True

    def spoken_list(self, status_filter: str = "want_to_watch") -> tuple[str, bool]:
        """Return a spoken-friendly list of watchlist items."""
        items = self.get_list(status_filter)
        if not items:
            return "Deine Watchlist ist leer.", False
        parts = [f"{i + 1}. {item['title']}" for i, item in enumerate(items[:10])]
        return "Deine Watchlist: " + ", ".join(parts) + ".", False
