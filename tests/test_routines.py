"""Tests for server/intelligence/routines.py.

Every sub-section of each briefing degrades independently — we mock the
external data sources (calendar, weather, productivity) and verify that
the routines handle both happy-path and failure cases correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

_TZ = ZoneInfo("Europe/Berlin")


# ── helpers ───────────────────────────────────────────────────────────── #

def _cal_event(title: str, hours_from_now: float = 2.0,
               duration: float = 1.0, location: str = "") -> object:
    """Return a minimal CalendarEvent-like stub."""
    now = datetime.now(_TZ)
    start = now + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=duration)
    return SimpleNamespace(
        title=title, start=start, end=end,
        location=location, is_all_day=False,
    )


def _forecast(min_c: float, max_c: float, cond: str = "sonnig",
              precip: float = 0.0) -> object:
    return SimpleNamespace(
        temp_min_c=min_c, temp_max_c=max_c,
        condition=cond, precipitation_mm=precip,
    )


def _current_weather(temp: float = 18.0, cond: str = "sonnig",
                     precip: float = 0.0) -> object:
    return SimpleNamespace(
        temp_c=temp, condition=cond,
        precipitation_mm=precip, location_label="Berlin",
    )


# Patch targets — routines.py imports modules at the top and calls e.g.
# weather.get_current(), so we patch the attribute on the module object
# that routines.py holds a reference to.
_CAL_PATCH = "server.intelligence.routines.calendar_tool.get_today_events"
_WX_CURRENT = "server.intelligence.routines.weather.get_current"
_WX_FORECAST = "server.intelligence.routines.weather.get_forecast"
_CAL_EVENTS = "server.intelligence.routines.calendar_tool.get_events"


# ── morning_briefing ─────────────────────────────────────────────────── #

class TestMorningBriefing:
    def test_returns_string(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_PATCH, lambda: [])
        monkeypatch.setattr(_WX_CURRENT, lambda: None)
        from server.intelligence.routines import morning_briefing
        result = morning_briefing()
        assert isinstance(result, str)
        assert len(result) > 10

    def test_includes_event_title(self, monkeypatch) -> None:
        ev = _cal_event("Standup-Meeting")
        monkeypatch.setattr(_CAL_PATCH, lambda: [ev])
        monkeypatch.setattr(_WX_CURRENT, lambda: None)
        from server.intelligence.routines import morning_briefing
        assert "Standup-Meeting" in morning_briefing()

    def test_includes_weather(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_PATCH, lambda: [])
        monkeypatch.setattr(_WX_CURRENT,
                            lambda: _current_weather(22.0, "bewölkt"))
        from server.intelligence.routines import morning_briefing
        result = morning_briefing()
        assert "22" in result
        assert "bewölkt" in result

    def test_rain_sentence_included(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_PATCH, lambda: [])
        monkeypatch.setattr(_WX_CURRENT,
                            lambda: _current_weather(15.0, "Regen", precip=5.5))
        from server.intelligence.routines import morning_briefing
        result = morning_briefing()
        assert "5" in result  # precipitation_mm in output

    def test_calendar_crash_does_not_break_briefing(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_PATCH,
                            lambda: (_ for _ in ()).throw(RuntimeError("no cal")))
        monkeypatch.setattr(_WX_CURRENT, lambda: None)
        from server.intelligence.routines import morning_briefing
        result = morning_briefing()
        assert isinstance(result, str)

    def test_weather_crash_does_not_break_briefing(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_PATCH, lambda: [])
        monkeypatch.setattr(_WX_CURRENT,
                            lambda: (_ for _ in ()).throw(RuntimeError("no wx")))
        from server.intelligence.routines import morning_briefing
        result = morning_briefing()
        assert isinstance(result, str)


# ── evening_briefing ─────────────────────────────────────────────────── #

class TestEveningBriefing:
    def test_returns_string(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_EVENTS, lambda s, e: [])
        monkeypatch.setattr(_WX_FORECAST, lambda days=2: [])
        from server.intelligence.routines import evening_briefing
        result = evening_briefing()
        assert isinstance(result, str)
        assert "Feierabend" in result

    def test_tomorrow_event_included(self, monkeypatch) -> None:
        ev = _cal_event("Arzttermin", hours_from_now=24)
        monkeypatch.setattr(_CAL_EVENTS, lambda s, e: [ev])
        monkeypatch.setattr(_WX_FORECAST, lambda days=2: [])
        from server.intelligence.routines import evening_briefing
        assert "Arzttermin" in evening_briefing()

    def test_no_tomorrow_events(self, monkeypatch) -> None:
        monkeypatch.setattr(_CAL_EVENTS, lambda s, e: [])
        monkeypatch.setattr(_WX_FORECAST, lambda days=2: [])
        from server.intelligence.routines import evening_briefing
        result = evening_briefing()
        assert "frei" in result.lower() or "keine" in result.lower()

    def test_tomorrow_weather_included(self, monkeypatch) -> None:
        today_fc = _forecast(5, 12, "Regen")
        tomorrow_fc = _forecast(8, 18, "Sonnenschein")
        monkeypatch.setattr(_CAL_EVENTS, lambda s, e: [])
        monkeypatch.setattr(_WX_FORECAST,
                            lambda days=2: [today_fc, tomorrow_fc])
        from server.intelligence.routines import evening_briefing
        result = evening_briefing()
        assert "Sonnenschein" in result


# ── weekly_summary ────────────────────────────────────────────────────── #

class TestWeeklySummary:
    def test_returns_string(self) -> None:
        from server.intelligence.routines import weekly_summary
        result = weekly_summary()
        assert isinstance(result, str)
        assert "Wochenrückblick" in result

    def test_ends_with_weekend_wish(self) -> None:
        from server.intelligence.routines import weekly_summary
        result = weekly_summary()
        assert "Wochenende" in result

    def test_no_data_still_completes(self, monkeypatch) -> None:
        """Gracefully handles all DB errors."""
        monkeypatch.setattr(
            "server.productivity.task_manager.TaskManager.__init__",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no db")),
        )
        from server.intelligence.routines import weekly_summary
        result = weekly_summary()
        assert "Wochenrückblick" in result
        assert "Wochenende" in result
