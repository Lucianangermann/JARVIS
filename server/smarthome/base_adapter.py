"""Abstract base class every platform adapter must implement."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeviceState:
    power: bool = False
    brightness: int = 100       # 0-100
    color: tuple[int, int, int] = (255, 255, 255)
    color_temp: int = 4000      # kelvin
    temperature: float = 0.0    # thermostats
    locked: bool = False        # locks
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalDevice:
    id: str                                     # "platform:device_id"
    name: str
    platform: str
    type: str                                   # light/plug/thermostat/lock/camera/speaker/tv/sensor
    room: str = ""
    state: DeviceState = field(default_factory=DeviceState)
    capabilities: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "type": self.type,
            "room": self.room,
            "state": {
                "power": self.state.power,
                "brightness": self.state.brightness,
                "color": list(self.state.color),
                "color_temp": self.state.color_temp,
                "temperature": self.state.temperature,
                "locked": self.state.locked,
                "online": self.state.online,
            },
            "capabilities": self.capabilities,
        }


class BaseAdapter(ABC):
    """Every platform adapter inherits from this."""

    platform_name: str = "base"
    enabled: bool = False
    connected: bool = False

    # ── required ───────────────────────────────────────────────────────── #

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection. Return True on success."""

    @abstractmethod
    async def get_devices(self) -> list[UniversalDevice]:
        """Return all devices as UniversalDevice objects."""

    @abstractmethod
    async def turn_on(self, device_id: str) -> bool: ...

    @abstractmethod
    async def turn_off(self, device_id: str) -> bool: ...

    @abstractmethod
    async def set_brightness(self, device_id: str, level: int) -> bool: ...

    @abstractmethod
    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool: ...

    @abstractmethod
    async def set_color_temp(self, device_id: str, kelvin: int) -> bool: ...

    @abstractmethod
    async def get_state(self, device_id: str) -> DeviceState: ...

    # ── optional (override where platform supports it) ──────────────────── #

    async def set_scene(self, device_id: str, scene: str) -> bool:
        return False

    async def get_energy(self, device_id: str) -> float:
        return 0.0

    async def set_thermostat(self, device_id: str, temp: float) -> bool:
        return False

    async def lock(self, device_id: str) -> bool:
        return False

    async def unlock(self, device_id: str) -> bool:
        return False

    async def get_camera_feed(self, device_id: str) -> bytes:
        return b""

    # ── helpers ─────────────────────────────────────────────────────────── #

    def _log(self, msg: str) -> None:
        tag = self.platform_name.upper()
        print(f"[{tag}] {msg}")
        logger.info(msg)

    def status_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform_name,
            "enabled": self.enabled,
            "connected": self.connected,
        }
