"""Tests for the vision layer (Phase 1).

We deliberately do not call the real Claude Vision endpoint here —
the Anthropic client is replaced with a stub that records the
payload it would have sent and returns a canned response. That keeps
the suite fast, offline-runnable, and stable when Anthropic ships a
new model version.

mss / Screen Recording is mocked similarly for ScreenReader tests:
the real ``mss.mss()`` call would either need a display or raise on
CI.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from server.vision import OCR, ScreenReader, VisionManager
from server.vision.ocr import TranslationResult


# --- shared fixtures ------------------------------------------------------ #

class _StubClient:
    """Records the last messages.create() call and returns a configured
    text reply. Public attributes mirror the bits VisionManager touches."""

    def __init__(self, reply: str = "stubbed reply") -> None:
        self.messages = SimpleNamespace(create=self._create)
        self._reply = reply
        self.last_kwargs: dict | None = None

    def set_reply(self, reply: str) -> None:
        self._reply = reply

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._reply)],
        )


@pytest.fixture()
def manager() -> VisionManager:
    return VisionManager(client=_StubClient())


def _png_bytes(size=(64, 48), color=(120, 200, 80)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- image_to_base64 ------------------------------------------------------ #

def test_image_to_base64_accepts_bytes(manager):
    b64 = manager.image_to_base64(_png_bytes())
    assert b64 is not None
    raw = base64.b64decode(b64)
    # JPEG SOI marker — confirms we re-encoded to JPEG.
    assert raw.startswith(b"\xff\xd8\xff")


def test_image_to_base64_accepts_pil_image(manager):
    img = Image.new("RGB", (32, 32), (255, 0, 0))
    b64 = manager.image_to_base64(img)
    assert b64 is not None
    assert base64.b64decode(b64).startswith(b"\xff\xd8\xff")


def test_image_to_base64_accepts_path(tmp_path, manager):
    p = tmp_path / "test.png"
    p.write_bytes(_png_bytes())
    b64 = manager.image_to_base64(p)
    assert b64 is not None


def test_image_to_base64_missing_path_returns_none(manager, tmp_path):
    assert manager.image_to_base64(tmp_path / "nope.png") is None


def test_image_to_base64_unsupported_type_returns_none(manager):
    assert manager.image_to_base64(12345) is None


def test_image_to_base64_resizes_oversized_image(manager):
    # 4000-wide image should come back resized to ≤1920 on the long edge.
    big = Image.new("RGB", (4000, 2000), (50, 50, 50))
    b64 = manager.image_to_base64(big)
    assert b64 is not None
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert max(decoded.size) <= 1920


def test_image_to_base64_handles_rgba(manager):
    # RGBA inputs (with alpha) used to crash the JPEG encoder
    # before the explicit `.convert('RGB')` step.
    img = Image.new("RGBA", (64, 64), (200, 100, 50, 128))
    b64 = manager.image_to_base64(img)
    assert b64 is not None


# --- analyze_image -------------------------------------------------------- #

def test_analyze_image_returns_model_text(manager):
    manager._client.set_reply("eine rote Box")  # type: ignore[attr-defined]
    out = manager.analyze_image("ZmFrZQ==", "Was siehst du?")
    assert out == "eine rote Box"


def test_analyze_image_includes_image_and_text_blocks(manager):
    manager.analyze_image("ZmFrZQ==", "lies das")
    sent = manager._client.last_kwargs  # type: ignore[attr-defined]
    assert sent is not None
    content = sent["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert types == ["image", "text"]
    assert content[0]["source"]["data"] == "ZmFrZQ=="
    assert content[1]["text"] == "lies das"


def test_analyze_image_returns_none_on_api_failure(manager):
    def _boom(**_):
        raise RuntimeError("api down")
    manager._client.messages.create = _boom  # type: ignore[attr-defined]
    assert manager.analyze_image("ZmFrZQ==", "x") is None


def test_analyze_image_returns_none_on_empty_inputs(manager):
    assert manager.analyze_image("", "prompt") is None
    assert manager.analyze_image("data", "") is None


# --- ScreenReader (without touching the real screen) --------------------- #

def test_screen_reader_capture_handles_missing_mss(monkeypatch, manager):
    # Force the import inside capture_screen to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mss":
            raise ImportError("simulated: mss not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert manager.screen.capture_screen() is None


def test_screen_reader_analyze_screen_uses_preset(manager, monkeypatch):
    # Pretend the capture succeeded; verify the preset prompt text
    # flows through to the model.
    monkeypatch.setattr(
        manager.screen, "capture_screen", lambda *a, **kw: "ZmFrZQ==",
    )
    manager._client.set_reply("kein Fehler erkennbar")  # type: ignore[attr-defined]
    out = manager.screen.analyze_screen("error")
    assert out == "kein Fehler erkennbar"
    sent = manager._client.last_kwargs  # type: ignore[attr-defined]
    prompt = sent["messages"][0]["content"][1]["text"]
    # Preset prompt mentions "Fehler" — confirms we substituted from
    # _PROMPT_PRESETS["error"] rather than passing the raw key.
    assert "Fehler" in prompt


def test_screen_reader_free_form_question_passes_through(manager, monkeypatch):
    monkeypatch.setattr(
        manager.screen, "capture_screen", lambda *a, **kw: "ZmFrZQ==",
    )
    manager.screen.analyze_screen("ist das eine Katze?")
    prompt = manager._client.last_kwargs["messages"][0]["content"][1]["text"]
    assert "Katze" in prompt


def test_screen_reader_capture_failure_yields_none(manager, monkeypatch):
    monkeypatch.setattr(
        manager.screen, "capture_screen", lambda *a, **kw: None,
    )
    assert manager.screen.analyze_screen("describe") is None


def test_is_all_black_detects_black_image(manager):
    black = Image.new("RGB", (32, 32), (0, 0, 0))
    coloured = Image.new("RGB", (32, 32), (50, 50, 50))
    assert ScreenReader._is_all_black(black) is True
    assert ScreenReader._is_all_black(coloured) is False


# --- OCR ----------------------------------------------------------------- #

def test_ocr_extract_text_returns_model_reply(manager):
    manager._client.set_reply("Hallo Welt\nZeile zwei")  # type: ignore[attr-defined]
    out = manager.ocr.extract_text("ZmFrZQ==")
    assert out == "Hallo Welt\nZeile zwei"


def test_ocr_extract_text_handles_empty_input(manager):
    assert manager.ocr.extract_text("") is None


def test_ocr_extract_text_with_language_hint_includes_label(manager):
    manager.ocr.extract_text("ZmFrZQ==", language="en")
    prompt = manager._client.last_kwargs["messages"][0]["content"][1]["text"]
    assert "English" in prompt


def test_ocr_translate_parses_json_reply(manager):
    manager._client.set_reply(  # type: ignore[attr-defined]
        '{"original": "Hello world", "translated": "Hallo Welt"}'
    )
    result = manager.ocr.extract_and_translate(
        "ZmFrZQ==", target_language="de",
    )
    assert isinstance(result, TranslationResult)
    assert result.original == "Hello world"
    assert result.translated == "Hallo Welt"
    assert result.target_language == "de"


def test_ocr_translate_strips_markdown_code_fence(manager):
    manager._client.set_reply(  # type: ignore[attr-defined]
        '```json\n{"original": "A", "translated": "B"}\n```'
    )
    result = manager.ocr.extract_and_translate("ZmFrZQ==")
    assert result is not None
    assert result.original == "A"
    assert result.translated == "B"


def test_ocr_translate_falls_back_to_raw_on_unparseable_json(manager):
    manager._client.set_reply("just plain text, no JSON here")  # type: ignore[attr-defined]
    result = manager.ocr.extract_and_translate("ZmFrZQ==")
    # Caller still gets a result so the model effort isn't wasted —
    # the raw reply lands in `original`, translated stays empty.
    assert result is not None
    assert result.original == "just plain text, no JSON here"
    assert result.translated == ""


def test_ocr_translate_empty_json_returns_none(manager):
    manager._client.set_reply(  # type: ignore[attr-defined]
        '{"original": "", "translated": ""}'
    )
    assert manager.ocr.extract_and_translate("ZmFrZQ==") is None
