"""Tests for the agentic Planner (intelligence/planner.py). No network:
calendar/weather facts are stubbed out so we exercise gather + synthesis.
"""
from __future__ import annotations

import asyncio

from server.intelligence.planner import Planner


class _Tasks:
    def get_top3(self):
        return [{"title": "Präsentation"}, {"title": "Arzt anrufen"}]

    def get_overdue(self):
        return [{"title": "Steuer"}]


class _Prod:
    tasks = _Tasks()


class _Exp:
    def month_total(self):
        return 1240.0

    def budget_status(self):
        return [{"category": "essen", "over": True}]


class _Fin:
    expenses = _Exp()


class _Block:
    type = "text"
    text = "Plan: erst die Präsentation, dann anrufen."


class _Resp:
    content = [_Block()]


class _Client:
    last_prompt = ""

    class messages:
        @staticmethod
        def create(**kw):
            _Client.last_prompt = kw["messages"][0]["content"]
            return _Resp()


def _planner(client=None, **kw):
    p = Planner(client=client, productivity=_Prod(), finance=_Fin(), **kw)
    # Stub the network/macOS sources so tests stay offline + deterministic.
    p._calendar_facts = lambda: ["Termine heute: 10:00 Meeting."]
    p._weather_facts = lambda: ["Wetter: sonnig, 21 Grad."]
    return p


def test_gather_collects_all_layers() -> None:
    facts = _planner()._gather()
    blob = " ".join(facts)
    assert "Meeting" in blob and "sonnig" in blob
    assert "Präsentation" in blob          # tasks
    assert "1240" in blob                   # finance


def test_plan_day_synthesises_via_claude() -> None:
    plan = asyncio.run(_planner(client=_Client()).plan_day())
    assert plan == "Plan: erst die Präsentation, dann anrufen."
    # The synthesis prompt must include the gathered facts.
    assert "Präsentation" in _Client.last_prompt
    assert "1240" in _Client.last_prompt


def test_plan_day_falls_back_without_client() -> None:
    plan = asyncio.run(_planner(client=None).plan_day())
    # No model → plain read-out of the facts (still useful).
    assert "Top-Aufgaben" in plan or "Termine heute" in plan


def test_empty_sources_safe() -> None:
    p = Planner(client=None)
    p._calendar_facts = lambda: []
    p._weather_facts = lambda: []
    assert "fehlen" in asyncio.run(p.plan_day()).lower()
