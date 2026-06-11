"""JARVIS finance layer — expenses, budgets, market watchlist, subscriptions.

Phased build (see tasks/todo.md):
  P1 — finance_db, expense_tracker        [done]
  P2 — market (Yahoo Finance) + alerts
  P3 — subscription_detector
  P4 — finance_manager + integration
"""
from __future__ import annotations

from .finance_db import FinanceDB
from .expense_tracker import ExpenseTracker
from .market import MarketManager
from .subscription_detector import SubscriptionDetector
from .finance_manager import FinanceManager

__all__ = ["FinanceDB", "ExpenseTracker", "MarketManager",
           "SubscriptionDetector", "FinanceManager"]
