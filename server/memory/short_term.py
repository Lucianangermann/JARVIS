"""Short-term (in-session) conversation memory.

This is the sliding window of recent messages that Claude sees as
``messages`` on every API call. The existing :class:`Brain` keeps a
per-session ``dict[session_id] -> list`` directly; we wrap that in a
class so other memory layers can introspect it (eg. summarise the
session for long-term storage).

Design notes
------------
- Hard cap at ``MAX_MESSAGES`` raw role/content entries (default 20:
  10 user + 10 assistant pairs). Older entries are dropped from the
  **start** of the buffer so the most recent context survives.
- The system message lives outside this buffer — Anthropic's API
  takes ``system`` as a top-level parameter, not a message. We never
  store/drop it here.
- :meth:`get_context` returns the same shape the Anthropic SDK
  expects: ``list[{"role": str, "content": str | list}]``. ``content``
  may be a raw string (user/assistant text) or the rich block list
  the model produced when it used tools.
- Thread-safe — the brain runs FastAPI handlers in a threadpool.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_MESSAGES = 20         # 10 user/assistant pairs
TURN_TIMEOUT_S = 30 * 60   # consider the session "ended" after 30 min idle


@dataclass
class _Message:
    role: str                                   # "user" | "assistant"
    content: Any                                # str or list[block]
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class ShortTermMemory:
    """In-session conversation buffer keyed by session_id.

    The brain's existing per-session history dict gets folded into
    this class. Same shape, plus helpers (summarise, clear, last_user_text).
    """

    def __init__(self, max_messages: int = MAX_MESSAGES) -> None:
        self.max_messages = max_messages
        self._buffers: dict[str, list[_Message]] = {}
        self._lock = threading.Lock()

    # ---- write paths -----------------------------------------------------

    def add(self, session_id: str, role: str, content: Any,
            metadata: dict[str, Any] | None = None) -> None:
        """Append a message and auto-trim. ``content`` is whatever the
        Anthropic SDK accepts for a message entry — a plain string for
        user input, the raw block list for assistant tool-use turns."""
        msg = _Message(role=role, content=content, metadata=metadata or {})
        with self._lock:
            buf = self._buffers.setdefault(session_id, [])
            buf.append(msg)
            self._trim(buf)

    def clear(self, session_id: str) -> None:
        """Drop the in-memory buffer for a session. Call this AFTER
        the long-term layer has consumed a summary, not before."""
        with self._lock:
            self._buffers.pop(session_id, None)

    # ---- read paths ------------------------------------------------------

    def get_context(self, session_id: str) -> list[dict[str, Any]]:
        """Return the buffer in Anthropic-API shape.

        Returns a *copy* so the caller can mutate (eg. append a tool
        result) without racing with concurrent writers."""
        with self._lock:
            buf = self._buffers.get(session_id, [])
            return [{"role": m.role, "content": m.content} for m in buf]

    def get_messages(self, session_id: str) -> list[_Message]:
        """Raw access with metadata + timestamps. Used by the summary
        builder when packing a session for long-term storage."""
        with self._lock:
            return list(self._buffers.get(session_id, []))

    def session_count(self) -> int:
        with self._lock:
            return len(self._buffers)

    def message_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._buffers.get(session_id, []))

    def last_user_text(self, session_id: str) -> str:
        """Most recent plain-text user message — used by the long-term
        layer to decide what to semantic-search for."""
        with self._lock:
            buf = self._buffers.get(session_id, [])
            for m in reversed(buf):
                if m.role == "user" and isinstance(m.content, str):
                    return m.content
        return ""

    def is_idle(self, session_id: str, *, timeout_s: float = TURN_TIMEOUT_S) -> bool:
        """True iff the last message in ``session_id`` is older than
        ``timeout_s``. Used by the manager to auto-flush stale sessions
        to long-term storage."""
        with self._lock:
            buf = self._buffers.get(session_id, [])
            if not buf:
                return True
            return (time.time() - buf[-1].timestamp) > timeout_s

    # ---- summarisation ---------------------------------------------------

    def summarise(self, session_id: str, *, max_chars: int = 800) -> str:
        """Quick plain-text rollup of the session, used as the
        document body when we save the session into long-term semantic
        memory. Cheap to compute (no LLM call) — just joins user/
        assistant text turns and clips."""
        with self._lock:
            buf = self._buffers.get(session_id, [])
            lines: list[str] = []
            for m in buf:
                if isinstance(m.content, str):
                    body = m.content
                else:
                    # Tool-use turn: pull any text blocks the assistant produced.
                    body = " ".join(
                        b.get("text", "") for b in (m.content or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                body = body.strip()
                if not body:
                    continue
                tag = "USER" if m.role == "user" else "JARVIS"
                lines.append(f"{tag}: {body}")
            joined = "\n".join(lines)
            return joined[:max_chars]

    # ---- internals -------------------------------------------------------

    def _trim(self, buf: list[_Message]) -> None:
        """Drop from the front when over capacity. Keep the most
        recent messages so the active context is preserved."""
        overflow = len(buf) - self.max_messages
        if overflow > 0:
            del buf[:overflow]
