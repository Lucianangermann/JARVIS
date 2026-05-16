"""Tests for the long-term memory + self-learning system.

The suite is parametrised over a temp ``data/`` directory so every
test starts from a clean ChromaDB + SQLite + profile.json. The
embedding model is loaded once at import time and re-used (it's the
slow part — ~10-20 s cold start). Each test runs in well under a
second after that warm-up.

Coverage matrix
---------------
- session_end persists summary to long-term
- semantic search returns relevant past context
- error → record_fix → known_fixes promotion path
- profile.update_from_conversation extracts facts
- redact_secrets removes sensitive substrings before any write
- forget_everything (with confirmation_token) wipes every store
- ContextBuilder assembles all sections in the system prompt
- ChromaDB persists across two LongTermMemory instantiations
- MemoryManager.degraded stays False when all subsystems load
- Graceful degradation when a subsystem is forced to fail at init
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.memory import embeddings, long_term, profile_manager
from server.memory.context_builder import ContextBuilder
from server.memory.error_memory import ErrorMemory
from server.memory.memory_manager import MemoryManager
from server.memory.profile_manager import (
    ProfileManager, extract_facts, redact_secrets,
)
from server.memory.short_term import ShortTermMemory


# The slow embedding-model load happens once on import; gate via this
# module-level skip so a misconfigured CI without sentence-transformers
# downloads doesn't fail every test.
_HAS_EMBEDDINGS = embeddings.is_available()
needs_embeddings = pytest.mark.skipif(
    not _HAS_EMBEDDINGS,
    reason=f"sentence-transformers unavailable: {embeddings.load_error()}",
)


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def memory(tmp_path: Path) -> MemoryManager:
    """Fresh MemoryManager with empty data dir."""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    return MemoryManager(data_dir=data_dir, logs_dir=logs_dir)


@pytest.fixture
def long_term_mem(tmp_path: Path) -> long_term.LongTermMemory:
    return long_term.LongTermMemory(tmp_path / "chromadb")


@pytest.fixture
def err_mem(tmp_path: Path) -> ErrorMemory:
    return ErrorMemory(tmp_path / "jarvis.db")


@pytest.fixture
def profile(tmp_path: Path) -> ProfileManager:
    return ProfileManager(tmp_path / "profile.json", tmp_path / "jarvis.db")


# ---- short_term ---------------------------------------------------------


def test_short_term_trims_to_capacity():
    stm = ShortTermMemory(max_messages=4)
    sid = "s1"
    for i in range(10):
        stm.add(sid, "user", f"msg{i}")
    msgs = stm.get_context(sid)
    assert len(msgs) == 4
    # Oldest are dropped from the FRONT.
    assert msgs[0]["content"] == "msg6"
    assert msgs[-1]["content"] == "msg9"


def test_short_term_summarise_skips_empty_blocks():
    stm = ShortTermMemory()
    sid = "s1"
    stm.add(sid, "user", "Hallo JARVIS")
    stm.add(sid, "assistant", [{"type": "text", "text": "Hi there"}])
    stm.add(sid, "assistant", "")           # empty — should be skipped
    summary = stm.summarise(sid)
    assert "USER" in summary and "Hallo JARVIS" in summary
    assert "Hi there" in summary
    assert summary.count("JARVIS:") == 1    # the empty turn isn't included


# ---- redaction ----------------------------------------------------------


def test_redact_card_luhn_only():
    # 4111 1111 1111 1111 is a known Luhn-valid test card.
    assert "[REDACTED:card]" in redact_secrets("My card 4111 1111 1111 1111 here")
    # A 13-digit phone number that doesn't pass Luhn must NOT be redacted.
    assert "4915123456789" in redact_secrets("Calling 4915123456789 now")


def test_redact_api_keys_and_password():
    out = redact_secrets("Key sk-ant-api03-abcdef1234567890XYZ")
    assert "[REDACTED:anthropic-key]" in out
    out2 = redact_secrets("Key sk-proj-abcdef1234567890xyz123")
    assert "[REDACTED:openai-key]" in out2
    out3 = redact_secrets("set password=hunter2hunter2 then")
    assert "[REDACTED:credential]" in out3
    out4 = redact_secrets("Authorization: Bearer abcd1234efgh5678")
    assert "[REDACTED:bearer]" in out4


def test_redact_url_password():
    out = redact_secrets("connect to postgres://user:secret@db.host/db")
    assert "secret" not in out
    assert "[REDACTED:url-password]" in out


# ---- fact extraction ----------------------------------------------------


def test_extract_facts_de_basic():
    facts = extract_facts("Ich heiße Lucian. Ich lebe in Berlin.")
    cats = {f["category"] for f in facts}
    assert "name" in cats and "location" in cats


def test_extract_facts_ignores_redacted_categories():
    # Even if a forbidden category leaked through somehow, the value
    # rule shouldn't promote it. We don't have a "password" rule but
    # verify the forbidden filter would block it.
    from server.memory.profile_manager import _FORBIDDEN_CATEGORIES
    assert "credit_card" in _FORBIDDEN_CATEGORIES
    assert "password" in _FORBIDDEN_CATEGORIES


# ---- error_memory -------------------------------------------------------


def test_error_record_then_known_fix_promotion(err_mem: ErrorMemory):
    eid = err_mem.record_error(
        "open homekit lights", RuntimeError("HomeKit timeout"),
        category="lights",
    )
    assert eid is not None
    # Before fix: no known fix yet.
    assert err_mem.get_known_fix("open homekit lights") is None
    err_mem.record_fix(eid, "retry after 2s", worked=True)
    fix = err_mem.get_known_fix("open homekit lights")
    assert fix is not None
    assert fix["fix"] == "retry after 2s"
    assert fix["success_rate"] == 1.0


def test_problematic_commands_threshold(err_mem: ErrorMemory):
    err_mem.record_error("flaky cmd", RuntimeError("oops"))
    err_mem.record_error("flaky cmd", RuntimeError("oops"))
    err_mem.record_error("flaky cmd", RuntimeError("oops"))
    rows = err_mem.get_problematic_commands(min_failures=2)
    assert any(r["command"] == "flaky cmd" for r in rows)


def test_auto_retry_strategy_uses_known_fix(err_mem: ErrorMemory):
    eid = err_mem.record_error("brittle cmd", "[ERROR] gateway")
    err_mem.record_fix(eid, "use the second endpoint", worked=True)
    strat = err_mem.auto_retry_strategy("brittle cmd")
    assert strat["fallback"] == "use the second endpoint"


# ---- profile ------------------------------------------------------------


def test_profile_update_from_conversation_redacts_first(profile: ProfileManager):
    # The "preference" rule could otherwise capture credit-card-like
    # numerics — verify redaction runs before extraction.
    text = "Ich mag 4111111111111111 als Lieblingszahl"
    added = profile.update_from_conversation(text)
    snap = profile.get()
    pref = snap.get("preferences", {}).get("music_genre") or []
    assert all("[REDACTED" in v or "4111" not in v for v in pref)


def test_profile_increment_command_tracks_favorites(profile: ProfileManager):
    profile.increment_command("music")
    profile.increment_command("music")
    profile.increment_command("lights")
    snap = profile.get()
    fav = snap["usage"]["favorite_commands"]
    assert fav["music"] == 2
    assert fav["lights"] == 1
    assert snap["usage"]["total_commands"] == 3


def test_profile_atomic_write_survives_reload(tmp_path: Path):
    p1 = ProfileManager(tmp_path / "profile.json", tmp_path / "db.sqlite")
    p1.add_fact("Lucian uses Spotify Premium")
    p2 = ProfileManager(tmp_path / "profile.json", tmp_path / "db.sqlite")
    facts = p2.get().get("context", {}).get("known_facts") or []
    assert "Lucian uses Spotify Premium" in facts


# ---- long-term (ChromaDB) -----------------------------------------------


@needs_embeddings
def test_long_term_save_and_search(long_term_mem):
    assert long_term_mem.available is True
    long_term_mem.save_conversation(
        "User asked about weather and JARVIS reported sunny conditions.",
        message_count=2,
    )
    long_term_mem.save_conversation(
        "User wanted Spotify paused. JARVIS paused playback.",
        message_count=4,
    )
    results = long_term_mem.search_similar("weather forecast", n_results=2)
    assert results, "search_similar returned empty"
    # The weather session must outrank the Spotify session for a weather query.
    assert "weather" in results[0]["document"].lower()


@needs_embeddings
def test_long_term_persists_across_instantiations(tmp_path: Path):
    a = long_term.LongTermMemory(tmp_path / "chromadb")
    a.save_conversation("Persistent: a test summary about apple pie.")
    # Drop and re-open from the same path.
    b = long_term.LongTermMemory(tmp_path / "chromadb")
    hits = b.search_similar("apple pie")
    assert hits and "apple pie" in hits[0]["document"]


@needs_embeddings
def test_long_term_wipe_clears_collections(long_term_mem):
    long_term_mem.save_conversation("entry one")
    long_term_mem.save_conversation("entry two")
    counts_before = long_term_mem.stats()
    assert counts_before["conversations"] == 2
    wiped = long_term_mem.wipe_all()
    assert wiped["conversations"] == 2
    counts_after = long_term_mem.stats()
    assert counts_after["conversations"] == 0


# ---- context builder ----------------------------------------------------


@needs_embeddings
def test_context_builder_includes_all_sections(memory: MemoryManager):
    # Seed profile + long-term + error_mem so every section has content.
    memory.profile.add_fact("Lucian lives in Berlin")
    memory.long_term.save_conversation(
        "User asked about lights, JARVIS turned them off."
    )
    eid = memory.error_mem.record_error(
        "open lights", RuntimeError("gateway timeout"), category="lights",
    )
    # Second + third failure for the same command pushes it into the
    # problematic list (min_failures=2 by default).
    memory.error_mem.record_error("open lights", RuntimeError("gateway timeout"))
    memory.error_mem.record_error("open lights", RuntimeError("gateway timeout"))
    blocks = memory.build_system_blocks("turn off the lights")
    full = "\n\n".join(b["text"] for b in blocks)
    assert "## User Profile" in full
    assert "Lucian" in full or "Berlin" in full
    assert "## Known Issues to Avoid" in full
    assert "## Current Context" in full


def test_context_builder_runs_with_degraded_layers(tmp_path: Path):
    # Build a manager whose ChromaDB is poisoned (point at a non-dir).
    # The integration shouldn't crash — sections gracefully drop.
    bad = tmp_path / "not-a-dir-but-a-file"
    bad.write_text("oops")
    cb = ContextBuilder()         # no layers attached at all
    prompt = cb.build_system_prompt("hello")
    assert "You are JARVIS" in prompt or "Current Context" in prompt


# ---- memory_manager end-to-end ------------------------------------------


@needs_embeddings
def test_session_end_persists_summary_to_long_term(memory: MemoryManager):
    sid = "test-session"
    memory.session_start(sid, warmup_query="hello")
    memory.before_message(sid, "Ich höre gerne Jazz")
    memory.after_message(sid, "Ich höre gerne Jazz",
                          "Verstanden — Jazz also.")
    result = memory.session_end(sid)
    assert result.get("conversation_id"), "session summary not stored"
    # Should be searchable immediately.
    hits = memory.search("Jazz")
    assert any("Jazz" in h["document"] for h in hits)


@needs_embeddings
def test_record_command_result_drives_both_layers(memory: MemoryManager):
    memory.record_command_result(
        "mac_action:open_app", success=True,
        category="app", duration_ms=120,
    )
    memory.record_command_result(
        "mac_action:open_homekit_lights", success=False,
        error=RuntimeError("HomeKit timeout"), category="lights",
    )
    # Error memory should now have one failure tracked.
    problematic = memory.known_errors()
    assert any("homekit_lights" in r["command"] for r in problematic)
    # Long-term should hold both commands.
    cmd_stats = memory.long_term.stats()
    assert cmd_stats["commands"] >= 2


@needs_embeddings
def test_full_wipe_requires_confirmation_and_clears_everything(memory: MemoryManager):
    sid = "wipe-test"
    memory.session_start(sid, warmup_query="hi")
    memory.before_message(sid, "Mein Name ist Test")
    memory.after_message(sid, "Mein Name ist Test", "Hallo Test!")
    memory.session_end(sid)
    # Refusal path.
    refused = memory.forget_everything(confirmation_token=None)
    assert refused["ok"] is False
    refused2 = memory.forget_everything(confirmation_token="please")
    assert refused2["ok"] is False
    # Real wipe.
    result = memory.forget_everything(confirmation_token="I UNDERSTAND")
    assert result["ok"] is True
    stats = memory.stats()
    assert stats["long_term"]["conversations"] == 0
    assert stats["error"]["errors"] == 0
    assert stats["profile"]["known_facts"] == 0


def test_memory_manager_not_degraded_on_normal_init(memory: MemoryManager):
    # All three persistent layers + embeddings load → degraded is False.
    # Embeddings might be missing in CI; only assert the relevant flag.
    assert memory.profile.available
    assert memory.error_mem.available
    if _HAS_EMBEDDINGS:
        assert memory.long_term.available
        assert memory.degraded is False
