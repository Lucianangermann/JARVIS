"""Universal device registry — single source of truth across all platforms."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base_adapter import BaseAdapter, UniversalDevice

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "devices.json"

# Common room aliases
_ROOM_ALIASES: dict[str, str] = {
    "wohnzimmer": "wohnzimmer",
    "living": "wohnzimmer",
    "living room": "wohnzimmer",
    "schlafzimmer": "schlafzimmer",
    "bedroom": "schlafzimmer",
    "küche": "küche",
    "kitchen": "küche",
    "bad": "bad",
    "bathroom": "bad",
    "arbeitszimmer": "arbeitszimmer",
    "office": "arbeitszimmer",
    "büro": "arbeitszimmer",
    "flur": "flur",
    "hallway": "flur",
    "keller": "keller",
    "basement": "keller",
}


def _normalise_room(name: str) -> str:
    return _ROOM_ALIASES.get(name.lower(), name.lower())


class DeviceRegistry:
    """Aggregates devices from all active adapters."""

    def __init__(self) -> None:
        self._devices: dict[str, "UniversalDevice"] = {}
        self._adapters: dict[str, "BaseAdapter"] = {}

    def register_adapter(self, adapter: "BaseAdapter") -> None:
        self._adapters[adapter.platform_name] = adapter

    def update_devices(self, devices: list["UniversalDevice"]) -> None:
        for d in devices:
            self._devices[d.id] = d
        self._save_cache()

    def get_all(self) -> list["UniversalDevice"]:
        return list(self._devices.values())

    def get_by_id(self, device_id: str) -> "UniversalDevice | None":
        return self._devices.get(device_id)

    def get_by_name(self, name: str) -> "UniversalDevice | None":
        """Fuzzy match — case-insensitive, substring."""
        name_lower = name.lower()
        for d in self._devices.values():
            if d.name.lower() == name_lower:
                return d
        for d in self._devices.values():
            if name_lower in d.name.lower():
                return d
        return None

    def get_by_room(self, room: str) -> list["UniversalDevice"]:
        norm = _normalise_room(room)
        return [d for d in self._devices.values()
                if _normalise_room(d.room) == norm]

    def get_by_type(self, device_type: str) -> list["UniversalDevice"]:
        return [d for d in self._devices.values()
                if d.type.lower() == device_type.lower()]

    def get_adapter(self, device: "UniversalDevice") -> "BaseAdapter | None":
        return self._adapters.get(device.platform)

    async def refresh_all(self) -> None:
        for adapter in self._adapters.values():
            if adapter.enabled and adapter.connected:
                try:
                    devices = await adapter.get_devices()
                    self.update_devices(devices)
                except Exception as exc:  # noqa: BLE001
                    print(f"[REGISTRY] refresh failed for {adapter.platform_name}: {exc}")

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self._devices.values():
            counts[d.type] = counts.get(d.type, 0) + 1
        return counts

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [d.to_dict() for d in self._devices.values()]
            CACHE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[REGISTRY] cache write failed: {exc}")
