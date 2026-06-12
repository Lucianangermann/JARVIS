"""Tests for the ProactiveEngine.

We exercise the gating + persistence machinery with a temp SQLite
file and a stub ContextEngine — none of the real triggers run.
Trigger check functions themselves are smoke-tested separately
through synthetic engine state because their data sources (battery,
calendar, weather, OSRM) are real-world side-effectful and not
worth mocking exhaustively in this slice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

from server.intelligence.context import ContextEngine
from server.intelligence.proactive import (
    NotificationSpec,
    ProactiveEngine,
    _build_specs,
)


_TZ = ZoneInfo("Europe/Berlin")


# --- helpers -------------------------------------------------------------- #

class _Sink:
    """Captures delivered (text, priority) pairs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, priority: str) -> None:
        self.calls.append((text, priority))


def _engine(tmp_path: Path) -> tuple[ProactiveEngine, _Sink]:
    ctx = ContextEngine()
    sink = _Sink()
    eng = ProactiveEngine(
        db_path=tmp_path / "test.db",
        context=ctx,
        handler=sink,
    )
    return eng, sink


def _spec(
    name: str = "demo",
    priority: str = "medium",
    cooldown: int = 30,
    max_per_day: Optional[int] = None,
    active_hours: Optional[tuple[int, int]] = None,
    message: Optional[str] = "hello",
) -> NotificationSpec:
    return NotificationSpec(
        name=name,
        priority=priority,
        cooldown_minutes=cooldown,
        max_per_day=max_per_day,
        active_hours=active_hours,
        check=lambda _eng: message,
    )


# --- registry ------------------------------------------------------------- #

def test_registry_has_all_triggers():
    names = {s.name for s in _build_specs()}
    assert names == {
        "battery_warning", "meeting_warning", "traffic_warning",
        "important_email", "weather_morning", "package_delivery",
        "hydration", "forgotten_task", "productivity_slump",
        "morning_learning", "flashcard_due", "lernziel_reminder",
    }


# --- gating --------------------------------------------------------------- #

def test_first_fire_delivers(tmp_path):
    eng, sink = _engine(tmp_path)
    spec = _spec()
    assert eng._should_notify(spec) is True
    eng._deliver(spec, "hello")
    assert sink.calls == [("hello", "medium")]


def test_cooldown_blocks_second_fire_within_window(tmp_path):
    eng, _ = _engine(tmp_path)
    spec = _spec(cooldown=30)
    eng._record_fire(spec.name)
    assert eng._should_notify(spec) is False  # immediate retry blocked


def test_cooldown_lets_through_after_window(tmp_path):
    eng, _ = _engine(tmp_path)
    spec = _spec(cooldown=30)
    eng._record_fire(spec.name)
    # Rewrite the row to pretend last fire was 31 min ago.
    past = datetime.now(_TZ).timestamp() - 31 * 60
    with eng._connect() as conn:
        conn.execute(
            "UPDATE notification_log SET last_triggered=? WHERE type=?",
            (past, spec.name),
        )
        conn.commit()
    assert eng._should_notify(spec) is True


def test_max_per_day_caps_fires(tmp_path):
    eng, _ = _engine(tmp_path)
    spec = _spec(cooldown=1, max_per_day=2)
    # Fire twice — should be at the cap afterwards.
    eng._record_fire(spec.name)
    eng._record_fire(spec.name)
    # Drag cooldown out of the way to isolate the day-cap.
    past = datetime.now(_TZ).timestamp() - 60 * 60
    with eng._connect() as conn:
        conn.execute(
            "UPDATE notification_log SET last_triggered=? WHERE type=?",
            (past, spec.name),
        )
        conn.commit()
    assert eng._should_notify(spec) is False


def test_count_today_resets_on_new_day(tmp_path):
    eng, _ = _engine(tmp_path)
    spec = _spec(cooldown=1, max_per_day=1)
    eng._record_fire(spec.name)
    # Backdate the row's `day` field to yesterday so should_notify
    # treats count_today as 0.
    with eng._connect() as conn:
        conn.execute(
            "UPDATE notification_log SET day=?, last_triggered=? WHERE type=?",
            ("1999-01-01", datetime.now(_TZ).timestamp() - 60 * 60, spec.name),
        )
        conn.commit()
    assert eng._should_notify(spec) is True


def test_active_hours_window(tmp_path, monkeypatch):
    eng, _ = _engine(tmp_path)
    # Force "now.hour == 20" by patching the engine's datetime.now.
    import server.intelligence.proactive as p

    class _Fake:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 5, 19, 20, 0, tzinfo=_TZ)

    monkeypatch.setattr(p, "datetime", _Fake)
    spec = _spec(active_hours=(9, 18))
    assert eng._should_notify(spec) is False
    spec2 = _spec(name="other", active_hours=(9, 21))
    assert eng._should_notify(spec2) is True


def test_low_priority_suppressed_late_night(tmp_path, monkeypatch):
    eng, _ = _engine(tmp_path)
    import server.intelligence.proactive as p

    class _Fake:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 5, 19, 2, 0, tzinfo=_TZ)

    monkeypatch.setattr(p, "datetime", _Fake)
    low = _spec(priority="low")
    high = _spec(name="high", priority="high")
    assert eng._should_notify(low) is False
    assert eng._should_notify(high) is True


def test_in_meeting_suppresses_non_high(tmp_path, monkeypatch):
    eng, _ = _engine(tmp_path)
    # Force the context engine to report "in_meeting".
    monkeypatch.setattr(eng._ctx, "activity", lambda **_: "in_meeting")
    med = _spec(priority="medium")
    low = _spec(name="low", priority="low")
    high = _spec(name="high", priority="high")
    assert eng._should_notify(med) is False
    assert eng._should_notify(low) is False
    assert eng._should_notify(high) is True


# --- tick loop ------------------------------------------------------------ #

def test_tick_isolates_per_spec_exceptions(tmp_path):
    eng, sink = _engine(tmp_path)

    def boom(_eng):
        raise RuntimeError("kaboom")

    good_spec = _spec(name="good", message="ok")
    bad_spec = NotificationSpec(
        name="bad", priority="medium",
        cooldown_minutes=1, max_per_day=None, active_hours=None,
        check=boom,
    )
    # Inject both specs into the engine in deterministic order.
    eng._specs = (bad_spec, good_spec)
    eng.tick()
    # bad raised, good still delivered.
    assert sink.calls == [("ok", "medium")]


def test_handler_none_does_not_crash(tmp_path):
    eng, _ = _engine(tmp_path)
    eng.set_handler(None)  # type: ignore[arg-type]
    eng._deliver(_spec(), "hi")  # would crash if handler not None-checked


def test_check_returning_none_skips_delivery(tmp_path):
    eng, sink = _engine(tmp_path)
    eng._specs = (_spec(message=None),)
    eng.tick()
    assert sink.calls == []


def test_stub_logged_only_once(tmp_path, capsys):
    eng, _ = _engine(tmp_path)
    eng._log_stub_once("forgotten_task", "no source")
    eng._log_stub_once("forgotten_task", "no source")
    out = capsys.readouterr().out
    # The "trigger registered but inactive" line should appear once.
    assert out.count("forgotten_task") == 1
