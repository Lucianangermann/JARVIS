"""Emergency response: SOS, fire, intrusion, medical.

These paths are the highest-priority surface in JARVIS and are
**always available, never gated by auth, guest mode, or any filter**
(spec §IMPORTANT). Every trigger: speaks immediately, drives the smart
home (flash lights, unlock locks), notifies emergency contacts, records
camera footage where possible, and logs with a full timestamp.

Actuation is best-effort and decoupled: lights/locks go through the
smart-home layer, the spoken line through the alert handler, and contact
notifications through an injected notify handler (so the transport —
iMessage / WhatsApp / email — is the integrator's choice and this module
stays testable). A missing integration degrades to a logged no-op, never
an exception, because an emergency path must not be the thing that
crashes.
"""
from __future__ import annotations

import time
from typing import Any, Callable

# severity → what we do (documentation; behaviour is per-trigger below).
EMERGENCY_LEVELS: dict[str, str] = {
    "INFO":     "log only",
    "LOW":      "notify user",
    "MEDIUM":   "notify + smart home action",
    "HIGH":     "full alert + contacts",
    "CRITICAL": "everything + 911 info",
}

SpeakHandler = Callable[[str, str], None]          # (message, severity)
NotifyHandler = Callable[[str, list[str]], None]   # (message, contacts)


