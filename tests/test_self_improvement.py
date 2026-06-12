"""Tests for the self-improvement / lesson-learning engine."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.memory.self_improvement import SelfImprovementDB


@pytest.fixture()
def si(tmp_path: Path) -> SelfImprovementDB:
    db = SelfImprovementDB(tmp_path / "test.db")
    yield db
    db.close()


# ── schema / availability ─────────────────────────────────────────────────── #

def test_available(si: SelfImprovementDB) -> None:
    assert si.available is True


# ── add / get / deactivate ────────────────────────────────────────────────── #

def test_add_and_get_lesson(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Immer Celsius verwenden.", source="manual")
    assert lid is not None
    lessons = si.get_active_lessons()
    assert len(lessons) == 1
    assert lessons[0]["lesson"] == "Immer Celsius verwenden."
    assert lessons[0]["source"] == "manual"


def test_dedup_prevents_duplicate(si: SelfImprovementDB) -> None:
    si.add_lesson("Gleiche Regel.")
    lid2 = si.add_lesson("Gleiche Regel.")
    assert lid2 is None
    assert len(si.get_active_lessons()) == 1


def test_deactivate_lesson(si: SelfImprovementDB) -> None:
    lid = si.add_lesson("Zu löschende Regel.")
    assert lid is not None
    ok = si.deactivate_lesson(lid)
    assert ok is True
    assert si.get_active_lessons() == []


def test_deactivate_nonexistent(si: SelfImprovementDB) -> None:
    ok = si.deactivate_lesson(9999)
    assert ok is True  # SQL UPDATE on missing row = no error


# ── correction signal detection ────────────────────────────────────────────── #

def test_has_correction_signal_nein(si: SelfImprovementDB) -> None:
    assert si._has_correction_signal("Nein, das stimmt nicht.") is True


def test_has_correction_signal_ich_meinte(si: SelfImprovementDB) -> None:
    assert si._has_correction_signal("Ich meinte eigentlich etwas anderes.") is True


def test_no_correction_in_neutral_text(si: SelfImprovementDB) -> None:
    assert si._has_correction_signal("Wie wird das Wetter morgen?") is False


def test_has_positive_signal(si: SelfImprovementDB) -> None:
    assert si._has_positive_signal("Genau, das war perfekt!") is True


def test_no_positive_in_neutral_text(si: SelfImprovementDB) -> None:
    assert si._has_positive_signal("Ruf bitte Max an.") is False


# ── maybe_learn without client ────────────────────────────────────────────── #

def test_maybe_learn_no_client_no_lesson(si: SelfImprovementDB) -> None:
    result = si.maybe_learn(
        jarvis_response="Die Temperatur ist 72 Fahrenheit.",
        user_reply="Nein, ich will Celsius.",
        client=None,
    )
    assert result is None


def test_maybe_learn_no_correction_no_lesson(si: SelfImprovementDB) -> None:
    result = si.maybe_learn(
        jarvis_response="Erinnerung gesetzt.",
        user_reply="Danke.",
        client=None,
    )
    assert result is None


def test_maybe_learn_positive_signal_records(si: SelfImprovementDB, monkeypatch):
    """Positive signals are stored as feedback but don't create a lesson."""
    result = si.maybe_learn(
        jarvis_response="Hier sind die Nachrichten.",
        user_reply="Genau, perfekt!",
        client=None,
    )
    assert result is None
    # Signal was recorded in feedback_signals
    rows = si._conn.execute(
        "SELECT signal_type FROM feedback_signals"
    ).fetchall()
    assert any(r["signal_type"] == "positive" for r in rows)


# ── spoken summary ────────────────────────────────────────────────────────── #

def test_spoken_summary_empty(si: SelfImprovementDB) -> None:
    text = si.spoken_summary()
    assert "Noch keine" in text


def test_spoken_summary_with_lessons(si: SelfImprovementDB) -> None:
    si.add_lesson("Regel eins.")
    si.add_lesson("Regel zwei.")
    text = si.spoken_summary()
    assert "2" in text
    assert "Regel" in text


# ── context builder integration ───────────────────────────────────────────── #

def test_lessons_block_injected(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx.db")
    si.add_lesson("Nie Fahrenheit benutzen.")
    cb = ContextBuilder(self_improvement=si)
    block = cb._lessons_block()
    assert "Learned Behaviors" in block
    assert "Fahrenheit" in block
    si.close()


def test_lessons_block_empty_when_no_lessons(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx2.db")
    cb = ContextBuilder(self_improvement=si)
    assert cb._lessons_block() == ""
    si.close()


def test_lessons_appear_in_system_prompt(tmp_path: Path) -> None:
    from server.memory.context_builder import ContextBuilder
    si = SelfImprovementDB(tmp_path / "ctx3.db")
    si.add_lesson("Immer kurze Antworten geben.")
    cb = ContextBuilder(self_improvement=si)
    prompt = cb.build_system_prompt()
    assert "Immer kurze Antworten geben." in prompt
    si.close()
