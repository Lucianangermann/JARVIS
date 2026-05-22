"""Universal scene manager — works across all active adapters simultaneously."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .device_registry import DeviceRegistry

SCENES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "scenes.json"

BUILT_IN_SCENES: dict[str, dict[str, Any]] = {
    "kinoabend": {
        "description": "Kinoabend — Lichter gedimmt, warme Farben",
        "actions": [
            {"type": "all_lights", "action": "scene_movie"},
        ],
    },
    "guten_morgen": {
        "description": "Guten Morgen — sanfter Sonnenaufgang",
        "actions": [
            {"type": "all_lights", "action": "scene_sunrise", "duration": 20},
        ],
    },
    "gute_nacht": {
        "description": "Gute Nacht — langsam abdunkeln",
        "actions": [
            {"type": "all_lights", "action": "scene_sunset", "duration": 15},
        ],
    },
    "verlasse_haus": {
        "description": "Verlasse Haus — alles aus",
        "actions": [
            {"type": "all_lights", "action": "turn_off"},
            {"type": "all_plugs", "action": "turn_off"},
        ],
    },
    "ankunft_zuhause": {
        "description": "Ankunft — Willkommen zuhause",
        "actions": [
            {"type": "all_lights", "action": "turn_on", "brightness": 70},
        ],
    },
    "arbeiten": {
        "description": "Arbeiten — fokussiertes Licht",
        "actions": [
            {"type": "all_lights", "action": "scene_work"},
        ],
    },
    "entspannen": {
        "description": "Entspannen — warmes gedimmtes Licht",
        "actions": [
            {"type": "all_lights", "action": "scene_relax"},
        ],
    },
    "party": {
        "description": "Party — bunte Lichter",
        "actions": [
            {"type": "all_lights", "action": "scene_party"},
        ],
    },
    "lesen": {
        "description": "Lesen — helles neutrales Licht",
        "actions": [
            {"type": "all_lights", "action": "scene_reading"},
        ],
    },
    "fokus": {
        "description": "Fokus — kühles helles Licht",
        "actions": [
            {"type": "all_lights", "action": "scene_focus"},
        ],
    },
    "gaming": {
        "description": "Gaming — lila Akzentlicht",
        "actions": [
            {"type": "all_lights", "action": "scene_gaming"},
        ],
    },
    "romantisch": {
        "description": "Romantisch — gedimmtes rotes Licht",
        "actions": [
            {"type": "all_lights", "action": "scene_romantic"},
        ],
    },
    "alles_aus": {
        "description": "Alles aus",
        "actions": [
            {"type": "all_lights", "action": "turn_off"},
            {"type": "all_plugs", "action": "turn_off"},
        ],
    },
    "alles_an": {
        "description": "Alles an",
        "actions": [
            {"type": "all_lights", "action": "turn_on"},
            {"type": "all_plugs", "action": "turn_on"},
        ],
    },
}


class SceneManager:
    def __init__(self, registry: "DeviceRegistry") -> None:
        self._registry = registry
        self._custom: dict[str, dict[str, Any]] = {}
        self._load_custom()

    def all_scenes(self) -> dict[str, dict[str, Any]]:
        return {**BUILT_IN_SCENES, **self._custom}

    def get(self, name: str) -> dict[str, Any] | None:
        name = name.lower().replace(" ", "_").replace("-", "_")
        return BUILT_IN_SCENES.get(name) or self._custom.get(name)

    async def run_scene(self, name: str) -> str:
        scene = self.get(name)
        if scene is None:
            return f"Szene '{name}' nicht gefunden."

        print(f"[SCENE] Running: {name}")
        results: list[str] = []

        for action in scene.get("actions", []):
            result = await self._execute_action(action)
            results.append(result)

        return f"Szene '{name}' aktiviert."

    async def _execute_action(self, action: dict[str, Any]) -> str:
        action_type = action.get("type", "")
        action_cmd = action.get("action", "")
        brightness = action.get("brightness", None)
        duration = action.get("duration", None)

        targets: list[Any] = []

        if action_type == "all_lights":
            targets = self._registry.get_by_type("light")
        elif action_type == "all_plugs":
            targets = self._registry.get_by_type("plug")
        elif action_type == "device_name":
            dev = self._registry.get_by_name(action.get("name", ""))
            if dev:
                targets = [dev]
        elif action_type == "thermostat":
            targets = self._registry.get_by_type("thermostat")
        elif action_type == "locks":
            targets = self._registry.get_by_type("lock")

        for device in targets:
            adapter = self._registry.get_adapter(device)
            if adapter is None or not adapter.connected:
                continue
            try:
                await self._dispatch(adapter, device.id, action_cmd, brightness, duration)
            except Exception as exc:  # noqa: BLE001
                print(f"[SCENE] action failed for {device.name}: {exc}")

        return f"  {action_type}: {action_cmd} → {len(targets)} device(s)"

    async def _dispatch(self, adapter: Any, device_id: str,
                        cmd: str, brightness: int | None, duration: int | None) -> None:
        if cmd == "turn_on":
            await adapter.turn_on(device_id)
            if brightness is not None:
                await adapter.set_brightness(device_id, brightness)
        elif cmd == "turn_off":
            await adapter.turn_off(device_id)
        elif cmd in ("scene_movie", "scene_relax", "scene_work", "scene_focus",
                     "scene_reading", "scene_gaming", "scene_romantic", "scene_party",
                     "scene_sunrise", "scene_sunset"):
            method = getattr(adapter, cmd, None)
            if method:
                if duration is not None:
                    try:
                        await method(device_id, duration_minutes=duration)
                    except TypeError:
                        await method(device_id)
                else:
                    await method(device_id)
            else:
                await adapter.set_scene(device_id, cmd)
        elif cmd == "lock":
            await adapter.lock(device_id)
        elif cmd == "unlock":
            await adapter.unlock(device_id)

    async def create_custom_scene(self, name: str, actions: list[dict[str, Any]]) -> str:
        key = name.lower().replace(" ", "_")
        self._custom[key] = {"description": name, "actions": actions}
        self._save_custom()
        return f"Szene '{name}' gespeichert."

    def _load_custom(self) -> None:
        if SCENES_PATH.exists():
            try:
                self._custom = json.loads(SCENES_PATH.read_text())
            except Exception:  # noqa: BLE001
                self._custom = {}

    def _save_custom(self) -> None:
        try:
            SCENES_PATH.parent.mkdir(parents=True, exist_ok=True)
            SCENES_PATH.write_text(json.dumps(self._custom, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[SCENE] save failed: {exc}")
