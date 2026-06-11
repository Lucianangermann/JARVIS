"""Tests for the in-process metrics collector (server/common/metrics.py)."""
from __future__ import annotations

from server.common.metrics import Metrics, time_block, metrics as _singleton


def test_counters_and_snapshot() -> None:
    m = Metrics()
    m.incr("requests")
    m.incr("requests", 4)
    snap = m.snapshot()
    assert snap["counters"]["requests"] == 5
    assert "uptime_s" in snap


def test_latency_summary() -> None:
    m = Metrics()
    m.observe_ms("ep", 10.0)
    m.observe_ms("ep", 30.0)
    ep = m.snapshot()["latency"]["ep"]
    assert ep["count"] == 2
    assert ep["avg_ms"] == 20.0
    assert ep["max_ms"] == 30.0


def test_tool_and_claude_recording() -> None:
    m = Metrics()
    m.record_tool("finance")
    m.record_tool("finance", error=True)
    m.record_claude(100, 40)
    m.record_claude(50, 10)
    snap = m.snapshot()
    assert snap["tools"]["calls"]["finance"] == 2
    assert snap["tools"]["errors"]["finance"] == 1
    assert snap["claude"]["calls"] == 2
    assert snap["claude"]["input_tokens"] == 150
    assert snap["claude"]["output_tokens"] == 50


def test_recent_errors_ring_capped() -> None:
    m = Metrics()
    for i in range(30):
        m.record_error("x", f"err {i}")
    snap = m.snapshot()
    assert snap["counters"]["errors"] == 30
    assert len(snap["recent_errors"]) == 20      # ring capped
    assert snap["recent_errors"][-1]["message"] == "err 29"


def test_time_block_records() -> None:
    before = _singleton.snapshot()["latency"].get("unit_test_block", {}).get("count", 0)
    with time_block("unit_test_block"):
        pass
    after = _singleton.snapshot()["latency"]["unit_test_block"]["count"]
    assert after == before + 1
