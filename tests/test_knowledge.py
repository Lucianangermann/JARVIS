"""Tests for the Second Brain Phase 1 (remember & recall).

Uses a temp ChromaDB so the real knowledge store is untouched. Skips if
sentence-transformers isn't installed (embeddings unavailable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.memory.long_term import LongTermMemory
from server.memory.context_builder import ContextBuilder


@pytest.fixture()
def lt(tmp_path: Path) -> LongTermMemory:
    store = LongTermMemory(tmp_path / "chromadb")
    if not store.available:
        pytest.skip("embeddings/chromadb unavailable")
    return store


def _seed(lt: LongTermMemory) -> None:
    lt.save_knowledge("Lucian bevorzugt ein dunkles UI-Theme mit Orbitron-Schrift.",
                      source="explicit", category="reference")
    lt.save_knowledge("Die JARVIS-Server-Architektur nutzt FastAPI mit Prompt-Caching.",
                      source="explicit", category="learning")
    lt.save_knowledge("Idee: ein Finanz-Layer mit Beleg-Scan aus Mail.",
                      source="explicit", category="idea")


def test_save_and_recall(lt: LongTermMemory) -> None:
    assert lt.save_knowledge("Mein Lieblingston ist Cyan.", category="reference")
    hits = lt.search_knowledge("welche farbe mag ich")
    assert hits and "Cyan" in hits[0]["document"]


def test_list_and_by_category(lt: LongTermMemory) -> None:
    _seed(lt)
    assert len(lt.list_knowledge()) == 3
    ideas = lt.list_knowledge(category="idea")
    assert len(ideas) == 1 and "Finanz-Layer" in ideas[0]["document"]


def test_knowledge_block_injects_relevant(lt: LongTermMemory) -> None:
    _seed(lt)
    cb = ContextBuilder(long_term=lt)
    block = cb._knowledge_block("welches UI theme nutze ich")
    assert "Saved Knowledge" in block
    assert "UI-Theme" in block


def test_knowledge_block_empty_for_unrelated(lt: LongTermMemory) -> None:
    _seed(lt)
    cb = ContextBuilder(long_term=lt)
    # An unrelated query must NOT pull saved facts into the prompt.
    assert cb._knowledge_block("wie ist das wetter morgen in tokyo") == ""


def test_save_knowledge_returns_id(lt: LongTermMemory) -> None:
    eid = lt.save_knowledge("Testfakt", category="learning")
    assert isinstance(eid, str) and eid.startswith("kn-")
