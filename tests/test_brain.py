"""Direct tests for brain.py's routing core — the dispatch table, the
pre-Claude short-circuits, and the empty-input guard. No real Claude calls.

Brain() degrades gracefully without chromadb/sentence-transformers (memory
just goes offline), so these run in the light CI environment too.
"""
from __future__ import annotations

import pytest

from server.brain import Brain


@pytest.fixture(scope="module")
def brain() -> Brain:
    return Brain()


def test_dispatch_covers_every_registered_tool(brain: Brain) -> None:
    """Every tool offered to Claude must resolve to a handler — a missing
    one would surface as 'Unknown tool' at runtime."""
    table = brain._tool_dispatch()
    names = {t["name"] for t in brain._tools
             if isinstance(t, dict) and "name" in t}
    # web_search is Anthropic's server-side tool (no local handler).
    missing = [n for n in names if n not in table and n != "web_search"]
    assert missing == [], f"tools without a handler: {missing}"


def test_dispatch_unknown_tool_is_none(brain: Brain) -> None:
    assert brain._tool_dispatch().get("does_not_exist") is None


def test_dispatch_table_is_cached(brain: Brain) -> None:
    assert brain._tool_dispatch() is brain._tool_dispatch()


def test_security_short_circuit_routes(brain: Brain) -> None:
    class _Sec:
        async def process_command(self, text):
            return "SOS-HANDLED" if text == "sos" else None
    brain._security = _Sec()
    try:
        assert brain._run_security_command("sos") == "SOS-HANDLED"
        assert brain._run_security_command("plaudern") is None
    finally:
        brain._security = None


def test_communication_short_circuit_routes(brain: Brain) -> None:
    class _Comm:
        async def process_command(self, text):
            return "MSG-HANDLED" if "nachricht" in text else None
    brain._communication = _Comm()
    try:
        assert brain._run_communication_command("neue nachrichten") == "MSG-HANDLED"
        assert brain._run_communication_command("wetter") is None
    finally:
        brain._communication = None


def test_short_circuit_swallows_handler_errors(brain: Brain) -> None:
    class _Boom:
        async def process_command(self, text):
            raise RuntimeError("boom")
    brain._security = _Boom()
    try:
        # Must not raise — a crashing security handler returns None and the
        # turn falls through to Claude.
        assert brain._run_security_command("x") is None
    finally:
        brain._security = None


def test_empty_input_short_circuits_before_claude(brain: Brain) -> None:
    # Whitespace-only input returns immediately without any API call.
    assert brain.reply("sess", "   ") == "I didn't catch that."
    assert brain.reply("sess", "") == "I didn't catch that."


def test_client_has_retries_and_timeout(brain: Brain) -> None:
    from server.config import settings
    assert brain.client.max_retries == settings.CLAUDE_MAX_RETRIES
    assert brain.client.timeout == settings.CLAUDE_TIMEOUT_S


def test_model_escalation(brain: Brain) -> None:
    from server.config import settings
    assert brain._pick_model("wie spät ist es") == settings.MODEL
    assert brain._pick_model("denk gründlich nach darüber") == settings.MODEL_HARD
    assert brain._pick_model("erkläre das step by step") == settings.MODEL_HARD


def test_cost_guard_blocks_and_reply_refuses(brain: Brain) -> None:
    from server.config import settings
    assert brain._cost_guard_ok() is True
    for _ in range(settings.MAX_CLAUDE_CALLS_PER_HOUR + 1):
        brain._record_claude_call()
    assert brain._cost_guard_ok() is False
    # reply() must refuse without making a Claude call (returns the pause msg).
    assert "pausiere" in brain.reply("s", "irgendwas")
    brain._claude_calls.clear()  # reset for other tests in the module
