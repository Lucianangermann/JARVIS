"""Process-wide kill switch.

When triggered, the dispatcher refuses every Tier 2+ action with a
KILL_SWITCH error until ``resume()`` is called. Tier 1 (read-only info)
remains available so the user can still ask "what's the state?".

Triggers
--------
- Voice "stop" / "halt" / "Schluss" → wired in voice_loop.py
- HTTP POST /emergency-stop
- WebSocket {"action": "emergency-stop"}
- Programmatic kill_switch.trigger(reason)

Reset is *explicit only*: no auto-clear on timeout. A panicked user can
walk away from the machine and the system stays disarmed until they
deliberately resume.
"""
from __future__ import annotations

import threading

from . import action_logger, permission_manager

_event = threading.Event()
_reason: str = ""
_lock = threading.Lock()


def trigger(reason: str = "user request") -> None:
    """Disarm JARVIS. Also revokes the Tier-2 session unlock so resume
    won't silently leave apps under unattended control."""
    global _reason
    with _lock:
        already_set = _event.is_set()
        _event.set()
        _reason = reason
    permission_manager.lock_tier2()
    if not already_set:
        action_logger.log_action("system", "kill_switch", "TRIGGERED", reason)


def resume() -> None:
    """Re-arm JARVIS. Tier 2 stays locked — user must explicitly unlock
    apps again if they want them."""
    global _reason
    with _lock:
        was_set = _event.is_set()
        _event.clear()
        _reason = ""
    if was_set:
        action_logger.log_action("system", "kill_switch", "RESUMED", "")


def is_set() -> bool:
    return _event.is_set()


def reason() -> str:
    return _reason


def status() -> dict[str, object]:
    """Snapshot safe to expose via API/UI."""
    return {"killed": _event.is_set(), "reason": _reason}
