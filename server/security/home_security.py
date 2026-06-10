"""Home security: doors, windows, environmental sensors, arm/disarm.

Integrates with the smart-home layer for actuation (flash lights, run
scenes) and reads sensor state from it where adapters expose it. Most of
the project's adapters are still stubs, so this module also keeps an
in-memory manual sensor model and degrades gracefully — you can arm the
system and run the leaving checklist with zero real sensors.

Two hard rules from the spec are enforced here:
  * Smoke / water / CO2 handlers are SAFETY paths — they fire regardless
    of armed state, guest mode, or any auth filter, and they always log.
  * Intrusion response is immediate and loud; it delegates to the
    EmergencySystem when one is wired, else runs an inline fallback.
"""
from __future__ import annotations

import time
from typing import Any, Callable

SENSOR_TYPES: dict[str, list[str]] = {
    "door":        ["front_door", "back_door", "garage"],
    "window":      ["living_room_window", "bedroom_window"],
    "motion":      ["hallway", "garden", "garage"],
    "smoke":       ["kitchen", "living_room"],
    "water":       ["bathroom", "kitchen", "basement"],
    "co2":         ["living_room", "bedroom"],
    "temperature": ["living_room", "outdoor"],
}

_ARM_MODES = ("away", "night", "home", "vacation")

AlertHandler = Callable[[str, str], None]  # (spoken_message, severity)


