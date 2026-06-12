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


# ── recurring triggers ───────────────────────────────────────────────────── #

def test_daily_trigger_reschedules(store: TriggerStore) -> None:
    """A daily recurring trigger stays pending and reschedules after firing."""
    fire_at = time.time() - 1
    store.add(fire_at, "Sport", recurrence="daily")
    assert store.fire_due() == 1
    pending = store.pending()
    # Still one pending entry — rescheduled, NOT marked fired
    assert len(pending) == 1
    assert pending[0]["fire_at"] > time.time()  # ~24h from now

def test_weekly_trigger_reschedules(store: TriggerStore) -> None:
    fire_at = time.time() - 1
    store.add(fire_at, "Wochenrückblick", recurrence="weekly")
    store.fire_due()
    pending = store.pending()
    assert len(pending) == 1
    # 7 days ± small tolerance
    delay = pending[0]["fire_at"] - time.time()
    assert 6.9 * 86400 <= delay <= 7.1 * 86400

def test_one_shot_trigger_is_marked_fired(store: TriggerStore) -> None:
    store.add(time.time() - 1, "Einmalig")
    store.fire_due()
    assert store.pending() == []

def test_recurring_label_in_spoken_pending(store: TriggerStore) -> None:
    store.add(time.time() + 3600, "Meditation", recurrence="daily")
    text = store.spoken_pending()
    assert "täglich" in text

def test_weekdays_recurrence_skips_weekend(store: TriggerStore) -> None:
    """_next_fire_at for 'weekdays' must land on Mon–Fri."""
    import datetime as _dt
    # fire_at set to right now
    trigger = {"fire_at": time.time() - 1, "recurrence": "weekdays"}
    next_at = store._next_fire_at(trigger)
    wd = _dt.datetime.fromtimestamp(next_at).weekday()
    assert wd < 5, f"Expected weekday, got weekday {wd}"
