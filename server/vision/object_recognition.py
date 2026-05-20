"""Object / plant / food / animal / damage / style classifiers.

All paths funnel through Claude Vision. The prompts are tuned per
subject so the model commits to one schema (plants want care tips,
food wants calories, damage wants severity grading) instead of
returning a wishy-washy general description.

The optional ``scan_barcode_qr`` method uses ``pyzbar`` for local
decoding — that lets us hand back a clean numeric code without
spending a Claude call. ``pyzbar`` needs the native ``libzbar``
library installed (``brew install zbar`` on macOS); if it isn't
available the method gracefully returns ``None`` instead of crashing
the whole module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vision_manager import VisionManager


@dataclass
class RecognitionResult:
    """Generic recognition result for the per-subject classifiers.

    ``summary`` is always populated and speakable. ``details`` carries
    any additional structured fields the prompt extracted (calories,
    species name, severity, etc.); empty for the general identify().
    """
    category: str               # "object", "plant", "food", "animal", "damage", "style"
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BarcodeResult:
    """Outcome of ``scan_barcode_qr``.

    ``symbology`` is the pyzbar-reported barcode type (EAN13, QRCODE, …)
    — useful for picking the right product-lookup API in a future
    integration. ``product`` is reserved for Phase 3 when the Open Food
    Facts client is added; for now it stays ``None``.
    """
    code: str
    symbology: str
    product: dict[str, Any] | None = None


# Per-subject prompts. Each one tells Claude the schema we want so the
# free-form description doesn't drift into trivia.
_PROMPTS: dict[str, str] = {
    "identify": (
        "Was ist auf diesem Bild zu sehen? Beschreibe konkret:\n"
        "- Hauptmotiv (Objekt, Person, Szene)\n"
        "- Marke / Modell, falls erkennbar\n"
        "- Zustand (neu / gebraucht / beschädigt)\n"
        "- Kontext oder Umgebung\n"
        "- Sichtbarer Text\n"
        "Antworte auf Deutsch, kompakt."
    ),
    "plant": (
        "Identifiziere die Pflanze auf diesem Bild. Antworte "
        "AUSSCHLIESSLICH mit JSON, ohne Markdown:\n"
        '{"common_name": "<deutscher Name>", '
        '"scientific_name": "<lateinischer Name>", '
        '"health": "<gesund / erste Anzeichen / krank>", '
        '"care": "<kurze Pflegeanleitung>", '
        '"toxic_to_pets": "<ja/nein/unbekannt>"}'
    ),
    "food": (
        "Analysiere das Gericht oder Lebensmittel auf diesem Bild. "
        "Antworte AUSSCHLIESSLICH mit JSON, ohne Markdown:\n"
        '{"dish": "<Name des Gerichts>", '
        '"approx_calories": "<geschätzte Kalorien pro Portion>", '
        '"ingredients": ["<Zutat 1>", "<Zutat 2>"], '
        '"cuisine": "<Küchenstil>"}'
    ),
    "animal": (
        "Welches Tier ist auf diesem Bild? Antworte AUSSCHLIESSLICH "
        "mit JSON, ohne Markdown:\n"
        '{"species": "<deutscher Name>", '
        '"scientific_name": "<lateinischer Name>", '
        '"breed_or_type": "<Rasse falls Haustier, sonst leer>", '
        '"key_features": ["<Erkennungsmerkmal 1>", "<...>"], '
        '"fact": "<eine interessante Tatsache>"}'
    ),
    "damage": (
        "Beurteile sichtbare Schäden auf diesem Bild. Antworte "
        "AUSSCHLIESSLICH mit JSON, ohne Markdown:\n"
        '{"item": "<was ist beschädigt>", '
        '"damage": "<konkrete Beschreibung aller Schäden>", '
        '"severity": "<gering / mittel / schwer>", '
        '"repair_complexity": "<einfach / mittel / aufwendig>"}'
    ),
    "style": (
        "Gib eine Stilberatung zum Bild (Outfit, Raum, Design). "
        "Strukturiere:\n"
        "1. Was funktioniert gut\n"
        "2. Was könnte verbessert werden\n"
        "3. Konkrete Vorschläge\n"
        "Antworte auf Deutsch, kompakt."
    ),
}

# Subjects whose prompt returns JSON — drives the parse step.
_JSON_SUBJECTS: frozenset[str] = frozenset({
    "plant", "food", "animal", "damage",
})


class ObjectRecognizer:
    """Stateless wrapper around the per-subject Claude Vision prompts."""

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager

    # --- general identification --------------------------------------- #

    def identify(self, image_base64: str) -> RecognitionResult | None:
        """Free-form 'what is this' identification. Always returns a
        prose summary in ``RecognitionResult.summary``; the structured
        per-field methods below should be preferred when the user's
        intent is known."""
        return self._run("identify", "object", image_base64)

    def identify_plant(self, image_base64: str) -> RecognitionResult | None:
        return self._run("plant", "plant", image_base64)

    def identify_food(self, image_base64: str) -> RecognitionResult | None:
        return self._run("food", "food", image_base64)

    def identify_animal(self, image_base64: str) -> RecognitionResult | None:
        return self._run("animal", "animal", image_base64)

    def assess_damage(self, image_base64: str) -> RecognitionResult | None:
        return self._run("damage", "damage", image_base64)

    def style_advice(self, image_base64: str) -> RecognitionResult | None:
        return self._run("style", "style", image_base64)

    # --- local barcode/QR decoding ---------------------------------- #

    def scan_barcode_qr(self, image_base64: str) -> BarcodeResult | None:
        """Decode the first barcode/QR code found in the image using
        pyzbar (local — no Claude call). Returns ``None`` if:

        * pyzbar isn't installed (``brew install zbar; pip install pyzbar``)
        * no barcode is visible in the image
        * the base64 is malformed

        Product lookup (Open Food Facts etc.) is Phase 3 — for now
        :attr:`BarcodeResult.product` always stays ``None``.
        """
        if not image_base64:
            return None
        try:
            import pyzbar.pyzbar as pyzbar  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] pyzbar not available — `brew install zbar` "
                  f"and `pip install pyzbar`: {exc}")
            return None

        try:
            import base64 as _b64
            import io
            from PIL import Image
            img_bytes = _b64.b64decode(image_base64)
            img = Image.open(io.BytesIO(img_bytes))
            results = pyzbar.decode(img)
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] barcode decode failed: {exc}")
            return None

        if not results:
            return None
        first = results[0]
        try:
            code = first.data.decode("utf-8", errors="replace")
            symbology = first.type
        except Exception:  # noqa: BLE001
            return None
        return BarcodeResult(code=code, symbology=symbology, product=None)

    # --- internals --------------------------------------------------- #

    def _run(
        self,
        prompt_key: str,
        category: str,
        image_base64: str,
    ) -> RecognitionResult | None:
        if not image_base64:
            return None
        prompt = _PROMPTS.get(prompt_key)
        if prompt is None:
            return None
        raw = self._mgr.analyze_image(image_base64, prompt)
        if raw is None:
            return None

        if prompt_key in _JSON_SUBJECTS:
            parsed = _parse_json_dict(raw)
            summary = _summarise_recognition(category, parsed) if parsed else raw.strip()
            return RecognitionResult(
                category=category,
                summary=summary,
                details=parsed,
            )
        return RecognitionResult(
            category=category,
            summary=raw.strip(),
            details={},
        )


# --- shared parsers (module-level so tests can hit them) ---------------- #

def _parse_json_dict(text: str) -> dict[str, Any]:
    """Strip code fences and pull the first JSON object out. Returns
    ``{}`` if nothing parseable."""
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        import json
        obj = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}


def _summarise_recognition(category: str, data: dict[str, Any]) -> str:
    """One-line German summary suitable for TTS playback."""
    if category == "plant":
        name = data.get("common_name") or "unbekannte Pflanze"
        health = data.get("health") or "Zustand unklar"
        return f"{name}, {health}."
    if category == "food":
        dish = data.get("dish") or "unbekanntes Gericht"
        cal = data.get("approx_calories")
        return f"{dish}, etwa {cal} kcal." if cal else f"{dish}."
    if category == "animal":
        species = data.get("species") or "unbekanntes Tier"
        breed = data.get("breed_or_type")
        return f"{species}, {breed}." if breed else f"{species}."
    if category == "damage":
        item = data.get("item") or "Gegenstand"
        severity = data.get("severity") or "unbekannte Schwere"
        return f"{item}: Schaden, Schwere {severity}."
    return "Identifikation abgeschlossen."
