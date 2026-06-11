"""Detect recurring subscriptions from email (Apple Mail) via Claude.

The testable core is :meth:`detect_from_texts`: given a batch of email
bodies/subjects it asks Claude to pull out recurring charges (name +
amount + interval) and upserts them. :meth:`scan_mail` is the best-effort
wrapper that feeds it from ``mail_tool`` — mail-coupled, so it degrades to
an empty result rather than failing when Mail isn't reachable.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings


class SubscriptionDetector:
    def __init__(self, db: Any, client: Any = None) -> None:
        self._db = db
        self._client = client

    def detect_from_texts(self, texts: list[str]) -> list[dict[str, Any]]:
        """Extract recurring subscriptions from email text and persist them."""
        if self._client is None or not texts:
            return []
        blob = "\n---\n".join(t[:1000] for t in texts[:20])
        prompt = (
            "From these emails, identify RECURRING subscriptions or "
            "memberships (not one-off purchases). Return ONLY JSON: "
            '{"subscriptions":[{"name":"...","amount":0.0,'
            '"currency":"EUR","interval":"monthly|yearly"}]}. '
            "Empty list if none.\n\n" + blob)
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=1024,
                messages=[{"role": "user", "content": prompt}])
            raw = ""
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    raw = (b.text or "").strip()
                    break
            subs = self._parse(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[Subscriptions] detect failed: {exc}")
            return []
        for s in subs:
            self._db.upsert_subscription(
                s.get("name", ""), s.get("amount"),
                interval=s.get("interval", "monthly"),
                currency=s.get("currency", "EUR"), detected_from="email")
        return subs

    @staticmethod
    def _parse(raw: str) -> list[dict[str, Any]]:
        text = raw.strip()
        if "```" in text:
            for p in text.split("```"):
                p = p.strip()
                if p.startswith("{") or p.startswith("json"):
                    text = p[4:].strip() if p.startswith("json") else p
                    break
        if not text.startswith("{"):
            i, j = text.find("{"), text.rfind("}")
            if i != -1 and j != -1:
                text = text[i:j + 1]
        try:
            data = json.loads(text)
            subs = data.get("subscriptions", []) if isinstance(data, dict) else []
            return [s for s in subs if isinstance(s, dict) and s.get("name")]
        except Exception:  # noqa: BLE001
            return []

    def scan_mail(self, limit: int = 30) -> list[dict[str, Any]]:
        """Best-effort: pull recent Apple Mail and detect subscriptions."""
        try:
            from ..tools import mail_tool
            listing, err = mail_tool.list_unread(limit=limit)
            if err or not listing:
                return []
            return self.detect_from_texts(listing.splitlines())
        except Exception as exc:  # noqa: BLE001
            print(f"[Subscriptions] scan_mail failed: {exc}")
            return []

    def spoken_summary(self) -> str:
        subs = self._db.active_subscriptions()
        if not subs:
            return "Keine Abos erfasst."
        monthly = sum((s["amount"] or 0) / (12 if s["interval"] == "yearly" else 1)
                      for s in subs)
        names = ", ".join(s["name"] for s in subs[:6])
        return (f"{len(subs)} Abos, rund {monthly:.0f} Euro pro Monat: {names}.")
