"""Unified messaging interface across iMessage / Telegram (/ WhatsApp later).

Same commands work on every platform. The defining rule (spec
§IMPORTANT): **never auto-send** — every send is staged as a *pending*
action with a spoken preview, and only goes out after an explicit
confirmation ("ja" / "bestätigen"). The pending slot has a 30s TTL, the
same contract as ``mac_control/confirmation.py``, kept self-contained
here so the messaging flow doesn't depend on the mac_action dispatcher.

Claude is reused (the brain's client) for multi-message summarisation and
reply drafting — no extra dependency, no extra key.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from ...config import settings
from .imessage import iMessageController
from .telegram_bot import TelegramController
from .whatsapp import WhatsAppReader

_PENDING_TTL_S = 120.0
_BROADCAST_RATE_S = 2.0  # 1 message / 2 s (spec)


class MessagingManager:
    """Unified send/read with confirm-before-send across platforms."""

    def __init__(self, db: Any = None, client: Any = None,
                 telegram: TelegramController | None = None) -> None:
        self._db = db
        self._client = client
        self.imessage = iMessageController(db=db)
        self.telegram = telegram or TelegramController(db=db)
        self.whatsapp = WhatsAppReader(comm_db=db)
        # One pending send/broadcast awaiting confirmation.
        self._pending: dict[str, Any] | None = None

    # ── platform routing ───────────────────────────────────────────────── #

    def _platform(self, name: str) -> Any:
        return {
            "imessage": self.imessage,
            "telegram": self.telegram,
            "whatsapp": self.whatsapp,
        }.get(name)

    async def _raw_send(self, platform: str, contact: str, message: str) -> bool:
        ctrl = self._platform(platform)
        if ctrl is None:
            print(f"[Messaging] platform '{platform}' unavailable")
            return False
        if platform == "telegram":
            # contact is a chat_id; fall back to owner chat if "self"/empty.
            if not contact or contact in ("self", "me"):
                return await self.telegram.send_to_self(message)
            return await self.telegram.send(contact, message)
        if platform == "whatsapp":
            # WhatsApp send is synchronous (URL scheme + System Events) and
            # returns a status string, not a bool. Run it in a thread so the
            # sleep inside doesn't block the event loop.
            try:
                status = await asyncio.to_thread(self.whatsapp.send, contact, message)
                print(f"[Messaging] whatsapp: {status}")
                # "gesendet" → fully sent via System Events
                # "offen" / "eingetragen" / "vorbereitet" → chat opened, user presses Enter
                # Anything else (Konnte WhatsApp nicht öffnen...) → real failure
                ok = ("gesendet" in status or "offen" in status
                      or "eingetragen" in status or "vorbereitet" in status)
                self._last_wa_status = status  # save always — used in confirm_pending
                return ok
            except Exception as exc:  # noqa: BLE001
                print(f"[Messaging] whatsapp send error: {exc}")
                self._last_wa_status = None
                return False
        return await ctrl.send(contact, message)

    # ── unread aggregation ─────────────────────────────────────────────── #

    async def get_all_unread(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch unread from all platforms concurrently."""
        async def _im() -> list[dict[str, Any]]:
            try:
                return await self.imessage.get_unread()
            except Exception as exc:  # noqa: BLE001
                print(f"[Messaging] imessage unread failed: {exc}")
                return []

        async def _tg() -> list[dict[str, Any]]:
            try:
                # Telegram inbound arrives via polling into the DB; surface
                # recent inbound telegram messages from there.
                if self._db is None:
                    return []
                rows = self._db.recent_messages(platform="telegram", limit=20)
                return [{"sender": r["contact"], "message": r["content"],
                         "time": r["timestamp"]}
                        for r in rows if r["direction"] == "in"]
            except Exception:  # noqa: BLE001
                return []

        im, tg = await asyncio.gather(_im(), _tg())
        wa = self.whatsapp.get_unread_chats() if self.whatsapp else []
        return {"imessage": im, "telegram": tg, "whatsapp": wa}

    async def spoken_unread_summary(self) -> str:
        data = await self.get_all_unread()
        im, tg = len(data["imessage"]), len(data["telegram"])
        wa_chats = data.get("whatsapp", [])
        wa = sum(c.unread for c in wa_chats) if wa_chats else 0
        total = im + tg + wa
        if total == 0:
            return "Du hast keine neuen Nachrichten."
        parts = []
        if im:
            senders = ", ".join(sorted({m["sender"] for m in data["imessage"]})[:3])
            parts.append(f"{im} iMessage{'s' if im != 1 else ''}"
                         f"{' von ' + senders if senders else ''}")
        if tg:
            parts.append(f"{tg} Telegram-Nachricht{'en' if tg != 1 else ''}")
        if wa:
            names = ", ".join(c.name for c in wa_chats[:3])
            parts.append(f"{wa} WhatsApp-Nachricht{'en' if wa != 1 else ''}"
                         f" von {names}")
        return f"Du hast {total} neue Nachrichten: " + ", ".join(parts) + "."

    # ── reading + summarising ──────────────────────────────────────────── #

    async def read_messages(self, platform: str = "all", contact: str | None = None,
                            n: int = 5) -> str:
        if platform == "whatsapp" and self.whatsapp:
            if contact:
                return self.whatsapp.get_conversation_summary(contact, limit=n)
            return self.whatsapp.spoken_unread_summary()
        msgs: list[dict[str, Any]] = []
        if platform in ("all", "imessage") and contact:
            msgs = await self.imessage.get_conversation(contact, n)
        elif self._db is not None:
            rows = self._db.recent_messages(
                platform=None if platform == "all" else platform,
                contact=contact, limit=n)
            msgs = [{"sender": r["contact"], "content": r["content"]} for r in rows]
        if not msgs:
            return "Keine Nachrichten gefunden."
        if len(msgs) > 3 and self._client is not None:
            return await self._summarize(msgs)
        return ". ".join(
            f"{m.get('sender', '?')}: {m.get('content') or m.get('message', '')}"
            for m in msgs)

    async def _summarize(self, msgs: list[dict[str, Any]]) -> str:
        convo = "\n".join(
            f"{m.get('sender', '?')}: {m.get('content') or m.get('message', '')}"
            for m in msgs)
        prompt = ("Summarize this message thread in 2-3 short German "
                  "sentences:\n\n" + convo)
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=300,
                messages=[{"role": "user", "content": prompt}])
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    return (b.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[Messaging] summarize failed: {exc}")
        return f"{len(msgs)} Nachrichten."

    # ── send (staged) ──────────────────────────────────────────────────── #

    def _stage(self, kind: str, sends: list[tuple[str, str, str]]) -> str:
        """Stage a pending send and return its server-issued id. The id lets
        the API enforce a genuine two-step (a confirm must reference the id
        from a prior response), instead of a caller-set boolean that both
        stages and fires in one request."""
        pending_id = secrets.token_hex(8)
        self._pending = {"kind": kind, "sends": sends, "ts": time.time(),
                         "id": pending_id}
        return pending_id

    async def send(self, platform: str, contact: str, message: str) -> dict[str, Any]:
        """Stage a send for confirmation. Returns a preview + pending_id;
        the actual send happens on confirm_pending(pending_id)."""
        pid = self._stage("send", [(platform, contact, message)])
        preview = (f"Sende an {contact} via {platform}: \"{message}\". "
                   f"Bestätigen?")
        return {"needs_confirm": True, "preview": preview, "pending_id": pid}

    async def reply_to_last(self, platform: str, instructions: str) -> dict[str, Any]:
        """Draft a reply to the last received message via Claude, stage it."""
        last = None
        if platform == "imessage":
            unread = await self.imessage.get_unread()
            last = unread[0] if unread else None
        elif self._db is not None:
            rows = self._db.recent_messages(platform=platform, limit=20)
            inbound = [r for r in rows if r["direction"] == "in"]
            if inbound:
                last = {"sender": inbound[0]["contact"],
                        "message": inbound[0]["content"]}
        if not last:
            return {"needs_confirm": False,
                    "preview": "Keine empfangene Nachricht zum Antworten gefunden."}
        draft = await self._draft_reply(last.get("message", ""), instructions)
        pid = self._stage("send", [(platform, last["sender"], draft)])
        return {"needs_confirm": True, "pending_id": pid,
                "preview": f"Antwort an {last['sender']}: \"{draft}\". Bestätigen?"}

    async def _draft_reply(self, original: str, instructions: str) -> str:
        if self._client is None:
            return instructions  # fall back to the literal instruction text
        prompt = (f"Draft a short reply to this message. The reply should: "
                  f"{instructions}\n\nOriginal message: {original}\n\n"
                  f"Return ONLY the reply text, in the same language as the "
                  f"original.")
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=300,
                messages=[{"role": "user", "content": prompt}])
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    return (b.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[Messaging] draft failed: {exc}")
        return instructions

    async def broadcast(self, message: str, contacts: list[str],
                        platforms: list[str] | None = None) -> dict[str, Any]:
        platforms = platforms or ["imessage"]
        sends = [(p, c, message) for c in contacts for p in platforms]
        pid = self._stage("broadcast", sends)
        names = ", ".join(contacts)
        return {"needs_confirm": True, "pending_id": pid,
                "preview": f"Sende an {len(contacts)} Kontakte: {names}. Bestätigen?"}

    # ── confirmation ───────────────────────────────────────────────────── #

    def has_pending(self) -> bool:
        if self._pending is None:
            return False
        if time.time() - self._pending["ts"] > _PENDING_TTL_S:
            self._pending = None
            return False
        return True

    async def confirm_pending(self, pending_id: str | None = None) -> str:
        if not self.has_pending():
            return "Es gibt nichts zu bestätigen."
        # API path passes the id from the prior /send response; it must match
        # the staged send. The voice path passes None (same in-process session
        # — "ja" right after the preview), which is already a two-step.
        if pending_id is not None and self._pending.get("id") != pending_id:
            return "Bestätigungs-ID passt nicht."
        pending = self._pending
        # Clear pending AFTER the send so a failed send can be retried.
        sends = pending["sends"]
        ok = 0
        try:
            for i, (platform, contact, message) in enumerate(sends):
                if i > 0 and pending["kind"] == "broadcast":
                    await asyncio.sleep(_BROADCAST_RATE_S)  # rate limit
                if await self._raw_send(platform, contact, message):
                    ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[Messaging] confirm_pending error: {exc}")
            return "Senden fehlgeschlagen."
        finally:
            self._pending = None  # always clear, success or failure
        if pending["kind"] == "broadcast":
            return f"An {ok} von {len(sends)} Kontakte gesendet."
        wa_status = getattr(self, "_last_wa_status", None)
        if ok:
            # Propagate the real WhatsApp status: "drück Enter" when System
            # Events couldn't press Return, or the full "gesendet" confirmation.
            return wa_status or "Nachricht gesendet."
        # Propagate the real failure reason (contact not found, URL failed, …)
        # so the user hears something useful instead of the generic fallback.
        return wa_status or "Senden fehlgeschlagen."

    def cancel_pending(self) -> str:
        self._pending = None
        return "Abgebrochen."
