"""Communication automation — auto-replies, follow-ups, broadcast, status.

Auto-reply rules persist to ``data/auto_replies.json`` and mirror into the
``auto_replies`` table for the sent-count audit. A rule never fires for a
sender on its exceptions list (VIPs always get through). Follow-up
tracking and broadcast lean on the CommunicationDB and MessagingManager
respectively; broadcast still goes through the manager's
confirm-before-send gate. All best-effort.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class CommunicationAutomation:
    def __init__(self, db: Any = None, messaging: Any = None,
                 rules_path: Path | str = "data/auto_replies.json") -> None:
        self._db = db
        self._messaging = messaging
        self._rules_path = Path(rules_path)
        self._rules: list[dict[str, Any]] = []
        # Communication status (busy/away/dnd/available) + optional auto-revert.
        self._status: dict[str, Any] = {"status": "available", "message": None,
                                         "revert_at": None}
        self._load_rules()

    # ── rule persistence ───────────────────────────────────────────────── #

    def _load_rules(self) -> None:
        if self._rules_path.is_file():
            try:
                self._rules = json.loads(self._rules_path.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                print(f"[CommAutomation] rules load failed: {exc}")
                self._rules = []

    def _save_rules(self) -> None:
        try:
            self._rules_path.parent.mkdir(parents=True, exist_ok=True)
            self._rules_path.write_text(
                json.dumps(self._rules, ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[CommAutomation] rules save failed: {exc}")

    # ── auto-reply ─────────────────────────────────────────────────────── #

    async def check_auto_reply(self, platform: str, sender: str,
                               message: str) -> str | None:
        """Return the auto-reply text if an active rule applies to this
        incoming message, else None. VIPs (exceptions) never get an
        auto-reply."""
        for rule in self._rules:
            if not rule.get("active"):
                continue
            if platform not in rule.get("platforms", []):
                continue
            exceptions = [e.lower() for e in rule.get("exceptions", [])]
            if any(e in (sender or "").lower() for e in exceptions):
                return None  # VIP — let it through, no auto-reply
            return rule.get("message")
        return None

    async def enable_auto_reply(
        self, trigger: str = "focus_mode",
        message: str = "Bin gerade beschäftigt, melde mich später",
        platforms: list[str] | None = None,
        exceptions: list[str] | None = None,
    ) -> dict[str, Any]:
        platforms = platforms or ["imessage", "whatsapp"]
        # Deactivate any existing rule with the same trigger, then add.
        for r in self._rules:
            if r["trigger"] == trigger:
                r["active"] = False
        rule = {
            "name": f"auto_{trigger}", "trigger": trigger,
            "platforms": platforms, "message": message,
            "exceptions": exceptions or [], "active": True,
            "created_at": time.time(),
        }
        self._rules.append(rule)
        self._save_rules()
        if self._db is not None:
            self._db.add_auto_reply(trigger, ",".join(platforms), message)
        print("[COMM] Auto-reply activated")
        return {"ok": True, "trigger": trigger}

    async def disable_auto_reply(self, trigger: str | None = None) -> dict[str, Any]:
        for r in self._rules:
            if trigger is None or r["trigger"] == trigger:
                r["active"] = False
        self._save_rules()
        if self._db is not None:
            self._db.deactivate_auto_replies(trigger)
        return {"ok": True}

    def active_rules(self) -> list[dict[str, Any]]:
        return [r for r in self._rules if r.get("active")]

    # ── follow-up tracking ─────────────────────────────────────────────── #

    async def track_sent_message(self, platform: str, contact: str,
                                 message: str, followup_days: int = 3) -> None:
        if self._db is not None:
            self._db.track_followup(
                platform, contact, message[:120],
                followup_due=time.time() + followup_days * 86400)

    async def check_followups(self) -> list[dict[str, Any]]:
        """Due follow-ups (sent, no reply, past due). Called daily."""
        if self._db is None:
            return []
        due = self._db.due_followups(time.time())
        return [{
            "platform": r["platform"], "contact": r["contact"],
            "preview": r["message_preview"],
            "spoken": (f"Keine Antwort von {r['contact']} seit dem Senden. "
                       f"Nachfassen?"),
            "id": r["id"],
        } for r in due]

    async def note_reply_received(self, platform: str, contact: str) -> None:
        """Call when an inbound message arrives — clears any open follow-up."""
        if self._db is not None:
            self._db.mark_response_received(platform, contact)

    # ── broadcast ──────────────────────────────────────────────────────── #

    async def send_broadcast(self, message: str, contacts: list[str],
                             platform: str = "imessage") -> dict[str, Any]:
        if self._messaging is None:
            return {"needs_confirm": False,
                    "preview": "Messaging nicht verfügbar."}
        # Reuse the manager's staged broadcast (confirm-before-send + rate
        # limit live there).
        return await self._messaging.broadcast(message, contacts, [platform])

    # ── status / OOO ───────────────────────────────────────────────────── #

    async def set_communication_status(
        self, status: str, message: str | None = None,
        duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        valid = ("busy", "away", "do_not_disturb", "available")
        if status not in valid:
            status = "available"
        revert_at = (time.time() + duration_minutes * 60
                     if duration_minutes else None)
        self._status = {"status": status, "message": message,
                        "revert_at": revert_at}
        # Optional auto-reply while busy/dnd.
        if status in ("busy", "do_not_disturb") and message:
            await self.enable_auto_reply(
                trigger="status", message=message,
                platforms=["imessage", "whatsapp"])
        elif status == "available":
            await self.disable_auto_reply("status")
        spoken = (f"Status: {status}"
                  + (f" für {duration_minutes} Minuten" if duration_minutes else "")
                  + ".")
        return {"status": status, "revert_at": revert_at, "spoken": spoken}

    def current_status(self) -> str:
        # Lazily auto-revert an expired temporary status.
        revert = self._status.get("revert_at")
        if revert and time.time() >= revert:
            self._status = {"status": "available", "message": None,
                            "revert_at": None}
        return self._status["status"]

    async def enable_out_of_office(
        self, start_date: str, end_date: str, message: str | None = None,
        platforms: list[str] | None = None,
    ) -> dict[str, Any]:
        platforms = platforms or ["email", "imessage"]
        msg = message or (f"Ich bin von {start_date} bis {end_date} abwesend "
                          f"und antworte danach.")
        # Activate an auto-reply rule covering the messaging platforms.
        msg_platforms = [p for p in platforms if p != "email"]
        if msg_platforms:
            await self.enable_auto_reply(trigger="out_of_office", message=msg,
                                         platforms=msg_platforms)
        if self._db is not None:
            self._db.log_notification(
                "Abwesenheit aktiviert", msg, "info", "comm_automation")
        return {"ok": True, "start": start_date, "end": end_date,
                "platforms": platforms, "spoken": "Abwesenheitsnotiz aktiviert."}
