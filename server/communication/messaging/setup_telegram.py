"""Interactive Telegram setup helper.

Run once to wire JARVIS's Telegram push channel:

    python -m server.communication.messaging.setup_telegram

Steps it walks you through:
  1. Create a bot via @BotFather on Telegram, copy the token.
  2. Paste the token here; we verify it with getMe.
  3. Send any message to your new bot from your phone.
  4. We poll getUpdates, auto-detect your chat_id, and send a test push.
  5. Print the two .env lines to paste in.

Nothing is written to disk automatically — you copy the two lines into
``.env`` yourself, so the helper needs no write access to secrets.
"""
from __future__ import annotations

import asyncio
import time

from .telegram_bot import TelegramController


async def _run() -> None:
    print("=== JARVIS Telegram Setup ===\n")
    print("1. Open Telegram, search for @BotFather, send /newbot, follow the")
    print("   prompts, and copy the HTTP API token it gives you.\n")
    token = input("Bot-Token einfügen: ").strip()
    if not token:
        print("Kein Token — abgebrochen.")
        return

    tg = TelegramController(token=token, chat_id="")
    info = await tg.connect()
    if not info.get("connected"):
        print(f"Token-Check fehlgeschlagen: {info.get('reason')}")
        return
    print(f"✓ Bot erkannt: @{info['bot']}\n")

    print("2. Schicke deinem Bot jetzt EINE beliebige Nachricht in Telegram")
    print("   (z. B. 'hallo'). Ich warte darauf…\n")

    chat_id = ""
    for _ in range(60):  # ~60s
        # learn_chat_id=True is safe HERE (interactive setup) — the user is
        # the one messaging the bot right now.
        tg.poll_once(learn_chat_id=True)
        if tg.chat_id:
            chat_id = tg.chat_id
            break
        time.sleep(1)

    if not chat_id:
        print("Keine Nachricht empfangen. Starte erneut und schreibe dem Bot.")
        return
    print(f"✓ chat_id erkannt: {chat_id}\n")

    await tg.send_to_self("✅ JARVIS Telegram-Push funktioniert!")
    print("✓ Testnachricht gesendet — schau in Telegram.\n")

    print("Füge diese Zeilen in deine .env ein:\n")
    print(f"TELEGRAM_BOT_TOKEN={token}")
    print(f"TELEGRAM_CHAT_ID={chat_id}")
    print("TELEGRAM_NOTIFICATIONS=true")


if __name__ == "__main__":
    asyncio.run(_run())
