"""Tuya local-protocol adapter for ANTELA smart plugs (and similar).

Reads device configs from devices.json (tinytuya wizard output) in the
project root. Talks to devices directly over LAN using tinytuya — no
cloud round-trip at runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from ..base_adapter import BaseAdapter, DeviceState, UniversalDevice

# devices.json lives in the project root (JARVIS/)
_DEVICES_JSON = Path(__file__).parent.parent.parent.parent / "devices.json"


def _load_device_configs() -> list[dict[str, Any]]:
    if _DEVICES_JSON.exists():
        try:
            return json.loads(_DEVICES_JSON.read_text())
        except Exception:
            return []
    return []


def _detect_room(name: str) -> str:
    n = name.lower()
    rooms = {
        "wohnzimmer": ["wohnzimmer", "wohn", "living"],
        "schlafzimmer": ["schlafzimmer", "schlaf", "bedroom"],
        "küche": ["küche", "kitchen"],
        "arbeitszimmer": ["arbeit", "büro", "office", "schreibtisch", "desk", "gaming"],
        "flur": ["flur", "hall"],
    }
    for room, keywords in rooms.items():
        if any(k in n for k in keywords):
            return room
    return ""


class TuyaAdapter(BaseAdapter):
    platform_name = "tuya"

    def __init__(self) -> None:
        self._configs: list[dict[str, Any]] = []

    async def connect(self) -> bool:
        self._configs = _load_device_configs()
        if not self._configs:
            self._log("No devices found in devices.json")
            return False
        online = [c for c in self._configs if c.get("ip")]
        self._log(f"Loaded {len(self._configs)} device(s), {len(online)} online")
        return len(online) > 0

    async def get_devices(self) -> list[UniversalDevice]:
        devices = []
        for cfg in self._configs:
            dev_id = cfg.get("id", "")
            name = cfg.get("name", dev_id)
            ip = cfg.get("ip", "")
            state = DeviceState(power=False, online=bool(ip))
            if ip:
                try:
                    status = await asyncio.get_event_loop().run_in_executor(
                        None, self._get_status, cfg
                    )
                    if status and "dps" in status:
                        state.power = bool(status["dps"].get("1", False))
                except Exception:
                    pass
            devices.append(UniversalDevice(
                id=f"tuya:{dev_id}",
                name=name,
                platform="tuya",
                type="plug",
                room=_detect_room(name),
                state=state,
                capabilities=["power"],
                raw_data=cfg,
            ))
        return devices

    # ── helpers ──────────────────────────────────────────────────────── #

    def _cfg_for(self, device_id: str) -> dict[str, Any] | None:
        raw_id = device_id.removeprefix("tuya:")
        return next((c for c in self._configs if c.get("id") == raw_id), None)

    def _make_device(self, cfg: dict[str, Any]):
        try:
            import tinytuya
        except ImportError:
            raise RuntimeError("tinytuya not installed — run: pip install tinytuya")
        version = float(cfg.get("version") or 3.4)
        d = tinytuya.OutletDevice(
            dev_id=cfg["id"],
            address=cfg["ip"],
            local_key=cfg["key"],
            version=version,
        )
        d.set_version(version)
        return d

    def _get_status(self, cfg: dict[str, Any]) -> dict | None:
        try:
            d = self._make_device(cfg)
            return d.status()
        except Exception:
            return None

    def _set_switch(self, cfg: dict[str, Any], on: bool) -> bool:
        try:
            d = self._make_device(cfg)
            if on:
                d.turn_on()
            else:
                d.turn_off()
            return True
        except Exception as exc:
            self._log(f"set_switch failed for {cfg.get('name')}: {exc}")
            return False

    # ── BaseAdapter interface ─────────────────────────────────────────── #

    async def turn_on(self, device_id: str) -> bool:
        cfg = self._cfg_for(device_id)
        if not cfg or not cfg.get("ip"):
            return False
        return await asyncio.get_event_loop().run_in_executor(
            None, self._set_switch, cfg, True
        )

    async def turn_off(self, device_id: str) -> bool:
        cfg = self._cfg_for(device_id)
        if not cfg or not cfg.get("ip"):
            return False
        return await asyncio.get_event_loop().run_in_executor(
            None, self._set_switch, cfg, False
        )

    async def get_state(self, device_id: str) -> DeviceState:
        cfg = self._cfg_for(device_id)
        if not cfg or not cfg.get("ip"):
            return DeviceState(online=False)
        status = await asyncio.get_event_loop().run_in_executor(
            None, self._get_status, cfg
        )
        if status and "dps" in status:
            return DeviceState(power=bool(status["dps"].get("1", False)), online=True)
        return DeviceState(online=False)

    async def set_brightness(self, device_id: str, level: int) -> bool:
        return False  # plugs don't support brightness

    async def set_color(self, device_id: str, r: int, g: int, b: int) -> bool:
        return False

    async def set_color_temp(self, device_id: str, kelvin: int) -> bool:
        return False
