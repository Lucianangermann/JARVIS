"""Tests for the finance layer (expenses, budgets, market, subscriptions).

Market tests that hit the network are marked and skipped if offline; the
rest run fully offline against a temp finance.db.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.finance import FinanceDB, ExpenseTracker, MarketManager
from server.finance.subscription_detector import SubscriptionDetector


@pytest.fixture()
def db(tmp_path: Path) -> FinanceDB:
    d = FinanceDB(tmp_path / "finance.db")
    yield d
    d.close()


# ── expenses / categorisation ───────────────────────────────────────────── #

def test_keyword_categorization(db: FinanceDB) -> None:
    et = ExpenseTracker(db)  # no client → keyword-only
    assert et.categorize("REWE Markt") == "lebensmittel"
    assert et.categorize("Netflix") == "abos"
    assert et.categorize("Shell Tankstelle") == "transport"
    assert et.categorize("Unbekannter Laden") == "sonstiges"


def test_add_expense_and_summary(db: FinanceDB) -> None:
    et = ExpenseTracker(db)
    assert et.add_expense(45.30, "REWE")["category"] == "lebensmittel"
    et.add_expense(12.99, "Netflix")
    assert round(et.month_total(), 2) == 58.29
    assert "58" in et.spoken_month_summary()


def test_budget_warning_on_overrun(db: FinanceDB) -> None:
    et = ExpenseTracker(db)
    et.set_budget("transport", 50)
    et.add_expense(40, "Shell")
    r = et.add_expense(20, "Aral")          # 60 > 50
    assert "überschritten" in r["spoken"].lower()
    status = et.budget_status()
    assert status[0]["over"] is True


def test_add_expense_rejects_nonpositive(db: FinanceDB) -> None:
    et = ExpenseTracker(db)
    assert et.add_expense(0, "x")["ok"] is False


def test_claude_categorization_fallback(db: FinanceDB) -> None:
    class _Block:
        type = "text"
        text = "gesundheit"

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    et = ExpenseTracker(db, client=_Client())
    # No keyword match → Claude fallback returns a valid category.
    assert et.categorize("Yoga Retreat Buchung") == "gesundheit"


# ── subscriptions ───────────────────────────────────────────────────────── #

def test_subscription_detection(db: FinanceDB) -> None:
    class _Block:
        type = "text"
        text = ('{"subscriptions":[{"name":"Netflix","amount":12.99,'
                '"currency":"EUR","interval":"monthly"},'
                '{"name":"Amazon Prime","amount":89.0,"currency":"EUR",'
                '"interval":"yearly"}]}')

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    sd = SubscriptionDetector(db, client=_Client())
    subs = sd.detect_from_texts(["Ihre Netflix-Rechnung", "Amazon Prime Verlängerung"])
    assert len(subs) == 2
    assert len(db.active_subscriptions()) == 2
    # ~12.99 + 89/12 ≈ 20.4 €/month
    assert "Abos" in sd.spoken_summary()


# ── market (network) ────────────────────────────────────────────────────── #

def _online() -> bool:
    # Skip the live-market tests in CI (network flakiness / Yahoo rate limits).
    import os
    if os.getenv("CI"):
        return False
    try:
        import httpx
        return httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
            "?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=6).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _online(), reason="no network / Yahoo unreachable")
def test_market_fetch_and_watchlist(db: FinanceDB) -> None:
    mk = MarketManager(db)
    p = mk.fetch_price("AAPL")
    assert p and p["price"] > 0
    r = mk.add_to_watchlist("AAPL", "Apple", "stock", quantity=2)
    assert r["ok"]
    pv = mk.portfolio_value()
    assert pv["holdings"] and pv["holdings"][0]["value"] > 0


@pytest.mark.skipif(not _online(), reason="no network / Yahoo unreachable")
def test_price_alert_fires_once(db: FinanceDB) -> None:
    alerts = []

    class _NC:
        def send(self, title, body, prio, src):
            alerts.append(body)

    mk = MarketManager(db, notification_center=_NC())
    cur = mk.fetch_price("AAPL")["price"]
    # target_below far above current price → fires on first check.
    mk.add_to_watchlist("AAPL", "Apple", "stock", target_below=cur + 1000)
    mk.refresh_prices()
    assert len(alerts) == 1
    mk.refresh_prices()                 # disarmed → no re-fire
    assert len(alerts) == 1


# ── spending trends ──────────────────────────────────────────────────────── #

def test_spending_trend_empty(db: FinanceDB) -> None:
    et = ExpenseTracker(db)
    trends = et.spending_trend()
    assert trends == []

def test_spending_trend_current_only(db: FinanceDB) -> None:
    """Expenses only in current month → previous=0, change_pct=None."""
    et = ExpenseTracker(db)
    et.add_expense(50.0, "REWE")
    trends = et.spending_trend()
    assert len(trends) == 1
    assert trends[0]["category"] == "lebensmittel"
    assert trends[0]["previous"] == 0.0
    assert trends[0]["change_pct"] is None

def test_spending_trend_with_previous_month(db: FinanceDB) -> None:
    """Inject an expense in previous month via raw DB and verify trend."""
    from datetime import datetime
    et = ExpenseTracker(db)
    now = datetime.now()
    # Previous month start timestamp
    if now.month == 1:
        prev_ts = datetime(now.year - 1, 12, 15).timestamp()
    else:
        prev_ts = datetime(now.year, now.month - 1, 15).timestamp()
    # Insert directly with old timestamp
    db.execute(
        "INSERT INTO expenses (ts, amount, currency, category, source) "
        "VALUES (?, ?, 'EUR', 'lebensmittel', 'manual')",
        (prev_ts, 40.0),
    )
    et.add_expense(60.0, "REWE")  # current month
    trends = et.spending_trend()
    lm = next(t for t in trends if t["category"] == "lebensmittel")
    assert lm["previous"] == 40.0
    assert lm["current"] == 60.0
    assert lm["change_pct"] == 50.0

def test_spoken_trend_stable(db: FinanceDB) -> None:
    et = ExpenseTracker(db)
    # No data → fallback message
    result = et.spoken_trend()
    assert isinstance(result, str)

def test_spoken_trend_has_spike(db: FinanceDB) -> None:
    from datetime import datetime
    et = ExpenseTracker(db)
    now = datetime.now()
    if now.month == 1:
        prev_ts = datetime(now.year - 1, 12, 15).timestamp()
    else:
        prev_ts = datetime(now.year, now.month - 1, 15).timestamp()
    db.execute(
        "INSERT INTO expenses (ts, amount, currency, category, source) "
        "VALUES (?, ?, 'EUR', 'essen', 'manual')",
        (prev_ts, 20.0),
    )
    et.add_expense(80.0, "restaurant XYZ", "essen")  # +300 %
    result = et.spoken_trend()
    assert "essen" in result
    assert "%" in result
