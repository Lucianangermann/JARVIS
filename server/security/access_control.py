"""Guest / family / temporary access control.

Owner is authenticated elsewhere (voice_auth). This module governs
*delegated* access: a time-boxed token you can hand to a guest or family
member that unlocks a restricted command set and auto-expires. Every
access decision is written to ``access_log`` so there's a full audit
trail of who ran what and whether it was allowed.

Tokens live in memory only — they're ephemeral by design (max a few
hours) and we don't want a stolen ``security.db`` to leak live
credentials. A server restart invalidates all outstanding temp grants,
which is the safe default.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

# Command categories each access level may invoke. "all" is the owner
# wildcard. Mirrors the spec's ACCESS_LEVELS.
ACCESS_LEVELS: dict[str, list[str]] = {
    "owner":  ["all"],
    "guest":  ["lights", "music", "weather", "time"],
    "family": ["lights", "music", "weather", "time",
               "calendar_read", "smart_home_basic"],
    "none":   [],
}

# Keyword → category resolver. We classify a free-text command by the
# first category whose keywords appear. Anything unmatched is treated as
# "system" (the most restricted) so unknown commands fail closed.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "lights":            ("licht", "lights", "lamp", "lampe", "dimm",
                          "helligkeit", "brightness"),
    "music":             ("musik", "music", "spiel", "play", "song", "lied",
                          "spotify", "lautstärke", "volume", "pause"),
    "weather":           ("wetter", "weather", "regen", "temperatur",
                          "forecast"),
    "time":              ("uhrzeit", "zeit", "time", "datum", "date",
                          "wie spät"),
    "calendar_read":     ("kalender", "calendar", "termin", "appointment"),
    "smart_home_basic":  ("steckdose", "plug", "szene", "scene",
                          "thermostat", "heizung"),
    "smart_home":        ("smart home", "smarthome", "gerät", "device"),
    "email":             ("email", "e-mail", "mail", "schreib", "send",
                          "nachricht", "message"),
    "files":             ("datei", "file", "ordner", "folder", "dokument",
                          "speicher"),
    "system":            ("system", "terminal", "befehl", "command", "neustart",
                          "restart", "sudo", "shutdown"),
}


def classify_command(command: str) -> str:
    """Map a free-text command to an access category. Fails closed to
    'system' (most restricted) when nothing matches."""
    c = (command or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in c for k in keywords):
            return category
    return "system"


class AccessController:
    """Token-based temporary access with a full audit trail."""

    def __init__(self, db: Any = None) -> None:
        self._db = db
        # token -> grant dict; and name -> token for revoke-by-name.
        self._grants: dict[str, dict[str, Any]] = {}
        self._name_to_token: dict[str, str] = {}
        # token -> last_activity epoch (active-session tracking).
        self._sessions: dict[str, float] = {}

    # ── temp access lifecycle ──────────────────────────────────────────── #

    async def create_temp_access(
        self,
        name: str,
        level: str = "guest",
        duration_hours: int = 2,
        allowed_commands: list[str] | None = None,
    ) -> dict[str, Any]:
        if level not in ACCESS_LEVELS:
            level = "guest"
        token = secrets.token_urlsafe(16)
        expiry = time.time() + max(1, duration_hours) * 3600
        grant = {
            "name": name,
            "token": token,
            "level": level,
            "allowed_commands": allowed_commands,  # None = use level defaults
            "expiry": expiry,
            "created": time.time(),
        }
        # Revoke any prior grant for the same name first.
        if name in self._name_to_token:
            self._grants.pop(self._name_to_token[name], None)
        self._grants[token] = grant
        self._name_to_token[name] = token

        if self._db is not None:
            self._db.log_event(
                event_type="temp_access_created",
                severity="INFO",
                source="access_control",
                description=f"Temp access for {name}: {level}, {duration_hours}h",
            )
        print(f"[AccessController] temp access for {name}: "
              f"{level}, {duration_hours}h")
        return {
            "name": name,
            "token": token,
            "level": level,
            "expiry": expiry,
            "spoken": f"Teile diesen Code mit {name}: {token}",
        }

    async def revoke_access(self, name: str) -> bool:
        token = self._name_to_token.pop(name, None)
        if token is None:
            return False
        self._grants.pop(token, None)
        self._sessions.pop(token, None)
        if self._db is not None:
            self._db.log_event(
                event_type="temp_access_revoked",
                severity="INFO",
                source="access_control",
                description=f"Access revoked for {name}",
            )
        print(f"[AccessController] access revoked for {name}")
        return True

    def _grant_for(self, token: str) -> dict[str, Any] | None:
        grant = self._grants.get(token)
        if grant is None:
            return None
        if time.time() >= grant["expiry"]:
            # Lazily expire.
            self._grants.pop(token, None)
            self._name_to_token.pop(grant["name"], None)
            self._sessions.pop(token, None)
            return None
        return grant

    # ── permission decisions ───────────────────────────────────────────── #

    def is_command_allowed(self, token: str, command: str) -> bool:
        """Does this temp token permit this command right now?"""
        grant = self._grant_for(token)
        if grant is None:
            return False
        # Explicit per-grant allowlist overrides level defaults.
        explicit = grant.get("allowed_commands")
        category = classify_command(command)
        if explicit is not None:
            allowed = category in explicit or command.lower() in [
                e.lower() for e in explicit
            ]
        else:
            level_cats = ACCESS_LEVELS.get(grant["level"], [])
            allowed = "all" in level_cats or category in level_cats
        # Touch the session on every check so get_active_sessions reflects
        # real activity.
        self._sessions[token] = time.time()
        return allowed

    async def get_active_sessions(self) -> list[dict[str, Any]]:
        """Currently-valid temp grants with their last activity."""
        out: list[dict[str, Any]] = []
        for token, grant in list(self._grants.items()):
            if self._grant_for(token) is None:  # prunes expired as a side-effect
                continue
            out.append({
                "name": grant["name"],
                "level": grant["level"],
                "expires_in_min": round((grant["expiry"] - time.time()) / 60, 1),
                "last_activity": self._sessions.get(token, grant["created"]),
            })
        return out

    # ── audit ──────────────────────────────────────────────────────────── #

    async def log_access_event(
        self,
        user: str,
        command: str,
        allowed: bool,
        ip_address: str | None = None,
        voice_confidence: float | None = None,
        permission_level: str | None = None,
        reason: str | None = None,
    ) -> None:
        if self._db is not None:
            self._db.log_access(
                user=user,
                command=command,
                ip_address=ip_address,
                voice_confidence=voice_confidence,
                permission_level=permission_level,
                allowed=allowed,
                reason=reason,
            )
