"""Claude tool_use definition for brain.py finance integration."""
from __future__ import annotations

from typing import Any


def finance_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "finance",
            "description": (
                "Personal finance. Actions: 'add_expense' (amount + merchant/"
                "description, auto-categorised) to log a spend; 'summary' for "
                "this month's spending; 'set_budget' (category + amount) for a "
                "monthly budget; 'budget_status'; 'watch_add' (symbol like AAPL "
                "/ SAP.DE / BTC-EUR, optional quantity + target_above/"
                "target_below price alerts); 'watch_remove'; 'watchlist' for "
                "live prices; 'portfolio' for holdings value; 'price' (symbol) "
                "for a quick quote; 'subscriptions' to list detected abos. "
                "Use for 'ich habe X für Y ausgegeben', 'wie viel diesen "
                "Monat', 'beobachte Apple-Aktie', 'wie steht Bitcoin'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_expense", "summary", "set_budget",
                                 "budget_status", "watch_add", "watch_remove",
                                 "watchlist", "portfolio", "price",
                                 "subscriptions"],
                    },
                    "amount": {"type": "number"},
                    "merchant": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "symbol": {"type": "string",
                               "description": "Ticker e.g. AAPL, SAP.DE, BTC-EUR."},
                    "quantity": {"type": "number"},
                    "target_above": {"type": "number"},
                    "target_below": {"type": "number"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    ]
