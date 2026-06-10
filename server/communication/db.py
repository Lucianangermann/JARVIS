"""SQLite store for the JARVIS communication layer (``data/communication.db``).

Separate database from jarvis.db / security.db — the communication tables
have their own retention rule (message *content* is pruned after 7 days,
per spec) and their own write pattern (messaging threads, the Telegram
poller, the notification center, and FastAPI handlers all write
concurrently). One :class:`CommunicationDB` is created by
:class:`~server.communication.communication_manager.CommunicationManager`
and shared by every sub-component, mirroring SecurityDB.

Connection conventions match the rest of the codebase: a single
``check_same_thread=False`` connection in WAL mode with a ``Row`` factory,
guarded by a lock. Every write is best-effort — a failed insert prints
and returns a falsy value rather than raising, because a comms log write
must NEVER crash JARVIS.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# ── Schema (spec §12) ───────────────────────────────────────────────────── #

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          REAL NOT NULL,
    platform           TEXT NOT NULL,
    direction          TEXT NOT NULL,            -- 'in' | 'out'
    contact            TEXT,
    content            TEXT,
    translated_content TEXT,
    delivered          INTEGER NOT NULL DEFAULT 0,
    read               INTEGER NOT NULL DEFAULT 0,
    replied            INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_CALLS = """
CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         REAL NOT NULL,
    contact           TEXT,
    direction         TEXT NOT NULL,             -- 'in' | 'out'
    duration_seconds  INTEGER,
    method            TEXT,                       -- facetime | phone | …
    outcome           TEXT,                       -- completed | missed | declined
    callback_reminder INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    REAL NOT NULL,
    title        TEXT,
    body         TEXT,
    priority     TEXT NOT NULL DEFAULT 'medium',
    source       TEXT,
    delivered_via TEXT,                           -- csv of channels
    read         INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_AUTO_REPLIES = """
CREATE TABLE IF NOT EXISTS auto_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger    TEXT NOT NULL,
    platform   TEXT,
    message    TEXT,
    sent_count INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
)
"""

