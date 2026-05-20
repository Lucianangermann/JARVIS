"""Document-flavoured wrappers around Claude Vision.

Different document classes benefit from different prompt shapes:
a receipt wants line-items + totals as JSON, a contract wants prose
summary + action items, a business card wants strict field
extraction. ``DocumentScanner`` keeps that prompt catalogue in one
place so callers don't have to reinvent the wheel per surface (PWA
upload, brain trigger phrase, future API route).

Auto-detection
--------------
``scan_document(image, doc_type="auto")`` first asks Claude to
classify the document into one of the known types, then dispatches
to the type-specific prompt. That's two round-trips in the worst
case but keeps the prompt language tight per-type — a single mega-
prompt covering every document class hallucinates fields that don't
exist in the image. For latency-sensitive callers (PWA), passing the
explicit ``doc_type`` skips the classification step.

Result shape
------------
:class:`DocumentResult` is intentionally permissive: ``structured_data``
is whatever JSON Claude returned (or ``{}`` if the type uses prose
output), ``summary`` is always populated, ``action_items`` is empty
unless the type implies follow-ups. ``raw_text`` mirrors what an OCR
pass would produce so callers can fall back to plain transcription if
they don't care about structure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vision_manager import VisionManager


# Canonical type list. ``auto`` is the user-facing default and triggers
# the classification round-trip; everything else is dispatched directly.
KNOWN_DOC_TYPES: tuple[str, ...] = (
    "receipt", "invoice", "contract", "letter", "business_card",
    "form", "handwriting", "table", "id_document", "general",
)


@dataclass
class DocumentResult:
    """Outcome of one document scan. ``structured_data`` is the
    JSON-shaped extraction (or ``{}`` for prose-only doc types);
    ``raw_text`` is always populated (fallback to summary text if the
    type didn't include a transcription step)."""
    doc_type: str
    raw_text: str = ""
    structured_data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    action_items: list[str] = field(default_factory=list)
    confidence: float = 0.0


# Per-type prompts. Each prompt:
#  * tells Claude what kind of doc this is (cuts hallucination)
#  * specifies the output shape (JSON vs prose vs hybrid)
#  * asks for German output where the user is the consumer
#
# Prompts ending in JSON are parsed by ``_parse_json_or_blank``;
# everything else lands in ``DocumentResult.summary`` verbatim.
_PROMPTS: dict[str, str] = {
    "receipt": (
        "Das Bild zeigt einen Kassenbon oder eine Rechnung. Extrahiere "
        "die folgenden Felder. Antworte AUSSCHLIESSLICH mit gültigem "
        "JSON in genau dieser Form, ohne Markdown:\n"
        '{"store": "<Geschäft/Firma>", "date": "<YYYY-MM-DD oder leer>", '
        '"total": "<Gesamtbetrag inkl. Währung>", '
        '"items": [{"name": "<Artikel>", "price": "<Preis>"}], '
        '"payment_method": "<Zahlungsart oder leer>"}'
    ),
    "invoice": (
        "Das Bild zeigt eine Rechnung. Extrahiere die folgenden "
        "Felder. Antworte AUSSCHLIESSLICH mit JSON, ohne Markdown:\n"
        '{"vendor": "<Aussteller>", "invoice_number": "<Nummer>", '
        '"date": "<YYYY-MM-DD>", "due_date": "<YYYY-MM-DD oder leer>", '
        '"total": "<Gesamtbetrag>", '
        '"items": [{"description": "<Position>", "price": "<Preis>"}]}'
    ),
    "contract": (
        "Das Bild zeigt einen Vertrag oder ein offizielles Schreiben. "
        "Fasse den Inhalt zusammen und liste explizit auf:\n"
        "1. Vertragstyp / Dokumenttyp\n"
        "2. Beteiligte Parteien\n"
        "3. Hauptzweck oder Thema\n"
        "4. Wichtige Daten oder Fristen (mit Datum)\n"
        "5. Zentrale Pflichten und Bedingungen\n"
        "6. Action Items / nötige Schritte für den Empfänger\n"
        "Antworte auf Deutsch in Fließtext mit Aufzählungen."
    ),
    "letter": (
        "Das Bild zeigt einen Brief oder eine offizielle "
        "Mitteilung. Fasse zusammen: Absender, Empfänger, "
        "Hauptanliegen, Fristen, und konkrete Handlungsschritte für "
        "den Empfänger. Antworte auf Deutsch."
    ),
    "business_card": (
        "Das Bild zeigt eine Visitenkarte. Extrahiere die "
        "Kontaktdaten. Antworte AUSSCHLIESSLICH mit JSON, ohne "
        "Markdown:\n"
        '{"name": "<Vor- und Nachname>", "title": "<Position>", '
        '"company": "<Firma>", "phone": "<Telefon>", '
        '"email": "<E-Mail>", "website": "<Website>", '
        '"address": "<Adresse>"}'
    ),
    "form": (
        "Das Bild zeigt ein Formular. Liste jedes Feld zusammen mit "
        "seinem aktuellen Wert auf. Markiere leere Felder explizit "
        'als "<leer>". Antworte auf Deutsch als Aufzählung Feld: Wert.'
    ),
    "handwriting": (
        "Das Bild enthält handschriftlichen Text. Transkribiere "
        "den Text EXAKT so, wie er geschrieben wurde (inklusive "
        "Tippfehler oder Streichungen, falls erkennbar). Gib danach "
        "eine bereinigte, lesbare Version aus.\n"
        "Format:\n"
        "EXAKT:\n<wörtliche Transkription>\n\n"
        "BEREINIGT:\n<lesbare Version>"
    ),
    "table": (
        "Das Bild zeigt eine Tabelle. Extrahiere sie als CSV mit "
        "Header-Zeile. Trenne Spalten mit Kommas. Antworte "
        "AUSSCHLIESSLICH mit den CSV-Zeilen, ohne Markdown oder "
        "weitere Erklärung."
    ),
    "id_document": (
        "Das Bild zeigt ein Ausweis- oder Identitätsdokument. "
        "Beschreibe den Dokumenttyp und die sichtbaren Felder. "
        "Aus Datenschutzgründen: gib KEINE Personalnummern, "
        "Geburtsdaten oder Adressen wörtlich aus, sondern kennzeichne "
        "sie nur als 'vorhanden'."
    ),
    "general": (
        "Beschreibe den Inhalt dieses Dokuments. Welcher Dokumenttyp "
        "ist es? Was sind die wichtigsten Informationen? Antworte "
        "auf Deutsch."
    ),
}

# JSON-output types — drives whether we parse the reply into
# structured_data or keep it as summary prose.
_JSON_TYPES: frozenset[str] = frozenset({
    "receipt", "invoice", "business_card",
})


class DocumentScanner:
    """Document-shaped Claude Vision calls.

    Stateless; the manager holds the only persistent reference. Tests
    swap the manager's stub client and exercise prompt selection /
    JSON parsing without hitting the real API.
    """

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager

    # --- public API --------------------------------------------------- #

    def scan_document(
        self,
        image_base64: str,
        *,
        doc_type: str = "auto",
    ) -> DocumentResult | None:
        """Scan one document image and return structured + prose data.

        Pass ``doc_type="auto"`` (default) to let Claude classify the
        document first; pass an explicit type to skip the classification
        and save one round-trip. Returns ``None`` on a Claude API
        failure on the FIRST call (classification or extraction);
        partial failures inside parsing fall back to prose output.
        """
        if not image_base64:
            return None

        chosen = doc_type.strip().lower() if doc_type else "auto"
        if chosen not in KNOWN_DOC_TYPES and chosen != "auto":
            chosen = "general"

        if chosen == "auto":
            chosen = self._classify(image_base64) or "general"

        prompt = _PROMPTS.get(chosen, _PROMPTS["general"])
        raw = self._mgr.analyze_image(image_base64, prompt)
        if raw is None:
            return None

        if chosen in _JSON_TYPES:
            parsed = self._parse_json_or_blank(raw)
            summary = self._summarise_structured(chosen, parsed) if parsed else raw
            return DocumentResult(
                doc_type=chosen,
                raw_text=raw,
                structured_data=parsed,
                summary=summary,
                action_items=[],
                confidence=0.9 if parsed else 0.5,
            )

        # Prose-shaped doc type — keep everything in summary, leave
        # structured_data empty. action_items extracted heuristically
        # from numbered/bulleted lines for the contract/letter case.
        action_items = self._extract_action_items(raw) if chosen in {"contract", "letter"} else []
        return DocumentResult(
            doc_type=chosen,
            raw_text=raw,
            structured_data={},
            summary=raw.strip(),
            action_items=action_items,
            confidence=0.85,
        )

    # --- convenience shortcuts --------------------------------------- #

    def process_receipt(self, image_base64: str) -> DocumentResult | None:
        """Force-classify as receipt and return the parsed result."""
        return self.scan_document(image_base64, doc_type="receipt")

    def extract_business_card(self, image_base64: str) -> DocumentResult | None:
        """Parse a business card. The future "save to macOS Contacts"
        step belongs in mac_control where the AppleScript bridge lives;
        this function just hands back the parsed contact dict."""
        return self.scan_document(image_base64, doc_type="business_card")

    def extract_table(self, image_base64: str) -> str | None:
        """Return CSV text for a table image. Returns ``None`` on
        capture/API failure; otherwise the raw model reply (which is
        already CSV per the prompt)."""
        result = self.scan_document(image_base64, doc_type="table")
        return result.summary if result else None

    # --- internals --------------------------------------------------- #

    def _classify(self, image_base64: str) -> str | None:
        """One Claude call: pick the closest type from KNOWN_DOC_TYPES."""
        type_list = ", ".join(KNOWN_DOC_TYPES)
        prompt = (
            "Welcher Dokumenttyp ist auf diesem Bild zu sehen? "
            f"Wähle GENAU einen Wert aus dieser Liste: {type_list}. "
            "Antworte mit einem einzigen Wort, ohne Erklärung."
        )
        reply = self._mgr.analyze_image(image_base64, prompt, max_tokens=20)
        if not reply:
            return None
        guess = reply.strip().lower().split()[0] if reply.strip() else ""
        # Tolerate trailing punctuation, German plurals, etc.
        guess = re.sub(r"[^a-z_]", "", guess)
        return guess if guess in KNOWN_DOC_TYPES else None

    @staticmethod
    def _parse_json_or_blank(text: str) -> dict[str, Any]:
        """Best-effort JSON extraction. Tolerates ```json fences and
        leading prose; returns ``{}`` if nothing parseable was found."""
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
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _summarise_structured(doc_type: str, data: dict[str, Any]) -> str:
        """One-sentence German summary for the speakable reply. The
        full structured data still lives in ``structured_data`` for
        clients that want to render it."""
        if doc_type == "receipt":
            store = data.get("store") or "unbekanntes Geschäft"
            total = data.get("total") or "unbekannter Betrag"
            date  = data.get("date")  or "ohne Datum"
            n     = len(data.get("items") or [])
            return f"Kassenbon von {store}, {n} Position{'en' if n != 1 else ''}, gesamt {total} ({date})."
        if doc_type == "invoice":
            vendor = data.get("vendor") or "unbekannter Aussteller"
            total = data.get("total") or "unbekannter Betrag"
            num    = data.get("invoice_number") or "ohne Nummer"
            return f"Rechnung von {vendor}, Nummer {num}, gesamt {total}."
        if doc_type == "business_card":
            name = data.get("name") or "unbekannter Name"
            company = data.get("company") or "unbekannte Firma"
            return f"Visitenkarte: {name}, {company}."
        # Fallback — should not be hit because _JSON_TYPES is the
        # only caller, but harmless if it is.
        return "Dokument verarbeitet."

    @staticmethod
    def _extract_action_items(text: str) -> list[str]:
        """Pull bulleted / numbered lines out of a prose reply. Used
        for contracts/letters where "what should I do" is a separate
        section in the assistant's reply."""
        if not text:
            return []
        items: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Match common bullet markers: -, *, •, 1., 1)
            if re.match(r"^([-*•]|\d+[.)])\s+", stripped):
                # Drop the marker for a cleaner stored item.
                cleaned = re.sub(r"^([-*•]|\d+[.)])\s+", "", stripped)
                if cleaned:
                    items.append(cleaned)
        return items
