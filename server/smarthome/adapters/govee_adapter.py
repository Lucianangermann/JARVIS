"""Govee Cloud API adapter — ACTIVE."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import requests

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice

BASE_URL = "https://developer-api.govee.com/v1"

COLOR_MAP: dict[str, tuple[int, int, int]] = {
    "rot": (255, 0, 0), "red": (255, 0, 0),
    "grün": (0, 200, 0), "green": (0, 200, 0),
    "blau": (0, 0, 255), "blue": (0, 0, 255),
    "weiß": (255, 255, 255), "weiss": (255, 255, 255), "white": (255, 255, 255),
    "gelb": (255, 220, 0), "yellow": (255, 220, 0),
    "orange": (255, 140, 0),
    "lila": (128, 0, 200), "purple": (128, 0, 200),
    "pink": (255, 100, 180),
    "cyan": (0, 220, 255),
    "türkis": (64, 224, 208),
    "sonnenuntergang": (255, 100, 50),
    "ozean": (0, 105, 148),
    "wald": (34, 139, 34),
    "lagerfeuer": (255, 68, 0),
    "lavendel": (230, 200, 255),
}

# Rate limiter: max 100 req/min
_REQ_TIMES: list[float] = []
_RATE_LOCK = asyncio.Lock()


async def _rate_limit() -> None:
    async with _RATE_LOCK:
        now = time.monotonic()
        _REQ_TIMES[:] = [t for t in _REQ_TIMES if now - t < 60]
        if len(_REQ_TIMES) >= 95:
            wait = 60 - (now - _REQ_TIMES[0])
            if wait > 0:
                await asyncio.sleep(wait)
        _REQ_TIMES.append(time.monotonic())


def _detect_type(model: str, name: str) -> str:
    m = model.upper()
    n = name.lower()
    if "H61" in m or "strip" in n or "led" in n or "H6" in m[:3]:
        return "light"
    if "H5080" in m or "plug" in n or "steckdose" in n or "stecker" in n:
        return "plug"
    if "H6" in m:
        return "light"
    return "light"


def _detect_room(name: str) -> str:
    name_lower = name.lower()
    rooms = {
        "wohnzimmer": ["wohnzimmer", "wohn", "living"],
        "schlafzimmer": ["schlafzimmer", "schlaf", "bedroom"],
        "küche": ["küche", "kitchen"],
        "bad": ["bad", "bathroom", "wc"],
        "arbeitszimmer": ["arbeit", "büro", "office", "schreibtisch", "desk"],
        "flur": ["flur", "hall"],
        "keller": ["keller", "basement"],
    }
    for room, keywords in rooms.items():
        if any(k in name_lower for k in keywords):
            return room
    return ""


class GoveeAdapter(BaseAdapter):
    platform_name = "govee"

    def __init__(self) -> None:
        self._api_key: str = ""
        self._device_models: dict[str, str] = {}
        # Background cycle tasks (party/sunrise/sunset) keyed by raw device id.
        # Cancelled on turn_off so a running cycle can't re-enable the device.
        self._cycle_tasks: dict[str, asyncio.Task] = {}

    async def connect(self) -> bool:
        self._api_key = os.getenv("GOVEE_API_KEY", "")
        if not self._api_key:
            self._log("No GOVEE_API_KEY set — disabled")
            return False
        try:
            devices = await self._get_devices_raw()
            n = len(devices)
            self._log(f"Connected — {n} device(s) found")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"Connection failed: {exc}")
            return False

    async def get_devices(self) -> list[UniversalDevice]:
        raw = await self._get_devices_raw()
        result: list[UniversalDevice] = []
        # Read name aliases from env
        name_aliases = self._load_name_aliases()

        for d in raw:
            device_id = d.get("device", "")
            model = d.get("model", "")
            name = name_aliases.get(device_id, d.get("deviceName", device_id))
            self._device_models[device_id] = model
            dtype = _detect_type(model, name)
            room = _detect_room(name)
            caps = d.get("supportCmds", [])
            result.append(UniversalDevice(
                id=f"govee:{device_id}",
                name=name,
                platform="govee",
                type=dtype,
                room=room,
                capabilities=caps,
                raw_data=d,
            ))
        return result

    async def turn_on(self, device_id: str) -> bool:
        return await self._control(device_id, "turn", "on")

    async def turn_off(self, device_id: str) -> bool:
        # Cancel any running cycle (party/sunrise/sunset) for this device
        # so it can't override the turn_off a moment later.
        raw = self._raw_id(device_id)
        task = self._cycle_tasks.pop(raw, None)
        if task and not task.done():
            task.cancel()
        return await self._control(device_id, "turn", "off")

    async def set_brightness(self, device_id: str, level: int) -> bool:
        level = max(0, min(100, level))
        return await self._control(device_id, "brightness", level)

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return await self._control(device_id, "color", {"r": r, "g": g, "b": b})

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return await self._control(device_id, "colorTem", kelvin)

    async def get_state(self, device_id: str) -> DeviceState:
        raw_id = self._raw_id(device_id)
        model = self._device_models.get(raw_id, "")
        try:
            await _rate_limit()
            api_key = self._api_key
            resp = await asyncio.to_thread(
                lambda: requests.get(
                    f"{BASE_URL}/devices/state",
                    headers={"Govee-API-Key": api_key},
                    params={"device": raw_id, "model": model},
                    timeout=10,
                )
            )
            resp.raise_for_status()
            props = resp.json().get("data", {}).get("properties", [])
            state = DeviceState()
            for p in props:
                if "powerSwitch" in p:
                    state.power = p["powerSwitch"] == 1
                elif "brightness" in p:
                    state.brightness = p["brightness"]
                elif "color" in p:
                    c = p["color"]
                    state.color = (c.get("r", 255), c.get("g", 255), c.get("b", 255))
                elif "colorTemInKelvin" in p:
                    state.color_temp = p["colorTemInKelvin"]
            return state
        except Exception:  # noqa: BLE001
            return DeviceState()

    # ── Scene presets ────────────────────────────────────────────────────── #

    async def scene_relax(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color_temp(device_id, 2700)
        await self.set_brightness(device_id, 30)

    async def scene_work(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color_temp(device_id, 5000)
        await self.set_brightness(device_id, 80)

    async def scene_movie(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color(device_id, 255, 50, 0)
        await self.set_brightness(device_id, 10)

    async def scene_focus(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color_temp(device_id, 6500)
        await self.set_brightness(device_id, 100)

    async def scene_reading(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color_temp(device_id, 4000)
        await self.set_brightness(device_id, 90)

    async def scene_gaming(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color(device_id, 75, 0, 130)
        await self.set_brightness(device_id, 70)

    async def scene_romantic(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_color(device_id, 180, 0, 50)
        await self.set_brightness(device_id, 20)

    async def scene_party(self, device_id: str, **_: Any) -> None:
        await self.turn_on(device_id)
        await self.set_brightness(device_id, 100)
        raw = self._raw_id(device_id)
        old = self._cycle_tasks.pop(raw, None)
        if old and not old.done():
            old.cancel()
        self._cycle_tasks[raw] = asyncio.create_task(self._party_cycle(device_id))

    async def scene_sunrise(self, device_id: str, duration_minutes: int = 20, **_: Any) -> None:
        raw = self._raw_id(device_id)
        old = self._cycle_tasks.pop(raw, None)
        if old and not old.done():
            old.cancel()
        self._cycle_tasks[raw] = asyncio.create_task(
            self._sunrise_cycle(device_id, duration_minutes)
        )

    async def scene_sunset(self, device_id: str, duration_minutes: int = 15, **_: Any) -> None:
        raw = self._raw_id(device_id)
        old = self._cycle_tasks.pop(raw, None)
        if old and not old.done():
            old.cancel()
        self._cycle_tasks[raw] = asyncio.create_task(
            self._sunset_cycle(device_id, duration_minutes)
        )

    async def _party_cycle(self, device_id: str) -> None:
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255),
                  (255, 255, 0), (255, 0, 255), (0, 255, 255)]
        try:
            for _ in range(30):
                for r, g, b in colors:
                    await self.set_color(device_id, r, g, b)
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def _sunrise_cycle(self, device_id: str, duration_minutes: int) -> None:
        steps = min(duration_minutes * 2, 40)
        interval = (duration_minutes * 60) / steps
        try:
            await self.turn_on(device_id)
            for i in range(steps):
                pct = i / steps
                brightness = int(pct * 100)
                r = int(255)
                g = int(100 + pct * 155)
                b = int(pct * 200)
                await self.set_brightness(device_id, max(1, brightness))
                await self.set_color(device_id, r, g, b)
                await asyncio.sleep(interval)
            await self.set_color_temp(device_id, 3500)
            await self.set_brightness(device_id, 100)
        except asyncio.CancelledError:
            pass

    async def _sunset_cycle(self, device_id: str, duration_minutes: int) -> None:
        steps = min(duration_minutes * 2, 30)
        interval = (duration_minutes * 60) / steps
        try:
            for i in range(steps):
                pct = 1 - (i / steps)
                brightness = int(pct * 80)
                r = 255
                g = int(pct * 80)
                b = 0
                await self.set_brightness(device_id, max(1, brightness))
                await self.set_color(device_id, r, g, b)
                await asyncio.sleep(interval)
            await self.turn_off(device_id)
        except asyncio.CancelledError:
            pass

    # ── Internal helpers ─────────────────────────────────────────────────── #

    def _raw_id(self, device_id: str) -> str:
        return device_id.removeprefix("govee:")

    def _get_model(self, device_id: str) -> str:
        return self._device_models.get(self._raw_id(device_id), "")

    async def _control(self, device_id: str, cmd_name: str, cmd_value: Any) -> bool:
        raw_id = self._raw_id(device_id)
        model = self._get_model(device_id)
        payload = {
            "device": raw_id,
            "model": model,
            "cmd": {"name": cmd_name, "value": cmd_value},
        }
        api_key = self._api_key
        for attempt in range(2):
            try:
                await _rate_limit()
                resp = await asyncio.to_thread(
                    lambda: requests.put(
                        f"{BASE_URL}/devices/control",
                        headers={
                            "Govee-API-Key": api_key,
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=10,
                    )
                )
                resp.raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    await asyncio.sleep(1)
                else:
                    self._log(f"Control failed ({cmd_name}={cmd_value}): {exc}")
                    return False
        return False

    async def _get_devices_raw(self) -> list[dict[str, Any]]:
        await _rate_limit()
        api_key = self._api_key
        resp = await asyncio.to_thread(
            lambda: requests.get(
                f"{BASE_URL}/devices",
                headers={"Govee-API-Key": api_key},
                timeout=10,
            )
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("devices", [])

    def _load_name_aliases(self) -> dict[str, str]:
        """Load device name aliases from env."""
        aliases: dict[str, str] = {}
        names_str = os.getenv("GOVEE_DEVICE_NAMES", "")
        if names_str:
            # Format: "device_id:Name, device_id2:Name2" or just "Name1, Name2" for order
            for part in names_str.split(","):
                part = part.strip()
                if ":" in part:
                    did, name = part.split(":", 1)
                    aliases[did.strip()] = name.strip()
        return aliases
