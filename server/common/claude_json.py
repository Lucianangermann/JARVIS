"""Shared Claude JSON helpers.

Several layers (flashcards, subscriptions, meeting summaries, camera
analysis) ask Claude for JSON and then strip Markdown fences before
``json.loads``. That fence-stripping parser was copy-pasted (byte-
identical) in 4+ places; this module is the single tested implementation.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings


def parse_json_block(raw: str) -> dict[str, Any] | None:
    """Extract a JSON object from a model reply that may be wrapped in
    ```json … ``` fences or surrounded by prose. Returns the parsed dict,
    or None if no valid JSON object can be recovered."""
    if not raw:
        return None
    text = raw.strip()
    if "```" in text:
        for part in text.split("```"):
            p = part.strip()
            if p.startswith("{") or p.startswith("json"):
                text = p[4:].strip() if p.startswith("json") else p
                break
    if not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j != -1:
            text = text[i:j + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def ask_json(client: Any, prompt: str, *, max_tokens: int = 1024,
             model: str | None = None) -> dict[str, Any] | None:
    """One non-streaming Claude call that returns parsed JSON (or None).
    Best-effort: a missing client or any API/parse failure yields None."""
    if client is None:
        return None
    try:
        resp = client.messages.create(
            model=model or settings.MODEL, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                return parse_json_block((b.text or "").strip())
    except Exception as exc:  # noqa: BLE001
        print(f"[claude_json] call failed: {exc}")
    return None
