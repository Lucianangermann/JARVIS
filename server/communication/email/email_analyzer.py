"""AI-powered email analysis via the existing Claude client.

Summarisation, importance triage, and newsletter detection. All calls are
best-effort — an API failure returns a neutral default (e.g. importance
"normal") rather than raising, so analysis never blocks the mail flow.
"""
from __future__ import annotations

import re
from typing import Any

from ...config import settings

# Heuristic unsubscribe markers, used to flag newsletters without an API
# call (the AI pass is a refinement, not a requirement).
_UNSUB_RE = re.compile(
    r"unsubscribe|abbestellen|abmelden|opt[\s-]?out|newsletter", re.I)


class EmailAnalyzer:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _ask(self, prompt: str, max_tokens: int = 400) -> str | None:
        if self._client is None:
            return None
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    return (b.text or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            print(f"[EmailAnalyzer] Claude call failed: {exc}")
        return None

    async def summarize(self, subject: str, body: str) -> str:
        prompt = (f"Summarize this email in one short German sentence.\n\n"
                  f"Subject: {subject}\n\n{body[:2000]}")
        return self._ask(prompt) or subject

    async def classify_importance(self, subject: str, body: str,
                                  sender: str = "") -> str:
        """Return 'important' | 'normal' | 'low'."""
        prompt = (
            "Classify this email's importance as exactly one word: "
            "important, normal, or low. Consider whether it needs a timely "
            "human response.\n\n"
            f"From: {sender}\nSubject: {subject}\n\n{body[:1500]}")
        out = (self._ask(prompt, max_tokens=8) or "normal").lower()
        for level in ("important", "normal", "low"):
            if level in out:
                return level
        return "normal"

    @staticmethod
    def looks_like_newsletter(subject: str, body: str, sender: str = "") -> bool:
        return bool(_UNSUB_RE.search(f"{subject}\n{body}\n{sender}"))

    @staticmethod
    def extract_unsubscribe_link(body: str) -> str | None:
        # Find a URL near an unsubscribe keyword.
        for m in re.finditer(r"https?://[^\s\"'<>]+", body):
            window = body[max(0, m.start() - 60):m.start()]
            if _UNSUB_RE.search(window) or _UNSUB_RE.search(m.group()):
                return m.group()
        return None
