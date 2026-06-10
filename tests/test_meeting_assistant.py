"""Tests for the meeting assistant (record → summarise → action items → tasks).

The Claude client is mocked, so no network. The valuable core
(process_transcript) is fully exercised; live mic recording degrades
gracefully and is only state-checked.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.productivity.task_manager import TaskManager
from server.productivity.meeting_assistant import MeetingAssistant


def _run(coro):
    return asyncio.run(coro)


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


def _client(json_text: str):
    class C:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp(json_text)
    return C()


_FENCED = ('```json\n{"summary":"Kurze Zusammenfassung.",'
           '"action_items":["A erledigen","B anrufen"],'
           '"decisions":["Entscheidung X"]}\n```')


@pytest.fixture()
def tasks(tmp_path: Path) -> TaskManager:
    return TaskManager(tmp_path / "jarvis.db")


def test_process_transcript_creates_tasks(tasks: TaskManager, monkeypatch) -> None:
    # Don't touch Apple Notes in tests.
    import server.productivity.meeting_assistant as mod
    monkeypatch.setattr(mod.MeetingAssistant, "_save_note",
                        lambda *a, **k: None)
    ma = MeetingAssistant(task_manager=tasks, client=_client(_FENCED))
    r = _run(ma.process_transcript("transcript", title="Sync"))
    assert r["ok"]
    assert r["summary"] == "Kurze Zusammenfassung."
    assert r["action_items"] == ["A erledigen", "B anrufen"]
    assert r["decisions"] == ["Entscheidung X"]
    assert r["tasks_created"] == 2
    today = tasks.get_today_tasks()
    assert len(today) == 2
    assert all(t["tags"] == "meeting" for t in today)


def test_process_transcript_plain_json(tasks: TaskManager, monkeypatch) -> None:
    import server.productivity.meeting_assistant as mod
    monkeypatch.setattr(mod.MeetingAssistant, "_save_note", lambda *a, **k: None)
    plain = '{"summary":"S","action_items":[],"decisions":[]}'
    ma = MeetingAssistant(task_manager=tasks, client=_client(plain))
    r = _run(ma.process_transcript("x"))
    assert r["ok"] and r["tasks_created"] == 0
    assert "Keine Action Items" in r["spoken"]


def test_empty_transcript_handled(tasks: TaskManager) -> None:
    ma = MeetingAssistant(task_manager=tasks, client=_client(_FENCED))
    r = _run(ma.process_transcript(""))
    assert r["ok"] is False


def test_summarise_without_client_is_safe(tasks: TaskManager, monkeypatch) -> None:
    import server.productivity.meeting_assistant as mod
    monkeypatch.setattr(mod.MeetingAssistant, "_save_note", lambda *a, **k: None)
    ma = MeetingAssistant(task_manager=tasks, client=None)
    r = _run(ma.process_transcript("some text"))
    # No client → no summary/items, but never raises.
    assert r["ok"] and r["tasks_created"] == 0


def test_end_meeting_without_recording(tasks: TaskManager) -> None:
    ma = MeetingAssistant(task_manager=tasks, client=_client(_FENCED))
    r = _run(ma.end_meeting())
    assert r["ok"] is False  # nothing recorded


def test_productivity_manager_wires_meeting(tmp_path: Path) -> None:
    from server.productivity.productivity_manager import ProductivityManager
    pm = ProductivityManager(tmp_path / "jarvis.db", client=_client(_FENCED))
    assert pm.meeting is not None
    assert pm.meeting.is_recording() is False
