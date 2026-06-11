"""Tests for response preferences (memory/preferences.py) + brain routing."""
from __future__ import annotations

from pathlib import Path

from server.memory.preferences import Preferences


def test_defaults_render_empty(tmp_path: Path) -> None:
    p = Preferences(path=tmp_path / "p.json")
    assert p.as_prompt_block() == ""           # no bloat at defaults
    assert p.get("length") == "normal"


def test_set_and_render(tmp_path: Path) -> None:
    p = Preferences(path=tmp_path / "p.json")
    p.set("length", "kurz")
    p.set("tone", "förmlich")
    p.set("language", "en")
    block = p.as_prompt_block()
    assert "knapp" in block and "sieze" in block and "English" in block


def test_invalid_key_rejected(tmp_path: Path) -> None:
    p = Preferences(path=tmp_path / "p.json")
    assert p.set("nonsense", "x") is False


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    Preferences(path=path).set("length", "ausführlich")
    assert Preferences(path=path).get("length") == "ausführlich"


def test_brain_short_circuit_sets_preference(tmp_path: Path) -> None:
    import server.memory.preferences as pm
    pm.preferences = Preferences(path=tmp_path / "p.json")
    from server.brain import Brain
    b = Brain()
    assert "kürzer" in b._run_preference("bitte antworte kürzer").lower()
    assert pm.preferences.get("length") == "kurz"
    assert b._run_preference("antworte auf englisch") is not None
    assert pm.preferences.get("language") == "en"
    assert b._run_preference("wie ist das wetter") is None   # not a preference


def test_context_builder_includes_preferences(tmp_path: Path) -> None:
    import server.memory.preferences as pm
    pm.preferences = Preferences(path=tmp_path / "p.json")
    pm.preferences.set("length", "kurz")
    from server.memory.context_builder import ContextBuilder
    blocks = ContextBuilder().build_system_blocks("hallo")
    stable_text = blocks[0]["text"]
    assert "Präferenzen" in stable_text and "knapp" in stable_text
