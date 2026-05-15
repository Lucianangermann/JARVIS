"""Single dispatch entry point for every mac_control action.

This is the *only* function callers (brain.py, the API routes, tests)
should use. All tier logic, kill-switch checks, sandbox enforcement
side-effects, and audit logging live here.

Flow
----
``dispatch(action, params)``
    → looks up the intrinsic tier from permission_manager
    → applies the gate:
        Tier 1 INFO   — runs inline
        Tier 2 APPS   — runs inline if session-unlocked, else PENDING
        Tier 3 FILES  — always PENDING (per-action confirmation)
        Tier 4 SYSTEM — always PENDING + password required at consume time

``consume(pid, *, password=None)``
    → finalises a pending action returned earlier by ``dispatch``.
    → enforces the password check for Tier 4.
    → also unlocks Tier 2 if the consumed pending was Tier 2 (so the
      "single startup confirmation" semantics work).

Result envelope
---------------
Every function returns a dict with at least ``status`` ∈
{"ok", "pending", "rejected"}. Brain wraps this to JSON for tool_result.
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from . import action_logger, confirmation, kill_switch, permission_manager
from .permission_manager import Tier


# --- helpers --------------------------------------------------------------- #

def _summary(action: permission_manager.Action, params: dict[str, Any]) -> str:
    """Render the action's human-readable summary, never crashing."""
    try:
        return action.summary(**params)
    except Exception:  # noqa: BLE001
        return action.name


def _run_inline(action: permission_manager.Action, params: dict[str, Any]) -> dict[str, Any]:
    """Run a handler synchronously and log the outcome."""
    summary = _summary(action, params)
    try:
        result = action.handler(**params)
    except TypeError as exc:
        # Bad params from caller — log + reject. This is how Claude finds
        # out it called the action with the wrong signature.
        action_logger.log_action(action.tier, action.name, "FAILED", f"params: {exc}")
        return {"status": "rejected", "tier": int(action.tier),
                "action": action.name, "reason": f"Parameter-Fehler: {exc}"}
    except Exception as exc:  # noqa: BLE001
        action_logger.log_action(action.tier, action.name, "FAILED", str(exc))
        return {"status": "rejected", "tier": int(action.tier),
                "action": action.name, "reason": str(exc)}
    action_logger.log_action(action.tier, action.name, "SUCCESS", summary)
    return {"status": "ok", "tier": int(action.tier), "action": action.name, "result": result}


# --- dispatch -------------------------------------------------------------- #

