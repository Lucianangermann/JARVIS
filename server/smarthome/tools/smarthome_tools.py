"""Claude tool_use definitions for Smart Home control."""
from __future__ import annotations

from typing import Any


def smarthome_tool(device_names: list[str] | None = None) -> dict[str, Any]:
    """Tool schema for Claude to control smart home devices."""
    device_hint = ""
    if device_names:
        device_hint = (
            f"\n\nKnown devices (use exact names with turn_on/turn_off): "
            + ", ".join(f"'{n}'" for n in device_names)
            + "\nIMPORTANT: These are device names, NOT scene names. "
            "E.g. 'Gaming Setup einschalten' → action='turn_on', device='Gaming Setup'."
        )
    return {
        "name": "smarthome_control",
        "description": (
            "Control smart home devices and scenes. "
            "Use this for ANY request involving lights, plugs, scenes, "
            "or device control. Commands can be natural language (German or English).\n\n"
            "Actions:\n"
            "  command: Run a natural language command (e.g. 'mach das licht blau', 'alles aus')\n"
            "  scene: Activate a named scene (kinoabend, gute_nacht, entspannen, party, etc.)\n"
            "  turn_on: Turn on device by name\n"
            "  turn_off: Turn off device by name\n"
            "  brightness: Set brightness (0-100%) for device\n"
            "  color: Set color by name for device (rot, blau, grün, weiß, etc.)\n"
            "  devices: List all known devices\n"
            "  scenes: List all available scenes\n"
            "  status: Get smart home system status\n"
            "  energy: Get current energy consumption\n\n"
            "Available scenes: kinoabend, gute_nacht, guten_morgen, entspannen, "
            "arbeiten, party, lesen, fokus, gaming, romantisch, alles_aus, alles_an, "
            "verlasse_haus, ankunft_zuhause\n\n"
            "Examples:\n"
            "  smarthome_control(action='command', command='mach das licht blau')\n"
            "  smarthome_control(action='scene', scene='kinoabend')\n"
            "  smarthome_control(action='turn_off', device='wohnzimmer')\n"
            "  smarthome_control(action='brightness', device='schreibtisch', level=50)\n"
            "  smarthome_control(action='color', device='strip', color='rot')\n"
            + device_hint
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["command", "scene", "turn_on", "turn_off",
                             "brightness", "color", "devices", "scenes",
                             "status", "energy"],
                    "description": "The action to perform",
                },
                "command": {
                    "type": "string",
                    "description": "Natural language command (for action='command')",
                },
                "scene": {
                    "type": "string",
                    "description": "Scene name (for action='scene')",
                },
                "device": {
                    "type": "string",
                    "description": "Device name or partial name (for turn_on/off/brightness/color)",
                },
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Brightness level 0-100 (for action='brightness')",
                },
                "color": {
                    "type": "string",
                    "description": "Color name: rot, blau, grün, weiß, gelb, orange, lila, pink, cyan, türkis",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }


async def execute_smarthome_tool(
    smarthome_manager: Any,
    action: str,
    command: str | None = None,
    scene: str | None = None,
    device: str | None = None,
    level: int | None = None,
    color: str | None = None,
) -> str:
    """Execute a smarthome tool call from Claude."""
    if smarthome_manager is None:
        return "Smart Home ist nicht verfügbar."

    try:
        if action == "command":
            return await smarthome_manager.process_command(command or "")

        elif action == "scene":
            return await smarthome_manager.run_scene(scene or "")

        elif action in ("turn_on", "turn_off"):
            if device:
                dev = smarthome_manager.registry.get_by_name(device)
                if dev is None:
                    return f"Gerät '{device}' nicht gefunden."
                adapter = smarthome_manager.registry.get_adapter(dev)
                if adapter is None:
                    return f"Kein Adapter für {dev.name}."
                if action == "turn_on":
                    await adapter.turn_on(dev.id)
                    return f"{dev.name} eingeschaltet."
                else:
                    await adapter.turn_off(dev.id)
                    return f"{dev.name} ausgeschaltet."
            else:
                # broadcast
                return await smarthome_manager.process_command(
                    "alles " + ("an" if action == "turn_on" else "aus")
                )

        elif action == "brightness":
            if device is None:
                return "Bitte ein Gerät angeben."
            dev = smarthome_manager.registry.get_by_name(device)
            if dev is None:
                return f"Gerät '{device}' nicht gefunden."
            adapter = smarthome_manager.registry.get_adapter(dev)
            if adapter is None:
                return f"Kein Adapter für {dev.name}."
            l = level if level is not None else 70
            await adapter.set_brightness(dev.id, l)
            return f"{dev.name} auf {l}% gedimmt."

        elif action == "color":
            if device is None:
                return "Bitte ein Gerät angeben."
            dev = smarthome_manager.registry.get_by_name(device)
            if dev is None:
                return f"Gerät '{device}' nicht gefunden."
            adapter = smarthome_manager.registry.get_adapter(dev)
            if adapter is None:
                return f"Kein Adapter für {dev.name}."
            from ..smarthome_manager import COLOR_MAP
            rgb = COLOR_MAP.get(color or "weiß", (255, 255, 255))
            await adapter.turn_on(dev.id)
            await adapter.set_color(dev.id, *rgb)
            return f"{dev.name}: Farbe {color}."

        elif action == "devices":
            devices = smarthome_manager.get_all_devices()
            if not devices:
                return "Keine Geräte gefunden."
            lines = [f"- {d['name']} ({d['type']}, {d['platform']})" for d in devices]
            return "Geräte:\n" + "\n".join(lines)

        elif action == "scenes":
            scenes = smarthome_manager.get_scenes()
            lines = [f"- {k}: {v}" for k, v in scenes.items()]
            return "Szenen:\n" + "\n".join(lines)

        elif action == "status":
            s = smarthome_manager.status()
            active = [a["platform"] for a in s["adapters"] if a["connected"]]
            return (f"Smart Home: {s['total_devices']} Geräte, "
                    f"aktive Adapter: {', '.join(active) or 'keine'}.")

        elif action == "energy":
            data = await smarthome_manager.get_energy()
            total = data.get("total_watts", 0)
            return f"Energieverbrauch: {total} W gesamt."

        return f"Unbekannte Aktion: {action}"

    except Exception as exc:  # noqa: BLE001
        return f"Smart Home Fehler: {exc}"
