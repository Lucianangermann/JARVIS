"""Expense tracking + monthly budgets with auto-categorisation.

Categorisation is keyword-first (fast, free, deterministic) and falls
back to Claude only when the merchant/description doesn't match any known
keyword — so common spends never cost an API call. Budgets are monthly
per category; adding an expense reports how much of the budget is left and
warns on overrun. All best-effort.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from ..config import settings

# Canonical German categories + keyword hints for the free fast path.
CATEGORIES = ["lebensmittel", "essen", "transport", "wohnen", "abos",
              "unterhaltung", "gesundheit", "kleidung", "bildung", "sonstiges"]

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "lebensmittel": ("rewe", "edeka", "aldi", "lidl", "kaufland", "supermarkt",
                     "penny", "netto", "dm", "rossmann"),
    "essen":        ("restaurant", "mcdonald", "burger", "pizza", "lieferando",
                     "uber eats", "cafe", "bäcker", "imbiss", "kantine"),
    "transport":    ("tankstelle", "shell", "aral", "esso", "db ", "bahn",
                     "bvg", "uber", "taxi", "benzin", "diesel", "ticket"),
    "wohnen":       ("miete", "strom", "gas", "wasser", "vattenfall", "eon",
                     "nebenkosten", "hausrat"),
    "abos":         ("netflix", "spotify", "disney", "amazon prime", "youtube",
                     "abo", "icloud", "dropbox", "adobe"),
    "unterhaltung": ("kino", "konzert", "steam", "playstation", "game",
                     "buch", "thalia"),
    "gesundheit":   ("apotheke", "arzt", "fitness", "gym", "mcfit", "drogerie"),
    "kleidung":     ("zara", "h&m", "zalando", "nike", "adidas", "kleidung"),
    "bildung":      ("udemy", "coursera", "kurs", "seminar", "buchhandlung"),
}


class ExpenseTracker:
    def __init__(self, db: Any, client: Any = None) -> None:
        self._db = db
        self._client = client

    # ── categorisation ─────────────────────────────────────────────────── #

    def categorize(self, merchant: str = "", description: str = "") -> str:
        text = f"{merchant} {description}".lower()
        for cat, kws in _KEYWORDS.items():
            # Word-boundary match so "buch" doesn't match "Buchung" and
            # "dm" doesn't match "Edmund" — substring matching produced
            # false-positive categories.
            if any(re.search(r"\b" + re.escape(k.strip()) + r"\b", text)
                   for k in kws):
                return cat
        # Fall back to Claude only when keywords miss.
        if self._client is not None and text.strip():
            cat = self._claude_category(text)
            if cat:
                return cat
        return "sonstiges"

    def _claude_category(self, text: str) -> str | None:
        prompt = ("Classify this expense into exactly one of these German "
                  f"categories: {', '.join(CATEGORIES)}. Respond with ONLY the "
                  f"category word.\n\nExpense: {text}")
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=8,
                messages=[{"role": "user", "content": prompt}])
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    word = (b.text or "").strip().lower().split()[0]
                    return word if word in CATEGORIES else None
        except Exception as exc:  # noqa: BLE001
            print(f"[Expense] claude category failed: {exc}")
        return None

    # ── adding ─────────────────────────────────────────────────────────── #

    def add_expense(self, amount: float, merchant: str = "",
                    description: str = "", category: str | None = None,
                    currency: str = "EUR", source: str = "manual") -> dict[str, Any]:
        if amount <= 0:
            return {"ok": False, "spoken": "Betrag muss größer als null sein."}
        cat = category or self.categorize(merchant, description)
        self._db.add_expense(amount, cat, merchant or None, description or None,
                             currency, source)
        spoken = f"{amount:.2f} {currency} unter {cat} erfasst."
        warn = self._budget_warning(cat)
        if warn:
            spoken += " " + warn
        return {"ok": True, "category": cat, "spoken": spoken}

    def _budget_warning(self, category: str) -> str | None:
        budget = self._db.get_budget(category)
        if not budget:
            return None
        spent = self._category_spend_this_month(category)
        limit = budget["monthly_limit"]
        if spent >= limit:
            return (f"Achtung: Budget für {category} überschritten "
                    f"({spent:.0f} von {limit:.0f} {budget['currency']}).")
        if spent >= 0.8 * limit:
            return (f"Hinweis: {spent:.0f} von {limit:.0f} {budget['currency']} "
                    f"für {category} ausgegeben.")
        return None

    # ── budgets ────────────────────────────────────────────────────────── #

    def set_budget(self, category: str, monthly_limit: float,
                   currency: str = "EUR") -> str:
        self._db.set_budget(category, monthly_limit, currency)
        return f"Budget für {category}: {monthly_limit:.0f} {currency} pro Monat."

    def budget_status(self) -> list[dict[str, Any]]:
        out = []
        for b in self._db.get_budgets():
            spent = self._category_spend_this_month(b["category"])
            out.append({
                "category": b["category"], "limit": b["monthly_limit"],
                "spent": round(spent, 2),
                "remaining": round(b["monthly_limit"] - spent, 2),
                "currency": b["currency"],
                "over": spent > b["monthly_limit"],
            })
        return out

    # ── summaries ──────────────────────────────────────────────────────── #

    @staticmethod
    def _month_start() -> float:
        now = datetime.now()
        return datetime(now.year, now.month, 1).timestamp()

    def _category_spend_this_month(self, category: str) -> float:
        rows = self._db.query(
            "SELECT SUM(amount) AS total FROM expenses WHERE ts >= ? AND category=?",
            (self._month_start(), category))
        return float(rows[0]["total"] or 0) if rows else 0.0

    def month_total(self) -> float:
        rows = self._db.query(
            "SELECT SUM(amount) AS total FROM expenses WHERE ts >= ?",
            (self._month_start(),))
        return float(rows[0]["total"] or 0) if rows else 0.0

    def monthly_summary(self) -> dict[str, Any]:
        by_cat = self._db.expenses_by_category(self._month_start())
        return {"total": round(self.month_total(), 2),
                "by_category": by_cat,
                "budgets": self.budget_status()}

    def spending_trend(self) -> list[dict]:
        """Compare current-month vs previous-month spend per category.

        Returns a list sorted by current-month spend, descending.
        Each entry: {category, current, previous, change_pct | None}."""
        now = datetime.now()
        # Current month start
        cur_start = datetime(now.year, now.month, 1).timestamp()
        # Previous month start / end
        if now.month == 1:
            prev_start = datetime(now.year - 1, 12, 1).timestamp()
        else:
            prev_start = datetime(now.year, now.month - 1, 1).timestamp()
        prev_end = cur_start

        cur_by_cat = {r["category"]: float(r["total"] or 0)
                      for r in self._db.expenses_by_category(cur_start)}
        prev_by_cat = {r["category"]: float(r["total"] or 0)
                       for r in self._db.expenses_by_category(prev_start, prev_end)}

        all_cats = set(cur_by_cat) | set(prev_by_cat)
        result = []
        for cat in all_cats:
            cur = cur_by_cat.get(cat, 0.0)
            prev = prev_by_cat.get(cat, 0.0)
            change_pct: float | None = None
            if prev > 0:
                change_pct = round((cur - prev) / prev * 100, 1)
            result.append({"category": cat, "current": round(cur, 2),
                           "previous": round(prev, 2), "change_pct": change_pct})
        result.sort(key=lambda x: x["current"], reverse=True)
        return result

    def spoken_trend(self) -> str:
        """One-sentence spoken summary of month-over-month changes."""
        trends = self.spending_trend()
        if not trends:
            return "Noch keine Ausgaben erfasst."
        has_prev = any(t["previous"] > 0 for t in trends)
        if not has_prev:
            return "Keine Vormonatsdaten für Vergleich vorhanden."
        significant = [t for t in trends
                       if t.get("change_pct") is not None
                       and abs(t["change_pct"]) >= 10]
        if not significant:
            return "Ausgaben im Vergleich zum Vormonat stabil."
        up = sorted([t for t in significant if t["change_pct"] > 0],
                    key=lambda t: t["change_pct"], reverse=True)
        down = sorted([t for t in significant if t["change_pct"] < 0],
                      key=lambda t: t["change_pct"])
        parts = []
        for t in up[:2]:
            parts.append(f"{t['category']} +{t['change_pct']:.0f}%")
        for t in down[:2]:
            parts.append(f"{t['category']} {t['change_pct']:.0f}%")
        return "Monatstrend: " + ", ".join(parts) + "."

    def spoken_month_summary(self) -> str:
        s = self.monthly_summary()
        if s["total"] == 0:
            return "Diesen Monat noch keine Ausgaben erfasst."
        top = s["by_category"][:3]
        parts = [f"{c['category']} {c['total']:.0f} Euro" for c in top]
        over = [b for b in s["budgets"] if b["over"]]
        text = (f"Diesen Monat {s['total']:.0f} Euro ausgegeben. "
                f"Größte Posten: " + ", ".join(parts) + ".")
        if over:
            text += (" Überschrittene Budgets: "
                     + ", ".join(b["category"] for b in over) + ".")
        return text
