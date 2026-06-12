"""Central coordinator for the JARVIS communication layer.

Owns the single CommunicationDB and every sub-manager, wires the
NotificationCenter's channels (voice/ui/telegram/macos), connects Telegram
if configured, and exposes:

  * ``start()`` / ``stop()`` — lifecycle.
  * ``process_command()`` — natural-language routing for the comm trigger
    phrases (spec §16), called by the brain BEFORE Claude. Handles the
    confirm-before-send flow ("ja"/"nein" when a send is pending).
  * ``morning_comm_brief()`` — unread + missed calls + important mail for
    the intelligence morning routine.

Construction never raises: a failed component is logged and left None so
the rest of the layer — and JARVIS — keeps working.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .db import CommunicationDB
from .notifications import NotificationCenter
from .translation import CommunicationTranslator
from .messaging import MessagingManager, TelegramController
from .calls import CallManager
from .email import ExtendedEmailManager
from .social import SocialManager
from .automation import CommunicationAutomation

_CONFIRM_WORDS = ("ja", "j", "jo", "bestätigen", "bestätige", "senden", "send",
                  "ok", "okay", "sicher", "klar", "mach es", "tue es")
_CANCEL_WORDS = ("nein", "abbrechen", "abbruch", "stop", "cancel", "nicht senden")


def deliver_emergency(message: str, contacts: list[str],
                      *, imessage: Any = None, telegram: Any = None) -> dict[str, Any]:
    """Push an emergency message out through every available transport.

    On a Mac the reliable, zero-cost contact channel is iMessage (the
    owner is signed into Messages.app), so each contact is texted directly.
    The owner's own phone also gets a Telegram push when that bot is set up.
    Best-effort by design: a failing transport is logged, never raised —
    an emergency must not be aborted because one channel is down.

    Returns a small report dict for the audit log.
    """
    imsg_ok = 0
    for contact in contacts or []:
        try:
            if imessage is not None and imessage.send_sync(contact, message):
                imsg_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency] iMessage to {contact} failed: {exc}")
    tg_ok = False
    try:
        if telegram is not None and getattr(telegram, "configured", False):
            telegram.notify_sync("NOTFALL", message, "critical")
            tg_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"[Emergency] telegram push failed: {exc}")
    return {"contacts": len(contacts or []), "imessage": imsg_ok, "telegram": tg_ok}


class CommunicationManager:
    """Coordinator that owns and wires the whole communication layer."""

    def __init__(
        self,
        db_path: Path | str = "data/communication.db",
        client: Any = None,
        speak_handler: Callable[[str], None] | None = None,
        ui_handler: Callable[[dict], None] | None = None,
        macos_handler: Callable[[str, str], None] | None = None,
        meeting_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._client = client
        self.db = CommunicationDB(db_path)

        self.telegram = self._build(lambda: TelegramController(db=self.db),
                                    "telegram")
        self.notifications = self._build(lambda: NotificationCenter(
            db=self.db, voice_handler=speak_handler, ui_handler=ui_handler,
            macos_handler=macos_handler, meeting_probe=meeting_probe,
            quiet_start=settings.QUIET_HOURS_START,
            quiet_end=settings.QUIET_HOURS_END,
        ), "notifications")
        # Telegram is the NotificationCenter's telegram channel.
        if self.notifications is not None and self.telegram is not None \
                and settings.TELEGRAM_NOTIFICATIONS:
            self.notifications.set_handlers(telegram=self.telegram.notify_sync)

        self.translator = self._build(
            lambda: CommunicationTranslator(client=client), "translator")
        self.messaging = self._build(
            lambda: MessagingManager(db=self.db, client=client,
                                     telegram=self.telegram), "messaging")
        self.calls = self._build(
            lambda: CallManager(db=self.db,
                                imessage=getattr(self.messaging, "imessage", None)),
            "calls")
        self.email = self._build(
            lambda: ExtendedEmailManager(db=self.db, client=client), "email")
        self.social = self._build(
            lambda: SocialManager(db=self.db, client=client,
                                  default_subreddits=settings.REDDIT_SUBREDDITS),
            "social")
        self.automation = self._build(
            lambda: CommunicationAutomation(db=self.db, messaging=self.messaging),
            "automation")

    @staticmethod
    def _build(factory: Callable[[], Any], name: str) -> Any:
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            print(f"[CommunicationManager] {name} init failed: {exc}")
            return None

    # ── lifecycle ──────────────────────────────────────────────────────── #

    def start(self) -> None:
        tg_state = "not configured"
        if self.telegram is not None and self.telegram.configured:
            try:
                # start() runs inside the already-running event loop, so we
                # MUST NOT asyncio.run() here — use the sync connect.
                info = self.telegram.connect_blocking()
                if info.get("connected"):
                    tg_state = f"connected (@{info['bot']})"
                    self.telegram.start_polling(self._on_telegram_message)
            except Exception as exc:  # noqa: BLE001
                print(f"[COMM] telegram connect failed: {exc}")
        print("[COMM] iMessage: ready (AppleScript)")
        print(f"[COMM] WhatsApp: {'enabled' if settings.WHATSAPP_ENABLED else 'disabled'}")
        print(f"[COMM] Telegram: {tg_state}")
        print("[COMM] Notification center: active")
        print("[COMM] All communication systems online")

    def stop(self) -> None:
        if self.telegram is not None:
            self.telegram.stop()
        self.db.close()

    # ── emergency fan-out ──────────────────────────────────────────────── #

    def notify_emergency_contacts(self, message: str,
                                  contacts: list[str]) -> dict[str, Any]:
        """Text every emergency contact via iMessage and push to the owner
        via Telegram. Wired into EmergencySystem's notify seam in main.py.
        Thin wrapper over :func:`deliver_emergency` so the transport choice
        lives in one testable place."""
        imsg = getattr(self.messaging, "imessage", None)
        report = deliver_emergency(message, contacts,
                                   imessage=imsg, telegram=self.telegram)
        print(f"[Emergency] fan-out: {report['imessage']}/{report['contacts']} "
              f"iMessage, telegram={report['telegram']}")
        return report

    def _on_telegram_message(self, msg: dict[str, Any]) -> None:
        """Inbound Telegram message: clear any open follow-up to that
        contact (best-effort)."""
        if self.automation is not None:
            import asyncio
            try:
                asyncio.run(self.automation.note_reply_received(
                    "telegram", msg.get("from", "")))
            except Exception:  # noqa: BLE001
                pass

    # ── notification convenience ───────────────────────────────────────── #

    def notify(self, title: str, body: str, priority: str = "medium",
               source: str = "jarvis") -> None:
        """Public entry the rest of JARVIS can route notifications through."""
        if self.notifications is not None:
            self.notifications.send(title, body, priority, source)

    # ── morning briefing ───────────────────────────────────────────────── #

    async def morning_comm_brief(self) -> str:
        parts: list[str] = []
        if self.messaging is not None:
            try:
                parts.append(await self.messaging.spoken_unread_summary())
            except Exception:  # noqa: BLE001
                pass
        if self.calls is not None:
            try:
                parts.append(await self.calls.get_missed_calls())
            except Exception:  # noqa: BLE001
                pass
        if self.email is not None:
            try:
                parts.append(await self.email.get_all_accounts_summary())
            except Exception:  # noqa: BLE001
                pass
        return " ".join(p for p in parts if p) or "Keine neuen Mitteilungen."

    # ── natural-language routing ───────────────────────────────────────── #

    async def process_command(self, command: str) -> str | None:
        """Route comm trigger phrases. Returns a spoken reply or None to
        fall through to Claude."""
        try:
            c = (command or "").lower().strip()

            # ── confirm-before-send flow (only when something is pending) ─ #
            pend_msg = self.messaging is not None and self.messaging.has_pending()
            pend_mail = self.email is not None and self.email.has_pending()
            if pend_msg or pend_mail:
                if any(c == w or c.startswith(w) for w in _CONFIRM_WORDS):
                    if pend_msg:
                        return await self.messaging.confirm_pending()
                    return await self.email.confirm_pending()
                if any(w in c for w in _CANCEL_WORDS):
                    if pend_msg:
                        return self.messaging.cancel_pending()
                    return self.email.cancel_pending()

            # ── translation ───────────────────────────────────────────── #
            if self.translator is not None:
                t = await self._route_translation(command, c)
                if t is not None:
                    return t

            # ── messaging ─────────────────────────────────────────────── #
            if self.messaging is not None:
                if "neue nachrichten" in c or "was habe ich verpasst" in c \
                        or "ungelesene nachrichten" in c:
                    return await self.messaging.spoken_unread_summary()
                if "whatsapp" in c and ("ungelesen" in c or "nachrichten" in c
                                        or "was habe" in c or "neu" in c):
                    wa = getattr(self.messaging, "whatsapp", None)
                    if wa:
                        return wa.spoken_unread_summary()
                if "lies whatsapp" in c or ("whatsapp" in c and "lies" in c):
                    name = ""
                    for kw in ("von ", "mit ", "chat "):
                        if kw in c:
                            name = c.split(kw, 1)[1].strip()
                            break
                    wa = getattr(self.messaging, "whatsapp", None)
                    if wa:
                        return (wa.get_conversation_summary(name)
                                if name else wa.spoken_unread_summary())
                if c.startswith("schreib ") or c.startswith("sende nachricht"):
                    return await self._route_send(command)
                if c.startswith("antworte ") or c.startswith("antwort an"):
                    return await self._route_reply(command)
                if c.startswith("lies nachrichten von ") or c.startswith("lies von "):
                    name = command.split("von", 1)[1].strip().rstrip(" vor").strip()
                    return await self.messaging.read_messages("imessage", name, 5)
                if c.startswith("sende allen") or c.startswith("broadcast"):
                    return ("Sag mir die Empfänger und die Nachricht für den "
                            "Broadcast.")

            # ── calls ─────────────────────────────────────────────────── #
            if self.calls is not None:
                if c.startswith("ruf ") and "an" in c or c.startswith("call "):
                    name = self._extract_call_target(command)
                    if name:
                        r = await self.calls.make_call(name)
                        return r["spoken"]
                if "wer hat angerufen" in c or "verpasste anrufe" in c:
                    return await self.calls.get_missed_calls()
                if "zurückzurufen" in c or "zurückrufen erinner" in c \
                        or ("erinnere" in c and "zurück" in c):
                    name = self._extract_callback_target(command)
                    return await self.calls.set_callback_reminder(name)

            # ── email ─────────────────────────────────────────────────── #
            if self.email is not None:
                if "mails zusammenfassen" in c or "alle mails" in c \
                        or "email zusammenfassen" in c:
                    return await self.email.get_important_summary()
                if "newsletter abbestellen" in c or "newsletter" in c:
                    nls = await self.email.find_newsletters()
                    if not nls:
                        return "Keine Newsletter gefunden."
                    return "Gefundene Newsletter: " + ", ".join(nls[:6]) + "."

            # ── notifications ─────────────────────────────────────────── #
            if self.notifications is not None:
                if "nicht stören" in c and ("aktiv" in c or "an" in c or "ein" in c):
                    self.notifications.set_dnd(True)
                    return "Nicht stören aktiviert."
                if "nicht stören" in c and ("deaktiv" in c or "aus" in c):
                    self.notifications.set_dnd(False)
                    return "Nicht stören deaktiviert."
                if "benachrichtigungen zusammenfassen" in c \
                        or "benachrichtigungen" in c and "zusammen" in c:
                    return self.notifications.batch_summary()
                if "stille stunden" in c:
                    return self._route_quiet_hours(command, c)

            # ── social ────────────────────────────────────────────────── #
            if self.social is not None:
                if "twitter mentions" in c or "twitter" in c and "mention" in c:
                    return await self.social.get_twitter_mentions()
                if "linkedin" in c:
                    return await self.social.get_linkedin_messages()
                if "geburtstage" in c:
                    return await self.social.get_birthday_reminders()
                if "reddit" in c:
                    return await self.social.get_reddit_feed()
                if ("entwurf" in c or "draft" in c) and \
                        ("twitter" in c or "linkedin" in c or "post" in c):
                    return await self._route_draft(command, c)

            # ── automation ────────────────────────────────────────────── #
            if self.automation is not None:
                if "auto-reply aktivieren" in c or "automatische antwort" in c:
                    await self.automation.enable_auto_reply(
                        message=settings.AUTO_REPLY_MESSAGE)
                    return "Automatische Antwort aktiviert."
                if "auto-reply deaktivieren" in c:
                    await self.automation.disable_auto_reply()
                    return "Automatische Antwort deaktiviert."
                if "ich bin beschäftigt" in c or "bin beschäftigt" in c:
                    await self.automation.set_communication_status(
                        "busy", settings.AUTO_REPLY_MESSAGE)
                    return "Status auf beschäftigt gesetzt."
                if "abwesenheitsnotiz" in c or "out of office" in c:
                    return ("Sag mir Start- und Enddatum für die "
                            "Abwesenheitsnotiz.")
                if c.startswith("hat ") and "geantwortet" in c:
                    return "Ich prüfe offene Follow-ups beim nächsten Durchlauf."

            return None
        except Exception as exc:  # noqa: BLE001
            print(f"[CommunicationManager] process_command failed: {exc}")
            return None

    # ── routing helpers ────────────────────────────────────────────────── #

    async def _route_translation(self, command: str, c: str) -> str | None:
        # "übersetze auf englisch: X" / "übersetze: X" / "translate: X"
        if not (c.startswith("übersetze") or c.startswith("translate")
                or "was bedeutet" in c):
            return None
        # target language
        lang_map = {"englisch": "en", "english": "en", "deutsch": "de",
                    "französisch": "fr", "spanisch": "es", "italienisch": "it"}
        target = self.translator._default_lang  # noqa: SLF001
        for word, code in lang_map.items():
            if word in c:
                target = code
                break
        # text after the colon
        if ":" in command:
            text = command.split(":", 1)[1].strip()
        elif "was bedeutet" in c:
            text = command.lower().split("was bedeutet", 1)[1]
            text = text.split("auf")[0].strip()
            target = "de"
        else:
            return "Was soll ich übersetzen?"
        if not text:
            return "Was soll ich übersetzen?"
        return await self.translator.translate(text, target)

    async def _route_send(self, command: str) -> str:
        # "schreib max: hallo wie gehts"
        body = command.split(" ", 1)[1] if " " in command else ""
        if ":" not in body:
            return "Wem soll ich was schreiben? Format: schreib Name: Nachricht."
        contact, message = body.split(":", 1)
        contact, message = contact.strip(), message.strip()
        cl = command.lower()
        platform = ("whatsapp" if "whatsapp" in cl
                    else "telegram" if "telegram" in cl
                    else "imessage")
        r = await self.messaging.send(platform, contact, message)
        return r["preview"]

    async def _route_reply(self, command: str) -> str:
        # "antworte max: sag dass ich später komme"
        body = command.split(" ", 1)[1] if " " in command else ""
        if ":" not in body:
            return "Wem soll ich was antworten?"
        contact, instructions = body.split(":", 1)
        r = await self.messaging.reply_to_last("imessage", instructions.strip())
        return r["preview"]

    async def _route_draft(self, command: str, c: str) -> str:
        platform = "linkedin" if "linkedin" in c else "twitter"
        topic = command.split("über", 1)[1].strip() if "über" in c \
            else command.split("about", 1)[1].strip() if "about" in c else ""
        if not topic:
            return "Worüber soll ich einen Entwurf schreiben?"
        d = await self.social.draft_post(platform, topic)
        if not d["ok"]:
            return d["note"]
        return f"Entwurf ({d['chars']} Zeichen): {d['draft']}"

    def _route_quiet_hours(self, command: str, c: str) -> str:
        # "stille stunden von 23 bis 7"
        import re
        nums = re.findall(r"\d{1,2}", c)
        if len(nums) >= 2:
            start = f"{int(nums[0]):02d}:00"
            end = f"{int(nums[1]):02d}:00"
            self.notifications.set_quiet_hours(start, end)
            return f"Stille Stunden von {start} bis {end} gesetzt."
        return "Sag mir Start- und Endzeit, z. B. von 23 bis 7."

    @staticmethod
    def _extract_call_target(command: str) -> str:
        c = command.lower()
        for kw in ("ruf ", "call "):
            if kw in c:
                rest = command[c.index(kw) + len(kw):]
                # strip a trailing "an"
                rest = rest.rsplit(" an", 1)[0] if rest.lower().endswith(" an") else rest
                return rest.strip()
        return ""

    @staticmethod
    def _extract_callback_target(command: str) -> str:
        c = command.lower()
        # "erinnere mich max zurückzurufen"
        if "mich" in c and "zurück" in c:
            seg = command[c.index("mich") + 4:]
            seg = seg.split("zurück")[0]
            return seg.strip() or "den Kontakt"
        return "den Kontakt"
