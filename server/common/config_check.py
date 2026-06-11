"""Startup configuration validation + a one-line enabled-features summary.

The required secrets are already enforced at import (config._required). This
adds *soft* checks: configurations that are enabled but incomplete (so they
silently won't work) get a clear warning at boot, and a summary line shows
what's actually on. Catches the common "I set X=true but forgot the token"
footguns without blocking startup.
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings


def validate_config() -> list[str]:
    """Return a list of human-readable warnings (also printed). Never raises."""
    warnings: list[str] = []

    # Voice auth on but no enrolled profile → it can't verify anyone.
    if settings.VOICE_AUTH_ENABLED:
        if not Path("data/voice_profiles/owner.npy").is_file():
            warnings.append(
                "VOICE_AUTH_ENABLED=true aber kein Stimmprofil — verifiziert "
                "niemanden. Enroll mit `python -m server.security.voice_auth "
                "--enroll`.")

    # Telegram notifications on but not configured → pushes go nowhere.
    if settings.TELEGRAM_NOTIFICATIONS and not settings.TELEGRAM_BOT_TOKEN:
        warnings.append(
            "TELEGRAM_NOTIFICATIONS=true aber kein TELEGRAM_BOT_TOKEN — "
            "Push-Nachrichten gehen nirgendwohin. Setup: "
            "`python -m server.communication.messaging.setup_telegram`.")
    if settings.TELEGRAM_BOT_TOKEN and not settings.TELEGRAM_CHAT_ID:
        warnings.append(
            "TELEGRAM_BOT_TOKEN gesetzt aber kein TELEGRAM_CHAT_ID — Owner-Push "
            "(SOS!) funktioniert nicht, bis die Chat-ID konfiguriert ist.")

    # Emergency contacts empty → emergency notifications can't reach anyone
    # (the owner Telegram push still works).
    if not settings.EMERGENCY_CONTACTS:
        warnings.append(
            "EMERGENCY_CONTACTS leer — Notfälle benachrichtigen keine Kontakte "
            "(Owner-Telegram-Push funktioniert weiterhin).")

    # DB encryption off → sensitive content is plaintext at rest.
    if not settings.JARVIS_DB_KEY:
        warnings.append(
            "JARVIS_DB_KEY nicht gesetzt — Nachrichten/Finanzen liegen als "
            "Klartext in der DB. Schlüssel: `python -m server.common.crypto`.")

    for w in warnings:
        print(f"[CONFIG] ⚠ {w}")
    return warnings


def feature_summary() -> str:
    """A compact 'what's enabled' line for the boot log."""
    on = []
    if settings.MAC_CONTROL_ENABLED: on.append("mac-control")
    if settings.VOICE_AUTH_ENABLED: on.append("voice-auth")
    if settings.CAMERA_ENABLED: on.append("camera")
    if settings.HOME_SECURITY_ENABLED: on.append("home-security")
    if settings.TELEGRAM_BOT_TOKEN: on.append("telegram")
    if settings.JARVIS_DB_KEY: on.append("db-encryption")
    if settings.WHATSAPP_ENABLED: on.append("whatsapp")
    summary = ", ".join(on) if on else "core only"
    return f"[CONFIG] enabled: {summary} · model={settings.MODEL}"
