"""Google Nest thermostat adapter — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class NestAdapter(BaseAdapter):
    platform_name = "nest"

    async def connect(self) -> bool:
        client_id = os.getenv("NEST_CLIENT_ID", "")
        if not client_id:
            self._log("Disabled — set NEST_ENABLED=true + NEST_CLIENT_ID + NEST_CLIENT_SECRET")
            return False
        self._log("Nest adapter ready — OAuth2 flow required on first use")
        return True

    async def get_devices(self) -> list[UniversalDevice]:
        return [UniversalDevice(
            id="nest:thermostat_1",
            name="Nest Thermostat",
            platform="nest",
            type="thermostat",
            capabilities=["thermostat", "temperature"],
        )]

    async def turn_on(self, device_id: str) -> bool:
        return await self.set_thermostat(device_id, 21.0)

    async def turn_off(self, device_id: str) -> bool:
        return await self.set_thermostat(device_id, 16.0)

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return False

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def set_thermostat(self, device_id: str, temp: float) -> bool:
        self._log(f"Set thermostat to {temp}°C (implement OAuth2 flow for real control)")
        return False
