"""Extended email manager — multi-account, templates, attachments.

Builds on the existing ``server/tools/mail_tool.py`` (Apple Mail via
AppleScript) rather than replacing it: basic send/read still go through
mail_tool, and this layer adds the template system, attachment sending
(argv-safe AppleScript), multi-account summaries, newsletter detection,
and the same confirm-before-send gate as messaging. Sends are NEVER
automatic — they stage a preview and require confirmation.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ...config import settings
from ...tools import mail_tool
from ..applescript import ASError, osa
from .email_templates import EmailTemplateManager
from .email_analyzer import EmailAnalyzer

_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB
_PENDING_TTL_S = 30.0

# Attachment send — all fields arrive as positional argv (injection-safe).
_ATTACH_SCRIPT = """
on run argv
    set theTo to item 1 of argv
    set theSub to item 2 of argv
    set theBody to item 3 of argv
    set theFile to item 4 of argv
    tell application "Mail"
        set msg to make new outgoing message with properties ¬
            {subject:theSub, content:theBody, visible:true}
        tell msg
            make new to recipient with properties {address:theTo}
            make new attachment with properties ¬
                {file name:(POSIX file theFile)} at after the last paragraph
        end tell
        send msg
    end tell
    return "sent"
end run
"""


class ExtendedEmailManager:
    def __init__(self, db: Any = None, client: Any = None) -> None:
        self._db = db
        self._client = client
        self.templates = EmailTemplateManager()
        self.analyzer = EmailAnalyzer(client=client)
        self._pending: dict[str, Any] | None = None

    # ── summaries ──────────────────────────────────────────────────────── #

    async def get_all_accounts_summary(self) -> str:
        """Unread overview across all Apple Mail accounts (mail_tool already
        iterates every account)."""
        count_out, err = mail_tool.get_unread_count()
        if err:
            return "E-Mail-Übersicht momentan nicht verfügbar."
        return f"Ungelesene E-Mails: {count_out}."

    async def get_important_summary(self, limit: int = 10) -> str:
        """List unread and classify importance via Claude (best-effort)."""
        listing, err = mail_tool.list_unread(limit=limit)
        if err or not listing:
            return "Keine ungelesenen E-Mails."
        return listing if self._client is None else (
            await self.analyzer.summarize("Ungelesene E-Mails", listing))

    # ── templates ──────────────────────────────────────────────────────── #

    def save_template(self, name: str, subject: str, body: str) -> dict[str, Any]:
        return self.templates.save_template(name, subject, body)

    def list_templates(self) -> list[str]:
        return self.templates.all_names()

    async def use_template(self, name: str, variables: dict[str, str],
                           to: str) -> dict[str, Any]:
        filled = self.templates.fill(name, variables)
        if filled is None:
            return {"needs_confirm": False,
                    "preview": f"Vorlage '{name}' nicht gefunden."}
        self._pending = {
            "kind": "send", "to": to, "subject": filled["subject"],
            "body": filled["body"], "attachment": None, "ts": time.time(),
        }
        return {"needs_confirm": True,
                "preview": (f"Sende E-Mail an {to}: Betreff "
                            f"\"{filled['subject']}\". Bestätigen?")}

    # ── attachments ────────────────────────────────────────────────────── #

    async def send_with_attachment(self, to: str, subject: str, body: str,
                                   file_path: str) -> dict[str, Any]:
        path = Path(file_path).expanduser()
        if not path.is_file():
            return {"needs_confirm": False,
                    "preview": f"Datei nicht gefunden: {file_path}"}
        size = path.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            return {"needs_confirm": False,
                    "preview": (f"Datei zu groß ({size/1e6:.1f} MB, "
                                f"max 25 MB).")}
        self._pending = {
            "kind": "send", "to": to, "subject": subject, "body": body,
            "attachment": str(path), "ts": time.time(),
        }
        return {"needs_confirm": True,
                "preview": (f"Sende '{path.name}' an {to}. Bestätigen?")}

    # ── plain send (staged) ────────────────────────────────────────────── #

    async def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self._pending = {
            "kind": "send", "to": to, "subject": subject, "body": body,
            "attachment": None, "ts": time.time(),
        }
        return {"needs_confirm": True,
                "preview": f"Sende E-Mail an {to}: \"{subject}\". Bestätigen?"}

    # ── confirmation ───────────────────────────────────────────────────── #

    def has_pending(self) -> bool:
        if self._pending is None:
            return False
        if time.time() - self._pending["ts"] > _PENDING_TTL_S:
            self._pending = None
            return False
        return True

    async def confirm_pending(self) -> str:
        if not self.has_pending():
            return "Es gibt nichts zu bestätigen."
        p = self._pending
        self._pending = None
        try:
            if p["attachment"]:
                osa(_ATTACH_SCRIPT, p["to"], p["subject"], p["body"],
                    p["attachment"])
                result = f"E-Mail mit Anhang an {p['to']} gesendet."
            else:
                out, err = mail_tool.send_message(p["to"], p["subject"], p["body"])
                result = out if not err else f"Fehler: {out}"
            if self._db is not None:
                self._db.log_message("email", "out", p["to"],
                                     f"{p['subject']}: {p['body'][:200]}",
                                     delivered=True)
            return result
        except ASError as exc:
            return f"E-Mail-Versand fehlgeschlagen: {exc}"

    def cancel_pending(self) -> str:
        self._pending = None
        return "Abgebrochen."

    # ── newsletters / unsubscribe ──────────────────────────────────────── #

    async def find_newsletters(self) -> list[str]:
        listing, err = mail_tool.list_unread(limit=30)
        if err or not listing:
            return []
        senders: set[str] = set()
        for line in listing.splitlines():
            if self.analyzer.looks_like_newsletter("", line, line):
                # Heuristic: pull a sender-ish token from the line.
                senders.add(line.split(":")[0].strip()[:60])
        return sorted(s for s in senders if s)

    async def unsubscribe(self, sender: str) -> str:
        body, err = mail_tool.read_message(sender)
        if err or not body:
            return f"Keine E-Mail von {sender} gefunden."
        link = self.analyzer.extract_unsubscribe_link(body)
        if not link:
            return f"Kein Abmelde-Link bei {sender} gefunden."
        try:
            osa('on run argv\nopen location item 1 of argv\nend run', link)
            return f"Abmelde-Link für {sender} geöffnet."
        except ASError as exc:
            return f"Konnte Link nicht öffnen: {exc}"

    # ── analytics ──────────────────────────────────────────────────────── #

    async def get_response_stats(self) -> dict[str, Any]:
        """Best-effort: we only see email we've routed through here, so
        stats are limited until more history accrues. Honest about it."""
        if self._db is None:
            return {"available": False, "note": "keine E-Mail-Historie"}
        rows = self._db.recent_messages(platform="email", limit=200)
        sent = sum(1 for r in rows if r["direction"] == "out")
        return {"available": sent > 0, "sent_tracked": sent,
                "note": "Antwortzeit-Analyse benötigt mehr Verlauf."}
