"""Geofencing — location-based automation triggers from PWA."""
from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .automations import AutomationEngine

_HOME_LAT = float(os.getenv("HOME_LAT", "0") or "0")
_HOME_LON = float(os.getenv("HOME_LON", "0") or "0")
_HOME_RADIUS_M = float(os.getenv("HOME_RADIUS_METERS", "200") or "200")


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class GeofencingEngine:
    def __init__(self, automations: "AutomationEngine") -> None:
        self._automations = automations
        self._was_home: bool | None = None

    async def update_location(self, lat: float, lon: float) -> dict[str, object]:
        if _HOME_LAT == 0 and _HOME_LON == 0:
            return {"status": "geofencing_disabled", "reason": "HOME_LAT/HOME_LON not set"}

        distance = _haversine(lat, lon, _HOME_LAT, _HOME_LON)
        is_home = distance <= _HOME_RADIUS_M

        event: str | None = None
        if self._was_home is not None and is_home != self._was_home:
            event = "arrival" if is_home else "departure"
            print(f"[GEO] {event.upper()} — distance={distance:.0f}m")
            await self._automations.fire(event)

        self._was_home = is_home
        return {
            "is_home": is_home,
            "distance_m": round(distance, 1),
            "event": event,
        }
