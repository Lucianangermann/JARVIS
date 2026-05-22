"""IKEA Dirigera/Tradfri adapter — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class IKEAAdapter(BaseAdapter):
    platform_name = "ikea"

    async def connect(self) -> bool:
        hub_ip = os.getenv("IKEA_HUB_IP", "")
        token = os.getenv("IKEA_ACCESS_TOKEN", "")
        if not hub_ip or not token:
            self._log("Disabled — set IKEA_ENABLED=true + IKEA_HUB_IP + IKEA_ACCESS_TOKEN to activate")
            return False
        self._hub_ip = hub_ip
        self._token = token
        try:
            import requests
            resp = requests.get(
                f"https://{hub_ip}:8443/v1/devices",
                headers={"Authorization": f"Bearer {token}"},
                verify=False, timeout=5,
            )
            resp.raise_for_status()
            n = len(resp.json())
            self._log(f"Connected to IKEA hub — {n} devices")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"Hub connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        try:
            import requests
            resp = requests.get(
                f"https://{self._hub_ip}:8443/v1/devices",
                headers={"Authorization": f"Bearer {self._token}"},
                verify=False, timeout=5,
            )
            devices = resp.json()
            result = []
            for d in devices:
                attrs = d.get("attributes", {})
                result.append(UniversalDevice(
                    id=f"ikea:{d['id']}",
                    name=attrs.get("customName", d["id"]),
                    platform="ikea",
                    type="light",
                    capabilities=["on_off", "brightness", "color_temp"],
                    raw_data=d,
                ))
            return result
        except Exception:  # noqa: BLE001
            return []

    async def turn_on(self, device_id: str) -> bool:
        return await self._patch(device_id, {"isOn": True})

    async def turn_off(self, device_id: str) -> bool:
        return await self._patch(device_id, {"isOn": False})

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return await self._patch(device_id, {"lightLevel": level})

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False  # Tradfri bulbs don't support full RGB

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return await self._patch(device_id, {"colorTemperature": kelvin})

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def _patch(self, device_id: str, payload: dict) -> bool:
        did = device_id.removeprefix("ikea:")
        try:
            import requests
            requests.patch(
                f"https://{self._hub_ip}:8443/v1/devices/{did}",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"attributes": payload},
                verify=False, timeout=5,
            )
            return True
        except Exception:  # noqa: BLE001
            return False
