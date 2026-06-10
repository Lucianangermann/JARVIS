"""Phone / FaceTime calls via macOS Continuity.

Calling: resolve the contact's number from Contacts (AppleScript), then
open the appropriate URL scheme — ``facetime-audio:`` for FaceTime,
``tel:`` to place a cellular call through the linked iPhone. All argv-safe.

Missed calls: read macOS's CallHistory store (read-only, best-effort —
needs Full Disk Access) and merge with anything we've logged ourselves.

Callback reminders reuse the existing ``reminders_tool`` so they land in
Apple Reminders like every other JARVIS reminder. Voicemail transcription
has no supported API on macOS, so that method is honest about being
unavailable rather than faking it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ...tools import reminders_tool
from ..applescript import ASError, osa

# Core Data store for the Phone app's call history (Continuity).
_CALL_HISTORY_DB = (Path.home() / "Library" / "Application Support"
                    / "CallHistoryDB" / "CallHistory.storedata")
# Mac absolute time epoch (2001-01-01).
_MAC_EPOCH = 978307200

_LOOKUP_NUMBER = """
on run argv
    set theName to item 1 of argv
    tell application "Contacts"
        set thePeople to (every person whose name contains theName)
        if (count of thePeople) = 0 then return ""
        set thePhones to phones of item 1 of thePeople
        if (count of thePhones) = 0 then return ""
        return value of item 1 of thePhones
    end tell
end run
"""

_OPEN_URL = "on run argv\nopen location item 1 of argv\nend run"


class CallManager:
    def __init__(self, db: Any = None, imessage: Any = None) -> None:
        self._db = db                 # CommunicationDB
        self._imessage = imessage     # iMessageController (for decline-with-text)

    # ── place a call ───────────────────────────────────────────────────── #

    def _resolve_number(self, contact: str) -> str | None:
        # If it already looks like a number, use it directly.
        if contact and all(c.isdigit() or c in "+ -()" for c in contact):
            return contact.replace(" ", "")
        try:
            num = osa(_LOOKUP_NUMBER, contact)
            return num or None
        except ASError as exc:
            print(f"[CallManager] contact lookup failed: {exc}")
            return None

    async def make_call(self, contact: str, method: str = "auto") -> dict[str, Any]:
        number = self._resolve_number(contact)
        if not number:
            return {"ok": False, "spoken": f"Keine Nummer für {contact} gefunden."}
        scheme = "tel" if method == "phone" else "facetime-audio"
        url = f"{scheme}://{number}"
        try:
            osa(_OPEN_URL, url)
        except ASError as exc:
            return {"ok": False, "spoken": f"Anruf fehlgeschlagen: {exc}"}
        if self._db is not None:
            self._db.log_call(contact, "out", method=scheme, outcome="initiated")
        return {"ok": True, "spoken": f"Rufe {contact} an."}

    # ── incoming-call handling ─────────────────────────────────────────── #

    async def announce_incoming_call(self, caller: str) -> str:
        if self._db is not None:
            self._db.log_call(caller, "in", outcome="ringing")
        return f"Eingehender Anruf von {caller}."

    async def decline_with_message(self, caller: str,
                                   message: str | None = None) -> str:
        msg = message or "Kann gerade nicht sprechen, melde mich später."
        sent = False
        if self._imessage is not None:
            try:
                sent = await self._imessage.send(caller, msg)
            except Exception as exc:  # noqa: BLE001
                print(f"[CallManager] decline message failed: {exc}")
        if self._db is not None:
            self._db.log_call(caller, "in", outcome="declined")
        return (f"Anruf von {caller} abgelehnt, Nachricht gesendet."
                if sent else f"Anruf von {caller} abgelehnt.")

    # ── missed calls ───────────────────────────────────────────────────── #

    async def get_missed_calls(self, hours: int = 24) -> str:
        missed = self._read_macos_missed(hours)
        if not missed and self._db is not None:
            rows = self._db.missed_calls(time.time() - hours * 3600)
            missed = [(r["contact"] or "Unbekannt", r["timestamp"]) for r in rows]
        if not missed:
            return "Keine verpassten Anrufe."
        parts = []
        for name, ts in missed[:5]:
            hhmm = time.strftime("%H:%M", time.localtime(ts))
            parts.append(f"{name} ({hhmm})")
        return f"{len(missed)} verpasste Anrufe: " + ", ".join(parts) + "."

    def _read_macos_missed(self, hours: int) -> list[tuple[str, float]]:
        if not _CALL_HISTORY_DB.exists():
            return []
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{_CALL_HISTORY_DB}?mode=ro", uri=True)
            cutoff_mac = (time.time() - hours * 3600) - _MAC_EPOCH
            rows = conn.execute(
                """SELECT ZADDRESS, ZDATE FROM ZCALLRECORD
                   WHERE ZANSWERED = 0 AND ZORIGINATED = 0 AND ZDATE >= ?
                   ORDER BY ZDATE DESC""",
                (cutoff_mac,),
            ).fetchall()
            conn.close()
            return [((a or b"").decode("utf-8", "ignore")
                     if isinstance(a, bytes) else str(a or "Unbekannt"),
                     float(d) + _MAC_EPOCH) for a, d in rows]
        except Exception as exc:  # noqa: BLE001
            print(f"[CallManager] call history read failed (Full Disk Access?): {exc}")
            return []

    # ── callback reminders ─────────────────────────────────────────────── #

    async def set_callback_reminder(self, contact: str,
                                    when: str = "later") -> str:
        due = self._resolve_when(when)
        title = f"{contact} zurückrufen"
        out, err = reminders_tool.create_reminder(title, due_date=due)
        if self._db is not None:
            self._db.log_call(contact, "in", outcome="callback_scheduled",
                              callback_reminder=True)
        if err:
            return f"Konnte Erinnerung nicht erstellen: {out}"
        return f"Erinnerung gesetzt: {contact} zurückrufen."

    @staticmethod
    def _resolve_when(when: str) -> str | None:
        """Map a coarse phrase to an ISO datetime for the reminder."""
        now = time.localtime()
        if when in ("later", "heute abend", "evening"):
            t = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 18, 0, 0,
                                  now.tm_wday, now.tm_yday, now.tm_isdst))
            return time.strftime("%Y-%m-%dT%H:%M", t)
        if when in ("tomorrow", "morgen"):
            ts = time.localtime(time.time() + 86400)
            t = time.struct_time((ts.tm_year, ts.tm_mon, ts.tm_mday, 9, 0, 0,
                                  ts.tm_wday, ts.tm_yday, ts.tm_isdst))
            return time.strftime("%Y-%m-%dT%H:%M", t)
        # Assume an explicit ISO string was passed.
        return when if "T" in when else None

    # ── voicemail (unsupported) ────────────────────────────────────────── #

    async def get_voicemail_summary(self) -> str:
        # macOS exposes no supported API to read/transcribe voicemail.
        return ("Voicemail-Zugriff wird von macOS nicht unterstützt. "
                "Bitte hör die Mailbox direkt am iPhone ab.")
