"""iMessage send (AppleScript) + read (chat.db SQLite).

Two halves with different mechanisms, because macOS gives us different
tools for each:

* **Sending** goes through AppleScript — Messages.app exposes a reliable
  ``send … to buddy`` verb. Contact + body are passed as positional argv
  (see ``applescript.osa``) so a message containing quotes/newlines can't
  break out of the script.

* **Reading** does NOT use AppleScript — Messages' scripting dictionary
  can't reliably enumerate unread chats on modern macOS. Instead we read
  ``~/Library/Messages/chat.db`` directly (read-only), the same proven
  approach ``reminders_tool`` uses for the Reminders store. This needs
  Full Disk Access (TCC); without it the read returns ``[]`` with a
  logged note rather than failing.

Everything is best-effort and zero-setup beyond Messages being signed in.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..applescript import ASError, osa

_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Apple stores message dates as nanoseconds since 2001-01-01 UTC. Offset to
# convert to a Unix epoch.
_APPLE_EPOCH = 978307200

# Send via the Messages "buddy" verb. iMessage service is resolved by
# `service type is iMessage`; contact + body arrive as argv items 1 and 2.
_SEND_SCRIPT = """
on run argv
    set targetContact to item 1 of argv
    set theMessage to item 2 of argv
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant targetContact of targetService
        send theMessage to targetBuddy
    end tell
    return "sent"
end run
"""

# A simpler, more compatible variant used as a fallback — addresses the
# buddy by handle directly.
_SEND_SCRIPT_FALLBACK = """
on run argv
    set targetContact to item 1 of argv
    set theMessage to item 2 of argv
    tell application "Messages"
        set theService to 1st service whose service type = iMessage
        set theBuddy to buddy targetContact of theService
        send theMessage to theBuddy
    end tell
    return "sent"
end run
"""


class iMessageController:
    """Send via AppleScript, read via the Messages chat.db."""

    def __init__(self, db: Any = None, chat_db: Path | str = _CHAT_DB) -> None:
        self._db = db                       # CommunicationDB (logging)
        self._chat_db = Path(chat_db)

    # ── sending ────────────────────────────────────────────────────────── #

    async def send(self, contact: str, message: str) -> bool:
        """Send an iMessage to ``contact`` (handle, phone, or email).
        Returns True on success. The CALLER is responsible for the
        confirm-before-send gate — this is the raw transport."""
        return self.send_sync(contact, message)

    def send_sync(self, contact: str, message: str) -> bool:
        """Synchronous twin of :meth:`send`. ``osa`` is a blocking
        subprocess anyway, so this carries no extra cost — it exists so
        emergency code paths (which run inside the asyncio loop thread and
        must not await) can text contacts without scheduling a coroutine.
        Same injection-safe argv transport + best-effort fallback."""
        last: Exception | None = None
        for script in (_SEND_SCRIPT, _SEND_SCRIPT_FALLBACK):
            try:
                osa(script, contact, message)
                if self._db is not None:
                    self._db.log_message("imessage", "out", contact, message,
                                         delivered=True)
                print(f"[iMessage] sent to {contact}")
                return True
            except ASError as exc:
                last = exc
                continue
        print(f"[iMessage] send failed: {last}")
        return False

    # ── reading (chat.db) ──────────────────────────────────────────────── #

    def _connect(self) -> sqlite3.Connection | None:
        if not self._chat_db.exists():
            print(f"[iMessage] chat.db not found at {self._chat_db}")
            return None
        try:
            # Read-only URI so we never lock/modify Messages' live DB.
            conn = sqlite3.connect(
                f"file:{self._chat_db}?mode=ro", uri=True,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            # Almost always missing Full Disk Access.
            print(f"[iMessage] cannot open chat.db (Full Disk Access?): {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            print(f"[iMessage] chat.db open failed: {exc}")
            return None

    @staticmethod
    def _apple_ts_to_epoch(raw: int) -> float:
        # Newer macOS stores nanoseconds; older stored seconds. Detect by
        # magnitude.
        if raw > 1_000_000_000_000:  # nanoseconds
            return raw / 1e9 + _APPLE_EPOCH
        return raw + _APPLE_EPOCH

    async def get_unread(self) -> list[dict[str, Any]]:
        """Unread incoming messages, newest first. [] if no DB access."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT h.id AS sender, m.text AS message, m.date AS raw_date
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.is_from_me = 0 AND m.is_read = 0 AND m.text IS NOT NULL
                ORDER BY m.date DESC
                LIMIT 50
                """
            ).fetchall()
            out = [{
                "sender": r["sender"],
                "message": r["message"],
                "time": self._apple_ts_to_epoch(r["raw_date"]),
            } for r in rows]
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"[iMessage] get_unread query failed: {exc}")
            return []
        finally:
            conn.close()

    async def get_conversation(self, contact: str, n: int = 10) -> list[dict[str, Any]]:
        """Last ``n`` messages with a contact (matched on handle id)."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT h.id AS handle, m.text AS content, m.is_from_me AS mine,
                       m.date AS raw_date
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                WHERE h.id LIKE ? AND m.text IS NOT NULL
                ORDER BY m.date DESC
                LIMIT ?
                """,
                (f"%{contact}%", n),
            ).fetchall()
            out = [{
                "sender": "me" if r["mine"] else r["handle"],
                "content": r["content"],
                "time": self._apple_ts_to_epoch(r["raw_date"]),
            } for r in reversed(rows)]
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"[iMessage] get_conversation query failed: {exc}")
            return []
        finally:
            conn.close()

    async def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT h.id AS handle, m.text AS content, m.is_from_me AS mine,
                       m.date AS raw_date
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.text LIKE ?
                ORDER BY m.date DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
            return [{
                "sender": "me" if r["mine"] else r["handle"],
                "content": r["content"],
                "time": self._apple_ts_to_epoch(r["raw_date"]),
            } for r in rows]
        except Exception as exc:  # noqa: BLE001
            print(f"[iMessage] search failed: {exc}")
            return []
        finally:
            conn.close()
