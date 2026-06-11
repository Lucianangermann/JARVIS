"""Central coordinator for the JARVIS finance layer.

Owns the FinanceDB and the expense / market / subscription components,
starts the market price-poll loop, and exposes a clean surface for the
brain tool, the API routes, and the morning briefing. Construction never
raises — a failed component is logged and left None.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .finance_db import FinanceDB
from .expense_tracker import ExpenseTracker
from .market import MarketManager
from .subscription_detector import SubscriptionDetector


class FinanceManager:
    def __init__(self, db_path: Path | str = "data/finance.db",
                 client: Any = None, notification_center: Any = None) -> None:
        self.db = FinanceDB(db_path)
        self.expenses = self._build(
            lambda: ExpenseTracker(self.db, client=client), "expenses")
        self.market = self._build(
            lambda: MarketManager(self.db, notification_center=notification_center),
            "market")
        self.subscriptions = self._build(
            lambda: SubscriptionDetector(self.db, client=client), "subscriptions")

    @staticmethod
    def _build(factory, name: str) -> Any:
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            print(f"[FinanceManager] {name} init failed: {exc}")
            return None

    def start(self) -> None:
        if self.market is not None:
            self.market.start()
        print("[FINANCE] expense tracker ready")
        print("[FINANCE] market watchlist ready (Yahoo Finance)")
        print("[FINANCE] all finance systems online")

    def stop(self) -> None:
        if self.market is not None:
            self.market.stop()
        self.db.close()

    # ── morning briefing ───────────────────────────────────────────────── #

    def morning_brief(self) -> str:
        bits: list[str] = []
        if self.expenses is not None:
            over = [b for b in self.expenses.budget_status() if b["over"]]
            if over:
                bits.append("Überschrittene Budgets: "
                            + ", ".join(b["category"] for b in over) + ".")
        return " ".join(bits)
