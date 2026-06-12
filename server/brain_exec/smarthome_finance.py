"""SmartHomeFinanceExecMixin — SmartHome and Finance tool handlers.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from typing import Any


class SmartHomeFinanceExecMixin:
    """Exec methods for SmartHome control and the Finance layer
    (expenses, budgets, market watchlist, subscriptions)."""

    def _exec_manage_automation(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """List, create, enable, or disable SmartHome time automations."""
        sm = getattr(self, "smarthome", None)  # type: ignore[attr-defined]
        if sm is None or getattr(sm, "automations", None) is None:
            return "SmartHome nicht verfügbar.", True
        eng = sm.automations
        inp = tool_input or {}
        action = inp.get("action", "list")

        if action == "list":
            items = eng.all_automations()
            if not items:
                return "Keine Automationen konfiguriert.", False
            lines = []
            for a in items:
                status = "✓" if a.get("enabled") else "✗"
                t = a.get("time", "")
                days = a.get("days", ["daily"])
                scene = a.get("scene", "")
                lines.append(
                    f"[{status}] {a['name']} — {t} ({', '.join(days)}) → {scene}"
                )
            return "\n".join(lines), False

        if action in ("enable", "disable"):
            aid = inp.get("id", "")
            if not aid:
                return "id ist erforderlich.", True
            ok = eng.enable(aid, action == "enable")
            if not ok:
                return f"Automation '{aid}' nicht gefunden.", True
            label = "aktiviert" if action == "enable" else "deaktiviert"
            return f"Automation {label}.", False

        if action == "create":
            name = inp.get("name", "")
            scene = inp.get("scene", "")
            trigger_time = inp.get("time", "")
            days_raw = inp.get("days", "daily")
            if not name or not scene:
                return "name und scene sind erforderlich.", True
            days = [days_raw] if isinstance(days_raw, str) else list(days_raw)
            import asyncio as _aio
            from .. import events as _events
            try:
                coro = eng.create(
                    name=name, trigger="time", scene=scene,
                    time=trigger_time, days=days,
                )
                main_loop = _events._loop
                if main_loop is not None and main_loop.is_running():
                    auto = _aio.run_coroutine_threadsafe(coro, main_loop).result(timeout=5)
                else:
                    auto = _aio.run(coro)
            except Exception as exc:  # noqa: BLE001
                return f"Automation konnte nicht erstellt werden: {exc}", True
            return (f"Automation '{name}' erstellt (ID: {auto['id']}). "
                    f"Szene '{scene}' wird täglich um {trigger_time} ausgeführt."), False

        if action == "delete":
            aid = inp.get("id", "")
            if not aid:
                return "id ist erforderlich.", True
            items = eng.all_automations()
            before = len(items)
            eng._automations = [a for a in items if a.get("id") != aid]
            if len(eng._automations) == before:
                return f"Automation '{aid}' nicht gefunden.", True
            eng._save()
            return "Automation gelöscht.", False

        return f"Unbekannte Aktion: {action}", True

    def _exec_smarthome_tool(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch a smarthome_control tool_use to the SmartHomeManager.

        Brain runs in a worker thread (asyncio.to_thread), so we
        schedule the coroutine on the main event loop via
        run_coroutine_threadsafe and wait for the result synchronously.
        Falls back to asyncio.run() if the main loop isn't captured yet
        (e.g. unit tests)."""
        import asyncio as _aio
        from ..smarthome.tools.smarthome_tools import execute_smarthome_tool
        from .. import events as _events
        inp = tool_input or {}
        try:
            coro = execute_smarthome_tool(
                self.smarthome,  # type: ignore[attr-defined]
                action=inp.get("action", ""),
                command=inp.get("command"),
                scene=inp.get("scene"),
                device=inp.get("device"),
                level=inp.get("level"),
                color=inp.get("color"),
            )
            main_loop = _events._loop
            if main_loop is not None and main_loop.is_running():
                future = _aio.run_coroutine_threadsafe(coro, main_loop)
                result = future.result(timeout=15)
            else:
                result = _aio.run(coro)
            return (result, False)
        except Exception as exc:  # noqa: BLE001
            return (f"Smart Home Fehler: {exc}", True)

    def _get_finance(self) -> Any:
        """Lazily build the finance manager. Shares the brain's Claude client
        (categorisation). No notification center on the lazy path — price
        alerts just print until main.py wires the real manager."""
        fm = getattr(self, "_finance", None)
        if fm is None:
            try:
                from pathlib import Path as _Path
                from ..finance import FinanceManager as _FM
                _db = _Path(__file__).resolve().parents[2] / "data" / "finance.db"
                fm = _FM(_db, client=self.client)  # type: ignore[attr-defined]
                self._finance = fm  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                print(f"[Brain] finance init failed: {exc}")
                self._finance = None  # type: ignore[attr-defined]
        return self._finance  # type: ignore[attr-defined]

    def _exec_finance(self, inp: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch the finance tool to the FinanceManager."""
        try:
            fm = self._get_finance()
            if fm is None:
                return "Finanz-Layer nicht verfügbar.", True
            inp = inp or {}
            action = inp.get("action", "")

            if action == "add_expense":
                amount = float(inp.get("amount") or 0)
                r = fm.expenses.add_expense(
                    amount, inp.get("merchant", ""), inp.get("description", ""),
                    inp.get("category"))
                return r["spoken"], not r["ok"]
            if action == "summary":
                return fm.expenses.spoken_month_summary(), False
            if action == "set_budget":
                cat, amt = inp.get("category", ""), inp.get("amount")
                if not cat or amt is None:
                    return "category und amount sind erforderlich.", True
                return fm.expenses.set_budget(cat, float(amt)), False
            if action == "budget_status":
                rows = fm.expenses.budget_status()
                if not rows:
                    return "Keine Budgets gesetzt.", False
                parts = [f"{b['category']}: {b['spent']:.0f} von {b['limit']:.0f} "
                         f"{b['currency']}" for b in rows]
                return "Budgets — " + "; ".join(parts) + ".", False
            if action == "watch_add":
                sym = inp.get("symbol", "")
                if not sym:
                    return "symbol ist erforderlich.", True
                r = fm.market.add_to_watchlist(
                    sym, asset_type="crypto" if "-" in sym else "stock",
                    quantity=float(inp.get("quantity") or 0),
                    target_above=inp.get("target_above"),
                    target_below=inp.get("target_below"))
                return r["spoken"], not r["ok"]
            if action == "watch_remove":
                return fm.market.remove_from_watchlist(inp.get("symbol", "")), False
            if action == "watchlist":
                return fm.market.spoken_watchlist(), False
            if action == "portfolio":
                pv = fm.market.portfolio_value()
                if not pv["totals"]:
                    return "Kein Portfolio erfasst (Stückzahlen fehlen).", False
                parts = [f"{v:.0f} {k}" for k, v in pv["totals"].items()]
                return "Portfolio-Wert: " + ", ".join(parts) + ".", False
            if action == "price":
                sym = inp.get("symbol", "")
                p = fm.market.fetch_price(sym) if sym else None
                return ((f"{sym.upper()} steht bei {p['price']:.2f} {p['currency']}.",
                         False) if p else (f"Kein Kurs für {sym}.", True))
            if action == "subscriptions":
                return fm.subscriptions.spoken_summary(), False
            return f"Unbekannte action: {action}", True
        except Exception as exc:  # noqa: BLE001
            return f"Finanz-Fehler: {exc}", True
