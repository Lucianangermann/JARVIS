"""Travel time lookup via OSRM. No API key required.

Routes through router.project-osrm.org (Project OSRM's public demo
server) using OpenStreetMap data. We use it for the morning /
evening / lunch briefings to answer "how long will it take to get
there?" — the same question Apple Maps and Google Maps answer with
real-time traffic, only without the real-time bit. The public OSRM
endpoint serves *free-flow* travel time (no live congestion), so
treat the result as a baseline; for a heavy-traffic warning a paid
service would be needed.

All functions are best-effort: they return ``None`` on any failure
(network, malformed response, unresolved location) and print a
short ``[traffic]`` log line. Callers must handle the absence of
data so a transient OSRM outage doesn't break a briefing.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import weather  # for the geocoder; we share the same lookup

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_HTTP_TIMEOUT = 7.0


@dataclass(frozen=True)
class TravelEstimate:
    minutes: float
    km: float
    origin_label: str
    destination_label: str
    profile: str            # "driving" (only one for now)

    def __str__(self) -> str:
        return (f"{self.origin_label} → {self.destination_label}: "
                f"{self.minutes:.0f} min, {self.km:.1f} km")


def get_travel_time(origin: str,
                    destination: str) -> TravelEstimate | None:
    """Estimate driving time + distance between two location queries.

    Inputs are free-form strings (eg. ``"Berlin"``, ``"München,DE"``,
    ``"Alexanderplatz Berlin"``) — same format the weather module's
    geocoder accepts.

    Returns ``None`` if either location can't be resolved or the
    OSRM lookup fails.
    """
    src = weather._resolve_location(origin)
    if src is None:
        return None
    dst = weather._resolve_location(destination)
    if dst is None:
        return None
    s_lat, s_lon, s_label = src
    d_lat, d_lon, d_label = dst

    # OSRM expects `lon,lat;lon,lat` — note the flipped order vs.
    # most other geocoding APIs.
    coords = f"{s_lon},{s_lat};{d_lon},{d_lat}"
    url = f"{_OSRM_URL}/{coords}"
    try:
        r = httpx.get(
            url,
            params={"overview": "false", "alternatives": "false"},
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — log + degrade
        print(f"[traffic] OSRM request failed: {exc}")
        return None

    if data.get("code") != "Ok":
        print(f"[traffic] OSRM code={data.get('code')!r}, "
              f"message={data.get('message')!r}")
        return None

    routes = data.get("routes") or []
    if not routes:
        print("[traffic] OSRM returned no routes")
        return None
    top = routes[0]
    try:
        seconds = float(top["duration"])
        meters = float(top["distance"])
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[traffic] OSRM response missing fields: {exc}")
        return None

    return TravelEstimate(
        minutes=seconds / 60.0,
        km=meters / 1000.0,
        origin_label=s_label,
        destination_label=d_label,
        profile="driving",
    )
