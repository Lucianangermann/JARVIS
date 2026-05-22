"""Energy monitoring — tracks watt consumption where platforms support it."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .device_registry import DeviceRegistry

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "energy.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS readings "
        "(device_id TEXT, timestamp TEXT, watts REAL)"
    )
    conn.commit()
    return conn


class EnergyMonitor:
    def __init__(self, registry: "DeviceRegistry") -> None:
        self._registry = registry

    async def get_current_consumption(self) -> dict[str, Any]:
        results: dict[str, float] = {}
        total = 0.0
        for device in self._registry.get_all():
            adapter = self._registry.get_adapter(device)
            if adapter is None or not adapter.connected:
                continue
            try:
                watts = await adapter.get_energy(device.id)
                if watts > 0:
                    results[device.name] = watts
                    total += watts
                    self._record(device.id, watts)
            except Exception:  # noqa: BLE001
                pass
        return {"devices": results, "total_watts": round(total, 1)}

    async def get_daily_report(self) -> str:
        data = await self.get_current_consumption()
        total = data["total_watts"]
        devices = data["devices"]
        if not devices:
            return "Keine Energiedaten verfügbar."
        top = max(devices, key=lambda k: devices[k])
        return (f"Aktueller Verbrauch: {total} W gesamt. "
                f"Größter Verbraucher: {top} ({devices[top]} W).")

    def _record(self, device_id: str, watts: float) -> None:
        try:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO readings VALUES (?, ?, ?)",
                (device_id, datetime.utcnow().isoformat(), watts),
            )
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
