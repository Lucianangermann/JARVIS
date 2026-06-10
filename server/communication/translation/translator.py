"""Real-time text translation via the existing Claude client.

Unlike ``vision/translator.py`` (which OCR-translates images), this is
pure text↔text: translate a string, detect its language, translate an
incoming message while preserving the original, and a turn-based
conversation mode. It reuses the brain's Anthropic client, so there's no
extra dependency and no extra API key — translation rides the same quota
as chat (spec: "uses existing Claude API, no extra cost").

Every call is best-effort: an API failure returns the original text (or a
clear marker) rather than raising, so a translation hiccup never breaks
the message flow it's embedded in.
"""
from __future__ import annotations

from typing import Any

from ...config import settings

SUPPORTED_LANGUAGES: dict[str, str] = {
    "de": "Deutsch", "en": "English", "fr": "Français", "es": "Español",
    "it": "Italiano", "pt": "Português", "nl": "Nederlands", "pl": "Polski",
    "ru": "Русский", "zh": "中文", "ja": "日本語", "ko": "한국어",
    "ar": "العربية", "tr": "Türkçe",
}


class CommunicationTranslator:
    """Claude-backed text translation + language detection."""

    def __init__(self, client: Any = None, default_lang: str | None = None) -> None:
        self._client = client
        self._default_lang = default_lang or getattr(
            settings, "DEFAULT_TRANSLATION_LANG", "de")

    @property
    def available(self) -> bool:
        return self._client is not None

    def _lang_name(self, code: str) -> str:
        return SUPPORTED_LANGUAGES.get(code, code)

    def _ask(self, prompt: str, max_tokens: int = 1024) -> str | None:
        """One non-streaming Claude call returning the first text block."""
        if self._client is None:
            return None
        try:
            resp = self._client.messages.create(
                model=settings.MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    return (getattr(block, "text", "") or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            print(f"[Translator] Claude call failed: {exc}")
        return None

    # ── core translation ───────────────────────────────────────────────── #

    async def translate(
        self, text: str, target_lang: str, source_lang: str = "auto",
    ) -> str:
        if not text.strip():
            return ""
        target_name = self._lang_name(target_lang)
        src = ("" if source_lang == "auto"
               else f" from {self._lang_name(source_lang)}")
        prompt = (
            f"Translate the following text{src} to {target_name}. "
            f"Return ONLY the translation, no quotes, no explanation, "
            f"preserve the original tone and any formatting.\n\n{text}"
        )
        return self._ask(prompt) or text  # fall back to original on failure

    async def translate_message(self, message: str, to_lang: str) -> str:
        """Translate but show original + translation together (for display
        of an incoming foreign-language message)."""
        translated = await self.translate(message, to_lang)
        return f"Original: {message}\n[{self._lang_name(to_lang)}]: {translated}"

    async def translate_incoming_message(
        self, message: str, sender_lang: str | None = None,
    ) -> dict[str, Any]:
        """Detect the message language; if it differs from the user's
        preferred language, translate it. Returns both."""
        detected = sender_lang or await self.detect_language(message)
        if detected == self._default_lang:
            return {"translated": False, "language": detected,
                    "original": message}
        translated = await self.translate(message, self._default_lang,
                                          source_lang=detected)
        return {"translated": True, "language": detected,
                "original": message, "translation": translated}

    async def detect_language(self, text: str) -> str:
        """Return a best-effort ISO 639-1 code for the text's language."""
        if not text.strip():
            return self._default_lang
        prompt = (
            "Identify the language of this text. Respond with ONLY the "
            "two-letter ISO 639-1 code (e.g. de, en, fr), nothing else.\n\n"
            f"{text}"
        )
        out = self._ask(prompt, max_tokens=8)
        if out:
            code = out.strip().lower()[:2]
            if code.isalpha():
                return code
        return self._default_lang

    # ── conversation mode ──────────────────────────────────────────────── #

    async def conversation_turn(
        self, text: str, speaker_lang: str, lang_a: str, lang_b: str,
    ) -> dict[str, Any]:
        """One turn of a bilingual conversation: translate from the
        speaker's language to the other side. Returns text + the language
        it should be spoken in."""
        target = lang_b if speaker_lang == lang_a else lang_a
        translated = await self.translate(text, target, source_lang=speaker_lang)
        return {"text": translated, "speak_lang": target,
                "from": speaker_lang, "to": target}
