"""Open-Meteo weather lookup. No API key required.

Open-Meteo (https://open-meteo.com/) exposes a free public API for
current weather + multi-day forecasts. We only need two endpoints —
geocoding to translate a place name into lat/lon, and the forecast
endpoint for the actual numbers.

All functions are best-effort: they return ``None`` (current) or
``[]`` (forecast) on any error and print a short ``[weather]`` log
line. Callers in the intelligence layer must handle the absence of
data so a network blip never breaks the morning briefing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import httpx

# WMO 4677 weather codes → short German labels. We only enumerate
# the buckets the briefing actually uses; anything outside this set
# falls back to "unbekannt" rather than leaking integer codes into
# the spoken text. Extend cautiously — every label is read aloud.
_CONDITION_LABEL: dict[int, str] = {
    0:  "klar",
    1:  "überwiegend klar",
    2:  "teils bewölkt",
    3:  "bedeckt",
    45: "Nebel",
    48: "Nebel mit Reif",
    51: "leichter Nieselregen",
    53: "Nieselregen",
    55: "starker Nieselregen",
    61: "leichter Regen",
    63: "Regen",
    65: "starker Regen",
    71: "leichter Schneefall",
    73: "Schneefall",
    75: "starker Schneefall",
    80: "leichte Schauer",
    81: "Schauer",
    82: "starke Schauer",
    95: "Gewitter",
    96: "Gewitter mit Hagel",
    99: "Gewitter mit starkem Hagel",
}

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = 5.0


@dataclass(frozen=True)
class Weather:
    temp_c: float
    condition: str
    precipitation_mm: float
    wind_kmh: float
    location_label: str


@dataclass(frozen=True)
class DailyForecast:
    date: date
    temp_min_c: float
    temp_max_c: float
    condition: str
    precipitation_mm: float


def _default_location() -> str:
    return (os.getenv("WEATHER_LOCATION") or "Berlin,DE").strip()


def _condition_label(code: int | float) -> str:
    try:
        return _CONDITION_LABEL.get(int(code), "unbekannt")
    except (TypeError, ValueError):
        return "unbekannt"


def _resolve_location(query: str) -> tuple[float, float, str] | None:
    """Open-Meteo geocoding: ``"Berlin,DE"`` → ``(lat, lon, "Berlin, DE")``.

    The Open-Meteo endpoint treats its ``name`` param as a single literal
    so ``"Berlin,DE"`` returns nothing. We split a "city,CC" query on the
    comma and filter the result list by country code on our side — that
    lets the user write the natural ``Berlin,DE`` form in .env without
    accidentally matching the wrong city in a different country.
    """
    name_part, _, cc_filter = query.partition(",")
    name_part = name_part.strip()
    cc_filter = cc_filter.strip().upper()
    if not name_part:
        return None
    try:
        r = httpx.get(
            _GEOCODE_URL,
            params={
                "name": name_part,
                # Pull a handful and pick the country match below.
                "count": 5 if cc_filter else 1,
                "language": "de",
            },
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            print(f"[weather] geocode miss for {query!r}")
            return None
        top = None
        if cc_filter:
            top = next(
                (rec for rec in results
                 if (rec.get("country_code") or "").upper() == cc_filter),
                None,
            )
        top = top or results[0]
        name = top.get("name") or name_part
        cc = (top.get("country_code") or "").upper()
        label = f"{name}, {cc}" if cc else name
        return float(top["latitude"]), float(top["longitude"]), label
    except Exception as exc:  # noqa: BLE001 — network/parse, log + degrade
        print(f"[weather] geocode error for {query!r}: {exc}")
        return None


def get_current(location: str | None = None) -> Weather | None:
    """Right-now temperature + condition for the configured location.

    Returns ``None`` on any failure (no network, geocode miss, malformed
    response) — the caller MUST tolerate that.
    """
    query = location or _default_location()
    resolved = _resolve_location(query)
    if resolved is None:
        return None
    lat, lon, label = resolved
    try:
        r = httpx.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,precipitation,"
                            "wind_speed_10m",
                "timezone": "auto",
            },
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        cur = r.json().get("current") or {}
        if not cur:
            print("[weather] empty 'current' block in forecast response")
            return None
        return Weather(
            temp_c=float(cur.get("temperature_2m", 0.0)),
            condition=_condition_label(cur.get("weather_code", -1)),
            precipitation_mm=float(cur.get("precipitation", 0.0)),
            wind_kmh=float(cur.get("wind_speed_10m", 0.0)),
            location_label=label,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[weather] current-weather error: {exc}")
        return None


def get_forecast(location: str | None = None,
                 days: int = 3) -> list[DailyForecast]:
    """Daily min/max/condition for ``days`` days starting today.

    Returns an empty list on any failure.
    """
    query = location or _default_location()
    resolved = _resolve_location(query)
    if resolved is None:
        return []
    lat, lon, _ = resolved
    days = max(1, min(int(days), 7))
    try:
        r = httpx.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_min,temperature_2m_max,"
                          "weather_code,precipitation_sum",
                "timezone": "auto",
                "forecast_days": days,
            },
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        daily = r.json().get("daily") or {}
        dates = daily.get("time") or []
        tmin = daily.get("temperature_2m_min") or []
        tmax = daily.get("temperature_2m_max") or []
        codes = daily.get("weather_code") or []
        precs = daily.get("precipitation_sum") or []
        out: list[DailyForecast] = []
        for i, dstr in enumerate(dates):
            try:
                out.append(DailyForecast(
                    date=date.fromisoformat(dstr),
                    temp_min_c=float(tmin[i]),
                    temp_max_c=float(tmax[i]),
                    condition=_condition_label(codes[i]),
                    precipitation_mm=float(precs[i]),
                ))
            except (IndexError, ValueError, TypeError):
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[weather] forecast error: {exc}")
        return []
