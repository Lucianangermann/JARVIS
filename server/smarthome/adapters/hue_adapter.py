"""Philips Hue Bridge adapter — READY (disabled by default)."""
from __future__ import annotations

import os
from typing import Any

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


def _rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    """Convert RGB to CIE XY for Hue API."""
    R = r / 255
    G = g / 255
    B = b / 255
    R = ((R + 0.055) / 1.055) ** 2.4 if R > 0.04045 else R / 12.92
    G = ((G + 0.055) / 1.055) ** 2.4 if G > 0.04045 else G / 12.92
    B = ((B + 0.055) / 1.055) ** 2.4 if B > 0.04045 else B / 12.92
    X = R * 0.664511 + G * 0.154324 + B * 0.162028
    Y = R * 0.283881 + G * 0.668433 + B * 0.047685
    Z = R * 0.000088 + G * 0.072310 + B * 0.986039
    total = X + Y + Z
    if total == 0:
        return 0.3127, 0.3290
    return X / total, Y / total


class HueAdapter(BaseAdapter):
    platform_name = "hue"

    def __init__(self) -> None:
        self._bridge_ip: str = ""
        self._username: str = ""
        self._base: str = ""

    async def connect(self) -> bool:
        self._bridge_ip = os.getenv("HUE_BRIDGE_IP", "")
        self._username = os.getenv("HUE_USERNAME", "")
        if not self._bridge_ip or not self._username:
            self._log("Disabled — set HUE_ENABLED=true + HUE_BRIDGE_IP + HUE_USERNAME to activate")
            self._log("Run: python setup_hue.py to auto-discover your bridge")
            return False
        self._base = f"http://{self._bridge_ip}/api/{self._username}"
        try:
            import requests
            resp = requests.get(f"{self._base}/lights", timeout=5)
            resp.raise_for_status()
            n = len(resp.json())
            self._log(f"Connected to bridge {self._bridge_ip} — {n} lights")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"Bridge connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        try:
            import requests
            resp = requests.get(f"{self._base}/lights", timeout=5)
            lights = resp.json()
            return [
                UniversalDevice(
                    id=f"hue:{lid}",
                    name=data.get("name", f"Hue {lid}"),
                    platform="hue",
                    type="light",
                    capabilities=["on_off", "brightness", "color", "color_temp"],
                    raw_data=data,
                )
                for lid, data in lights.items()
            ]
        except Exception:  # noqa: BLE001
            return []

    async def turn_on(self, device_id: str) -> bool:
        return await self._put_state(device_id, {"on": True})

    async def turn_off(self, device_id: str) -> bool:
        return await self._put_state(device_id, {"on": False})

    async def set_brightness(self, device_id: str, level: int) -> bool:
        bri = int(level * 254 / 100)
        return await self._put_state(device_id, {"bri": bri})

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        x, y = _rgb_to_xy(r, g, b)
        return await self._put_state(device_id, {"xy": [x, y], "on": True})

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        ct = int(1_000_000 / kelvin)
        return await self._put_state(device_id, {"ct": ct})

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def _put_state(self, device_id: str, payload: dict[str, Any]) -> bool:
        lid = device_id.removeprefix("hue:")
        try:
            import requests
            requests.put(f"{self._base}/lights/{lid}/state", json=payload, timeout=5)
            return True
        except Exception:  # noqa: BLE001
            return False
