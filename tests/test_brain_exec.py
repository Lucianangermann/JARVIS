"""Tests for brain_exec mixins — each exec method tested in isolation.

Brain() degrades gracefully when optional deps are absent, so we can
instantiate it directly and inject minimal stubs for the attributes each
method needs.
"""
from __future__ import annotations

import pytest

from server.brain import Brain


@pytest.fixture(scope="module")
def brain() -> Brain:
    return Brain()


# ── AppleAppsExecMixin ───────────────────────────────────────────────────── #

def test_macos_app_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_macos_app({"action": "fly", "app_name": "Safari"})
    assert err

def test_macos_app_missing_app_name(brain: Brain) -> None:
    msg, err = brain._exec_macos_app({"action": "open"})
    assert err
    assert "app_name" in msg

def test_apple_reminders_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_apple_reminders({"action": "zap"})
    assert err
    assert "zap" in msg

def test_apple_reminders_create_missing_title(brain: Brain) -> None:
    msg, err = brain._exec_apple_reminders({"action": "create"})
    assert err
    assert "title" in msg

def test_apple_music_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_apple_music({"action": "rewind"})
    assert err

def test_apple_music_volume_missing_level(brain: Brain) -> None:
    msg, err = brain._exec_apple_music({"action": "volume"})
    assert err
    assert "level" in msg

def test_apple_notes_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_apple_notes({"action": "shred"})
    assert err

def test_apple_notes_read_missing_title(brain: Brain) -> None:
    msg, err = brain._exec_apple_notes({"action": "read"})
    assert err
    assert "title" in msg

def test_get_calendar_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_get_calendar({"action": "guess"})
    assert err
    assert "guess" in msg


# ── CommunicationExecMixin ───────────────────────────────────────────────── #

def test_send_imessage_no_communication(brain: Brain) -> None:
    """Without _communication attached, returns a clear error."""
    brain._communication = None  # type: ignore[attr-defined]
    msg, err = brain._exec_send_imessage({"to": "test", "message": "hi"})
    assert err
    assert "nicht verfügbar" in msg

def test_send_imessage_missing_to(brain: Brain) -> None:
    brain._communication = None  # type: ignore[attr-defined]
    msg, err = brain._exec_send_imessage({"message": "hi"})
    assert err

def test_apple_mail_unknown_action(brain: Brain) -> None:
    msg, err = brain._exec_apple_mail({"action": "shred"})
    assert err
    assert "shred" in msg

def test_apple_mail_read_missing_subject(brain: Brain) -> None:
    msg, err = brain._exec_apple_mail({"action": "read"})
    assert err
    assert "subject" in msg

def test_apple_mail_send_missing_fields(brain: Brain) -> None:
    msg, err = brain._exec_apple_mail({"action": "send", "body": "hi"})
    assert err
    assert "subject" in msg or "to" in msg


# ── SmartHomeFinanceExecMixin ────────────────────────────────────────────── #

def test_exec_finance_unknown_action(brain: Brain) -> None:
    brain._finance = None  # type: ignore[attr-defined]

    class _FakeExpenses:
        def budget_status(self): return []
        def add_expense(self, *a, **kw): return {"ok": False, "spoken": "err"}
        def spoken_month_summary(self): return "0"
        def set_budget(self, c, a): return "ok"

    class _FakeFinance:
        expenses = _FakeExpenses()

    brain._finance = _FakeFinance()  # type: ignore[attr-defined]
    try:
        msg, err = brain._exec_finance({"action": "blorp"})
        assert err
        assert "blorp" in msg
    finally:
        brain._finance = None  # type: ignore[attr-defined]

def test_exec_finance_budget_status_empty(brain: Brain) -> None:
    class _FakeExpenses:
        def budget_status(self): return []

    class _FakeFinance:
        expenses = _FakeExpenses()

    brain._finance = _FakeFinance()  # type: ignore[attr-defined]
    try:
        msg, err = brain._exec_finance({"action": "budget_status"})
        assert not err
        assert "Keine" in msg
    finally:
        brain._finance = None  # type: ignore[attr-defined]

def test_exec_finance_set_budget_missing_category(brain: Brain) -> None:
    class _FakeExpenses:
        def set_budget(self, c, a): return "ok"

    class _FakeFinance:
        expenses = _FakeExpenses()

    brain._finance = _FakeFinance()  # type: ignore[attr-defined]
    try:
        msg, err = brain._exec_finance({"action": "set_budget", "amount": 100})
        assert err
        assert "category" in msg
    finally:
        brain._finance = None  # type: ignore[attr-defined]

def test_exec_finance_no_finance_manager(brain: Brain) -> None:
    """When finance layer fails to init, returns a graceful error."""
    brain._finance = None  # type: ignore[attr-defined]
    # Patch _get_finance to return None without trying to open a DB file
    original = brain._get_finance
    brain._get_finance = lambda: None  # type: ignore[method-assign]
    try:
        msg, err = brain._exec_finance({"action": "summary"})
        assert err
        assert "nicht verfügbar" in msg
    finally:
        brain._get_finance = original  # type: ignore[method-assign]


# ── KnowledgeExecMixin ───────────────────────────────────────────────────── #

def test_exec_track_learning_mark_missing_subject(brain: Brain) -> None:
    """mark action requires subject."""
    msg, err = brain._exec_track_learning({"action": "mark", "status": "offen"})
    assert err
    assert "subject" in msg

def test_exec_track_learning_status_action(brain: Brain) -> None:
    """Default status action returns a string without crashing."""
    msg, err = brain._exec_track_learning({"action": "status"})
    assert isinstance(msg, str)
    assert isinstance(err, bool)

def test_exec_track_task_missing_name(brain: Brain) -> None:
    msg, err = brain._exec_track_task({})
    assert err
    assert "name" in msg or "title" in msg
