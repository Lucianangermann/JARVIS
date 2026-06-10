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
import time
from typing import Any

from ...config import settings
from .imessage import iMessageController
from .telegram_bot import TelegramController

_PENDING_TTL_S = 30.0
_BROADCAST_RATE_S = 2.0  # 1 message / 2 s (spec)


class MessagingManager:
    """Unified send/read with confirm-before-send across platforms."""

    def __init__(self, db: Any = None, client: Any = None,
                 telegram: TelegramController | None = None) -> None:
        self._db = db
        self._client = client
        self.imessage = iMessageController(db=db)
        self.telegram = telegram or TelegramController(db=db)
        self.whatsapp = None  # deferred (see tasks/todo.md)
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
        return {"imessage": im, "telegram": tg, "whatsapp": []}

    async def spoken_unread_summary(self) -> str:
        data = await self.get_all_unread()
        im, tg = len(data["imessage"]), len(data["telegram"])
        total = im + tg
        if total == 0:
            return "Du hast keine neuen Nachrichten."
        parts = []
        if im:
            senders = ", ".join(sorted({m["sender"] for m in data["imessage"]})[:3])
            parts.append(f"{im} iMessage{'s' if im != 1 else ''}"
                         f"{' von ' + senders if senders else ''}")
        if tg:
            parts.append(f"{tg} Telegram-Nachricht{'en' if tg != 1 else ''}")
        return f"Du hast {total} neue Nachrichten: " + ", ".join(parts) + "."

    # ── reading + summarising ──────────────────────────────────────────── #

    async def read_messages(self, platform: str = "all", contact: str | None = None,
                            n: int = 5) -> str:
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

    async def send(self, platform: str, contact: str, message: str) -> dict[str, Any]:
        """Stage a send for confirmation. Returns a preview; the actual
        send happens on confirm_pending()."""
        self._pending = {
            "kind": "send",
            "sends": [(platform, contact, message)],
            "ts": time.time(),
        }
        preview = (f"Sende an {contact} via {platform}: \"{message}\". "
                   f"Bestätigen?")
        return {"needs_confirm": True, "preview": preview}

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
        self._pending = {
            "kind": "send",
            "sends": [(platform, last["sender"], draft)],
            "ts": time.time(),
        }
        return {"needs_confirm": True,
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
        self._pending = {"kind": "broadcast", "sends": sends, "ts": time.time()}
        names = ", ".join(contacts)
        return {"needs_confirm": True,
                "preview": f"Sende an {len(contacts)} Kontakte: {names}. Bestätigen?"}

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
        pending = self._pending
        self._pending = None
        sends = pending["sends"]
        ok = 0
        for i, (platform, contact, message) in enumerate(sends):
            if i > 0 and pending["kind"] == "broadcast":
                await asyncio.sleep(_BROADCAST_RATE_S)  # rate limit
            if await self._raw_send(platform, contact, message):
                ok += 1
        if pending["kind"] == "broadcast":
            return f"An {ok} von {len(sends)} Kontakte gesendet."
        return "Nachricht gesendet." if ok else "Senden fehlgeschlagen."

    def cancel_pending(self) -> str:
        self._pending = None
        return "Abgebrochen."
