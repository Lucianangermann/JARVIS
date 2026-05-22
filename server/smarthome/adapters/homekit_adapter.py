"""Apple HomeKit adapter via HomeBridge REST API — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class HomeKitAdapter(BaseAdapter):
    platform_name = "homekit"

    def __init__(self) -> None:
        self._base: str = ""
        self._token: str = ""

    async def connect(self) -> bool:
        url = os.getenv("HOMEBRIDGE_URL", "")
        self._token = os.getenv("HOMEBRIDGE_TOKEN", "")
        if not url:
            self._log("Disabled — set HOMEKIT_ENABLED=true + HOMEBRIDGE_URL + HOMEBRIDGE_TOKEN to activate")
            self._log("Install HomeBridge: https://homebridge.io")
            return False
        self._base = url.rstrip("/")
        try:
            import requests
            resp = requests.get(
                f"{self._base}/api/accessories",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5,
            )
            resp.raise_for_status()
            n = len(resp.json())
            self._log(f"Connected to HomeBridge — {n} accessories")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"HomeBridge connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        try:
            import requests
            resp = requests.get(
                f"{self._base}/api/accessories",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5,
            )
            accessories = resp.json()
            result = []
            for acc in accessories:
                service = acc.get("serviceCharacteristics", [{}])[0]
                dtype = self._map_type(acc.get("type", ""))
                result.append(UniversalDevice(
                    id=f"homekit:{acc['uniqueId']}",
                    name=acc.get("serviceName", "HomeKit Device"),
                    platform="homekit",
                    type=dtype,
                    capabilities=["on_off"],
                    raw_data=acc,
                ))
            return result
        except Exception:  # noqa: BLE001
            return []

    def _map_type(self, hk_type: str) -> str:
        mapping = {
            "Lightbulb": "light", "Outlet": "plug", "Switch": "plug",
            "Thermostat": "thermostat", "LockMechanism": "lock",
            "SecurityCamera": "camera", "Speaker": "speaker",
        }
        return mapping.get(hk_type, "device")

    async def turn_on(self, device_id: str) -> bool:
        return await self._set_char(device_id, "On", True)

    async def turn_off(self, device_id: str) -> bool:
        return await self._set_char(device_id, "On", False)

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return await self._set_char(device_id, "Brightness", level)

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        import colorsys
        h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
        await self._set_char(device_id, "Hue", h * 360)
        await self._set_char(device_id, "Saturation", s * 100)
        return True

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def _set_char(self, device_id: str, char: str, value: object) -> bool:
        uid = device_id.removeprefix("homekit:")
        try:
            import requests
            requests.put(
                f"{self._base}/api/accessories/{uid}",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"characteristicType": char, "value": value},
                timeout=5,
            )
            return True
        except Exception:  # noqa: BLE001
            return False
