"""Tests for the Phase 2 vision modules: document scanner, object
recognition (incl. barcode gating), and image comparator.

Same stub-client pattern as ``test_vision.py``: the Anthropic client
is replaced with a SimpleNamespace recording the last call and
returning a canned reply. Multi-image Claude calls (comparator) are
covered by inspecting the recorded payload — we never hit the real
endpoint.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.vision import (
    DocumentResult,
    DocumentScanner,
    ImageComparator,
    ObjectRecognizer,
    RecognitionResult,
    VisionManager,
)
from server.vision.comparator import ComparisonResult


class _StubClient:
    """Records the most recent .messages.create() call and returns a
    queue of canned text replies (so multi-step flows like auto-
    classification can be exercised end-to-end)."""

    def __init__(self, replies: list[str] | str) -> None:
        self.messages = SimpleNamespace(create=self._create)
        self._queue: list[str] = [replies] if isinstance(replies, str) else list(replies)
        self.calls: list[dict] = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._queue.pop(0) if self._queue else ""
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply)],
        )


def _mgr(replies) -> VisionManager:
    return VisionManager(client=_StubClient(replies))


# ============================================================ #
# DocumentScanner                                              #
# ============================================================ #

def test_document_scanner_receipt_parses_json():
    raw = (
        '{"store": "REWE", "date": "2026-05-20", "total": "23.50 EUR", '
        '"items": [{"name": "Apfel", "price": "1.99"}], '
        '"payment_method": "Karte"}'
    )
    mgr = _mgr(raw)
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="receipt")
    assert isinstance(result, DocumentResult)
    assert result.doc_type == "receipt"
    assert result.structured_data["store"] == "REWE"
    assert result.structured_data["total"] == "23.50 EUR"
    assert len(result.structured_data["items"]) == 1
    # Summary mentions the store + total for the speakable reply.
    assert "REWE" in result.summary
    assert "23.50" in result.summary


def test_document_scanner_business_card_extracts_fields():
    raw = (
        '{"name": "Maria Mustermann", "title": "CTO", '
        '"company": "Acme GmbH", "phone": "+49 30 1234567", '
        '"email": "maria@acme.de", "website": "", "address": ""}'
    )
    mgr = _mgr(raw)
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="business_card")
    assert result is not None
    assert result.structured_data["name"] == "Maria Mustermann"
    assert result.structured_data["email"] == "maria@acme.de"
    assert "Maria Mustermann" in result.summary
    assert "Acme GmbH" in result.summary


def test_document_scanner_contract_extracts_action_items():
    raw = (
        "Vertragstyp: Mietvertrag\n"
        "Parteien: Mieter A, Vermieter B\n"
        "Wichtige Daten: Kündigungsfrist 31.12.2026\n"
        "Action Items:\n"
        "1. Mietkaution überweisen\n"
        "2. Übergabeprotokoll unterschreiben\n"
        "- Schlüsselübergabe vereinbaren"
    )
    mgr = _mgr(raw)
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="contract")
    assert result is not None
    assert result.structured_data == {}
    # The bullet/numbered lines should land in action_items, marker removed.
    assert "Mietkaution überweisen" in result.action_items
    assert "Übergabeprotokoll unterschreiben" in result.action_items
    assert "Schlüsselübergabe vereinbaren" in result.action_items


def test_document_scanner_auto_classify_routes_to_typed_prompt():
    # First reply = classification ("receipt"), second = JSON payload.
    mgr = _mgr([
        "receipt",
        '{"store": "ALDI", "date": "2026-05-19", "total": "12.34", "items": [], "payment_method": ""}',
    ])
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="auto")
    assert result is not None
    assert result.doc_type == "receipt"
    assert result.structured_data["store"] == "ALDI"
    # Two API calls: classify + extract.
    assert len(mgr._client.calls) == 2  # type: ignore[attr-defined]


def test_document_scanner_auto_classify_falls_back_to_general():
    # Unknown classification → should land on 'general'.
    mgr = _mgr(["unverständliches_wort", "Allgemeine Beschreibung des Dokuments."])
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="auto")
    assert result is not None
    assert result.doc_type == "general"
    assert "Allgemeine Beschreibung" in result.summary


def test_document_scanner_unknown_type_normalises_to_general():
    mgr = _mgr("Beschreibung.")
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="something_weird")
    assert result is not None
    assert result.doc_type == "general"


def test_document_scanner_api_failure_returns_none():
    mgr = _mgr([])  # empty queue → empty reply → analyze_image returns None
    # Force analyze_image to fail completely.
    mgr._client.messages.create = lambda **_: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        RuntimeError("boom"),
    )
    assert mgr.scanner.scan_document("ZmFrZQ==", doc_type="receipt") is None


def test_document_scanner_extract_table_returns_csv_string():
    csv = "Name,Wert\nAlpha,1\nBeta,2"
    mgr = _mgr(csv)
    out = mgr.scanner.extract_table("ZmFrZQ==")
    assert out == csv


def test_document_scanner_strips_markdown_fence_in_json_reply():
    raw = '```json\n{"store": "X", "date": "", "total": "", "items": [], "payment_method": ""}\n```'
    mgr = _mgr(raw)
    result = mgr.scanner.scan_document("ZmFrZQ==", doc_type="receipt")
    assert result is not None
    assert result.structured_data["store"] == "X"


def test_document_scanner_handles_empty_base64():
    mgr = _mgr("never used")
    assert mgr.scanner.scan_document("", doc_type="receipt") is None


# ============================================================ #
# ObjectRecognizer                                             #
# ============================================================ #

def test_recognizer_identify_returns_summary():
    mgr = _mgr("Eine grüne Tasse aus Keramik auf einem Holztisch.")
    result = mgr.recognizer.identify("ZmFrZQ==")
    assert isinstance(result, RecognitionResult)
    assert result.category == "object"
    assert "Tasse" in result.summary
    assert result.details == {}  # general identify has no structured fields


def test_recognizer_plant_parses_json_and_summarises():
    raw = (
        '{"common_name": "Monstera deliciosa", '
        '"scientific_name": "Monstera deliciosa", '
        '"health": "gesund", "care": "wöchentlich gießen", '
        '"toxic_to_pets": "ja"}'
    )
    mgr = _mgr(raw)
    result = mgr.recognizer.identify_plant("ZmFrZQ==")
    assert result is not None
    assert result.category == "plant"
    assert result.details["common_name"] == "Monstera deliciosa"
    assert result.details["toxic_to_pets"] == "ja"
    assert "Monstera deliciosa" in result.summary
    assert "gesund" in result.summary


def test_recognizer_food_summary_includes_calories():
    raw = (
        '{"dish": "Wiener Schnitzel", "approx_calories": "850", '
        '"ingredients": ["Kalb", "Panade"], "cuisine": "österreichisch"}'
    )
    mgr = _mgr(raw)
    result = mgr.recognizer.identify_food("ZmFrZQ==")
    assert result is not None
    assert "Wiener Schnitzel" in result.summary
    assert "850" in result.summary


def test_recognizer_animal_breed_appended_when_present():
    raw = (
        '{"species": "Hund", "scientific_name": "Canis lupus familiaris", '
        '"breed_or_type": "Labrador Retriever", '
        '"key_features": ["hängende Ohren"], "fact": "guter Schwimmer"}'
    )
    mgr = _mgr(raw)
    result = mgr.recognizer.identify_animal("ZmFrZQ==")
    assert result is not None
    assert "Hund" in result.summary
    assert "Labrador" in result.summary


def test_recognizer_damage_summary_reports_severity():
    raw = (
        '{"item": "Frontstoßstange", "damage": "Risse und Kratzer", '
        '"severity": "mittel", "repair_complexity": "mittel"}'
    )
    mgr = _mgr(raw)
    result = mgr.recognizer.assess_damage("ZmFrZQ==")
    assert result is not None
    assert "Frontstoßstange" in result.summary
    assert "mittel" in result.summary


def test_recognizer_style_advice_returns_prose():
    raw = "Was gut funktioniert: die Farbpalette. Was zu verbessern: …"
    mgr = _mgr(raw)
    result = mgr.recognizer.style_advice("ZmFrZQ==")
    assert result is not None
    assert result.category == "style"
    assert result.details == {}
    assert "Farbpalette" in result.summary


def test_recognizer_empty_base64_returns_none():
    mgr = _mgr("ignored")
    assert mgr.recognizer.identify("") is None
    assert mgr.recognizer.identify_plant("") is None


def test_recognizer_barcode_missing_pyzbar_returns_none(monkeypatch):
    mgr = _mgr("ignored")
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pyzbar"):
            raise ImportError("simulated pyzbar missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert mgr.recognizer.scan_barcode_qr("ZmFrZQ==") is None


# ============================================================ #
# ImageComparator                                              #
# ============================================================ #

def test_comparator_parses_structured_reply():
    raw = (
        "ZUSAMMENFASSUNG: Im zweiten Bild ist ein zusätzliches Fenster geöffnet.\n"
        "UNTERSCHIEDE:\n"
        "- Neues Safari-Fenster oben rechts\n"
        "- Benachrichtigung in der Menüleiste\n"
        "BEDEUTUNG: mittel"
    )
    mgr = _mgr(raw)
    result = mgr.comparator.compare(
        "ZmFrZTE=", "ZmFrZTI=", context="screen",
    )
    assert isinstance(result, ComparisonResult)
    assert result.context == "screen"
    assert "zusätzliches Fenster" in result.summary
    assert len(result.differences) == 2
    assert "Safari-Fenster" in result.differences[0]
    assert result.significance == "moderate"


def test_comparator_unknown_context_falls_back_to_general():
    mgr = _mgr("ZUSAMMENFASSUNG: Test.")
    result = mgr.comparator.compare("a", "b", context="weirdmode")
    assert result is not None
    assert result.context == "general"


def test_comparator_sends_two_image_blocks():
    mgr = _mgr("ZUSAMMENFASSUNG: x.")
    mgr.comparator.compare("img1", "img2", context="general")
    sent = mgr._client.calls[-1]  # type: ignore[attr-defined]
    content = sent["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert types == ["image", "image", "text"]
    assert content[0]["source"]["data"] == "img1"
    assert content[1]["source"]["data"] == "img2"


def test_comparator_missing_image_returns_none():
    mgr = _mgr("ZUSAMMENFASSUNG: x.")
    assert mgr.comparator.compare("", "b") is None
    assert mgr.comparator.compare("a", "") is None


def test_comparator_significance_normalises_german_labels():
    raw_major = "ZUSAMMENFASSUNG: A.\nUNTERSCHIEDE:\nBEDEUTUNG: groß"
    raw_minor = "ZUSAMMENFASSUNG: A.\nUNTERSCHIEDE:\nBEDEUTUNG: gering"
    mgr = _mgr([raw_major, raw_minor])
    r1 = mgr.comparator.compare("a", "b")
    r2 = mgr.comparator.compare("a", "b")
    assert r1 is not None and r1.significance == "major"
    assert r2 is not None and r2.significance == "minor"


def test_comparator_unstructured_reply_lands_in_summary():
    mgr = _mgr("Einfach nur Fließtext ohne die Format-Header.")
    result = mgr.comparator.compare("a", "b")
    assert result is not None
    assert "Fließtext" in result.summary
    assert result.differences == []
    assert result.significance == "moderate"


def test_comparator_compare_with_snapshot_returns_none_without_snapshot():
    mgr = _mgr("ignored")
    assert mgr.comparator.compare_with_snapshot() is None


def test_comparator_snapshot_screen_handles_capture_failure(monkeypatch):
    mgr = _mgr("ignored")
    monkeypatch.setattr(mgr.screen, "capture_screen", lambda *a, **kw: None)
    assert mgr.comparator.snapshot_screen() is False
    # Snapshot stays empty.
    assert mgr.comparator._screen_snapshot is None  # noqa: SLF001


def test_comparator_snapshot_screen_round_trip(monkeypatch):
    mgr = _mgr("ZUSAMMENFASSUNG: nichts.")
    monkeypatch.setattr(mgr.screen, "capture_screen", lambda *a, **kw: "ZmFrZQ==")
    assert mgr.comparator.snapshot_screen() is True
    result = mgr.comparator.compare_with_snapshot()
    assert result is not None
    # After a successful compare, the snapshot should be the LATEST
    # capture so subsequent calls describe rolling changes.
    assert mgr.comparator._screen_snapshot == "ZmFrZQ=="  # noqa: SLF001
