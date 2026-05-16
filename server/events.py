"""Lightweight broadcast bus for server-side events.

Powers the Electron HUD's "what is JARVIS doing right now?" indicator
without spamming the voice loop with HTTP requests. The voice loop
runs in a plain thread; WS handlers run on the asyncio event loop —
so the bus is built around `asyncio.Queue` per subscriber and
`loop.call_soon_threadsafe()` for cross-thread publishes.

Wire format (broadcast to every connected WS client):

    {"type": "voice_state",   "state": "listening|wake|transcribing|thinking|speaking|idle"}
    {"type": "user_message",  "text":  "..."}     # transcribed speech
    {"type": "jarvis_reply",  "text":  "..."}     # brain output (voice path)

The HTTP/text path (`/chat`, `/ws` request→`{"reply": ...}`) stays
exactly as it was; events are purely additive and only fire when the
local voice loop is running (`JARVIS_LOCAL_VOICE=1`).

Backpressure: each subscriber gets a bounded queue. If a client is
slow to drain (eg. WS stalled), publish drops the event for THAT
subscriber rather than blocking voice transitions. Other clients are
unaffected.
"""
from __future__ import annotations

import asyncio
from typing import Any

# The event loop captured during FastAPI lifespan startup. Publishes
# from a non-asyncio thread must go through this loop's
# call_soon_threadsafe — directly calling Queue.put_nowait from a
# different thread is undefined behaviour.
_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()

# Per-subscriber queue depth. Voice state transitions fire at most a
# few per second; 64 leaves headroom for bursts (eg. tool-use chains)
# without holding pathological backlogs.
_QUEUE_MAX = 64


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the FastAPI event loop. Called once from the lifespan."""
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    """Register a new subscriber. Returns its queue; pass it back to
    `unsubscribe()` when the WS disconnects."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def publish(event: dict[str, Any]) -> None:
    """Broadcast an event to every current subscriber.

    Thread-safe: voice_loop's worker thread can call this directly.
    Drops the event for any subscriber whose queue is full, so a stuck
    client never blocks the voice pipeline.
    """
    loop = _loop
    if loop is None or not _subscribers:
        return  # server not yet ready, or nobody listening
    for q in list(_subscribers):
        def _put(_q: asyncio.Queue = q, _ev: dict[str, Any] = event) -> None:
            try:
                _q.put_nowait(_ev)
            except asyncio.QueueFull:
                # Drop this event for this subscriber. The next state
                # transition will overwrite the UI anyway.
                pass
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # Loop is closed mid-shutdown — fine, no one to deliver to.
            pass
