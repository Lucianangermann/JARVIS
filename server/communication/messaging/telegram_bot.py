"""Telegram bot bridge over the plain Bot HTTP API (via ``requests``).

We deliberately avoid ``python-telegram-bot``: it ships its own asyncio
event loop and a large dependency tree that would fight FastAPI's loop.
The Bot API is just REST — ``getMe`` / ``getUpdates`` / ``sendMessage`` —
and ``requests`` is already a project dependency, so this stays small and
robust.

Telegram is the most reliable push channel to the owner's iPhone (better
than PWA web-push, which iOS throttles). ``send_to_self`` /
``send_notification`` post to ``TELEGRAM_CHAT_ID``; the
:class:`NotificationCenter` uses them as its ``telegram`` channel.

Inbound polling (``poll_once`` / ``start_polling``) tracks an update
offset so each message is read once. Everything is best-effort with short
timeouts — a Telegram outage must never stall JARVIS.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ...config import settings

try:
    import requests  # type: ignore[import-not-found]
    _REQUESTS_OK = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore[assignment]
    _REQUESTS_OK = False

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 10

MessageHandler = Callable[[dict[str, Any]], None]


class TelegramController:
    """Minimal Telegram Bot API client (send + poll)."""

    def __init__(
        self,
        db: Any = None,
        token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._db = db
        self._token = token if token is not None else getattr(
            settings, "TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else getattr(
            settings, "TELEGRAM_CHAT_ID", "")
        self._offset = 0           # getUpdates pagination cursor
        self._bot_name: str | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def configured(self) -> bool:
        return bool(_REQUESTS_OK and self._token)

    # ── low-level call ─────────────────────────────────────────────────── #

    def _call(self, method: str, **params: Any) -> dict[str, Any] | None:
        if not self.configured:
            return None
        try:
            resp = requests.post(
                _API.format(token=self._token, method=method),
                json=params, timeout=_TIMEOUT,
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"[Telegram] {method} not ok: {data.get('description')}")
                return None
            return data.get("result")
        except Exception as exc:  # noqa: BLE001
            print(f"[Telegram] {method} failed: {exc}")
            return None

    # ── connect ────────────────────────────────────────────────────────── #

    async def connect(self) -> dict[str, Any]:
        if not self.configured:
            return {"connected": False, "reason": "no token / requests missing"}
        me = self._call("getMe")
        if me is None:
            return {"connected": False, "reason": "getMe failed"}
        self._bot_name = me.get("username")
        print(f"[TELEGRAM] Bot connected: @{self._bot_name}")
        return {"connected": True, "bot": self._bot_name}

    # ── sending ────────────────────────────────────────────────────────── #

    async def send(self, chat_id: str, message: str,
                   markdown: bool = True) -> bool:
        params: dict[str, Any] = {"chat_id": chat_id, "text": message}
        if markdown:
            params["parse_mode"] = "Markdown"
        ok = self._call("sendMessage", **params) is not None
        if ok and self._db is not None:
            self._db.log_message("telegram", "out", str(chat_id), message,
                                 delivered=True)
        return ok

    async def send_to_self(self, message: str) -> bool:
        if not self._chat_id:
            print("[Telegram] TELEGRAM_CHAT_ID not set — can't push to owner")
            return False
        return await self.send(self._chat_id, message)

    async def send_notification(self, title: str, body: str,
                                priority: str = "normal") -> bool:
        prefix = "🚨 " if priority in ("high", "critical") else "🔔 "
        text = f"{prefix}*{self._md_escape(title)}*\n{body}"
        return await self.send_to_self(text)

    # Synchronous wrapper so the NotificationCenter (sync) can use it as a
    # channel handler without awaiting.
    def notify_sync(self, title: str, body: str, priority: str = "normal") -> None:
        if not self.configured or not self._chat_id:
            return
        prefix = "🚨 " if priority in ("high", "critical") else "🔔 "
        text = f"{prefix}*{self._md_escape(title)}*\n{body}"
        self._call("sendMessage", chat_id=self._chat_id, text=text,
                   parse_mode="Markdown")

    @staticmethod
    def _md_escape(s: str) -> str:
        # Escape the Markdown-significant chars in the bolded title.
        for ch in ("_", "*", "`", "["):
            s = s.replace(ch, "\\" + ch)
        return s

    # ── inbound polling ────────────────────────────────────────────────── #

    def poll_once(self) -> list[dict[str, Any]]:
        """Fetch new updates since the last offset. Returns normalised
        message dicts and advances the cursor."""
        result = self._call("getUpdates", offset=self._offset, timeout=0)
        if not result:
            return []
        msgs: list[dict[str, Any]] = []
        for upd in result:
            self._offset = max(self._offset, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            # Auto-learn the owner chat_id on first inbound message.
            if not self._chat_id:
                self._chat_id = str(chat.get("id", ""))
                print(f"[Telegram] learned chat_id: {self._chat_id}")
            text = msg.get("text", "")
            sender = (msg.get("from", {}).get("first_name")
                      or str(chat.get("id", "")))
            msgs.append({"from": sender, "text": text,
                         "chat_id": str(chat.get("id", "")),
                         "date": msg.get("date", time.time())})
            if self._db is not None and text:
                self._db.log_message("telegram", "in", sender, text)
        return msgs

    def start_polling(self, handler: MessageHandler, interval_s: float = 3.0) -> None:
        """Background long-poll loop; calls ``handler(msg)`` per message."""
        if not self.configured or (self._poll_thread and self._poll_thread.is_alive()):
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    for msg in self.poll_once():
                        try:
                            handler(msg)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Telegram] handler failed: {exc}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[Telegram] poll loop error: {exc}")
                self._stop.wait(interval_s)

        self._poll_thread = threading.Thread(
            target=_loop, name="jarvis-telegram", daemon=True)
        self._poll_thread.start()
        print("[TELEGRAM] polling started")

    def stop(self) -> None:
        self._stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def bot_name(self) -> str | None:
        return self._bot_name
