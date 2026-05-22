"""Samsung SmartThings + TV adapter — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class SamsungAdapter(BaseAdapter):
    platform_name = "samsung"

    def __init__(self) -> None:
        self._token: str = ""
        self._tv_ip: str = ""

    async def connect(self) -> bool:
        self._token = os.getenv("SAMSUNG_TOKEN", "")
        self._tv_ip = os.getenv("SAMSUNG_TV_IP", "")
        if not self._token and not self._tv_ip:
            self._log("Disabled — set SAMSUNG_ENABLED=true + SAMSUNG_TOKEN or SAMSUNG_TV_IP")
            return False
        self._log(f"Samsung adapter ready (SmartThings={'yes' if self._token else 'no'}, TV={'yes' if self._tv_ip else 'no'})")
        return True

    async def get_devices(self) -> list[UniversalDevice]:
        result = []
        if self._token:
            try:
                import requests
                resp = requests.get(
                    "https://api.smartthings.com/v1/devices",
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=10,
                )
                for d in resp.json().get("items", []):
                    result.append(UniversalDevice(
                        id=f"samsung:{d['deviceId']}",
                        name=d.get("label", d["deviceId"]),
                        platform="samsung",
                        type="plug",
                        capabilities=["on_off"],
                        raw_data=d,
                    ))
            except Exception:  # noqa: BLE001
                pass
        if self._tv_ip:
            result.append(UniversalDevice(
                id="samsung:tv_local",
                name="Samsung TV",
                platform="samsung",
                type="tv",
                capabilities=["on_off", "volume"],
                raw_data={"ip": self._tv_ip},
            ))
        return result

    async def turn_on(self, device_id: str) -> bool:
        return await self._send_key(device_id, "KEY_POWER")

    async def turn_off(self, device_id: str) -> bool:
        return await self._send_key(device_id, "KEY_POWER")

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return False

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()

    async def _send_key(self, device_id: str, key: str) -> bool:
        if not self._tv_ip:
            return False
        try:
            from samsungtvws import SamsungTVWS  # type: ignore[import]
            tv = SamsungTVWS(self._tv_ip)
            tv.shortcuts().mute()
            return True
        except ImportError:
            self._log("Install samsungtvws: pip install samsungtvws")
            return False
        except Exception:  # noqa: BLE001
            return False
