"""Pending-action store for Tier 3+ confirmations.

Flow
----
A Tier 3 or Tier 4 action is *not* executed inline. Instead the dispatcher
calls ``stash()`` with a pre-bound handler and a human-readable summary.
The brain reports the pending ID + summary to the user via the assistant
reply. The user confirms over a separate channel (HTTP POST /confirm,
WebSocket message, or voice transcript), which calls ``consume()``.

Why not block?
--------------
``brain.reply()`` is synchronous and runs inside a FastAPI handler. Blocking
inside it on a 30 s confirmation timeout would freeze the WebSocket and
prevent the very message that confirms the action from being received. The
deferred model keeps the request/response loop snappy and lets the same WS
process the confirm message.

TTL
---
Every access purges entries older than ``CONFIRMATION_TIMEOUT_S`` (30 s).
No background thread required.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

CONFIRMATION_TIMEOUT_S = 30.0


@dataclass
class Pending:
    id: str
    tier: int
    action: str
    handler: Callable[[], str]
    summary: str
    requires_password: bool
    created_at: float

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return (now - self.created_at) > CONFIRMATION_TIMEOUT_S

    def age_s(self) -> float:
        return time.monotonic() - self.created_at


_pending: dict[str, Pending] = {}
_lock = threading.Lock()


def _purge_expired_locked() -> list[Pending]:
    now = time.monotonic()
    expired = [p for p in _pending.values() if p.is_expired(now)]
    for p in expired:
        _pending.pop(p.id, None)
    return expired


def stash(
    *,
    tier: int,
    action: str,
    handler: Callable[[], str],
    summary: str,
    requires_password: bool = False,
) -> Pending:
    """Register a pending action and return its ticket."""
    with _lock:
        _purge_expired_locked()
        p = Pending(
            id=secrets.token_urlsafe(8),
            tier=tier,
            action=action,
            handler=handler,
            summary=summary,
            requires_password=requires_password,
            created_at=time.monotonic(),
        )
        _pending[p.id] = p
        return p


def peek(pid: str) -> Pending | None:
    """Look up a pending action without consuming it. Returns None if
    unknown or expired."""
    with _lock:
        _purge_expired_locked()
        return _pending.get(pid)


def consume(pid: str) -> Pending | None:
    """Atomically remove and return the pending action.

    Returns None if no such pending exists (already consumed, never
    existed, or already timed out).
    """
    with _lock:
        _purge_expired_locked()
        return _pending.pop(pid, None)


def cancel(pid: str) -> Pending | None:
    """Synonym for consume() — semantically used when the user denied."""
    return consume(pid)


def list_pending() -> list[Pending]:
    with _lock:
        _purge_expired_locked()
        return list(_pending.values())


def purge_expired() -> list[Pending]:
    """Called by the dispatcher / a periodic tick to drop stale entries
    and return them so the caller can log the TIMEOUT events."""
    with _lock:
        return _purge_expired_locked()
