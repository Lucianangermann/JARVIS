"""CommunicationExecMixin — iMessage, WhatsApp, and Apple Mail handlers.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from typing import Any


class CommunicationExecMixin:
    """Exec methods for iMessage / WhatsApp sending and Apple Mail."""

    def _exec_send_imessage(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Send a text via iMessage through the communication layer's
        confirm-before-send flow. Closes the misroute where texting requests
        fell through to apple_mail (email) because the brain had no real
        texting tool — Claude would email instead and even claim it sent an
        'iMessage via Apple Mail'."""
        comm = getattr(self, "_communication", None)
        messaging = getattr(comm, "messaging", None) if comm is not None else None
        if messaging is None:
            return "Nachrichten-Versand ist nicht verfügbar.", True
        inp = tool_input or {}
        to = (inp.get("to") or "").strip()
        message = (inp.get("message") or "").strip()
        platform = (inp.get("platform") or "imessage").strip().lower()
        if platform not in ("imessage", "whatsapp"):
            platform = "imessage"
        if not to or not message:
            return "to und message sind erforderlich.", True
        import asyncio as _aio
        from .. import events as _events
        try:
            coro = messaging.send(platform, to, message)
            main_loop = _events._loop
            if main_loop is not None and main_loop.is_running():
                r = _aio.run_coroutine_threadsafe(coro, main_loop).result(timeout=25)
            else:
                r = _aio.run(coro)
        except Exception as exc:  # noqa: BLE001
            return f"{platform}-Versand fehlgeschlagen: {exc}", True
        return r.get("preview", "Nachricht vorbereitet. Bestätigen?"), False

    def _exec_apple_mail(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.mail_tool import list_unread, read_message, send_message, get_unread_count
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list_unread":
            return list_unread(inp.get("mailbox", "INBOX"))
        if action == "read":
            subject = inp.get("subject", "")
            if not subject:
                return "subject ist erforderlich.", True
            return read_message(subject)
        if action == "send":
            to = inp.get("to", "")
            subject = inp.get("subject", "")
            body = inp.get("body", "")
            if not to or not subject:
                return "to und subject sind erforderlich.", True
            return send_message(to, subject, body)
        if action == "unread_count":
            return get_unread_count()
        return f"Unbekannte Aktion: {action}", True
