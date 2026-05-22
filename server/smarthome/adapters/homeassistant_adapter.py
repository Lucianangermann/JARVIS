"""Home Assistant REST API adapter — READY (disabled by default)."""
from __future__ import annotations

import os
from typing import Any

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice

_HA_TYPE_MAP = {
    "light": "light",
    "switch": "plug",
    "climate": "thermostat",
    "lock": "lock",
    "camera": "camera",
    "media_player": "speaker",
    "binary_sensor": "sensor",
    "sensor": "sensor",
}


class HomeAssistantAdapter(BaseAdapter):
    platform_name = "homeassistant"

    def __init__(self) -> None:
        self._base: str = ""
        self._token: str = ""

    async def connect(self) -> bool:
        self._base = os.getenv("HA_URL", "").rstrip("/")
        self._token = os.getenv("HA_TOKEN", "")
        if not self._base or not self._token:
            self._log("Disabled — set HA_ENABLED=true + HA_URL + HA_TOKEN to activate")
            self._log("HA_URL example: http://homeassistant.local:8123")
            return False
        try:
            import requests
            resp = requests.get(
                f"{self._base}/api/",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5,
            )
            resp.raise_for_status()
            self._log(f"Connected to Home Assistant at {self._base}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"Connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        try:
            import requests
            resp = requests.get(
                f"{self._base}/api/states",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10,
            )
            states = resp.json()
            result = []
            for state in states:
                entity_id = state["entity_id"]
                domain = entity_id.split(".")[0]
                if domain not in _HA_TYPE_MAP:
                    continue
                attrs = state.get("attributes", {})
                result.append(UniversalDevice(
                    id=f"ha:{entity_id}",
                    name=attrs.get("friendly_name", entity_id),
                    platform="homeassistant",
                    type=_HA_TYPE_MAP[domain],
                    capabilities=["on_off"],
                    raw_data=state,
                ))
            return result
        except Exception:  # noqa: BLE001
            return []

    async def turn_on(self, device_id: str) -> bool:
        return await self._call_service(device_id, "turn_on")

    async def turn_off(self, device_id: str) -> bool:
        return await self._call_service(device_id, "turn_off")

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return await self._call_service(
            device_id, "turn_on", {"brightness_pct": level}
        )

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return await self._call_service(
            device_id, "turn_on", {"rgb_color": [r, g, b]}
        )

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return await self._call_service(
            device_id, "turn_on", {"color_temp_kelvin": kelvin}
        )

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def set_thermostat(self, device_id: str, temp: float) -> bool:
        return await self._call_service(
            device_id, "set_temperature", {"temperature": temp},
            domain="climate"
        )

    async def _call_service(self, device_id: str, service: str,
                            data: dict[str, Any] | None = None,
                            domain: str | None = None) -> bool:
        entity_id = device_id.removeprefix("ha:")
        if domain is None:
            domain = entity_id.split(".")[0]
        payload = {"entity_id": entity_id, **(data or {})}
        try:
            import requests
            requests.post(
                f"{self._base}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
                timeout=5,
            )
            return True
        except Exception:  # noqa: BLE001
            return False
