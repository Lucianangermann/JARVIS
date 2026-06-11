"""Tests for deferred/conditional actions (intelligence/triggers.py)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from server.intelligence.triggers import TriggerStore


@pytest.fixture()
def store(tmp_path: Path):
    fired = []
    ts = TriggerStore(tmp_path / "triggers.db",
                      deliver=lambda m, p: fired.append((p, m)))
    ts._fired_sink = fired   # expose for assertions
    yield ts
    ts.close()


def test_add_and_pending(store: TriggerStore) -> None:
    assert store.add(time.time() + 3600, "Termin") is not None
    assert store.add(0, "bad") is None          # invalid fire_at
    assert store.add(time.time() + 10, "") is None
    assert len(store.pending()) == 1


def test_fire_due_delivers_and_marks(store: TriggerStore) -> None:
    store.add(time.time() - 1, "Pizza fertig")   # already due
    store.add(time.time() + 9999, "später")
    n = store.fire_due()
    assert n == 1
    assert store._fired_sink == [("high", "Erinnerung: Pizza fertig")]
    # The due one is marked fired; the future one remains pending.
    assert len(store.pending()) == 1


def test_cancel(store: TriggerStore) -> None:
    tid = store.add(time.time() + 3600, "X")
    assert store.cancel(tid) is True
    assert store.pending() == []


def test_not_yet_due_does_not_fire(store: TriggerStore) -> None:
    store.add(time.time() + 3600, "future")
    assert store.fire_due() == 0
    assert store._fired_sink == []


def test_spoken_pending(store: TriggerStore) -> None:
    assert "Keine" in store.spoken_pending()
    store.add(time.time() + 3600, "Arzt anrufen")
    assert "Arzt anrufen" in store.spoken_pending()
