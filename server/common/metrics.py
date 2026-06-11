"""Lightweight in-process metrics for a 24/7 personal server.

Counters (requests, errors, tool calls, Claude calls/tokens), per-endpoint
latency summaries, and uptime — enough to answer "what is JARVIS doing and
roughly what is it costing me" without a metrics backend. Thread-safe;
``snapshot()`` powers the ``/metrics`` endpoint.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._counters: dict[str, int] = defaultdict(int)
        # name -> (count, sum_ms, max_ms) for latency summaries.
        self._timers: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        self._tool_calls: dict[str, int] = defaultdict(int)
        self._tool_errors: dict[str, int] = defaultdict(int)
        self._claude_input_tokens = 0
        self._claude_output_tokens = 0
        # Last few error messages (ring) for quick triage.
        self._recent_errors: list[dict[str, Any]] = []

    # ── recording ──────────────────────────────────────────────────────── #

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def observe_ms(self, name: str, ms: float) -> None:
        with self._lock:
            t = self._timers[name]
            t[0] += 1
            t[1] += ms
            t[2] = max(t[2], ms)

    def record_tool(self, tool: str, *, error: bool = False) -> None:
        with self._lock:
            self._tool_calls[tool] += 1
            if error:
                self._tool_errors[tool] += 1

    def record_claude(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._lock:
            self._counters["claude_calls"] += 1
            self._claude_input_tokens += int(input_tokens or 0)
            self._claude_output_tokens += int(output_tokens or 0)

    def record_error(self, where: str, message: str) -> None:
        with self._lock:
            self._counters["errors"] += 1
            self._recent_errors.append(
                {"where": where, "message": message[:200], "ts": time.time()})
            self._recent_errors = self._recent_errors[-20:]

    # ── snapshot ───────────────────────────────────────────────────────── #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timers = {
                name: {
                    "count": int(c),
                    "avg_ms": round(s / c, 1) if c else 0.0,
                    "max_ms": round(mx, 1),
                }
                for name, (c, s, mx) in self._timers.items()
            }
            return {
                "uptime_s": round(time.time() - self._started, 1),
                "counters": dict(self._counters),
                "latency": timers,
                "tools": {
                    "calls": dict(self._tool_calls),
                    "errors": dict(self._tool_errors),
                },
                "claude": {
                    "calls": self._counters.get("claude_calls", 0),
                    "input_tokens": self._claude_input_tokens,
                    "output_tokens": self._claude_output_tokens,
                },
                "recent_errors": list(self._recent_errors),
            }


# Process-wide singleton.
metrics = Metrics()


class time_block:
    """Context manager that records elapsed ms into a named latency timer."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._t0 = 0.0

    def __enter__(self) -> "time_block":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        metrics.observe_ms(self._name, (time.perf_counter() - self._t0) * 1000)
