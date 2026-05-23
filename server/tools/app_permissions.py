"""App permission registry — controls which non-Apple apps JARVIS may open.

Apple first-party apps are pre-approved. Third-party apps require the user
to confirm with JARVIS_APP_PASSWORD before JARVIS gets access; the approval
is then persisted so it survives restarts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_STORE = Path.home() / ".jarvis" / "approved_apps.json"

# All Apple first-party apps are always allowed — no password needed.
APPLE_FIRST_PARTY: frozenset[str] = frozenset({
    "Calendar", "Reminders", "Mail", "Music", "Notes", "Safari", "Messages",
    "Contacts", "Maps", "Photos", "FaceTime", "Finder", "App Store",
    "Podcasts", "News", "Stocks", "Weather", "Books", "Keynote", "Pages",
    "Numbers", "Preview", "TextEdit", "Calculator", "Voice Memos", "Clock",
    "System Settings", "System Preferences", "Terminal", "Activity Monitor",
    "Automator", "Script Editor", "Shortcuts", "Home", "TV",
    "Photo Booth", "QuickTime Player", "Dictionary", "Font Book",
    "Image Capture", "Migration Assistant", "Disk Utility",
})

# German ↔ macOS app name aliases (user says → actual app name)
APP_ALIASES: dict[str, str] = {
    "kamera": "Photo Booth",
    "camera": "Photo Booth",
    "foto": "Photos",
    "fotos": "Photos",
    "musik": "Music",
    "nachrichten": "Messages",
    "kalender": "Calendar",
    "erinnerungen": "Reminders",
    "notizen": "Notes",
    "einstellungen": "System Settings",
    "systemeinstellungen": "System Settings",
    "rechner": "Calculator",
    "taschenrechner": "Calculator",
    "vorschau": "Preview",
    "finder": "Finder",
    "terminal": "Terminal",
    "wetter": "Weather",
    "karten": "Maps",
    "bücher": "Books",
    "buecher": "Books",
    "sprachmemos": "Voice Memos",
    "uhr": "Clock",
    "tv": "TV",
    "news": "News",
}


def _load() -> dict[str, bool]:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save(data: dict[str, bool]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def is_approved(app_name: str) -> bool:
    if app_name in APPLE_FIRST_PARTY:
        return True
    return _load().get(app_name, False)


def approve_app(app_name: str, password: str) -> tuple[bool, str]:
    """Returns (success, message)."""
    admin_pw = os.getenv("JARVIS_APP_PASSWORD", "")
    if not admin_pw:
        return False, "JARVIS_APP_PASSWORD ist nicht gesetzt — bitte in .env eintragen."
    if password != admin_pw:
        return False, "Falsches Passwort."
    data = _load()
    data[app_name] = True
    _save(data)
    return True, f"'{app_name}' wurde freigegeben und ist ab sofort verfügbar."


def revoke_app(app_name: str) -> str:
    if app_name in APPLE_FIRST_PARTY:
        return f"'{app_name}' ist eine Apple-App und kann nicht gesperrt werden."
    data = _load()
    data.pop(app_name, None)
    _save(data)
    return f"'{app_name}' wurde gesperrt."


def list_approved() -> list[str]:
    custom = [k for k, v in _load().items() if v]
    return sorted(list(APPLE_FIRST_PARTY) + custom)