def dispatch(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Begin executing ``name`` with ``params``.

    Returns:
        {"status": "ok",       "tier": int, "action": str, "result": str}
        {"status": "pending",  "tier": int, "action": str, "pending_id": str,
         "summary": str, "requires_password": bool}
        {"status": "rejected", "tier": int|None, "action": str, "reason": str}
    """
    params = params or {}

    action = permission_manager.get(name)
    if action is None:
        action_logger.log_rejected("?", name, "unknown action")
        return {"status": "rejected", "tier": None, "action": name,
                "reason": f"Unbekannte Action: {name!r}"}

    if not settings.MAC_CONTROL_ENABLED:
        action_logger.log_rejected(action.tier, name, "MAC_CONTROL_ENABLED=0")
        return {"status": "rejected", "tier": int(action.tier), "action": name,
                "reason": ("mac_control ist deaktiviert. Setze MAC_CONTROL_ENABLED=1 "
                          "in .env nach Setup der TCC-Berechtigungen.")}

    if kill_switch.is_set() and action.tier != Tier.INFO:
        reason = kill_switch.reason() or "user request"
        action_logger.log_rejected(action.tier, name, f"kill switch: {reason}")
        return {"status": "rejected", "tier": int(action.tier), "action": name,
                "reason": (f"Kill-Switch ist aktiv ({reason}). "
                          "Erst /resume aufrufen, dann erneut versuchen.")}

    # Tier 1 — read-only, always inline.
    if action.tier == Tier.INFO:
        return _run_inline(action, params)

    # Tier 2 — apps & media. Inline if session-unlocked, else pending.
    if action.tier == Tier.APPS:
        if permission_manager.tier2_is_unlocked():
            return _run_inline(action, params)
        return _stash_pending(action, params, requires_password=False)

    # Tier 3 — files. Pending unless MAC_TIER3_AUTO_CONFIRM is on, in
    # which case the user's explicit command is the confirmation.
    if action.tier == Tier.FILES:
        if settings.MAC_TIER3_AUTO_CONFIRM:
            return _run_inline(action, params)
        return _stash_pending(action, params, requires_password=False)

    # Tier 4 — system. Pending + password required at consume time.
    if action.tier == Tier.SYSTEM:
        if not permission_manager.password_configured():
            action_logger.log_rejected(action.tier, name, "JARVIS_SUDO_PASSWORD missing")
            return {"status": "rejected", "tier": 4, "action": name,
                    "reason": ("Tier 4 ist nicht konfiguriert — JARVIS_SUDO_PASSWORD "
                              "in .env setzen, dann erneut versuchen.")}
        return _stash_pending(action, params, requires_password=True)

    # Unreachable.
    return {"status": "rejected", "tier": int(action.tier), "action": name,
            "reason": "Unbekannte Tier-Stufe."}


def _stash_pending(action, params, *, requires_password: bool) -> dict[str, Any]:
    summary = _summary(action, params)
    # Dedup: if Claude is called repeatedly for the same intent (user
    # retrying because they didn't see the existing pending card), we
    # don't want N copies of the same confirmation stacking up. Same
    # action + same summary = same effective request.
    for existing in confirmation.list_pending():
        if existing.action == action.name and existing.summary == summary:
            action_logger.log_confirmation(
                action.tier, action.name, "DEDUP",
                f"existing pending {existing.id} ({existing.age_s():.1f}s old)",
            )
            return {
                "status": "pending",
                "tier": int(action.tier),
                "action": action.name,
                "pending_id": existing.id,
                "summary": summary,
                "requires_password": existing.requires_password,
                "deduped": True,
            }

    # Bind params now — if the user takes 25 s to confirm we still get
    # the same call. Late-bound closures over the same params dict would
    # silently drift if something mutated it.
    bound_params = dict(params)
    pending = confirmation.stash(
        tier=int(action.tier),
        action=action.name,
        handler=lambda: action.handler(**bound_params),
        summary=summary,
        requires_password=requires_password,
    )
    action_logger.log_confirmation(action.tier, action.name, "PENDING", summary)
    return {
        "status": "pending",
        "tier": int(action.tier),
        "action": action.name,
        "pending_id": pending.id,
        "summary": summary,
        "requires_password": requires_password,
    }


def cancel_all() -> dict[str, Any]:
    """Bulk-cancel every outstanding pending. Used by /pending/clear so
    the user can wipe a stale queue without clicking each card."""
    ids = [p.id for p in confirmation.list_pending()]
    cancelled = 0
    for pid in ids:
        if cancel(pid).get("status") == "ok":
            cancelled += 1
    return {"cancelled": cancelled, "remaining": len(confirmation.list_pending())}


# --- consume --------------------------------------------------------------- #

def consume(pid: str, *, password: str | None = None) -> dict[str, Any]:
    """Finalise a pending action.

    For Tier 2 confirmations: also unlocks Tier 2 for the rest of the
    session — this is the "one-time startup unlock" model.

    For Tier 4 confirmations: ``password`` must match
    ``settings.JARVIS_SUDO_PASSWORD`` exactly (constant-time compare).
    """
    # Peek first so we can refuse on wrong password WITHOUT consuming —
    # otherwise a single typo would force the user to redo the whole
    # request through Claude.
    pending = confirmation.peek(pid)
    if pending is None:
        return {"status": "rejected",
                "reason": "Bestätigung unbekannt oder abgelaufen."}

    if pending.requires_password:
        if not permission_manager.check_password(password):
            action_logger.log_confirmation(pending.tier, pending.action,
                                           "REJECTED", "wrong/missing password")
            return {"status": "rejected", "tier": pending.tier,
                    "reason": "Falsches Passwort — bitte erneut eingeben."}

    # All gates passed — actually consume the pending now.
    pending = confirmation.consume(pid)
    if pending is None:
        # Race: expired between peek and consume.
        return {"status": "rejected",
                "reason": "Bestätigung gerade abgelaufen, bitte erneut anfordern."}

    # Re-check the kill switch — the user could have hit emergency-stop
    # in the seconds between dispatch and confirm.
    if kill_switch.is_set():
        reason = kill_switch.reason() or "user request"
        action_logger.log_action(pending.tier, pending.action, "REJECTED",
                                 f"kill switch tripped before confirm: {reason}")
        return {"status": "rejected", "tier": pending.tier,
                "reason": f"Kill-Switch ist aktiv ({reason})."}

    # Tier 2: confirming any Tier-2 pending unlocks future Tier-2 calls.
    if pending.tier == int(Tier.APPS):
        permission_manager.unlock_tier2()

    try:
        result = pending.handler()
    except Exception as exc:  # noqa: BLE001
        action_logger.log_action(pending.tier, pending.action, "FAILED", str(exc))
        return {"status": "rejected", "tier": pending.tier,
                "action": pending.action, "reason": str(exc)}

    action_logger.log_confirmation(pending.tier, pending.action,
                                   "CONFIRMED", pending.summary)
    action_logger.log_action(pending.tier, pending.action, "SUCCESS", pending.summary)
    return {"status": "ok", "tier": pending.tier,
            "action": pending.action, "result": result}


def cancel(pid: str) -> dict[str, Any]:
    """User said no. Drop the pending entry, log it."""
    pending = confirmation.cancel(pid)
    if pending is None:
        return {"status": "rejected",
                "reason": "Bestätigung unbekannt oder abgelaufen."}
    action_logger.log_confirmation(pending.tier, pending.action,
                                   "CANCELLED", pending.summary)
    return {"status": "ok", "tier": pending.tier,
            "action": pending.action, "result": "Aktion abgebrochen."}


# --- introspection used by /permissions and the UI ------------------------- #

def status() -> dict[str, Any]:
    perms = permission_manager.status()
    perms["kill_switch"] = kill_switch.status()
    perms["pending"] = [
        {"id": p.id, "tier": p.tier, "action": p.action,
         "summary": p.summary, "requires_password": p.requires_password,
         "age_s": round(p.age_s(), 1)}
        for p in confirmation.list_pending()
    ]
    return perms
