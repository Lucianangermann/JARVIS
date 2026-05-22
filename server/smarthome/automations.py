"""Automation rules engine — time-based and event-based triggers."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scenes import SceneManager

AUTOMATIONS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "automations.json"

BUILT_IN_AUTOMATIONS: list[dict[str, Any]] = [
    {
        "id": "morning_wakeup",
        "name": "Morgens aufwachen",
        "trigger": "time",
        "time": "07:00",
        "days": ["weekday"],
        "scene": "guten_morgen",
        "enabled": False,
    },
    {
        "id": "evening_relax",
        "name": "Abends entspannen",
        "trigger": "time",
        "time": "19:00",
        "days": ["daily"],
        "scene": "entspannen",
        "enabled": False,
    },
    {
        "id": "good_night",
        "name": "Gute Nacht",
        "trigger": "time",
        "time": "23:00",
        "days": ["daily"],
        "scene": "gute_nacht",
        "enabled": False,
    },
    {
        "id": "leave_home",
        "name": "Verlasse Haus",
        "trigger": "departure",
        "scene": "verlasse_haus",
        "enabled": False,
    },
    {
        "id": "arrive_home",
        "name": "Ankunft zuhause",
        "trigger": "arrival",
        "scene": "ankunft_zuhause",
        "enabled": False,
    },
]


class AutomationEngine:
    def __init__(self, scene_manager: "SceneManager") -> None:
        self._scenes = scene_manager
        self._automations: list[dict[str, Any]] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._load()

    def all_automations(self) -> list[dict[str, Any]]:
        return self._automations

    def get(self, automation_id: str) -> dict[str, Any] | None:
        return next((a for a in self._automations if a["id"] == automation_id), None)

    def enable(self, automation_id: str, enabled: bool) -> bool:
        auto = self.get(automation_id)
        if auto is None:
            return False
        auto["enabled"] = enabled
        self._save()
        return True

    async def create(self, name: str, trigger: str, scene: str,
                     **kwargs: Any) -> dict[str, Any]:
        import uuid
        auto = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "trigger": trigger,
            "scene": scene,
            "enabled": True,
            **kwargs,
        }
        self._automations.append(auto)
        self._save()
        return auto

    async def fire(self, trigger: str, context: dict[str, Any] | None = None) -> None:
        """Fire all enabled automations matching the trigger."""
        now = datetime.now()
        for auto in self._automations:
            if not auto.get("enabled"):
                continue
            if auto.get("trigger") != trigger:
                continue
            try:
                await self._run_automation(auto, now, context)
            except Exception as exc:  # noqa: BLE001
                print(f"[AUTO] {auto['name']} failed: {exc}")

    async def _run_automation(self, auto: dict[str, Any], now: datetime,
                               context: dict[str, Any] | None) -> None:
        trigger = auto.get("trigger")

        if trigger == "time":
            t_str = auto.get("time", "")
            try:
                h, m = map(int, t_str.split(":"))
                target = time(h, m)
            except (ValueError, AttributeError):
                return
            if now.hour != target.hour or now.minute != target.minute:
                return
            days = auto.get("days", ["daily"])
            if "weekday" in days and now.weekday() >= 5:
                return
            if "weekend" in days and now.weekday() < 5:
                return

        scene_name = auto.get("scene", "")
        if scene_name:
            print(f"[AUTO] Firing: {auto['name']} → scene={scene_name}")
            await self._scenes.run_scene(scene_name)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            # get_running_loop() raises RuntimeError if no loop is
            # running (e.g. unit tests) — caught below, safe to skip.
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._tick_loop())
        except RuntimeError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _tick_loop(self) -> None:
        while self._running:
            await self.fire("time")
            await asyncio.sleep(60)

    def _load(self) -> None:
        custom: list[dict[str, Any]] = []
        if AUTOMATIONS_PATH.exists():
            try:
                custom = json.loads(AUTOMATIONS_PATH.read_text())
            except Exception:  # noqa: BLE001
                pass
        built_in_ids = {a["id"] for a in BUILT_IN_AUTOMATIONS}
        custom_filtered = [a for a in custom if a.get("id") not in built_in_ids]
        self._automations = list(BUILT_IN_AUTOMATIONS) + custom_filtered

    def _save(self) -> None:
        built_in_ids = {a["id"] for a in BUILT_IN_AUTOMATIONS}
        custom = [a for a in self._automations if a.get("id") not in built_in_ids]
        try:
            AUTOMATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUTOMATIONS_PATH.write_text(json.dumps(custom, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[AUTO] save failed: {exc}")
