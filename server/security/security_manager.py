"""Central coordinator for the JARVIS security & monitoring layer.

Owns the single :class:`SecurityDB` and wires every sub-component to it,
to each other (camera↔home↔emergency), and to the host's speak / notify
handlers. Exposes three surfaces:

  * ``start()`` — boot the layer: system-monitor loop, baseline learning
    hook, voice-profile check (prompts enrollment on first run).
  * ``process_request()`` — the auth pipeline a request passes through:
    rate-limit → voice verify → anomaly → permission → allow/deny, all
    logged.
  * ``process_command()`` — natural-language routing for the security
    trigger phrases (spec §14), called by the brain, returns a spoken
    German string or ``None`` to fall through to Claude.

Construction never raises: a failed component is logged and left ``None``
so the rest of the layer — and JARVIS — keeps working.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .db import SecurityDB
from .system_monitor import SystemMonitor
from .access_control import AccessController
from .voice_auth import VoiceAuthenticator
from .anomaly_detector import AnomalyDetector
from .camera_monitor import CameraMonitor
from .home_security import HomeSecuritySystem
from .digital_security import DigitalSecurityMonitor
from .emergency import EmergencySystem

SpeakHandler = Callable[[str, str], None]
NotifyHandler = Callable[[str, list[str]], None]


class SecurityManager:
    """Coordinator that owns and wires the whole security layer."""

    def __init__(
        self,
        db_path: Path | str = "data/security.db",
        vision_manager: Any = None,
        smarthome: Any = None,
        speak_handler: SpeakHandler | None = None,
        notify_handler: NotifyHandler | None = None,
    ) -> None:
        self._speak = speak_handler
        self.db = SecurityDB(db_path)

        # A single alert callback every component shares → routes to TTS.
        def _alert(message: str, severity: str) -> None:
            if self._speak is not None:
                try:
                    self._speak(message, severity)
                except Exception as exc:  # noqa: BLE001
                    print(f"[SecurityManager] speak failed: {exc}")

        self._alert = _alert

        # Build components, each guarded so one failure can't sink the rest.
        self.system = self._build(lambda: SystemMonitor(
            db=self.db, alert_handler=_alert,
            interval_s=settings.SYSTEM_MONITOR_INTERVAL,
            thresholds={
                "cpu_critical": settings.CPU_ALERT_THRESHOLD,
                "ram_critical": settings.RAM_ALERT_THRESHOLD,
                "disk_critical": settings.DISK_ALERT_THRESHOLD,
                "temp_critical": settings.TEMP_ALERT_THRESHOLD,
            },
        ), "system_monitor")

        self.access = self._build(
            lambda: AccessController(db=self.db), "access_control")
        self.voice_auth = self._build(
            lambda: VoiceAuthenticator(db=self.db), "voice_auth")
        self.anomaly = self._build(
            lambda: AnomalyDetector(db=self.db), "anomaly_detector")

        self.camera = self._build(lambda: CameraMonitor(
            db=self.db, vision_manager=vision_manager, alert_handler=_alert,
            retention_days=settings.CAMERA_SNAPSHOT_RETENTION_DAYS,
        ), "camera_monitor")

        self.digital = self._build(lambda: DigitalSecurityMonitor(
            db=self.db, system_monitor=self.system, alert_handler=_alert,
        ), "digital_security")

        self.emergency = self._build(lambda: EmergencySystem(
            db=self.db, speak_handler=_alert, notify_handler=notify_handler,
            smarthome=smarthome, camera=self.camera,
            contacts=settings.EMERGENCY_CONTACTS,
            home_address=settings.HOME_ADDRESS,
        ), "emergency")

        # Home security depends on camera + emergency, so build it last.
        self.home = self._build(lambda: HomeSecuritySystem(
            db=self.db, smarthome=smarthome, alert_handler=_alert,
            emergency=self.emergency, camera=self.camera,
        ), "home_security")

    @staticmethod
    def _build(factory: Callable[[], Any], name: str) -> Any:
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityManager] {name} init failed: {exc}")
            return None

    # ── lifecycle ──────────────────────────────────────────────────────── #

    def start(self) -> None:
        if self.system is not None:
            self.system.start()
        # Learn the behavioural baseline from any existing history.
        if self.anomaly is not None:
            self.anomaly.learn_normal_patterns()

        enrolled = self.voice_auth is not None and self.voice_auth.has_profile
        armed = self.home is not None and self.home.is_armed
        print(f"[SECURITY] Voice auth: {'enrolled' if enrolled else 'not enrolled'}")
        print(f"[SECURITY] System monitor: "
              f"{'active' if (self.system and self.system.available) else 'unavailable'}")
        print(f"[SECURITY] Home security: {'armed' if armed else 'disarmed'}")
        if self.voice_auth is not None and self.voice_auth.enabled and not enrolled:
            print("[SECURITY] ⚠ Voice auth enabled but no profile — run "
                  "`python -m server.security.voice_auth --enroll`")
        print("[SECURITY] All security systems online")

    def stop(self) -> None:
        if self.system is not None:
            self.system.stop()
        if self.camera is not None and self.camera.is_running():
            self.camera.stop_monitoring()
        self.db.close()

    # ── request pipeline ───────────────────────────────────────────────── #

    async def process_request(
        self, command: str, audio: bytes | None = None, ip: str = "local",
    ) -> dict[str, Any]:
        """Run a request through rate-limit → voice → anomaly → permission.
        Returns {allowed, reason, confidence, level}. NEVER raises."""
        try:
            # 1. Rate limit (per-IP) + digital block list.
            if self.anomaly is not None and not self.anomaly.rate_limit_check(ip):
                return self._deny(command, ip, "rate limit exceeded")
            if self.digital is not None and self.digital.is_blocked(ip):
                return self._deny(command, ip, "ip blocked")

            # 2. Voice verification.
            confidence = 1.0
            if audio and self.voice_auth is not None:
                verdict = await self.voice_auth.verify_speaker(audio)
                confidence = verdict["confidence"]
                if verdict["action"] == "deny":
                    if self.anomaly is not None:
                        self.anomaly.record_auth_failure()
                    return self._deny(command, ip, "voice not recognised",
                                      confidence)

            # 3. Anomaly check (flag only, don't block unless severe).
            if self.anomaly is not None:
                if self.anomaly.analyze_command(command, confidence):
                    print(f"[SECURITY] anomaly flagged: {self.anomaly.last_reasons}")

            # 4. Permission check.
            level = (self.voice_auth.command_security_level(command)
                     if self.voice_auth is not None else "low")
            allowed = True
            if self.voice_auth is not None:
                allowed = await self.voice_auth.check_command_permission(
                    command, confidence, level)

            reason = "ok" if allowed else f"insufficient confidence for {level}"
            self.db.log_access("owner", command, ip, round(confidence, 3),
                               level, allowed, reason)
            return {"allowed": allowed, "reason": reason,
                    "confidence": confidence, "level": level}
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityManager] process_request failed: {exc}")
            # Fail OPEN for the single owner — never lock JARVIS up on a bug.
            return {"allowed": True, "reason": f"pipeline error: {exc}",
                    "confidence": 0.0, "level": "low"}

    def _deny(self, command: str, ip: str, reason: str,
              confidence: float = 0.0) -> dict[str, Any]:
        self.db.log_access("unknown", command, ip, confidence, None, False, reason)
        return {"allowed": False, "reason": reason,
                "confidence": confidence, "level": None}

    # ── briefing ───────────────────────────────────────────────────────── #

    async def daily_security_briefing(self) -> str:
        parts: list[str] = []
        if self.digital is not None:
            try:
                parts.append(await self.digital.daily_security_report())
            except Exception as exc:  # noqa: BLE001
                print(f"[SecurityManager] digital report failed: {exc}")
        if self.system is not None:
            parts.append(self.system.spoken_status())
        if self.camera is not None:
            try:
                parts.append(await self.camera.get_daily_summary())
            except Exception:  # noqa: BLE001
                pass
        # Overnight HIGH/CRITICAL events.
        import time
        overnight = self.db.query(
            "SELECT COUNT(*) AS n FROM security_events "
            "WHERE timestamp >= ? AND severity IN ('HIGH','CRITICAL')",
            (time.time() - 43200,),
        )
        n = overnight[0]["n"] if overnight else 0
        if n:
            parts.append(f"Achtung: {n} sicherheitsrelevante Ereignisse in den "
                         f"letzten 12 Stunden.")
        if not parts:
            return "Sicherheitsbericht: Alles in Ordnung."
        return "Sicherheitsbericht. " + " ".join(parts)

    # ── natural-language command routing ───────────────────────────────── #

    async def process_command(self, command: str) -> str | None:
        """Route security trigger phrases (spec §14). Returns a spoken
        German reply, or None so the caller falls through to Claude.

        Emergency triggers are checked FIRST and are always honoured —
        no auth, no guest-mode gate (spec §IMPORTANT)."""
        try:
            c = (command or "").lower().strip()

            # ── EMERGENCY (always first, always allowed) ──────────────── #
            if self.emergency is not None:
                if (settings.SOS_KEYWORD in c or c == "sos" or "notruf" in c):
                    r = await self.emergency.trigger_sos()
                    return r["spoken"]
                if "feueralarm" in c or "feuer" in c:
                    r = await self.emergency.trigger_fire_alarm()
                    return r["spoken"]
                if "einbrecher" in c or "einbruch" in c:
                    r = await self.emergency.trigger_intrusion_alarm()
                    return "Einbruchsalarm ausgelöst. Kontakte benachrichtigt."
                if "arzt" in c or "krankenwagen" in c or "sanitäter" in c:
                    r = await self.emergency.medical_emergency()
                    return r["spoken"]
                if "alarm abbrechen" in c or "fehlalarm" in c:
                    await self.emergency.cancel_alarm()
                    return "Alarm abgebrochen."
                if "notfallkontakte" in c:
                    contacts = self.emergency.get_contacts()
                    if not contacts:
                        return "Keine Notfallkontakte hinterlegt."
                    return f"{len(contacts)} Notfallkontakte hinterlegt."

            # ── SYSTEM ────────────────────────────────────────────────── #
            if self.system is not None:
                if "system status" in c or "wie geht es dem mac" in c \
                        or "systemstatus" in c:
                    return self.system.spoken_status()
                if "verbraucht ressourcen" in c or "ressourcenfresser" in c \
                        or "was verbraucht" in c:
                    return self.system.spoken_top_processes()
                if "speicherplatz" in c or "festplatte" in c:
                    h = self.system.get_system_health()
                    return (f"Festplatte zu {h.disk_percent:.0f} Prozent belegt, "
                            f"noch {h.disk_free_gb:.0f} Gigabyte frei.")
                if "ist jarvis gesund" in c or "jarvis gesund" in c:
                    hj = self.system.check_jarvis_health()
                    return ("JARVIS ist gesund, alle Komponenten laufen."
                            if hj["healthy"] else
                            "Achtung, eine JARVIS-Komponente meldet ein Problem.")

            # ── HOME SECURITY ─────────────────────────────────────────── #
            if self.home is not None:
                if "alarm aktivieren" in c or "arm security" in c \
                        or "sicherheit aktivieren" in c:
                    mode = ("night" if "nacht" in c else
                            "vacation" if "urlaub" in c else
                            "home" if "zuhause" in c else "away")
                    return await self.home.arm_system(mode)
                if "alarm deaktivieren" in c or "disarm" in c \
                        or "sicherheit deaktivieren" in c:
                    return await self.home.disarm_system()
                if "alles in ordnung" in c or "security status" in c \
                        or "sicherheitsstatus" in c:
                    return await self.home.spoken_status()
                if "verlasse" in c and "haus" in c or "haus verlassen" in c:
                    return await self.home.leaving_checklist()

            # ── CAMERA ────────────────────────────────────────────────── #
            if self.camera is not None:
                if "wer ist an der tür" in c or "who's at the door" in c \
                        or "wer ist an der tuer" in c:
                    return await self.camera.whos_at_door()
                if "nachtmodus" in c:
                    self.camera.enable_night_mode()
                    return "Nachtmodus aktiviert."
                if ("kamera" in c and ("an" in c or "starten" in c
                                       or "ein" in c)):
                    res = await self.camera.start_monitoring(
                        camera_index=settings.CAMERA_INDEX,
                        sensitivity=settings.CAMERA_SENSITIVITY, force=True)
                    return ("Kameraüberwachung aktiv." if res.get("ok")
                            else f"Kamera nicht verfügbar: {res.get('error')}")
                if "kamera" in c and ("aus" in c or "stopp" in c
                                      or "stop" in c):
                    self.camera.stop_monitoring()
                    return "Kameraüberwachung deaktiviert."
                if "was ist heute passiert" in c or "kamera" in c and "heute" in c:
                    return await self.camera.get_daily_summary()

            # ── DIGITAL SECURITY ──────────────────────────────────────── #
            if self.digital is not None:
                if "netzwerk scannen" in c or "scan network" in c \
                        or "unbekannte geräte" in c:
                    r = await self.digital.check_network()
                    if r["unknown_count"]:
                        return (f"{r['total']} Geräte im Netzwerk, davon "
                                f"{r['unknown_count']} unbekannt.")
                    return f"{r['total']} Geräte im Netzwerk, alle bekannt."
                if "sicherheitsbericht" in c or "security report" in c:
                    return await self.daily_security_briefing()
                if "email gehackt" in c or "e-mail gehackt" in c \
                        or "datenleck" in c:
                    return ("Sag mir deine E-Mail-Adresse, dann prüfe ich sie "
                            "gegen bekannte Datenlecks.")

            # ── ACCESS / GUEST ────────────────────────────────────────── #
            if self.voice_auth is not None:
                if "gast modus aktivieren" in c or "gastmodus aktivieren" in c:
                    await self.voice_auth.enable_guest_mode()
                    return "Gast-Modus aktiviert für zwei Stunden."
                if "gast modus deaktivieren" in c or "gastmodus deaktivieren" in c:
                    await self.voice_auth.disable_guest_mode()
                    return "Gast-Modus deaktiviert."
            if self.access is not None:
                if "wer ist verbunden" in c:
                    sessions = await self.access.get_active_sessions()
                    if not sessions:
                        return "Aktuell sind keine Gäste verbunden."
                    names = ", ".join(s["name"] for s in sessions)
                    return f"Verbundene Gäste: {names}."

            return None
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityManager] process_command failed: {exc}")
            return None
