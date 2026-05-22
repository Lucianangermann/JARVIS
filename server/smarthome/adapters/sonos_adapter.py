"""Sonos adapter via soco library — READY (disabled by default)."""
from __future__ import annotations

from typing import Any

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class SonosAdapter(BaseAdapter):
    platform_name = "sonos"

    def __init__(self) -> None:
        self._speakers: dict[str, Any] = {}

    async def connect(self) -> bool:
        try:
            import soco  # type: ignore[import]
            devices = list(soco.discover() or [])
            if not devices:
                self._log("No Sonos speakers found on network")
                return False
            for sp in devices:
                self._speakers[f"sonos:{sp.uid}"] = sp
            self._log(f"Found {len(devices)} Sonos speaker(s)")
            return True
        except ImportError:
            self._log("Disabled — install 'soco' library: pip install soco")
            return False
        except Exception as exc:  # noqa: BLE001
            self._log(f"Discovery failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        result = []
        for uid, sp in self._speakers.items():
            try:
                result.append(UniversalDevice(
                    id=uid,
                    name=sp.player_name,
                    platform="sonos",
                    type="speaker",
                    capabilities=["play", "pause", "volume", "group"],
                    raw_data={"ip": sp.ip_address},
                ))
            except Exception:  # noqa: BLE001
                pass
        return result

    async def turn_on(self, device_id: str) -> bool:
        return await self._play(device_id)

    async def turn_off(self, device_id: str) -> bool:
        return await self._pause(device_id)

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return await self._set_volume(device_id, level)

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def _play(self, device_id: str) -> bool:
        sp = self._speakers.get(device_id)
        if sp:
            try:
                sp.play()
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _pause(self, device_id: str) -> bool:
        sp = self._speakers.get(device_id)
        if sp:
            try:
                sp.pause()
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _set_volume(self, device_id: str, level: int) -> bool:
        sp = self._speakers.get(device_id)
        if sp:
            try:
                sp.volume = max(0, min(100, level))
                return True
            except Exception:  # noqa: BLE001
                pass
        return False
