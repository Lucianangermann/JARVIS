"""Read-only WhatsApp access via the local ChatStorage.sqlite database.

WhatsApp for macOS stores its messages in a Core Data SQLite database at
  ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite

Reading is purely local and offline — no WhatsApp API, no network calls.
The database is read-only opened (URI mode) so we never touch live state.

Timestamps are Core Data / macOS epoch: seconds since 2001-01-01 UTC,
the same as iMessage's chat.db (both use NSDate internally).

Key tables:
  ZWAMESSAGE      — individual messages (ZTEXT, ZISFROMME, ZMESSAGEDATE,
                    ZFROMJID, ZPUSHNAME, ZMESSAGETYPE, ZCHATSESSION)
  ZWACHATSESSION  — one row per conversation (ZPARTNERNAME, ZCONTACTJID,
                    ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT)

Limitations:
  * No sending — WhatsApp has no AppleScript dictionary and the unofficial
    URI scheme (whatsapp://send?phone=…&text=…) requires the user to press
    Send manually. Sending is therefore deliberately NOT implemented.
  * Group messages: ZFROMJID is the group JID; ZPUSHNAME is the sender's
    display name inside the group. Contact names come from ZPARTNERNAME
    on the ZWACHATSESSION row.
  * Message type 0 = text. Other types (audio, image, video, sticker, …)
    have no ZTEXT — we label them "[Medien]" so the summary stays clean.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..applescript import osa, ASError

_CHAT_DB = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared"
    / "ChatStorage.sqlite"
)
# Core Data timestamps: seconds since 2001-01-01 00:00:00 UTC.
_APPLE_EPOCH = 978307200


@dataclass(frozen=True)
class WAMessage:
    sender: str          # display name or phone JID
    text: str
    timestamp: float     # unix
    is_from_me: bool
    chat_name: str       # conversation / group name


@dataclass(frozen=True)
class WAChat:
    name: str
    unread: int
    last_message: str
    last_ts: float       # unix


class WhatsAppReader:
    """WhatsApp access: reads messages from ChatStorage.sqlite, sends via
    the whatsapp:// URL scheme + System Events."""

    def __init__(self, db_path: Path | str = _CHAT_DB, comm_db: Any = None) -> None:
        self._db = Path(db_path)          # the WhatsApp chat.db (read)
        self._comm_db = comm_db           # CommunicationDB for send logging

    @property
    def available(self) -> bool:
        return self._db.exists()

    def _connect(self) -> sqlite3.Connection | None:
        if not self._db.exists():
            return None
        try:
            return sqlite3.connect(
                f"file:{self._db}?mode=ro", uri=True,
                check_same_thread=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] db connect failed: {exc}")
            return None

    def _ts(self, apple_ts: float | None) -> float:
        """Convert Apple epoch → Unix epoch."""
        if apple_ts is None:
            return 0.0
        return float(apple_ts) + _APPLE_EPOCH

    # ── public API ─────────────────────────────────────────────────────── #

    def get_unread_chats(self, limit: int = 10) -> list[WAChat]:
        """Return chats with unread messages, newest first."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            # Join with ZWAMESSAGE to get the actual last text — ZLASTMESSAGETEXT
            # on ZWACHATSESSION is a protobuf blob in newer WA versions.
            cur = conn.execute(
                """
                SELECT s.ZPARTNERNAME, s.ZUNREADCOUNT, s.ZLASTMESSAGEDATE,
                       (SELECT m.ZTEXT FROM ZWAMESSAGE m
                        WHERE  m.ZCHATSESSION = s.Z_PK
                          AND  m.ZTEXT IS NOT NULL
                        ORDER  BY m.ZMESSAGEDATE DESC LIMIT 1) as last_text
                FROM   ZWACHATSESSION s
                WHERE  s.ZUNREADCOUNT > 0
                  AND  s.ZARCHIVED = 0
                  AND  s.ZHIDDEN = 0
                ORDER  BY s.ZLASTMESSAGEDATE DESC
                LIMIT  ?
                """,
                (limit,),
            )
            return [
                WAChat(
                    name=row[0] or "Unbekannt",
                    unread=row[1] or 0,
                    last_message=(row[3] or "")[:120],
                    last_ts=self._ts(row[2]),
                )
                for row in cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] get_unread_chats failed: {exc}")
            return []
        finally:
            conn.close()

    def get_recent_messages(self, contact: str,
                            limit: int = 10) -> list[WAMessage]:
        """Return the last ``limit`` messages from chats whose name
        contains ``contact`` (case-insensitive)."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            # Resolve session PK(s) matching the contact name.
            cur = conn.execute(
                """
                SELECT Z_PK, ZPARTNERNAME FROM ZWACHATSESSION
                WHERE  lower(ZPARTNERNAME) LIKE lower(?)
                  AND  ZARCHIVED = 0
                LIMIT  5
                """,
                (f"%{contact}%",),
            )
            sessions = cur.fetchall()
            if not sessions:
                return []
            pks = [str(row[0]) for row in sessions]
            chat_name = sessions[0][1] or contact
            # For 1:1 chats the partner IS the sender for inbound messages.
            partner_name = sessions[0][1] or contact
            placeholders = ",".join("?" * len(pks))
            cur2 = conn.execute(
                f"""
                SELECT ZPUSHNAME, ZFROMJID, ZTEXT, ZMESSAGEDATE,
                       ZISFROMME, ZMESSAGETYPE
                FROM   ZWAMESSAGE
                WHERE  ZCHATSESSION IN ({placeholders})
                ORDER  BY ZMESSAGEDATE DESC
                LIMIT  ?
                """,
                (*pks, limit),
            )
            rows = cur2.fetchall()
            msgs: list[WAMessage] = []
            for push, jid, text, ts, from_me, mtype in reversed(rows):
                # ZPUSHNAME is a human display name in old WA, but newer WA
                # stores a protobuf/base64 blob there. A real name has either
                # spaces, emoji, or no base64 padding — blobs are long,
                # no-space, and typically end with '='.
                clean_push = (push or "").strip()
                is_blob = (
                    len(clean_push) > 20
                    and " " not in clean_push
                    and (clean_push.endswith("=") or clean_push.endswith("=="))
                )
                if is_blob or not all(
                        c.isprintable() for c in clean_push[:20]):
                    clean_push = ""
                # For 1:1 chats fall back to the partner name from the session.
                sender = clean_push or partner_name or (jid or "").split("@")[0] or "Unbekannt"
                body = text if text else ("[Medien]" if mtype != 0 else "")
                msgs.append(WAMessage(
                    sender="Du" if from_me else sender,
                    text=body,
                    timestamp=self._ts(ts),
                    is_from_me=bool(from_me),
                    chat_name=chat_name,
                ))
            return msgs
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] get_recent_messages failed: {exc}")
            return []
        finally:
            conn.close()

    def spoken_unread_summary(self) -> str:
        """Return a spoken-style summary of unread WhatsApp chats."""
        if not self.available:
            return "WhatsApp-Datenbank nicht gefunden."
        chats = self.get_unread_chats(limit=8)
        if not chats:
            return "Keine ungelesenen WhatsApp-Nachrichten."
        total = sum(c.unread for c in chats)
        parts = [f"{total} ungelesene WhatsApp-Nachrichten"]
        for c in chats[:4]:
            snippet = c.last_message[:60].replace("\n", " ")
            parts.append(f"von {c.name}: {snippet}")
        if len(chats) > 4:
            parts.append(f"und {len(chats)-4} weitere Chats.")
        return ". ".join(parts) + "."

    def get_conversation_summary(self, contact: str,
                                 limit: int = 8) -> str:
        """Spoken summary of recent messages with ``contact``."""
        msgs = self.get_recent_messages(contact, limit=limit)
        if not msgs:
            return f"Keine WhatsApp-Nachrichten von {contact} gefunden."
        lines: list[str] = []
        for m in msgs[-5:]:
            ts = time.strftime("%H:%M", time.localtime(m.timestamp))
            lines.append(f"{ts} {m.sender}: {m.text[:80]}")
        return f"WhatsApp – {msgs[0].chat_name}. " + ". ".join(lines) + "."

    # ── sending ──────────────────────────────────────────────────────── #

    def resolve_phone(self, contact: str) -> tuple[str, str] | None:
        """Look up a contact's phone number from the chat DB by name.

        Returns (display_name, phone_digits) or None. Only resolves 1:1
        chats (ZCONTACTJID like '4915...@s.whatsapp.net') — group chats
        (@g.us) have no single phone and are skipped for sending."""
        conn = self._connect()
        if conn is None:
            return None
        try:
            cur = conn.execute(
                """
                SELECT ZPARTNERNAME, ZCONTACTJID FROM ZWACHATSESSION
                WHERE  lower(ZPARTNERNAME) LIKE lower(?)
                  AND  ZCONTACTJID LIKE '%@s.whatsapp.net'
                  AND  ZARCHIVED = 0
                ORDER  BY ZLASTMESSAGEDATE DESC
                LIMIT  1
                """,
                (f"%{contact}%",),
            )
            row = cur.fetchone()
            if not row:
                return None
            name, jid = row[0] or contact, row[1] or ""
            phone = jid.split("@")[0]
            if not phone.isdigit():
                return None
            return (name, phone)
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] resolve_phone failed: {exc}")
            return None
        finally:
            conn.close()

    def send(self, contact: str, message: str, *, auto_send: bool = True) -> str:
        """Send a WhatsApp message to ``contact``.

        Resolves the contact name to a phone number via the chat DB, opens
        the chat with the message prefilled via the whatsapp:// URL scheme,
        then (if auto_send) presses Return via System Events to send.

        Requires: WhatsApp signed in + running, and Accessibility permission
        for the controlling app (Electron/Terminal) to send the keystroke.
        Returns a spoken-style status string. The CALLER owns the
        confirm-before-send gate — this is the raw transport."""
        from urllib.parse import quote

        resolved = self.resolve_phone(contact)
        if resolved is None:
            return (f"Ich konnte keine WhatsApp-Nummer für '{contact}' finden. "
                    f"Schreib der Person einmal manuell, dann kenne ich den Chat.")
        name, phone = resolved

        # Open the chat with the text prefilled.
        url = f"whatsapp://send?phone={phone}&text={quote(message)}"
        try:
            osa('on run argv\nopen location (item 1 of argv)\nend run', url)
        except ASError as exc:
            return f"Konnte WhatsApp nicht öffnen: {exc}"

        if not auto_send:
            return (f"WhatsApp-Chat mit {name} ist geöffnet, die Nachricht ist "
                    f"vorbereitet. Drück Enter zum Senden.")

        # Give WhatsApp a moment to focus the message field, then press Return.
        time.sleep(1.5)
        press_return = (
            'tell application "WhatsApp" to activate\n'
            'delay 0.4\n'
            'tell application "System Events" to key code 36\n'  # 36 = Return
        )
        try:
            osa(press_return)
        except ASError as exc:
            return (f"Nachricht für {name} vorbereitet, aber das automatische "
                    f"Senden ging nicht ({exc}). Drück Enter im WhatsApp-Fenster.")
        if self._comm_db is not None:
            try:
                self._comm_db.log_message("whatsapp", "out", name, message,
                                          delivered=True)
            except Exception:  # noqa: BLE001
                pass
        return f"WhatsApp-Nachricht an {name} gesendet."


def _extract_last_text(raw: str | None) -> str:
    """ZLASTMESSAGETEXT is a protobuf blob for newer messages and plain
    text for older ones. Return a readable snippet either way."""
    if not raw:
        return ""
    # If the string contains enough printable German/Latin characters in the
    # first 30 bytes it's real text (older WA versions stored plain text).
    printable = sum(1 for c in raw[:30] if c.isprintable() and ord(c) < 0x200)
    if printable >= 6:
        return raw[:120]
    # Newer WA stores a protobuf blob in ZLASTMESSAGETEXT — not useful as
    # spoken output. Fall back to "[Nachricht]" as a neutral label.
    return "[Nachricht]"