_CREATE_FOLLOWUPS = """
CREATE TABLE IF NOT EXISTS followups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    platform          TEXT,
    contact           TEXT,
    sent_at           REAL NOT NULL,
    message_preview   TEXT,
    followup_due      REAL,
    followed_up       INTEGER NOT NULL DEFAULT 0,
    response_received INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_msg_ts      ON messages(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_msg_contact ON messages(platform, contact)",
    "CREATE INDEX IF NOT EXISTS idx_calls_ts    ON calls(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_notif_ts    ON notifications(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_followup_due ON followups(followup_due)",
]

# Message content is pruned after this many days (spec hard rule).
MESSAGE_RETENTION_DAYS = 7


class CommunicationDB:
    """Thread-safe SQLite wrapper for the communication layer."""

    def __init__(self, db_path: Path | str = "data/communication.db") -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            for stmt in (_CREATE_MESSAGES, _CREATE_CALLS, _CREATE_NOTIFICATIONS,
                         _CREATE_AUTO_REPLIES, _CREATE_FOLLOWUPS):
                self._conn.execute(stmt)
            for idx in _CREATE_INDICES:
                self._conn.execute(idx)
            self._conn.commit()
            self.prune_message_content()  # enforce retention on every boot
            print(f"[CommunicationDB] ready at {self._db_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[CommunicationDB] init failed: {exc}")

    # ── low-level helpers ──────────────────────────────────────────────── #

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            print(f"[CommunicationDB] write failed: {exc}")
            return None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"[CommunicationDB] query failed: {exc}")
            return []

    # ── messages ───────────────────────────────────────────────────────── #

    def log_message(
        self,
        platform: str,
        direction: str,
        contact: str | None,
        content: str | None,
        translated_content: str | None = None,
        delivered: bool = False,
        read: bool = False,
    ) -> int | None:
        return self._execute(
            """INSERT INTO messages
               (timestamp, platform, direction, contact, content,
                translated_content, delivered, read)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), platform, direction, contact, content,
             translated_content, int(delivered), int(read)),
        )

    def recent_messages(
        self, platform: str | None = None, contact: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if platform and platform != "all":
            sql += " AND platform = ?"
            params.append(platform)
        if contact:
            sql += " AND contact = ?"
            params.append(contact)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def mark_replied(self, message_id: int) -> None:
        self._execute("UPDATE messages SET replied=1 WHERE id=?", (message_id,))

    def prune_message_content(self) -> int:
        """Null out message *content* older than the retention window. We
        keep the row (for analytics: who/when/platform) but drop the body,
        honouring the 'never store content > 7 days' rule."""
        cutoff = time.time() - MESSAGE_RETENTION_DAYS * 86400
        n = self._execute(
            """UPDATE messages
               SET content = NULL, translated_content = NULL
               WHERE timestamp < ? AND content IS NOT NULL""",
            (cutoff,),
        )
        return n or 0

    # ── calls ──────────────────────────────────────────────────────────── #

    def log_call(
        self,
        contact: str | None,
        direction: str,
        method: str | None = None,
        outcome: str | None = None,
        duration_seconds: int | None = None,
        callback_reminder: bool = False,
    ) -> int | None:
        return self._execute(
            """INSERT INTO calls
               (timestamp, contact, direction, duration_seconds, method,
                outcome, callback_reminder)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), contact, direction, duration_seconds, method,
             outcome, int(callback_reminder)),
        )

    def recent_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM calls ORDER BY timestamp DESC LIMIT ?", (limit,))

    def missed_calls(self, since_ts: float) -> list[dict[str, Any]]:
        return self.query(
            """SELECT * FROM calls WHERE timestamp >= ? AND outcome = 'missed'
               ORDER BY timestamp DESC""",
            (since_ts,),
        )

    # ── notifications ──────────────────────────────────────────────────── #

    def log_notification(
        self,
        title: str,
        body: str,
        priority: str,
        source: str,
        delivered_via: list[str] | None = None,
    ) -> int | None:
        return self._execute(
            """INSERT INTO notifications
               (timestamp, title, body, priority, source, delivered_via)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), title, body, priority, source,
             ",".join(delivered_via or [])),
        )

    def recent_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM notifications ORDER BY timestamp DESC LIMIT ?",
            (limit,))

    # ── auto-replies ───────────────────────────────────────────────────── #

    def add_auto_reply(self, trigger: str, platform: str, message: str) -> int | None:
        return self._execute(
            """INSERT INTO auto_replies (trigger, platform, message, created_at)
               VALUES (?, ?, ?, ?)""",
            (trigger, platform, message, time.time()),
        )

    def active_auto_replies(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM auto_replies WHERE active=1")

    def deactivate_auto_replies(self, trigger: str | None = None) -> None:
        if trigger:
            self._execute("UPDATE auto_replies SET active=0 WHERE trigger=?",
                          (trigger,))
        else:
            self._execute("UPDATE auto_replies SET active=0")

    def bump_auto_reply(self, rule_id: int) -> None:
        self._execute(
            "UPDATE auto_replies SET sent_count = sent_count + 1 WHERE id=?",
            (rule_id,))

    # ── follow-ups ─────────────────────────────────────────────────────── #

    def track_followup(
        self, platform: str, contact: str, message_preview: str,
        followup_due: float,
    ) -> int | None:
        return self._execute(
            """INSERT INTO followups
               (platform, contact, sent_at, message_preview, followup_due)
               VALUES (?, ?, ?, ?, ?)""",
            (platform, contact, time.time(), message_preview, followup_due),
        )

    def due_followups(self, now_ts: float) -> list[dict[str, Any]]:
        return self.query(
            """SELECT * FROM followups
               WHERE followed_up = 0 AND response_received = 0
                 AND followup_due <= ?
               ORDER BY followup_due""",
            (now_ts,),
        )

    def mark_followed_up(self, followup_id: int) -> None:
        self._execute("UPDATE followups SET followed_up=1 WHERE id=?",
                      (followup_id,))

    def mark_response_received(self, platform: str, contact: str) -> None:
        self._execute(
            """UPDATE followups SET response_received=1
               WHERE platform=? AND contact=? AND response_received=0""",
            (platform, contact),
        )

    # ── lifecycle ──────────────────────────────────────────────────────── #

    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[CommunicationDB] close failed: {exc}")
            finally:
                self._conn = None