class HomeSecuritySystem:
    """Sensor tracking + arming + safety-alert handling."""

    def __init__(
        self,
        db: Any = None,
        smarthome: Any = None,
        alert_handler: AlertHandler | None = None,
        emergency: Any = None,
        camera: Any = None,
    ) -> None:
        self._db = db
        self._smarthome = smarthome
        self._alert = alert_handler
        self._emergency = emergency
        self._camera = camera

        self._armed = False
        self._mode = "home"
        self._presence_sim = False

        # Manual sensor model (fallback when adapters don't report).
        self._doors_locked = {"front": True, "back": True}
        self._windows_open: list[str] = []
        self._lights_on: list[str] = []
        self._plugs_on: list[str] = []
        self._motion_detected: list[str] = []
        self._last_check = time.time()

    # ── status ─────────────────────────────────────────────────────────── #

    async def get_security_status(self) -> dict[str, Any]:
        self._last_check = time.time()
        active_alerts = self._db.query(
            "SELECT event_type, description FROM security_events "
            "WHERE resolved=0 AND severity IN ('HIGH','CRITICAL') "
            "ORDER BY timestamp DESC LIMIT 10",
        ) if self._db is not None else []
        return {
            "armed": self._armed,
            "mode": self._mode,
            "doors_locked": dict(self._doors_locked),
            "windows_open": list(self._windows_open),
            "motion_detected": list(self._motion_detected),
            "alerts_active": [a["event_type"] for a in active_alerts],
            "presence_simulation": self._presence_sim,
            "last_check": self._last_check,
        }

    async def spoken_status(self) -> str:
        s = await self.get_security_status()
        parts = ["Alarm aktiv" if s["armed"] else "Alarm deaktiviert"]
        if s["windows_open"]:
            parts.append(f"{len(s['windows_open'])} Fenster offen")
        unlocked = [k for k, v in s["doors_locked"].items() if not v]
        if unlocked:
            parts.append(f"Türen offen: {', '.join(unlocked)}")
        if s["alerts_active"]:
            parts.append(f"{len(s['alerts_active'])} aktive Alarme")
        if len(parts) == 1:
            parts.append("alles sicher")
        return ". ".join(parts) + "."

    # ── arming ─────────────────────────────────────────────────────────── #

    async def arm_system(self, mode: str = "away") -> str:
        if mode not in _ARM_MODES:
            mode = "away"
        self._armed = True
        self._mode = mode
        # Start camera monitoring if available.
        if self._camera is not None:
            try:
                await self._camera.start_monitoring()
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] camera start failed: {exc}")
        if mode == "vacation":
            await self.presence_simulation(True)
        if self._db is not None:
            self._db.log_event("armed", "INFO", "home_security",
                               f"Security armed: {mode}")
        msg = f"[JARVIS 🔒 Security armed: {mode}]"
        print(msg)
        return f"Sicherheitssystem aktiviert im Modus {mode}."

    async def disarm_system(self) -> str:
        self._armed = False
        self._mode = "home"
        await self.presence_simulation(False)
        if self._camera is not None and self._camera.is_running():
            try:
                self._camera.stop_monitoring()
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] camera stop failed: {exc}")
        if self._db is not None:
            self._db.log_event("disarmed", "INFO", "home_security",
                               "Security disarmed (smoke/water/CO2 stay active)")
        print("[JARVIS 🔓 Security disarmed]")
        return "Sicherheitssystem deaktiviert. Rauch-, Wasser- und CO2-Melder bleiben aktiv."

    @property
    def is_armed(self) -> bool:
        return self._armed

    # ── leaving checklist ──────────────────────────────────────────────── #

    async def leaving_checklist(self) -> str:
        issues: list[str] = []
        if self._lights_on:
            issues.append(f"Lichter noch an: {', '.join(self._lights_on)}")
        if self._plugs_on:
            issues.append(f"Steckdosen noch an: {', '.join(self._plugs_on)}")
        if self._windows_open:
            issues.append(f"Fenster noch offen: {', '.join(self._windows_open)}")
        unlocked = [k for k, v in self._doors_locked.items() if not v]
        if unlocked:
            issues.append(f"Türen nicht verriegelt: {', '.join(unlocked)}")
        if not self._armed:
            issues.append("Alarm noch nicht aktiviert")

        if not issues:
            return "Alles in Ordnung — gute Fahrt!"
        return "Achtung: " + "; ".join(issues) + "."

    # ── presence simulation ────────────────────────────────────────────── #

    async def presence_simulation(self, enabled: bool) -> dict[str, Any]:
        self._presence_sim = enabled
        if self._db is not None:
            self._db.log_event(
                "presence_sim", "INFO", "home_security",
                f"Presence simulation {'on' if enabled else 'off'}",
            )
        # Actuation is best-effort via the smart-home layer; the real
        # random-pattern scheduler would live in the intelligence loop.
        print(f"[HomeSecurity] presence simulation {'ON' if enabled else 'OFF'}")
        return {"presence_simulation": enabled}

    # ── safety sensor handlers (NEVER blocked) ─────────────────────────── #

    async def on_smoke_detected(self, sensor: str) -> str:
        msg = f"Rauchmelder ausgelöst in {sensor}!"
        self._safety_alert("smoke", "CRITICAL", sensor, msg)
        await self._flash_lights("rot")
        await self._unlock_all_locks()
        if self._emergency is not None:
            try:
                await self._emergency.trigger_fire_alarm()
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] emergency fire failed: {exc}")
        return msg

    async def on_water_detected(self, sensor: str) -> str:
        msg = f"Wasseralarm in {sensor}! Möglicher Wasserschaden!"
        self._safety_alert("water", "HIGH", sensor, msg)
        await self._smart_command("wasserventil schließen")
        return msg

    async def on_co2_alert(self, level: int, sensor: str) -> str:
        critical = level > 1500
        msg = f"CO2 Alarm! {level} ppm in {sensor}. Bitte lüften!"
        self._safety_alert("co2", "CRITICAL" if critical else "HIGH", sensor, msg)
        if critical:
            await self._smart_command("fenster öffnen")
        return msg

    async def on_intrusion_detected(self) -> str:
        msg = "Alarm! Unbefugter Zutritt erkannt!"
        self._safety_alert("intrusion", "CRITICAL", "perimeter", msg)
        await self._flash_lights("rot blinken")
        if self._camera is not None:
            try:
                await self._camera.start_monitoring()
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] intrusion camera failed: {exc}")
        if self._emergency is not None:
            try:
                await self._emergency.trigger_intrusion_alarm()
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] emergency intrusion failed: {exc}")
        return msg

    def _safety_alert(self, kind: str, severity: str, sensor: str, msg: str) -> None:
        """Common path for the always-on safety sensors. Speaks + logs
        unconditionally — no filter can suppress this."""
        print(f"[JARVIS 🚨 {severity}] {msg}")
        if self._db is not None:
            self._db.log_event(kind, severity, "home_security",
                               f"{msg} (sensor={sensor})")
        if self._alert is not None:
            try:
                self._alert(msg, severity)
            except Exception as exc:  # noqa: BLE001
                print(f"[HomeSecurity] alert failed: {exc}")

    # ── smart-home actuation helpers ───────────────────────────────────── #

    async def _smart_command(self, command: str) -> None:
        if self._smarthome is None:
            return
        try:
            await self._smarthome.process_command(command)
        except Exception as exc:  # noqa: BLE001
            print(f"[HomeSecurity] smart command '{command}' failed: {exc}")

    async def _flash_lights(self, effect: str) -> None:
        await self._smart_command(f"alle lichter {effect}")

    async def _unlock_all_locks(self) -> None:
        await self._smart_command("alle türschlösser entriegeln")

    # ── manual sensor model (for fallback / integration) ───────────────── #

    def set_door_lock(self, door: str, locked: bool) -> None:
        self._doors_locked[door] = locked

    def set_window(self, name: str, is_open: bool) -> None:
        if is_open and name not in self._windows_open:
            self._windows_open.append(name)
        elif not is_open and name in self._windows_open:
            self._windows_open.remove(name)

    def set_light(self, name: str, on: bool) -> None:
        if on and name not in self._lights_on:
            self._lights_on.append(name)
        elif not on and name in self._lights_on:
            self._lights_on.remove(name)

    def set_plug(self, name: str, on: bool) -> None:
        if on and name not in self._plugs_on:
            self._plugs_on.append(name)
        elif not on and name in self._plugs_on:
            self._plugs_on.remove(name)

    def report_motion(self, zone: str) -> None:
        """A motion sensor fired. If armed in a perimeter-active mode,
        this is an intrusion."""
        if zone not in self._motion_detected:
            self._motion_detected.append(zone)
        # away/vacation: any motion is intrusion. night: only perimeter.
        if self._armed and self._mode in ("away", "vacation"):
            import asyncio
            try:
                asyncio.get_running_loop().create_task(self.on_intrusion_detected())
            except RuntimeError:
                # No running loop (sync context) — run it inline.
                asyncio.run(self.on_intrusion_detected())
