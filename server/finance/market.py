"""Market data + watchlist + price alerts.

Prices come from the free Yahoo Finance chart endpoint (no API key) —
one source covers stocks, ETFs, and crypto (e.g. AAPL, SAP.DE, BTC-EUR).
CoinGecko is a crypto fallback. A background poll loop refreshes watched
symbols and fires a NotificationCenter alert (rising-edge, then disarms)
when a price crosses a configured target, so you're told once, not every
tick.

All network calls are short-timeout and best-effort: an outage degrades
to a "konnte nicht abrufen" rather than raising.
"""
from __future__ import annotations

import threading
import time
from typing import Any

try:
    import httpx  # type: ignore[import-not-found]
    _HTTPX_OK = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]
    _HTTPX_OK = False

_YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
_CG = "https://api.coingecko.com/api/v3/simple/price"
_UA = {"User-Agent": "Mozilla/5.0 (JARVIS finance)"}
_TIMEOUT = 8.0

# Common crypto names → CoinGecko ids (fallback only).
_CG_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
           "ADA": "cardano", "XRP": "ripple", "DOGE": "dogecoin"}


class MarketManager:
    def __init__(self, db: Any, notification_center: Any = None,
                 poll_interval_s: int = 900) -> None:
        self._db = db
        self._nc = notification_center
        self._interval = max(60, int(poll_interval_s))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── price fetch ────────────────────────────────────────────────────── #

    def fetch_price(self, symbol: str) -> dict[str, Any] | None:
        """Return {price, currency, symbol} or None. Yahoo first, CoinGecko
        fallback for known crypto tickers."""
        if not _HTTPX_OK:
            return None
        sym = symbol.upper()
        yf = self._fetch_yahoo(sym)
        if yf is not None:
            return yf
        # Crypto fallback (strip a -EUR/-USD suffix to get the base ticker).
        base = sym.split("-")[0]
        if base in _CG_IDS:
            return self._fetch_coingecko(base, sym)
        return None

    def _fetch_yahoo(self, symbol: str) -> dict[str, Any] | None:
        try:
            r = httpx.get(_YF.format(sym=symbol), headers=_UA,
                          timeout=_TIMEOUT, follow_redirects=True)
            if r.status_code != 200:
                return None
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            if price is None:
                return None
            return {"symbol": symbol, "price": float(price),
                    "currency": meta.get("currency", "USD")}
        except Exception as exc:  # noqa: BLE001
            print(f"[Market] yahoo {symbol} failed: {exc}")
            return None

    def _fetch_coingecko(self, base: str, symbol: str) -> dict[str, Any] | None:
        try:
            vs = "eur" if symbol.endswith("EUR") else "usd"
            r = httpx.get(_CG, params={"ids": _CG_IDS[base], "vs_currencies": vs},
                          timeout=_TIMEOUT)
            price = r.json().get(_CG_IDS[base], {}).get(vs)
            if price is None:
                return None
            return {"symbol": symbol, "price": float(price),
                    "currency": vs.upper()}
        except Exception as exc:  # noqa: BLE001
            print(f"[Market] coingecko {base} failed: {exc}")
            return None

    # ── watchlist ──────────────────────────────────────────────────────── #

    def add_to_watchlist(self, symbol: str, name: str | None = None,
                         asset_type: str = "stock", quantity: float = 0,
                         target_above: float | None = None,
                         target_below: float | None = None) -> dict[str, Any]:
        sym = symbol.upper()
        price = self.fetch_price(sym)
        if price is None:
            return {"ok": False,
                    "spoken": f"Symbol {sym} nicht gefunden oder kein Kurs."}
        self._db.add_watch(sym, name, asset_type, quantity,
                           target_above, target_below)
        self._db.update_watch_price(sym, price["price"], price["currency"])
        return {"ok": True, "price": price,
                "spoken": (f"{sym} zur Watchlist hinzugefügt. "
                           f"Aktuell {price['price']:.2f} {price['currency']}.")}

    def remove_from_watchlist(self, symbol: str) -> str:
        ok = self._db.remove_watch(symbol)
        return (f"{symbol.upper()} entfernt." if ok
                else f"{symbol.upper()} war nicht in der Watchlist.")

    def refresh_prices(self) -> list[dict[str, Any]]:
        """Fetch current prices for all watched symbols, update the DB, and
        check alerts. Returns the enriched watchlist."""
        out: list[dict[str, Any]] = []
        for w in self._db.get_watchlist():
            p = self.fetch_price(w["symbol"])
            if p is not None:
                self._db.update_watch_price(w["symbol"], p["price"], p["currency"])
                w = {**w, "last_price": p["price"], "last_currency": p["currency"]}
                self._check_alert(w)
            out.append(w)
        return out

    def portfolio_value(self) -> dict[str, Any]:
        """Sum quantity × last price per currency."""
        totals: dict[str, float] = {}
        holdings = []
        for w in self.refresh_prices():
            qty = w.get("quantity") or 0
            price = w.get("last_price") or 0
            cur = w.get("last_currency") or "EUR"
            if qty and price:
                value = qty * price
                totals[cur] = totals.get(cur, 0) + value
                holdings.append({"symbol": w["symbol"], "quantity": qty,
                                 "price": price, "value": round(value, 2),
                                 "currency": cur})
        return {"totals": {k: round(v, 2) for k, v in totals.items()},
                "holdings": holdings}

    def spoken_watchlist(self) -> str:
        items = self.refresh_prices()
        if not items:
            return "Deine Watchlist ist leer."
        parts = [
            f"{w['symbol']} {w['last_price']:.2f} {w.get('last_currency', '')}"
            for w in items if w.get("last_price")]
        if not parts:
            return "Konnte gerade keine Kurse abrufen."
        return "Watchlist: " + ", ".join(parts) + "."

    # ── alerts ─────────────────────────────────────────────────────────── #

    def _check_alert(self, w: dict[str, Any]) -> None:
        if not w.get("alert_armed"):
            return
        price = w.get("last_price")
        if price is None:
            return
        sym, cur = w["symbol"], w.get("last_currency", "")
        above, below = w.get("target_above"), w.get("target_below")
        fired = None
        if above is not None and price >= above:
            fired = (f"{sym} ist über {above:.2f} {cur} gestiegen — "
                     f"aktuell {price:.2f} {cur}.")
        elif below is not None and price <= below:
            fired = (f"{sym} ist unter {below:.2f} {cur} gefallen — "
                     f"aktuell {price:.2f} {cur}.")
        if fired:
            self._db.set_alert_armed(sym, False)  # rising-edge: fire once
            if self._nc is not None:
                try:
                    self._nc.send("Kursalarm", fired, "high", "finance")
                except Exception as exc:  # noqa: BLE001
                    print(f"[Market] alert notify failed: {exc}")
            else:
                print(f"[Market] ALERT: {fired}")

    # ── background poll ────────────────────────────────────────────────── #

    def start(self) -> None:
        if not _HTTPX_OK or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-market",
                                        daemon=True)
        self._thread.start()
        print(f"[Market] price poll active (every {self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Only poll when there's something to watch with an armed alert.
            try:
                if any(w.get("alert_armed") and
                       (w.get("target_above") or w.get("target_below"))
                       for w in self._db.get_watchlist()):
                    self.refresh_prices()
            except Exception as exc:  # noqa: BLE001
                print(f"[Market] poll tick failed: {exc}")
            self._stop.wait(self._interval)
