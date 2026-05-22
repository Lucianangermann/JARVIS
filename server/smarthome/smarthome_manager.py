"""Central SmartHome coordinator — loads adapters, routes commands."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from .base_adapter import UniversalDevice
from .device_registry import DeviceRegistry
from .scenes import SceneManager
from .automations import AutomationEngine
from .energy_monitor import EnergyMonitor
from .geofencing import GeofencingEngine

# Color map for natural-language color names (DE + EN)
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
    "türkis": (64, 224, 208), "turquoise": (64, 224, 208),
    "magenta": (255, 0, 180),
    "sonnenuntergang": (255, 100, 50),
    "ozean": (0, 105, 148),
    "wald": (34, 139, 34),
    "lagerfeuer": (255, 68, 0),
    "lavendel": (230, 200, 255),
    "gold": (255, 200, 0),
    "schwarz": (0, 0, 0), "black": (0, 0, 0),
}

SCENE_ALIASES: dict[str, str] = {
    "kino": "kinoabend",
    "kinoabend": "kinoabend",
    "film": "kinoabend",
    "movie": "kinoabend",
    "nacht": "gute_nacht",
    "gute nacht": "gute_nacht",
    "gute_nacht": "gute_nacht",
    "morgen": "guten_morgen",
    "guten morgen": "guten_morgen",
    "guten_morgen": "guten_morgen",
    "sunrise": "guten_morgen",
    "arbeit": "arbeiten",
    "arbeiten": "arbeiten",
    "work": "arbeiten",
    "relax": "entspannen",
    "entspannen": "entspannen",
    "relaxen": "entspannen",
    "party": "party",
    "lesen": "lesen",
    "read": "lesen",
    "fokus": "fokus",
    "focus": "fokus",
    "gaming": "gaming",
    "romantisch": "romantisch",
    "romantic": "romantisch",
    "aus": "alles_aus",
    "alles aus": "alles_aus",
    "alles_aus": "alles_aus",
    "off": "alles_aus",
    "an": "alles_an",
    "alles an": "alles_an",
    "alles_an": "alles_an",
    "weg": "verlasse_haus",
    "verlasse haus": "verlasse_haus",
    "zuhause": "ankunft_zuhause",
    "ankunft": "ankunft_zuhause",
}


class SmartHomeManager:
    """Platform-agnostic smart home coordinator."""

    def __init__(self) -> None:
        self.registry = DeviceRegistry()
        self._adapters: dict[str, Any] = {}
        self._scenes: SceneManager | None = None
        self._automations: AutomationEngine | None = None
        self._energy: EnergyMonitor | None = None
        self._geofencing: GeofencingEngine | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        self._load_adapters()

        active: list[str] = []
        ready: list[str] = []

        for name, adapter in self._adapters.items():
            if adapter.enabled:
                try:
                    ok = await adapter.connect()
                    adapter.connected = ok
                    if ok:
                        active.append(name)
                        devices = await adapter.get_devices()
                        self.registry.update_devices(devices)
                    else:
                        ready.append(name)
                except Exception as exc:  # noqa: BLE001
                    print(f"[SMARTHOME] {name} connect failed: {exc}")
                    ready.append(name)
            else:
                ready.append(name)

        self._scenes = SceneManager(self.registry)
        self._automations = AutomationEngine(self._scenes)
        self._energy = EnergyMonitor(self.registry)
        self._geofencing = GeofencingEngine(self._automations)

        summary = self.registry.summary()
        device_str = ", ".join(f"{v} {k}" for k, v in summary.items()) or "no devices"

        print(f"[SMARTHOME] Active adapters: {', '.join(active) or 'none'}")
        if ready:
            print(f"[SMARTHOME] Ready (disabled): {', '.join(ready)}")
        print(f"[SMARTHOME] Devices: {device_str}")

    def _load_adapters(self) -> None:
        """Load all adapters whose enabled flag is True in env."""
        _ADAPTERS = [
            ("govee",         "GOVEE_ENABLED",   ".adapters.govee_adapter",           "GoveeAdapter"),
            ("hue",           "HUE_ENABLED",     ".adapters.hue_adapter",             "HueAdapter"),
            ("ikea",          "IKEA_ENABLED",    ".adapters.ikea_adapter",            "IKEAAdapter"),
            ("homekit",       "HOMEKIT_ENABLED", ".adapters.homekit_adapter",         "HomeKitAdapter"),
            ("homeassistant", "HA_ENABLED",      ".adapters.homeassistant_adapter",   "HomeAssistantAdapter"),
            ("sonos",         "SONOS_ENABLED",   ".adapters.sonos_adapter",           "SonosAdapter"),
            ("samsung",       "SAMSUNG_ENABLED", ".adapters.samsung_adapter",         "SamsungAdapter"),
            ("nest",          "NEST_ENABLED",    ".adapters.nest_adapter",            "NestAdapter"),
            ("tado",          "TADO_ENABLED",    ".adapters.tado_adapter",            "TadoAdapter"),
            ("ring",          "RING_ENABLED",    ".adapters.ring_adapter",            "RingAdapter"),
            ("mqtt",          "MQTT_ENABLED",    ".adapters.mqtt_adapter",            "MQTTAdapter"),
        ]
        for name, env_key, rel_module, cls in _ADAPTERS:
            self._try_load(name, env_key, rel_module, cls)

    def _try_load(self, name: str, env_key: str, module: str, cls: str) -> None:
        enabled = os.getenv(env_key, "false").lower() in ("true", "1", "yes")
        try:
            import importlib
            # Use __package__ so the import works regardless of whether
            # the server root is in sys.path (e.g. Electron launcher).
            pkg = __package__ or "server.smarthome"
            mod = importlib.import_module(module, package=pkg)
            adapter_cls = getattr(mod, cls)
            adapter = adapter_cls()
            adapter.enabled = enabled
            self.registry.register_adapter(adapter)
            self._adapters[name] = adapter
        except Exception as exc:  # noqa: BLE001
            print(f"[SMARTHOME] Could not load {name} adapter: {exc}")

    # ── Command routing ─────────────────────────────────────────────────── #

    async def process_command(self, command: str) -> str:
        """Route natural language command to the right adapter(s)."""
        cmd = command.lower().strip()

        # Scene check first
        scene_name = self._resolve_scene(cmd)
        if scene_name and self._scenes:
            return await self._scenes.run_scene(scene_name)

        # Device lookup
        device, remainder = self._find_device_in_command(cmd)

        if device is None:
            # Try "alles" / "all" commands
            if any(w in cmd for w in ("alles", "alle", "all", "überall", "komplett")):
                return await self._broadcast_command(cmd)
            return "Kein Gerät gefunden. Verfügbare Geräte: " + ", ".join(
                d.name for d in self.registry.get_all()
            )

        adapter = self.registry.get_adapter(device)
        if adapter is None or not adapter.connected:
            return f"{device.name} ist nicht erreichbar."

        return await self._dispatch_to_adapter(adapter, device, remainder or cmd)

    def _resolve_scene(self, cmd: str) -> str | None:
        for alias, scene in SCENE_ALIASES.items():
            if alias in cmd:
                return scene
        return None

    def _find_device_in_command(self, cmd: str) -> tuple[Any, str]:
        """Find a device mentioned by name in the command."""
        for device in self.registry.get_all():
            name_lower = device.name.lower()
            if name_lower in cmd:
                remainder = cmd.replace(name_lower, "").strip()
                return device, remainder
        return None, cmd

    async def _broadcast_command(self, cmd: str) -> str:
        """Apply command to all compatible devices."""
        devices = self.registry.get_all()
        count = 0
        for device in devices:
            adapter = self.registry.get_adapter(device)
            if adapter is None or not adapter.connected:
                continue
            try:
                await self._dispatch_to_adapter(adapter, device, cmd)
                count += 1
            except Exception:  # noqa: BLE001
                pass
        return f"{count} Geräte aktualisiert."

    async def _dispatch_to_adapter(self, adapter: Any, device: Any, cmd: str) -> str:
        """Route a parsed command to the right adapter method."""
        # Power
        if any(w in cmd for w in ("an", "ein", "on", "einschalten", "anmachen")):
            await adapter.turn_on(device.id)
            return f"{device.name} eingeschaltet."
        if any(w in cmd for w in ("aus", "off", "ausschalten", "ausmachen")):
            await adapter.turn_off(device.id)
            return f"{device.name} ausgeschaltet."

        # Brightness
        for word in cmd.split():
            if word.endswith("%") and word[:-1].isdigit():
                level = int(word[:-1])
                await adapter.set_brightness(device.id, max(0, min(100, level)))
                return f"{device.name} auf {level}% gedimmt."

        # Color
        for color_name, rgb in COLOR_MAP.items():
            if color_name in cmd:
                r, g, b = rgb
                await adapter.turn_on(device.id)
                await adapter.set_color(device.id, r, g, b)
                return f"{device.name}: Farbe {color_name}."

        # Scenes on specific device
        for scene in ("relax", "work", "movie", "focus", "reading", "gaming",
                      "romantic", "party", "sunrise", "sunset"):
            if scene in cmd:
                method = getattr(adapter, f"scene_{scene}", None)
                if method:
                    await method(device.id)
                    return f"{device.name}: Szene {scene}."

        return f"Befehl für {device.name} nicht verstanden: {cmd}"

    # ── Status ──────────────────────────────────────────────────────────── #

    def status(self) -> dict[str, Any]:
        return {
            "adapters": [a.status_dict() for a in self._adapters.values()],
            "devices": self.registry.summary(),
            "total_devices": len(self.registry.get_all()),
        }

    async def enable_adapter(self, platform: str) -> str:
        adapter = self._adapters.get(platform)
        if adapter is None:
            return f"Adapter '{platform}' nicht gefunden."
        adapter.enabled = True
        ok = await adapter.connect()
        adapter.connected = ok
        if ok:
            devices = await adapter.get_devices()
            self.registry.update_devices(devices)
            return f"{platform} aktiviert. {len(devices)} Geräte geladen."
        return f"{platform} konnte nicht verbunden werden."

    # ── Delegation helpers for API routes ───────────────────────────────── #

    def get_all_devices(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self.registry.get_all()]

    def get_scenes(self) -> dict[str, Any]:
        if self._scenes is None:
            return {}
        return {k: v.get("description", k)
                for k, v in self._scenes.all_scenes().items()}

    async def run_scene(self, name: str) -> str:
        if self._scenes is None:
            return "Szenen noch nicht geladen."
        return await self._scenes.run_scene(name)

    def get_automations(self) -> list[dict[str, Any]]:
        if self._automations is None:
            return []
        return self._automations.all_automations()

    async def update_location(self, lat: float, lon: float) -> dict[str, Any]:
        if self._geofencing is None:
            return {"status": "geofencing_not_ready"}
        return await self._geofencing.update_location(lat, lon)

    async def get_energy(self) -> dict[str, Any]:
        if self._energy is None:
            return {}
        return await self._energy.get_current_consumption()
