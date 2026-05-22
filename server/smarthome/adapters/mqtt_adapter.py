"""Generic MQTT adapter for DIY/Zigbee/Z-Wave devices — READY (disabled by default)."""
from __future__ import annotations

import os
from typing import Any, Callable

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice


class MQTTAdapter(BaseAdapter):
    platform_name = "mqtt"

    def __init__(self) -> None:
        self._client: Any = None
        self._broker: str = ""
        self._devices_discovered: dict[str, UniversalDevice] = {}

    async def connect(self) -> bool:
        self._broker = os.getenv("MQTT_BROKER_URL", "")
        if not self._broker:
            self._log("Disabled — set MQTT_ENABLED=true + MQTT_BROKER_URL to activate")
            self._log("Example: MQTT_BROKER_URL=mqtt://localhost:1883")
            return False
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import]
            host = self._broker.replace("mqtt://", "").split(":")[0]
            port = int(self._broker.split(":")[-1]) if ":" in self._broker else 1883

            self._client = mqtt.Client()
            self._client.on_message = self._on_message
            self._client.connect(host, port, 60)
            self._client.subscribe("#")
            self._client.loop_start()
            self._log(f"Connected to MQTT broker {host}:{port}")
            return True
        except ImportError:
            self._log("Install paho-mqtt: pip install paho-mqtt")
            return False
        except Exception as exc:  # noqa: BLE001
            self._log(f"Connection failed: {exc}")
            return False

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        topic = message.topic
        if topic not in self._devices_discovered:
            self._devices_discovered[topic] = UniversalDevice(
                id=f"mqtt:{topic}",
                name=topic.replace("/", " ").title(),
                platform="mqtt",
                type="device",
                capabilities=["on_off"],
            )

    async def get_devices(self) -> list[UniversalDevice]:
        return list(self._devices_discovered.values())

    async def publish(self, topic: str, payload: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.publish(topic, payload)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def turn_on(self, device_id: str) -> bool:
        topic = device_id.removeprefix("mqtt:")
        return await self.publish(f"{topic}/set", "ON")

    async def turn_off(self, device_id: str) -> bool:
        topic = device_id.removeprefix("mqtt:")
        return await self.publish(f"{topic}/set", "OFF")

    async def set_brightness(self, device_id: str, level: int) -> bool:
        topic = device_id.removeprefix("mqtt:")
        return await self.publish(f"{topic}/brightness/set", str(level))

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        topic = device_id.removeprefix("mqtt:")
        return await self.publish(f"{topic}/color/set", f"{r},{g},{b}")

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        topic = device_id.removeprefix("mqtt:")
        return await self.publish(f"{topic}/color_temp/set", str(kelvin))

    async def get_state(self, device_id: str) -> DeviceState:
        return DeviceState()
