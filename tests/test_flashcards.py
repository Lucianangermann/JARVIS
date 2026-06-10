"""Tests for the Second Brain Phase 2 — SM-2 flashcards."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from server.knowledge.flashcards import FlashcardManager


@pytest.fixture()
def fc(tmp_path: Path) -> FlashcardManager:
    m = FlashcardManager(tmp_path / "knowledge.db")
    yield m
    m.close()


def test_add_and_due(fc: FlashcardManager) -> None:
    fc.add_card("Q1", "A1")
    fc.add_card("Q2", "A2")
    assert fc.due_count() == 2          # new cards are due immediately
    assert fc.stats()["total"] == 2


def test_add_rejects_empty(fc: FlashcardManager) -> None:
    assert fc.add_card("", "A") is None
    assert fc.add_card("Q", "  ") is None


def test_sm2_progression(fc: FlashcardManager) -> None:
    cid = fc.add_card("Hauptstadt Frankreich?", "Paris")
    r1 = fc.review_card(cid, 4)
    assert r1["interval_days"] == 1 and r1["repetitions"] == 1
    fc._write("UPDATE flashcards SET next_review=? WHERE id=?", (time.time() - 1, cid))
    r2 = fc.review_card(cid, 5)
    assert r2["interval_days"] == 6 and r2["repetitions"] == 2
    fc._write("UPDATE flashcards SET next_review=? WHERE id=?", (time.time() - 1, cid))
    r3 = fc.review_card(cid, 4)
    assert r3["interval_days"] > 6      # interval = 6 * ease_factor


def test_sm2_failure_resets(fc: FlashcardManager) -> None:
    cid = fc.add_card("Q", "A")
    fc.review_card(cid, 5)
    fc.review_card(cid, 5)
    fc._write("UPDATE flashcards SET next_review=? WHERE id=?", (time.time() - 1, cid))
    r = fc.review_card(cid, 1)          # blackout
    assert r["interval_days"] == 1 and r["repetitions"] == 0


def test_scheduled_card_not_due(fc: FlashcardManager) -> None:
    cid = fc.add_card("Q", "A")
    fc.review_card(cid, 4)              # pushes next_review ~1 day out
    assert fc.due_count() == 0


def test_quality_from_feedback(fc: FlashcardManager) -> None:
    assert fc.quality_from_feedback("falsch") == 1
    assert fc.quality_from_feedback("schwer") == 3
    assert fc.quality_from_feedback("richtig") == 4
    assert fc.quality_from_feedback("einfach") == 5
    assert fc.quality_from_feedback("") == 4


def test_generate_from_text(fc: FlashcardManager) -> None:
    class _Block:
        type = "text"
        text = ('{"cards":[{"front":"Was ist FastAPI?","back":"Ein Python-'
                'Webframework"},{"front":"Was ist SM-2?","back":"Ein Spaced-'
                'Repetition-Algorithmus"}]}')

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    fc._client = _Client()
    ids = fc.generate_from_text("FastAPI und SM-2 erklären", "learning")
    assert len(ids) == 2
    cards = fc.list_cards()
    assert any("FastAPI" in c["front"] for c in cards)
    assert all(c["source"] == "generated" for c in cards)


def test_list_by_category(fc: FlashcardManager) -> None:
    fc.add_card("Q1", "A1", category="geo")
    fc.add_card("Q2", "A2", category="math")
    assert len(fc.list_cards(category="geo")) == 1
