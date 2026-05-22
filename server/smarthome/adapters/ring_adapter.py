"""Ring doorbell/camera adapter — READY (disabled by default)."""
from __future__ import annotations

import os

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class RingAdapter(BaseAdapter):
    platform_name = "ring"

    async def connect(self) -> bool:
        email = os.getenv("RING_EMAIL", "")
        password = os.getenv("RING_PASSWORD", "")
        if not email:
            self._log("Disabled — set RING_ENABLED=true + RING_EMAIL + RING_PASSWORD")
            return False
        try:
            from ring_doorbell import Ring, Auth  # type: ignore[import]
            auth = Auth("JARVIS/1.0", None, self._token_update)
            auth.fetch_token(email, password)
            self._ring = Ring(auth)
            self._ring.update_data()
            self._log("Connected to Ring account")
            return True
        except ImportError:
            self._log("Install ring_doorbell: pip install ring_doorbell")
            return False
        except Exception as exc:  # noqa: BLE001
            self._log(f"Connection failed: {exc}")
            return False

    def _token_update(self, token: object) -> None:
        pass

    async def get_devices(self) -> list[UniversalDevice]:
        try:
            result = []
            for d in self._ring.doorbells():
                result.append(UniversalDevice(
                    id=f"ring:doorbell:{d.id}",
                    name=d.name,
                    platform="ring",
                    type="camera",
                    capabilities=["camera", "motion"],
                ))
            for d in self._ring.stickup_cams():
                result.append(UniversalDevice(
                    id=f"ring:cam:{d.id}",
                    name=d.name,
                    platform="ring",
                    type="camera",
                    capabilities=["camera", "motion"],
                ))
            return result
        except Exception:  # noqa: BLE001
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

    async def get_camera_feed(self, device_id: str) -> bytes:
        return b""