class EmergencySystem:
    """Always-available emergency triggers + contact notification."""

    def __init__(
        self,
        db: Any = None,
        speak_handler: SpeakHandler | None = None,
        notify_handler: NotifyHandler | None = None,
        smarthome: Any = None,
        camera: Any = None,
        contacts: list[str] | None = None,
        home_address: str = "",
    ) -> None:
        self._db = db
        self._speak = speak_handler
        self._notify = notify_handler
        self._smarthome = smarthome
        self._camera = camera
        self._contacts = contacts or []
        self._home_address = home_address
        self._active_alarm: str | None = None

    @property
    def active_alarm(self) -> str | None:
        return self._active_alarm

    # ── triggers ───────────────────────────────────────────────────────── #

    async def trigger_sos(self) -> dict[str, Any]:
        self._active_alarm = "sos"
        self._log("sos", "CRITICAL", "SOS activated by user")
        self._say("SOS aktiviert! Sende Alarm an Notfallkontakte!", "CRITICAL")
        await self._flash("rot blinken")
        await self._record_footage()
        msg = (f"NOTFALL von JARVIS: SOS aktiviert. "
               f"Standort: {self._home_address or 'unbekannt'}. "
               f"Zeit: {self._now()}.")
        await self.send_emergency_notification(msg, self._contacts)
        return {
            "alarm": "sos",
            "emergency_numbers": {"Notruf": "112", "Polizei": "110"},
            "spoken": "SOS aktiviert. Notruf 112, Polizei 110.",
        }

    async def trigger_fire_alarm(self) -> dict[str, Any]:
        self._active_alarm = "fire"
        self._log("fire", "CRITICAL", "Fire alarm triggered")
        self._say("FEUERALARM! Sofort das Gebäude verlassen!", "CRITICAL")
        await self._flash("orange blinken")
        await self._smart("alle türschlösser entriegeln")   # escape routes
        await self._smart("alle steckdosen aus")            # cut non-critical power
        msg = (f"FEUERALARM von JARVIS. Standort: "
               f"{self._home_address or 'unbekannt'}. Zeit: {self._now()}.")
        await self.send_emergency_notification(msg, self._contacts)
        return {
            "alarm": "fire",
            "emergency_numbers": {"Feuerwehr": "112"},
            "spoken": "Feueralarm. Feuerwehr 112.",
        }

    async def trigger_intrusion_alarm(self) -> dict[str, Any]:
        self._active_alarm = "intrusion"
        self._log("intrusion", "CRITICAL", "Intrusion alarm triggered")
        self._say("EINBRUCHSALARM AUSGELÖST!", "CRITICAL")
        await self._flash("rot blinken")
        self._system_beep()
        snapshot = await self._record_footage()
        msg = (f"EINBRUCHSALARM von JARVIS. Standort: "
               f"{self._home_address or 'unbekannt'}. Zeit: {self._now()}.")
        await self.send_emergency_notification(
            msg, self._contacts, snapshot=snapshot
        )
        return {"alarm": "intrusion", "snapshot": snapshot}

    async def medical_emergency(self) -> dict[str, Any]:
        self._active_alarm = "medical"
        self._log("medical", "CRITICAL", "Medical emergency")
        self._say("Soll ich jetzt den Notruf 112 anrufen?", "CRITICAL")
        await self._smart("haustür entriegeln")  # let paramedics in
        msg = (f"MEDIZINISCHER NOTFALL von JARVIS. Standort: "
               f"{self._home_address or 'unbekannt'}. Zeit: {self._now()}.")
        await self.send_emergency_notification(msg, self._contacts)
        return {
            "alarm": "medical",
            "emergency_numbers": {"Notruf": "112"},
            "address": self._home_address,
            "spoken": "Notruf 112. Halte deine Adresse bereit.",
        }

    # ── notifications ──────────────────────────────────────────────────── #

    async def send_emergency_notification(
        self,
        message: str,
        contacts: list[str] | None = None,
        include_location: bool = True,
        snapshot: str | None = None,
    ) -> dict[str, Any]:
        contacts = contacts or self._contacts
        if include_location and self._home_address \
                and self._home_address not in message:
            message = f"{message} Adresse: {self._home_address}."
        delivered = False
        if contacts and self._notify is not None:
            try:
                self._notify(message, contacts)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                print(f"[Emergency] notify handler failed: {exc}")
        self._log(
            "emergency_notification",
            "HIGH",
            f"Notification to {len(contacts)} contacts "
            f"({'sent' if delivered else 'no transport'})"
            + (f" [snapshot {snapshot}]" if snapshot else ""),
        )
        if not contacts:
            print("[Emergency] WARN: no emergency contacts configured")
        return {"delivered": delivered, "contacts": len(contacts),
                "message": message}

    # ── cancel ─────────────────────────────────────────────────────────── #

    async def cancel_alarm(self, reason: str = "manual") -> dict[str, Any]:
        was = self._active_alarm
        self._active_alarm = None
        await self._smart("alle lichter normal")
        self._log("alarm_cancelled", "INFO", f"Alarm '{was}' cancelled ({reason})")
        if was in ("sos", "intrusion") and self._contacts:
            await self.send_emergency_notification(
                "Fehlalarm — alles in Ordnung.", self._contacts,
                include_location=False,
            )
        self._say("Alarm abgebrochen. Alles in Ordnung.", "INFO")
        return {"cancelled": was}

    def get_contacts(self) -> list[str]:
        return list(self._contacts)

    # ── helpers ────────────────────────────────────────────────────────── #

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _say(self, message: str, severity: str) -> None:
        print(f"[JARVIS 🚨 {severity}] {message}")
        if self._speak is not None:
            try:
                self._speak(message, severity)
            except Exception as exc:  # noqa: BLE001
                print(f"[Emergency] speak failed: {exc}")

    def _log(self, event_type: str, severity: str, description: str) -> None:
        if self._db is not None:
            self._db.log_event(event_type, severity, "emergency", description)

    async def _smart(self, command: str) -> None:
        if self._smarthome is None:
            return
        try:
            await self._smarthome.process_command(command)
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency] smart command '{command}' failed: {exc}")

    async def _flash(self, effect: str) -> None:
        await self._smart(f"alle lichter {effect}")

    async def _record_footage(self) -> str | None:
        if self._camera is None:
            return None
        try:
            if not self._camera.is_running():
                await self._camera.start_monitoring()
            # The monitor loop snapshots on detection; return a marker.
            return "camera_recording_started"
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency] footage failed: {exc}")
            return None

    @staticmethod
    def _system_beep() -> None:
        """Loud audible alarm via the macOS system bell (best-effort)."""
        try:
            import subprocess
            subprocess.run(
                ["osascript", "-e", "beep 5"],
                timeout=4, check=False,
                capture_output=True,
            )
        except Exception:  # noqa: BLE001
            print("\a", end="", flush=True)  # terminal bell fallback
