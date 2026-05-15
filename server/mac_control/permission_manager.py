"""Intrinsic tier registry + permission gating.

Design
------
The tier of every action is set at registration time, inside the module
that defines the action. Callers — including Claude via tool_use — pass
only an action name; the dispatcher looks up the tier here. There is no
parameter that lets a caller request "Tier 1 access to a Tier 3 action":
the registry is the only source of truth.

Tier semantics
--------------
1. INFO     — read-only. Always permitted (no confirmation, no unlock).
2. APPS     — single per-process unlock. After startup confirmation,
              subsequent Tier 2 actions run without re-prompting.
3. FILES    — per-action confirmation. Sandboxed to ALLOWED_*_DIRS only.
4. SYSTEM   — per-action confirmation AND password match. The Tier-4
              password lives in ``settings.JARVIS_SUDO_PASSWORD`` and is
              redacted from all logs.
"""
from __future__ import annotations

import hmac
import threading
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from ..config import settings


class Tier(IntEnum):
    INFO = 1
    APPS = 2
    FILES = 3
    SYSTEM = 4


@dataclass(frozen=True)
class Action:
    """One registered action.

    handler(**params) -> str           — the real work, returns a result
                                          message (used as tool_result).
    summary(**params) -> str           — human-readable preview for the
                                          confirmation prompt (Tier 3+).
    """

    name: str
    tier: Tier
    handler: Callable[..., str]
    summary: Callable[..., str]


_registry: dict[str, Action] = {}
_lock = threading.Lock()
_tier2_unlocked: bool = False


def register(
    name: str,
    tier: Tier,
    handler: Callable[..., str],
    summary: Callable[..., str],
) -> None:
    """Add (or replace) an action in the registry. Called once per action
    by each tierN_*.py module at import time."""
    with _lock:
        _registry[name] = Action(name=name, tier=tier, handler=handler, summary=summary)


def get(name: str) -> Action | None:
    with _lock:
        return _registry.get(name)


def all_actions() -> list[Action]:
    with _lock:
        return list(_registry.values())


# --- Tier-2 session unlock ------------------------------------------------- #

def unlock_tier2() -> None:
    """Grant Tier 2 for the lifetime of this process. Idempotent."""
    global _tier2_unlocked
    _tier2_unlocked = True


def lock_tier2() -> None:
    """Revoke Tier 2 — used by the kill switch and explicit relock."""
    global _tier2_unlocked
    _tier2_unlocked = False


def tier2_is_unlocked() -> bool:
    return _tier2_unlocked or settings.MAC_TIER2_AUTO_UNLOCK


# --- Tier-4 password gate -------------------------------------------------- #

def password_configured() -> bool:
    return bool(getattr(settings, "JARVIS_SUDO_PASSWORD", ""))


def check_password(provided: Any) -> bool:
    """Constant-time compare against the configured Tier-4 password.

    Returns False if no password is configured (Tier 4 effectively
    disabled) or if the provided value doesn't match. Never logs the
    comparison; never logs the password.
    """
    expected = getattr(settings, "JARVIS_SUDO_PASSWORD", "") or ""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), str(provided).encode("utf-8"))


# --- Status snapshot for the UI -------------------------------------------- #

def status() -> dict[str, Any]:
    """Snapshot of the current permission state — safe to send to the UI.

    Never includes the password or its hash, just whether it's configured.
    """
    return {
        "enabled": settings.MAC_CONTROL_ENABLED,
        "tier1": True,
        "tier2_unlocked": tier2_is_unlocked(),
        "tier3_mode": "confirmation_per_action",
        "tier4_available": password_configured(),
        "action_count": len(_registry),
    }
