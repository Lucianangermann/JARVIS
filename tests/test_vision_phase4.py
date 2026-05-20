"""Phase 4 integration tests: brain triggers + vision tool_use +
HTTP routes. The Anthropic client + VisionManager subcomponents are
stubbed so nothing leaves the test process.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.brain import (
    Brain,
    _vision_action_for,
    _VISION_TRIGGERS,
)


# --- shared vision stub --------------------------------------------------- #

class _StubVision:
    """A VisionManager-shaped stub with recordable subcomponents.

    Each subcomponent is a SimpleNamespace whose methods we override
    per test. Default behaviour: methods return a clearly-stub-flavoured
    string so an accidentally-unmocked call still results in a visible
    test failure rather than a silent ``None``.
    """

    def __init__(self) -> None:
        self.screen = SimpleNamespace(
            analyze_screen=lambda q: f"stub screen[{q}]",
            detect_error_on_screen=lambda: "stub error: none",
            capture_screen=lambda *a, **kw: "ZmFrZQ==",
        )
        self.scanner = SimpleNamespace(
            scan_document=lambda *a, **kw: SimpleNamespace(
                doc_type="receipt", summary="stub summary",
                structured_data={"store": "S"}, raw_text="raw",
                action_items=[], confidence=0.9,
            ),
        )
        self.translator = SimpleNamespace(
            translate_image=lambda image, target_language="de": SimpleNamespace(
                original="o", translated="t", target_language=target_language,
            ),
        )
        self.comparator = SimpleNamespace(
            snapshot_screen=lambda: True,
            compare_with_snapshot=lambda **kw: SimpleNamespace(
                summary="stub diff summary", differences=["a", "b"],
                significance="moderate", context="screen",
            ),
        )
        self.motion = SimpleNamespace(
            capture_once=lambda *a, **kw: "stub camera",
            start=lambda **kw: True,
            stop=lambda: None,
        )

    def analyze_image(self, image, prompt, **kw):
        return f"stub analyze[{prompt}]"


# --- _vision_action_for normalisation ----------------------------------- #

def test_vision_action_for_known_phrases():
    assert _vision_action_for("Was siehst du?") == "screen_describe"
    assert _vision_action_for("lies das.") == "screen_read"
    assert _vision_action_for("WAS HAT SICH VERÄNDERT") == "screen_compare"
    assert _vision_action_for("watch the door") == "motion_start"
    assert _vision_action_for("stop watching") == "motion_stop"


def test_vision_action_for_unknown_returns_none():
    assert _vision_action_for("erzähl mir einen witz") is None
    assert _vision_action_for("") is None


def test_vision_trigger_map_has_no_duplicates_to_different_actions():
    """A single phrase mapping to two actions would be a bug —
    confirm the dict is shaped correctly (basic sanity)."""
    # dict can't hold duplicate keys to begin with, but we also want
    # to ensure no phrase only differs by punctuation/whitespace
    # since the matcher normalises both away.
    normalised = {k.lower().strip(".!?,").strip() for k in _VISION_TRIGGERS}
    assert len(normalised) == len(_VISION_TRIGGERS)


# --- brain short-circuit routing ---------------------------------------- #

@pytest.fixture()
def brain_with_stub_vision(monkeypatch) -> Brain:
    """Brain with vision attached but Claude / memory stubbed so the
    reply() path stays fast and offline."""
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    # We don't want reply() to go anywhere near the actual model when
    # the short-circuit MISSES. Make _stream_one_turn explode if it's
    # called so we notice routing bugs immediately.
    def _explode(*a, **kw):
        raise AssertionError("Claude path called — short-circuit should have returned")
    monkeypatch.setattr(b, "_stream_one_turn", _explode)
    # Disable history persistence side-effects for speed.
    b.memory.short_term.add = lambda *a, **kw: None  # type: ignore[attr-defined]
    return b


def test_brain_screen_describe_short_circuits(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "was siehst du auf meinem bildschirm",
        speak_locally=False,
    )
    assert out == "stub screen[describe]"


def test_brain_screen_error_uses_detect_helper(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "was ist das problem", speak_locally=False,
    )
    assert out == "stub error: none"


def test_brain_screen_read_uses_read_preset(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "lies das", speak_locally=False,
    )
    assert out == "stub screen[read]"


def test_brain_snapshot_returns_user_visible_confirmation(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "merk dir den bildschirm", speak_locally=False,
    )
    assert "gespeichert" in out


def test_brain_compare_formats_differences(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "was hat sich verändert", speak_locally=False,
    )
    # Should include the stub summary AND the bullet differences.
    assert "stub diff summary" in out
    assert "Konkret:" in out
    assert "a" in out and "b" in out


def test_brain_compare_without_snapshot_returns_hint(monkeypatch, brain_with_stub_vision):
    monkeypatch.setattr(
        brain_with_stub_vision.vision.comparator,
        "compare_with_snapshot",
        lambda **kw: None,
    )
    out = brain_with_stub_vision.reply(
        "tester", "was hat sich verändert", speak_locally=False,
    )
    assert "keinen gespeicherten Bildschirm" in out


def test_brain_motion_start_acks(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "beobachte die tür", speak_locally=False,
    )
    assert "Kamera" in out


def test_brain_motion_stop_acks(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "stop watching", speak_locally=False,
    )
    assert "beendet" in out


def test_brain_camera_snapshot_uses_capture_once(brain_with_stub_vision):
    out = brain_with_stub_vision.reply(
        "tester", "ist jemand da", speak_locally=False,
    )
    assert out == "stub camera"


def test_brain_falls_through_when_vision_action_unknown(monkeypatch):
    """A non-trigger phrase must reach _stream_one_turn — i.e. NOT
    be short-circuited."""
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    called = []

    def fake_stream(*a, **kw):
        called.append(True)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="claude reply")],
        )

    monkeypatch.setattr(b, "_stream_one_turn", fake_stream)
    b.memory.short_term.add = lambda *a, **kw: None  # type: ignore[attr-defined]

    out = b.reply("t", "erzähl mir einen witz", speak_locally=False)
    assert called  # Claude path was taken
    assert out == "claude reply"


def test_brain_vision_none_falls_through_to_claude(monkeypatch):
    """If self.vision is None the short-circuit must NOT eat the
    turn — it must reach Claude (otherwise minimal installs lose
    chat entirely on otherwise-vision-matching phrases)."""
    b = Brain()
    b.vision = None  # type: ignore[assignment]

    def fake_stream(*a, **kw):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="claude reply")],
        )

    monkeypatch.setattr(b, "_stream_one_turn", fake_stream)
    b.memory.short_term.add = lambda *a, **kw: None  # type: ignore[attr-defined]
    out = b.reply("t", "lies das", speak_locally=False)
    assert out == "claude reply"


def test_brain_vision_action_returning_none_falls_through(monkeypatch):
    """If the vision call fails (returns None) the brain should
    fall through to Claude rather than handing the user empty text."""
    b = Brain()
    stub = _StubVision()
    stub.screen.analyze_screen = lambda q: None  # type: ignore[assignment]
    b.vision = stub  # type: ignore[assignment]

    def fake_stream(*a, **kw):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="fallback reply")],
        )

    monkeypatch.setattr(b, "_stream_one_turn", fake_stream)
    b.memory.short_term.add = lambda *a, **kw: None  # type: ignore[attr-defined]
    out = b.reply("t", "was siehst du", speak_locally=False)
    assert out == "fallback reply"


# --- _exec_vision_tool (tool_use surface) ------------------------------- #

def test_exec_vision_tool_returns_screen_text():
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool(
        "analyze_screen", {"question": "wo ist die Fehlermeldung"},
    )
    assert is_error is False
    assert "stub screen" in result
    assert "Fehlermeldung" in result


def test_exec_vision_tool_check_errors_uses_detect_helper():
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool(
        "check_screen_for_errors", {},
    )
    assert is_error is False
    assert "stub error" in result


def test_exec_vision_tool_read_uses_read_preset():
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool("read_screen_text", {})
    assert is_error is False
    assert "stub screen[read]" == result


def test_exec_vision_tool_unknown_name_returns_error():
    b = Brain()
    b.vision = _StubVision()  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool("totally_made_up", {})
    assert is_error is True
    assert "Unknown" in result or "unknown" in result


def test_exec_vision_tool_no_vision_returns_error():
    b = Brain()
    b.vision = None  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool("analyze_screen", {"question": "x"})
    assert is_error is True
    assert "unavailable" in result


def test_exec_vision_tool_empty_result_is_error():
    b = Brain()
    stub = _StubVision()
    stub.screen.analyze_screen = lambda q: None  # type: ignore[assignment]
    b.vision = stub  # type: ignore[assignment]
    result, is_error = b._exec_vision_tool(
        "analyze_screen", {"question": "describe"},
    )
    assert is_error is True
    assert "no result" in result or "permission" in result


# --- API routes --------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch) -> TestClient:
    """FastAPI test client that injects a stub vision into app.state
    after startup so we don't need to wait for the real lifespan to
    finish initialising memory + intelligence."""
    # Skip the real lifespan entirely — we just need the route table
    # and the request flow. Patch the contextmanager to a no-op
    # before importing the app.
    import server.main as main_mod

    class _SkipLifespan:
        def __init__(self, app):
            self.app = app
        async def __aenter__(self):
            return None
        async def __aexit__(self, *a):
            return None

    # Replace the @asynccontextmanager-decorated lifespan with a
    # context manager that does nothing — the routes themselves
    # don't need any of the per-app-state init for these tests
    # because we'll inject stubs after the app starts.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _empty_lifespan(_app):
        # Attach what's needed for require_token to work.
        yield

    monkeypatch.setattr(main_mod, "lifespan", _empty_lifespan)
    # Re-build the app so our patched lifespan is the one in use.
    from fastapi import FastAPI
    app = FastAPI(title="JARVIS", lifespan=_empty_lifespan)
    # Add all original routes by importing main again — clunky but
    # avoids exposing yet more knobs from the real module.
    for route in main_mod.app.routes:
        app.routes.append(route)
    app.state.brain = SimpleNamespace(client=None, vision=_StubVision())
    app.state.vision = app.state.brain.vision
    return TestClient(app)


def _auth(monkeypatch):
    """Wire a known token into the settings + the require_token
    dependency so route tests can authenticate without the real
    auth setup."""
    from server.config import settings as s
    monkeypatch.setattr(s, "JARVIS_AUTH_TOKEN", "test-token")
    return {"Authorization": "Bearer test-token"}


def test_vision_analyze_route(client, monkeypatch):
    headers = _auth(monkeypatch)
    r = client.post(
        "/vision/analyze",
        headers=headers,
        json={"image": "ZmFrZQ==", "question": "was ist das"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "analyze"
    assert "stub analyze" in body["result"]


def test_vision_screen_route(client, monkeypatch):
    headers = _auth(monkeypatch)
    r = client.post(
        "/vision/screen", headers=headers,
        json={"question": "describe"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "screen"
    assert body["result"] == "stub screen[describe]"


def test_vision_scan_route_returns_structured_data(client, monkeypatch):
    headers = _auth(monkeypatch)
    r = client.post(
        "/vision/scan", headers=headers,
        json={"image": "ZmFrZQ==", "doc_type": "receipt"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_type"] == "receipt"
    assert body["structured_data"] == {"store": "S"}
    assert body["summary"] == "stub summary"


def test_vision_translate_route(client, monkeypatch):
    headers = _auth(monkeypatch)
    r = client.post(
        "/vision/translate", headers=headers,
        json={"image": "ZmFrZQ==", "target_language": "en"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original"] == "o"
    assert body["translated"] == "t"
    assert body["target_language"] == "en"


def test_vision_routes_403_without_token(client):
    # No Authorization header → 403/401 from require_token.
    r = client.post(
        "/vision/screen", json={"question": "describe"},
    )
    assert r.status_code in {401, 403}


def test_vision_routes_503_when_manager_missing(client, monkeypatch):
    headers = _auth(monkeypatch)
    # Detach the vision manager — routes should report unavailable.
    client.app.state.vision = None
    client.app.state.brain.vision = None
    r = client.post("/vision/screen", headers=headers,
                    json={"question": "describe"})
    assert r.status_code == 503
