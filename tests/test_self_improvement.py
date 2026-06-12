"""Tests for the enhanced self-improvement / lesson-learning engine."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.memory.self_improvement import SelfImprovementDB


@pytest.fixture()
def si(tmp_path: Path) -> SelfImprovementDB:
    db = SelfImprovementDB(tmp_path / "test.db")
    yield db
    db.close()


# ── availability ──────────────────────────────────────────────────────────── #

def test_available(si: SelfImprovementDB) -> None:
    assert si.available is True


# ── add / get / deactivate ────────────────────────────────────────────────── #

def test_add_and_get_lesson(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Immer Celsius verwenden.", source="manual",
                        lesson_type="stil")
    assert lid is not None
    lessons = si.get_active_lessons()
    assert len(lessons) == 1
    assert lessons[0]["lesson"] == "Immer Celsius verwenden."
    assert lessons[0]["lesson_type"] == "stil"


def test_dedup_prevents_duplicate(si: SelfImprovementDB) -> None:
    si.add_lesson("Gleiche Regel.")
    lid2 = si.add_lesson("Gleiche Regel.")
    assert lid2 is None
    assert len(si.get_active_lessons()) == 1


def test_deactivate_lesson(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Zu löschende Regel.")
    ok = si.deactivate_lesson(lid)
    assert ok is True
    assert si.get_active_lessons() == []


# ── signal detection ──────────────────────────────────────────────────────── #

def test_has_correction_signal(si: SelfImprovementDB) -> None:
    assert si._has_correction_signal("Nein, das stimmt nicht.") is True
    assert si._has_correction_signal("Ich meinte eigentlich etwas anderes.") is True
    assert si._has_correction_signal("Wie wird das Wetter morgen?") is False


def test_has_positive_signal(si: SelfImprovementDB) -> None:
    assert si._has_positive_signal("Genau, das war perfekt!") is True
    assert si._has_positive_signal("Ruf bitte Max an.") is False


# ── Jaccard ───────────────────────────────────────────────────────────────── #

def test_jaccard_identical(si: SelfImprovementDB) -> None:
    score = si._jaccard("Celsius verwenden statt Fahrenheit",
                        "Celsius verwenden statt Fahrenheit")
    assert score == 1.0


def test_jaccard_disjoint(si: SelfImprovementDB) -> None:
    score = si._jaccard("Celsius Temperatur Wetter", "Pizza bestellen Abend")
    assert score == 0.0


def test_jaccard_partial(si: SelfImprovementDB) -> None:
    score = si._jaccard("Celsius verwenden", "Celsius statt Fahrenheit")
    assert 0.0 < score < 1.0


# ── reinforcement ─────────────────────────────────────────────────────────── #

def test_reinforce_increases_confidence(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Celsius verwenden.", lesson_type="stil")
    original_conf = si.get_active_lessons()[0]["confidence"]
    si._update_lesson(lid, "Celsius verwenden.", "stil", confidence_delta=+0.1)
    new_conf = si.get_active_lessons()[0]["confidence"]
    assert new_conf > original_conf


def test_confidence_capped_at_one(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Test Regel.", confidence=0.95)
    si._update_lesson(lid, "Test Regel.", "general", confidence_delta=+0.2)
    conf = si.get_active_lessons()[0]["confidence"]
    assert conf <= 1.0


# ── weakening ─────────────────────────────────────────────────────────────── #

def test_weaken_conflicting_reduces_confidence(si: SelfImprovementDB) -> None:
    si.add_lesson("Celsius statt Fahrenheit benutzen.", confidence=0.8)
    si._weaken_conflicting("Immer Celsius Fahrenheit vermeiden.")
    lessons = si.get_active_lessons()
    assert len(lessons) == 1
    assert lessons[0]["confidence"] < 0.8


def test_weaken_deactivates_below_threshold(si: SelfImprovementDB) -> None:
    si.add_lesson("Celsius Einheit Temperatur.", confidence=0.35)
    si._weaken_conflicting("Celsius Einheit Temperatur Skala.")
    # Confidence drops to 0.20 → deactivated
    assert si.get_active_lessons() == []


# ── decay ─────────────────────────────────────────────────────────────────── #

def test_decay_deactivates_old_low_confidence(tmp_path: Path) -> None:
    """Lessons with low confidence AND old timestamp get deactivated on init."""
    import time as _t
    si = SelfImprovementDB(tmp_path / "decay.db")
    # Insert an old, low-confidence lesson manually.
    old_ts = _t.time() - 40 * 86400  # 40 days ago
    si._conn.execute(
        "INSERT INTO learned_lessons (ts, lesson, confidence, last_reinforced, active) "
        "VALUES (?, 'Alte Regel.', 0.32, ?, 1)",
        (old_ts, old_ts),
    )
    si._conn.commit()
    # Trigger decay explicitly.
    si._maybe_decay()
    assert si.get_active_lessons() == []
    si.close()


# ── type filtering ────────────────────────────────────────────────────────── #

def test_get_active_lessons_by_type(si: SelfImprovementDB) -> None:
    si.add_lesson("Kurze Antworten.", lesson_type="stil")
    si.add_lesson("Celsius bevorzugen.", lesson_type="präferenz")
    si.add_lesson("manage_tasks statt text.", lesson_type="tool")
    stil = si.get_active_lessons(lesson_types=["stil"])
    assert len(stil) == 1 and stil[0]["lesson_type"] == "stil"


def test_get_lessons_for_prompt_sorts_by_relevance(si: SelfImprovementDB) -> None:
    si.add_lesson("Celsius statt Fahrenheit bei Temperatur.", lesson_type="stil")
    si.add_lesson("Kurze Antworten bei einfachen Fragen.", lesson_type="stil")
    result = si.get_lessons_for_prompt("Was ist die Temperatur heute Celsius?")
    # Celsius lesson should rank higher.
    assert "Celsius" in result[0]["lesson"]


# ── maybe_learn (no client) ───────────────────────────────────────────────── #

def test_maybe_learn_no_client_no_lesson(si: SelfImprovementDB) -> None:
    result = si.maybe_learn(
        jarvis_response="Die Temperatur ist 72 Fahrenheit.",
        user_reply="Nein, ich will Celsius.",
        client=None,
    )
    assert result is None


def test_maybe_learn_positive_records_signal(si: SelfImprovementDB) -> None:
    si.maybe_learn(
        jarvis_response="Hier sind die Nachrichten.",
        user_reply="Genau, perfekt!",
        client=None,
    )
    rows = si._conn.execute(
        "SELECT signal_type FROM feedback_signals"
    ).fetchall()
    assert any(r["signal_type"] == "positive" for r in rows)


def test_maybe_learn_long_reply_skipped(si: SelfImprovementDB) -> None:
    """Corrections longer than 200 chars are not processed (likely not corrections)."""
    mock_client = MagicMock()
    result = si.maybe_learn(
        jarvis_response="JARVIS reply.",
        user_reply="nein " + "x" * 200,
        client=mock_client,
    )
    assert result is None
    mock_client.messages.create.assert_not_called()


# ── context builder integration ───────────────────────────────────────────── #

def test_lessons_by_type_in_prompt(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx.db")
    si.add_lesson("Immer Celsius.", lesson_type="stil")
    si.add_lesson("Manage_tasks benutzen.", lesson_type="tool")
    cb = ContextBuilder(self_improvement=si)
    # Stable block: stil only
    stable = cb._lessons_block(lesson_types=["stil", "präferenz", "general"])
    assert "Celsius" in stable
    assert "Manage_tasks" not in stable
    # Dynamic block: tool only
    dynamic = cb._lessons_block(lesson_types=["fakt", "tool"])
    assert "Manage_tasks" in dynamic
    assert "Celsius" not in dynamic
    si.close()


def test_lessons_appear_in_full_system_prompt(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx3.db")
    si.add_lesson("Immer kurze Antworten geben.", lesson_type="stil")
    cb = ContextBuilder(self_improvement=si)
    prompt = cb.build_system_prompt()
    assert "Immer kurze Antworten geben." in prompt
    si.close()


def test_query_relevance_filters_lessons(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx4.db")
    si.add_lesson("Celsius statt Fahrenheit bei Temperatur.", lesson_type="fakt")
    si.add_lesson("Pizza immer mit Lieferando bestellen.", lesson_type="fakt")
    cb = ContextBuilder(self_improvement=si)
    # Query about temperature → Celsius lesson should appear
    block = cb._lessons_block("Wie warm ist es draußen Celsius?",
                               lesson_types=["fakt", "tool"], limit=1)
    assert "Celsius" in block
    si.close()


# ── spoken summary ────────────────────────────────────────────────────────── #

def test_spoken_summary_empty(si: SelfImprovementDB) -> None:
    assert "Noch keine" in si.spoken_summary()


def test_spoken_summary_with_lessons(si: SelfImprovementDB) -> None:
    si.add_lesson("Regel eins.")
    si.add_lesson("Regel zwei.")
    text = si.spoken_summary()
    assert "2" in text
