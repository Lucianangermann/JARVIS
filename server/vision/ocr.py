"""Text extraction from arbitrary images, powered by Claude Vision.

This isn't "OCR" in the Tesseract / EasyOCR sense — Claude reads the
text directly from a vision prompt. That's slower per call than a
local OCR engine but handles handwriting, low-contrast, photographed
documents, and multi-column layouts without per-engine tuning. Cost
control is handled at the manager layer (image resize to ≤1920 px).

Two surfaces:

* :py:meth:`OCR.extract_text` — pure transcription, no translation
* :py:meth:`OCR.extract_and_translate` — transcribe then translate
  in a single call (one Claude round-trip, JSON-formatted reply)

Both gracefully degrade to ``None`` on any failure: the caller
formats a German "ich konnte den Text nicht lesen" message.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .vision_manager import VisionManager


@dataclass(frozen=True)
class TranslationResult:
    """Pair of original + translated text returned by
    :py:meth:`OCR.extract_and_translate`. Either field may be empty
    if Claude found nothing to transcribe in the source image."""
    original: str
    translated: str
    target_language: str


# Quick map from common 2-letter codes to readable target labels. The
# brain may hand us "de" or "en" from a trigger phrase parser; Claude
# happily understands either form but the readable label gives the
# user-visible side a cleaner log line.
_LANGUAGE_LABELS: dict[str, str] = {
    "de": "Deutsch",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
    "nl": "Nederlands",
    "tr": "Türkçe",
    "pl": "Polski",
    "ru": "Русский",
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "ar": "العربية",
}


def _language_label(code_or_name: str) -> str:
    """Resolve a 2-letter ISO code to its native label, or echo the
    input if we don't have it mapped (Claude can still parse it)."""
    key = (code_or_name or "").strip().lower()
    return _LANGUAGE_LABELS.get(key, code_or_name or "")


class OCR:
    """Stateless wrapper around the manager's Claude-Vision call,
    parameterised with text-extraction prompts."""

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager

    # --- transcribe-only --------------------------------------------- #

    def extract_text(
        self,
        image_base64: str,
        *,
        language: str = "auto",
    ) -> str | None:
        """Transcribe all visible text from a base64-JPEG image.

        ``language`` is a hint to the model — pass "auto" (default)
        to let it detect, or a 2-letter code / native name to bias
        toward a specific language. Returns the transcribed string
        (preserving rough layout) or ``None`` on failure.
        """
        if not image_base64:
            return None

        if language and language.strip().lower() not in {"", "auto"}:
            lang_hint = (f"Die Sprache des sichtbaren Textes ist "
                         f"voraussichtlich {_language_label(language)}. ")
        else:
            lang_hint = ""

        prompt = (
            f"{lang_hint}Extrahiere ALLEN sichtbaren Text aus diesem "
            "Bild genau so, wie er dort steht. Behalte die ursprüngliche "
            "Reihenfolge und ungefähre Formatierung (Listen, Tabellen, "
            "Spalten) bei. Übersetze nichts. Wenn kein Text sichtbar "
            "ist, antworte mit dem leeren String."
        )
        result = self._mgr.analyze_image(image_base64, prompt)
        if result is None:
            return None
        return result.strip() or None

    # --- transcribe + translate (single round-trip) ------------------ #

    def extract_and_translate(
        self,
        image_base64: str,
        *,
        target_language: str = "de",
    ) -> Optional[TranslationResult]:
        """Transcribe the image's text and translate it in one call.

        Returns a :class:`TranslationResult` with both ``original``
        and ``translated`` fields populated, or ``None`` if Claude
        returned an unparseable answer / the API call failed.
        """
        if not image_base64:
            return None

        target_label = _language_label(target_language) or target_language
        prompt = (
            "1. Extrahiere ALLEN sichtbaren Text aus diesem Bild "
            "genau so, wie er dort steht.\n"
            f"2. Übersetze den extrahierten Text nach {target_label}.\n"
            "Antworte AUSSCHLIESSLICH mit gültigem JSON in genau dieser "
            "Form, ohne Markdown-Codeblock und ohne weitere Erklärung:\n"
            '{"original": "<extrahierter Text>", '
            '"translated": "<übersetzter Text>"}'
        )
        raw = self._mgr.analyze_image(image_base64, prompt)
        if raw is None:
            return None

        parsed = self._extract_json_object(raw)
        if parsed is None:
            # Fall back to a minimal result so the caller at least
            # gets the raw model output back as "original" — better
            # than silently dropping the work we already paid for.
            return TranslationResult(
                original=raw.strip(),
                translated="",
                target_language=target_language,
            )

        original = str(parsed.get("original") or "").strip()
        translated = str(parsed.get("translated") or "").strip()
        if not original and not translated:
            return None
        return TranslationResult(
            original=original,
            translated=translated,
            target_language=target_language,
        )

    # --- helpers ----------------------------------------------------- #

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """Pull the first ``{...}`` JSON object out of a model reply.

        Claude usually obeys "answer with ONLY JSON" but occasionally
        wraps it in a ```json fence or prepends a sentence. We strip
        common wrappers, then run a forgiving regex; full ``json.loads``
        is the only authority on whether the result is valid.
        """
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Drop the opening fence (with or without a language tag)
            # and the trailing fence.
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        # Find the outermost {...} — non-greedy is fine since we
        # expect a single object, and even if Claude returned two we
        # want the first one.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        return obj
