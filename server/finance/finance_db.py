"""SQLite store for the JARVIS finance layer (``data/finance.db``).

Own database (separate retention + privacy from the other layers). Holds
expenses, monthly budgets, detected subscriptions, and the market
watchlist. Thread-safe single connection in WAL mode with a lock, like
SecurityDB / CommunicationDB. Every write is best-effort — a failed insert
prints and returns a falsy value rather than crashing JARVIS.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..common.sqlite_store import ThreadSafeDB
from typing import Any

_CREATE_EXPENSES = """
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    category    TEXT NOT NULL DEFAULT 'sonstiges',
    merchant    TEXT,
    description TEXT,
    source      TEXT DEFAULT 'manual'
)
"""

_CREATE_BUDGETS = """
CREATE TABLE IF NOT EXISTS budgets (
    category      TEXT PRIMARY KEY,
    monthly_limit REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'EUR',
    created_at    REAL NOT NULL
)
"""

_CREATE_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    amount        REAL,
    currency      TEXT DEFAULT 'EUR',
    interval      TEXT DEFAULT 'monthly',
    next_charge   REAL,
    detected_from TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
)
"""

_CREATE_WATCHLIST = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol       TEXT PRIMARY KEY,
    name         TEXT,
    asset_type   TEXT DEFAULT 'stock',
    quantity     REAL DEFAULT 0,
    added_at     REAL NOT NULL,
    target_above REAL,
    target_below REAL,
    alert_armed  INTEGER NOT NULL DEFAULT 1,
    last_price   REAL,
    last_currency TEXT,
    last_checked REAL
)
"""

_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_exp_ts ON expenses(ts)",
    "CREATE INDEX IF NOT EXISTS idx_exp_cat ON expenses(category)",
]


class FinanceDB(ThreadSafeDB):
    """Thread-safe SQLite wrapper for the finance layer."""

    def __init__(self, db_path: Path | str = "data/finance.db") -> None:
        super().__init__(db_path, label="FinanceDB")

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        for stmt in (_CREATE_EXPENSES, _CREATE_BUDGETS,
                     _CREATE_SUBSCRIPTIONS, _CREATE_WATCHLIST):
            conn.execute(stmt)
        for idx in _INDICES:
            conn.execute(idx)

    # `execute` is this layer's public write name — alias the inherited impl.
    execute = ThreadSafeDB._execute

    # ── expenses ───────────────────────────────────────────────────────── #

    def add_expense(self, amount: float, category: str, merchant: str | None,
                    description: str | None, currency: str = "EUR",
                    source: str = "manual", ts: float | None = None) -> int | None:
        return self.execute(
            """INSERT INTO expenses (ts, amount, currency, category, merchant,
               description, source) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts if ts is not None else time.time(), amount, currency, category,
             merchant, description, source),
        )

    def expenses_since(self, since_ts: float) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM expenses WHERE ts >= ? ORDER BY ts DESC", (since_ts,))

    def expenses_by_category(self, since_ts: float) -> list[dict[str, Any]]:
        return self.query(
            """SELECT category, SUM(amount) AS total, COUNT(*) AS n
               FROM expenses WHERE ts >= ? GROUP BY category
               ORDER BY total DESC""",
            (since_ts,))

    # ── budgets ────────────────────────────────────────────────────────── #

    def set_budget(self, category: str, monthly_limit: float,
                   currency: str = "EUR") -> None:
        self.execute(
            """INSERT INTO budgets (category, monthly_limit, currency, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit,
               currency=excluded.currency""",
            (category, monthly_limit, currency, time.time()))

    def get_budgets(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM budgets ORDER BY category")

    def get_budget(self, category: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM budgets WHERE category=?", (category,))
        return rows[0] if rows else None

    # ── subscriptions ──────────────────────────────────────────────────── #

    def upsert_subscription(self, name: str, amount: float | None,
                            interval: str = "monthly", currency: str = "EUR",
                            detected_from: str | None = None) -> None:
        existing = self.query(
            "SELECT id FROM subscriptions WHERE LOWER(name)=LOWER(?)", (name,))
        if existing:
            self.execute(
                "UPDATE subscriptions SET amount=?, interval=?, active=1 WHERE id=?",
                (amount, interval, existing[0]["id"]))
        else:
            self.execute(
                """INSERT INTO subscriptions (name, amount, currency, interval,
                   detected_from, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (name, amount, currency, interval, detected_from, time.time()))

    def active_subscriptions(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM subscriptions WHERE active=1 ORDER BY amount DESC")

    # ── watchlist ──────────────────────────────────────────────────────── #

    def add_watch(self, symbol: str, name: str | None, asset_type: str,
                  quantity: float = 0, target_above: float | None = None,
                  target_below: float | None = None) -> None:
        self.execute(
            """INSERT INTO watchlist (symbol, name, asset_type, quantity,
               added_at, target_above, target_below)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET name=COALESCE(excluded.name, name),
               asset_type=excluded.asset_type, quantity=excluded.quantity,
               target_above=excluded.target_above, target_below=excluded.target_below,
               alert_armed=1""",
            (symbol.upper(), name, asset_type, quantity, time.time(),
             target_above, target_below))

    def remove_watch(self, symbol: str) -> bool:
        return self.execute(
            "DELETE FROM watchlist WHERE symbol=?", (symbol.upper(),)) is not None

    def get_watchlist(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM watchlist ORDER BY symbol")

    def update_watch_price(self, symbol: str, price: float, currency: str) -> None:
        self.execute(
            """UPDATE watchlist SET last_price=?, last_currency=?, last_checked=?
               WHERE symbol=?""",
            (price, currency, time.time(), symbol.upper()))

    def set_alert_armed(self, symbol: str, armed: bool) -> None:
        self.execute("UPDATE watchlist SET alert_armed=? WHERE symbol=?",
                     (int(armed), symbol.upper()))

    # close() / query() inherited from ThreadSafeDB; execute is aliased above.
