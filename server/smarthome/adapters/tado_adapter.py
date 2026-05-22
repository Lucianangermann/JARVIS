"""Tado thermostat adapter — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class TadoAdapter(BaseAdapter):
    platform_name = "tado"

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._home_id: int = 0

    async def connect(self) -> bool:
        self._username = os.getenv("TADO_USERNAME", "")
        self._password = os.getenv("TADO_PASSWORD", "")
        if not self._username:
            self._log("Disabled — set TADO_ENABLED=true + TADO_USERNAME + TADO_PASSWORD")
            return False
        try:
            import PyTado.interface as tado  # type: ignore[import]
            t = tado.Tado(self._username, self._password)
            me = t.getMe()
            self._home_id = me["homes"][0]["id"]
            self._tado = t
            self._log(f"Connected to Tado home ID {self._home_id}")
            return True
        except ImportError:
            self._log("Install PyTado: pip install python-tado")
            return False
        except Exception as exc:  # noqa: BLE001
            self._log(f"Connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        return []

    async def turn_on(self, device_id: str) -> bool:
        return False

    async def turn_off(self, device_id: str) -> bool:
        return False

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return False

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def set_thermostat(self, device_id: str, temp: float) -> bool:
        zone_id = device_id.removeprefix("tado:")
        try:
            self._tado.setZoneOverlay(
                self._home_id, int(zone_id), "HEATING", temp, "MANUAL"
            )
            return True
        except Exception:  # noqa: BLE001
            return False
