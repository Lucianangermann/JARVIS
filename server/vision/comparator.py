"""Two-image diff prompts for Claude Vision.

The single Claude call sends both images plus a context-tuned prompt
in one request: ``compare(image1, image2, context=…)`` always returns
a :class:`ComparisonResult` with the same shape regardless of
context, so the brain layer doesn't have to switch on the context to
read the answer.

Background-thread auto-comparison ("what changed while I was away")
is intentionally deferred to a later slice — it shares its scheduling
machinery with the motion detector and the proactive engine, and we
don't want a half-formed worker thread sitting in this file before
that infrastructure lands. For now the manual ``snapshot_screen`` +
``compare_with_snapshot`` pair gives the user the same end-to-end
capability with a synchronous trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vision_manager import VisionManager


@dataclass
class ComparisonResult:
    """Outcome of :py:meth:`ImageComparator.compare`.

    ``differences`` is a parsed bullet list of distinct changes; if
    Claude returned prose without obvious bullets the list will be
    empty and the prose lives only in ``summary``. ``significance``
    is one of ``"minor" | "moderate" | "major"``; the model may
    refuse to grade it and we then default to ``"moderate"``.
    """
    summary: str
    differences: list[str]
    significance: str
    context: str


# Context-tuned prompts. Each one keeps the same output skeleton so
# the parser at the bottom can be shared.
_PROMPTS: dict[str, str] = {
    "general": (
        "Vergleiche diese beiden Bilder. Bild 1 ist das erste Bild, "
        "Bild 2 das zweite. Beschreibe konkret was sich verändert, "
        "was hinzugekommen ist oder fehlt.\n"
        "Antworte auf Deutsch in genau diesem Format:\n"
        "ZUSAMMENFASSUNG: <ein bis zwei Sätze>\n"
        "UNTERSCHIEDE:\n"
        "- <Unterschied 1>\n"
        "- <Unterschied 2>\n"
        "BEDEUTUNG: <gering / mittel / groß>"
    ),
    "screen": (
        "Vergleiche diese beiden Screenshots. Konzentriere dich auf: "
        "neue Fenster, geänderte Inhalte, Fehlermeldungen, "
        "Benachrichtigungen, neue Tabs.\n"
        "Format:\n"
        "ZUSAMMENFASSUNG: <Hauptänderung in einem Satz>\n"
        "UNTERSCHIEDE:\n"
        "- <Änderung 1>\n"
        "BEDEUTUNG: <gering / mittel / groß>"
    ),
    "document": (
        "Vergleiche diese beiden Dokumentversionen. Konzentriere dich "
        "auf Textänderungen: was wurde hinzugefügt, entfernt oder "
        "umformuliert.\n"
        "Format:\n"
        "ZUSAMMENFASSUNG: <ein bis zwei Sätze>\n"
        "UNTERSCHIEDE:\n"
        "- <Änderung 1>\n"
        "BEDEUTUNG: <gering / mittel / groß>"
    ),
    "before_after": (
        "Bild 1 zeigt einen 'Vorher'-Zustand, Bild 2 den 'Nachher'-"
        "Zustand. Beschreibe die Transformation.\n"
        "Format:\n"
        "ZUSAMMENFASSUNG: <Transformation in einem Satz>\n"
        "UNTERSCHIEDE:\n"
        "- <Veränderung 1>\n"
        "BEDEUTUNG: <gering / mittel / groß>"
    ),
}

_KNOWN_CONTEXTS: tuple[str, ...] = tuple(_PROMPTS.keys())


class ImageComparator:
    """Pairwise image comparison via a single Claude Vision call.

    Stateless except for an optional one-slot screen snapshot (so the
    user can say "merk dir den Bildschirm" and later ask "was hat sich
    geändert"). The snapshot lives in memory only; it's not persisted
    to disk.
    """

    def __init__(self, manager: "VisionManager") -> None:
        self._mgr = manager
        # Single-slot rolling snapshot (most recent screen the user
        # asked us to remember). Could grow into a small ring buffer
        # later; one slot is enough for the original spec's
        # "what changed while I was away" flow.
        self._screen_snapshot: str | None = None

    # --- core comparison --------------------------------------------- #

    def compare(
        self,
        image1_base64: str,
        image2_base64: str,
        *,
        context: str = "general",
    ) -> ComparisonResult | None:
        """Send both images + a context-shaped prompt to Claude.

        Returns ``None`` if either base64 is empty or the API call
        fails; otherwise always returns a usable :class:`ComparisonResult`
        — even when the model's reply is unparseable we fall back to
        putting the raw text in ``summary``.
        """
        if not image1_base64 or not image2_base64:
            return None

        ctx = context.strip().lower() if context else "general"
        if ctx not in _PROMPTS:
            ctx = "general"
        prompt = _PROMPTS[ctx]

        raw = self._call_two_image(image1_base64, image2_base64, prompt)
        if raw is None:
            return None

        summary, differences, significance = self._parse_reply(raw)
        return ComparisonResult(
            summary=summary or raw.strip(),
            differences=differences,
            significance=significance,
            context=ctx,
        )

    # --- screen-snapshot flow ---------------------------------------- #

    def snapshot_screen(self) -> bool:
        """Capture the screen NOW and remember it for a later
        ``compare_with_snapshot`` call. Returns ``True`` on success.

        The capture goes through :class:`ScreenReader`, which prints
        the usual privacy indicators."""
        b64 = self._mgr.screen.capture_screen()
        if not b64:
            return False
        self._screen_snapshot = b64
        return True

    def compare_with_snapshot(
        self,
        *,
        context: str = "screen",
    ) -> ComparisonResult | None:
        """Capture the screen and compare it against the most recent
        snapshot. Returns ``None`` if no snapshot has been taken yet
        or either capture fails. The new screen REPLACES the stored
        snapshot on success — so successive calls describe a rolling
        diff rather than always comparing back to the original moment.
        """
        if self._screen_snapshot is None:
            return None
        current = self._mgr.screen.capture_screen()
        if not current:
            return None
        result = self.compare(
            self._screen_snapshot, current, context=context,
        )
        if result is not None:
            self._screen_snapshot = current
        return result

    def clear_snapshot(self) -> None:
        """Drop the in-memory screen snapshot. Future ``compare_with_
        snapshot`` returns None until a fresh snapshot is taken."""
        self._screen_snapshot = None

    # --- internals --------------------------------------------------- #

    def _call_two_image(
        self,
        image1_base64: str,
        image2_base64: str,
        prompt: str,
    ) -> str | None:
        """Multi-image call. We can't reuse VisionManager.analyze_image
        because that one assumes exactly one image; we mirror its
        failure-handling instead."""
        try:
            response = self._mgr._client.messages.create(  # noqa: SLF001
                model=__import__(
                    "server.config", fromlist=["settings"]
                ).settings.MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image1_base64,
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image2_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] comparator Claude call failed: {exc}")
            return None

        try:
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    return (getattr(block, "text", "") or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] comparator response parse failed: {exc}")
        return None

    @staticmethod
    def _parse_reply(text: str) -> tuple[str, list[str], str]:
        """Pull the three labelled fields out of the structured prompt
        format (ZUSAMMENFASSUNG / UNTERSCHIEDE / BEDEUTUNG).

        Falls back gracefully on malformed replies: an unlabeled prose
        answer goes entirely into the summary, differences stays empty,
        significance defaults to ``"moderate"``.
        """
        summary = ""
        differences: list[str] = []
        significance = "moderate"

        if not text:
            return summary, differences, significance

        section: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith("ZUSAMMENFASSUNG"):
                section = "summary"
                value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                if value:
                    summary = value
                continue
            if upper.startswith("UNTERSCHIEDE"):
                section = "diffs"
                continue
            if upper.startswith("BEDEUTUNG"):
                section = "significance"
                value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                if value:
                    significance = _normalise_significance(value)
                continue
            # Section body
            if section == "summary":
                # Allow multi-line summaries that wrap.
                summary = (summary + " " + stripped).strip()
            elif section == "diffs":
                # Strip leading bullet markers.
                cleaned = stripped.lstrip("-*•").strip()
                if cleaned:
                    differences.append(cleaned)
            elif section == "significance":
                significance = _normalise_significance(stripped)

        if not summary:
            # Model didn't follow the format — store the entire reply
            # as the summary so the user at least sees something.
            summary = text.strip()
        return summary, differences, significance


def _normalise_significance(text: str) -> str:
    """Map the German label words to the English level used by callers."""
    lower = text.lower()
    if "groß" in lower or "gross" in lower or "major" in lower:
        return "major"
    if "gering" in lower or "klein" in lower or "minor" in lower:
        return "minor"
    return "moderate"
