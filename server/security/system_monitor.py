"""MacBook + JARVIS-server health monitoring via ``psutil``.

Zero-config: the moment psutil is importable this works, no sensors, no
external services. The background :meth:`SystemMonitor.start` loop samples
every ``SYSTEM_MONITOR_INTERVAL`` seconds, writes a row to
``system_metrics``, and fires a spoken alert (via an injected handler)
when a threshold is breached — but only on the *rising edge*, so a
sustained-high CPU doesn't nag once a minute forever.

macOS caveats handled here:
  * CPU temperature: ``psutil.sensors_temperatures()`` is empty on macOS
    (Apple exposes temps only via SMC, which needs elevated access). We
    return ``None`` and the temp threshold simply never fires rather than
    pretending. ``cpu_temp`` is therefore best-effort, present on Linux.
  * ``cpu_percent`` is primed once at construction and read with
    ``interval=None`` everywhere after, so no call ever blocks the asyncio
    event loop waiting for a sampling window.

Like every security component, all public methods are best-effort: any
failure prints and returns a safe default rather than raising.
"""
from __future__ import annotations

import gc
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

try:
    import psutil  # type: ignore[import-not-found]
    _PSUTIL_OK = True
except Exception as _psutil_exc:  # noqa: BLE001
    print(f"[SystemMonitor] psutil unavailable: {_psutil_exc} — "
          "system monitoring disabled")
    psutil = None  # type: ignore[assignment]
    _PSUTIL_OK = False


# Thresholds per the spec. Overridable from .env via SecurityManager, but
# these defaults are sane for a 16 GB MacBook.
THRESHOLDS: dict[str, float] = {
    "cpu_warn": 80,
    "cpu_critical": 95,
    "ram_warn": 80,
    "ram_critical": 90,
    "disk_warn": 80,
    "disk_critical": 90,
    "temp_warn": 80,
    "temp_critical": 95,
    "battery_low": 20,
    "battery_critical": 10,
}

# Alert handler signature: (spoken_message, severity) -> None.
AlertHandler = Callable[[str, str], None]


@dataclass
class SystemHealth:
    cpu_percent: float
    cpu_temp: float | None
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    disk_percent: float
    disk_free_gb: float
    battery_percent: int | None
    battery_charging: bool
    network_upload_mb: float
    network_download_mb: float
    uptime_hours: float
    status: str  # healthy / warning / critical

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SystemMonitor:
    """Polls system health, logs metrics, and alerts on threshold breaches."""

    def __init__(
        self,
        db: Any = None,
        alert_handler: AlertHandler | None = None,
        interval_s: int = 60,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        self._db = db
        self._alert = alert_handler
        self._interval = max(5, int(interval_s))
        self._thresholds = {**THRESHOLDS, **(thresholds or {})}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Rising-edge alert de-dup: remember which keys are currently in a
        # breached state so we alert on entry, not every tick.
        self._breached: set[str] = set()

        if _PSUTIL_OK:
            # Prime the cpu_percent sampler so the first real read returns
            # a value relative to *now* instead of 0.0 / a blocking wait.
            try:
                psutil.cpu_percent(interval=None)
            except Exception:  # noqa: BLE001
                pass

    @property
    def available(self) -> bool:
        return _PSUTIL_OK

    # ── core health snapshot ───────────────────────────────────────────── #

    def _read_cpu_temp(self) -> float | None:
        """Best-effort CPU temperature. Returns None on macOS (no sudo-free
        SMC access) — the temp threshold simply never fires there."""
        if not _PSUTIL_OK:
            return None
        fn = getattr(psutil, "sensors_temperatures", None)
        if fn is None:
            return None
        try:
            temps = fn()
        except Exception:  # noqa: BLE001
            return None
        if not temps:
            return None
        # Prefer a coretemp-like sensor; otherwise take the first reading.
        for key in ("coretemp", "cpu_thermal", "cpu-thermal", "k10temp"):
            if key in temps and temps[key]:
                return float(temps[key][0].current)
        for readings in temps.values():
            if readings:
                return float(readings[0].current)
        return None

    def get_system_health(self) -> SystemHealth:
        """Synchronous snapshot. Safe to call from anywhere."""
        if not _PSUTIL_OK:
            return SystemHealth(
                cpu_percent=0.0, cpu_temp=None, ram_percent=0.0,
                ram_used_gb=0.0, ram_total_gb=0.0, disk_percent=0.0,
                disk_free_gb=0.0, battery_percent=None, battery_charging=False,
                network_upload_mb=0.0, network_download_mb=0.0,
                uptime_hours=0.0, status="unknown",
            )
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            net = psutil.net_io_counters()

            battery_pct: int | None = None
            charging = False
            try:
                batt = psutil.sensors_battery()
                if batt is not None:
                    battery_pct = int(batt.percent)
                    charging = bool(batt.power_plugged)
            except Exception:  # noqa: BLE001 — desktops have no battery
                pass

            temp = self._read_cpu_temp()
            uptime_h = max(0.0, (time.time() - psutil.boot_time()) / 3600.0)

            health = SystemHealth(
                cpu_percent=round(float(cpu), 1),
                cpu_temp=round(temp, 1) if temp is not None else None,
                ram_percent=round(float(vm.percent), 1),
                ram_used_gb=round(vm.used / 1e9, 2),
                ram_total_gb=round(vm.total / 1e9, 2),
                disk_percent=round(float(disk.percent), 1),
                disk_free_gb=round(disk.free / 1e9, 1),
                battery_percent=battery_pct,
                battery_charging=charging,
                network_upload_mb=round(net.bytes_sent / 1e6, 1),
                network_download_mb=round(net.bytes_recv / 1e6, 1),
                uptime_hours=round(uptime_h, 1),
                status="healthy",
            )
            health.status = self._classify(health)
            return health
        except Exception as exc:  # noqa: BLE001
            print(f"[SystemMonitor] get_system_health failed: {exc}")
            return SystemHealth(
                cpu_percent=0.0, cpu_temp=None, ram_percent=0.0,
                ram_used_gb=0.0, ram_total_gb=0.0, disk_percent=0.0,
                disk_free_gb=0.0, battery_percent=None, battery_charging=False,
                network_upload_mb=0.0, network_download_mb=0.0,
                uptime_hours=0.0, status="unknown",
            )

    def _classify(self, h: SystemHealth) -> str:
        t = self._thresholds
        crit = (
            h.cpu_percent >= t["cpu_critical"]
            or h.ram_percent >= t["ram_critical"]
            or h.disk_percent >= t["disk_critical"]
            or (h.cpu_temp is not None and h.cpu_temp >= t["temp_critical"])
            or (h.battery_percent is not None
                and not h.battery_charging
                and h.battery_percent <= t["battery_critical"])
        )
        if crit:
            return "critical"
        warn = (
            h.cpu_percent >= t["cpu_warn"]
            or h.ram_percent >= t["ram_warn"]
            or h.disk_percent >= t["disk_warn"]
            or (h.cpu_temp is not None and h.cpu_temp >= t["temp_warn"])
            or (h.battery_percent is not None
                and not h.battery_charging
                and h.battery_percent <= t["battery_low"])
        )
        return "warning" if warn else "healthy"

    # ── top processes ──────────────────────────────────────────────────── #

    def get_top_processes(self, n: int = 5) -> list[dict[str, Any]]:
        """Top N processes by combined CPU+RAM pressure."""
        if not _PSUTIL_OK:
            return []
        procs: list[dict[str, Any]] = []
        try:
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    info = p.info
                    mem = info.get("memory_info")
                    rss_gb = (mem.rss / 1e9) if mem else 0.0
                    # cpu_percent(None) needs a prior call to be meaningful;
                    # for a one-shot ranking RSS dominates anyway.
                    cpu = p.cpu_percent(interval=None)
                    procs.append({
                        "pid": info.get("pid"),
                        "name": info.get("name") or "?",
                        "cpu_percent": round(float(cpu), 1),
                        "ram_gb": round(rss_gb, 2),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            procs.sort(
                key=lambda d: (d["ram_gb"], d["cpu_percent"]), reverse=True
            )
            return procs[:n]
        except Exception as exc:  # noqa: BLE001
            print(f"[SystemMonitor] get_top_processes failed: {exc}")
            return []

    def spoken_top_processes(self, n: int = 3) -> str:
        top = self.get_top_processes(n)
        if not top:
            return "Ich kann die Prozessliste gerade nicht lesen."
        parts = [
            f"{p['name']} mit {p['ram_gb']:.1f} Gigabyte RAM" for p in top
        ]
        return "Die größten Ressourcenfresser: " + ", ".join(parts) + "."

    # ── JARVIS component health ────────────────────────────────────────── #

    def check_jarvis_health(self) -> dict[str, Any]:
        """Probe the JARVIS subsystems we can verify from inside the process."""
        components: dict[str, bool] = {}

        # SQLite security DB writable?
        try:
            components["security_db"] = bool(
                self._db is not None and self._db.query("SELECT 1") is not None
            )
        except Exception:  # noqa: BLE001
            components["security_db"] = False

        # ChromaDB store present on disk?
        components["chromadb"] = os.path.isdir("data/chromadb")

        # Productivity / main jarvis.db readable?
        components["jarvis_db"] = os.path.isfile("data/jarvis.db")

        # The server itself: if this code is running, the event loop is up.
        components["server"] = True

        healthy = all(components.values())
        return {
            "healthy": healthy,
            "components": components,
            "status": "healthy" if healthy else "degraded",
        }

    # ── network devices (light ARP-table read) ─────────────────────────── #

    def get_network_devices(self) -> list[dict[str, Any]]:
        """Parse the local ARP cache (``arp -a``). A lightweight inventory;
        ``digital_security`` does the active scan + unknown-device alerting."""
        devices: list[dict[str, Any]] = []
        try:
            # -n = numeric: skip reverse-DNS, which otherwise hangs for
            # seconds per unresolved host and blows the timeout.
            out = subprocess.run(
                ["arp", "-an"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if out.returncode != 0:
                return []
            for line in (out.stdout or "").splitlines():
                # macOS: "hostname (192.168.1.5) at aa:bb:cc:dd:ee:ff on en0 ..."
                if " at " not in line or "(" not in line:
                    continue
                try:
                    host = line.split(" (")[0].strip() or None
                    ip = line.split("(")[1].split(")")[0]
                    mac = line.split(" at ")[1].split(" on ")[0].strip()
                    if mac in ("(incomplete)", ""):
                        continue
                    devices.append({"ip": ip, "mac": mac, "hostname": host})
                except (IndexError, ValueError):
                    continue
            return devices
        except Exception as exc:  # noqa: BLE001
            print(f"[SystemMonitor] get_network_devices failed: {exc}")
            return []

    # ── memory cleanup ─────────────────────────────────────────────────── #

    def memory_cleanup(self) -> dict[str, Any]:
        """Best-effort RAM relief: force a GC pass and report the delta."""
        if not _PSUTIL_OK:
            gc.collect()
            return {"freed_mb": 0.0, "note": "psutil unavailable"}
        try:
            before = psutil.virtual_memory().used
            collected = gc.collect()
            after = psutil.virtual_memory().used
            freed_mb = max(0.0, (before - after) / 1e6)
            return {
                "freed_mb": round(freed_mb, 1),
                "gc_objects": collected,
            }
        except Exception as exc:  # noqa: BLE001
            print(f"[SystemMonitor] memory_cleanup failed: {exc}")
            return {"freed_mb": 0.0, "error": str(exc)}

    # ── spoken status ──────────────────────────────────────────────────── #

    def spoken_status(self) -> str:
        h = self.get_system_health()
        if h.status == "unknown":
            return "Systemüberwachung ist gerade nicht verfügbar."
        batt = (
            f", Akku {h.battery_percent} Prozent"
            f"{' am Laden' if h.battery_charging else ''}"
            if h.battery_percent is not None else ""
        )
        verdict = {
            "healthy": "Alles im grünen Bereich.",
            "warning": "Ein Wert liegt im Warnbereich.",
            "critical": "Achtung, ein Wert ist kritisch!",
        }.get(h.status, "")
        return (
            f"CPU {h.cpu_percent:.0f} Prozent, "
            f"RAM {h.ram_percent:.0f} Prozent, "
            f"Festplatte {h.disk_percent:.0f} Prozent belegt{batt}. "
            f"{verdict}"
        )

    # ── background monitor loop ────────────────────────────────────────── #

    def start(self) -> None:
        if not _PSUTIL_OK:
            print("[SystemMonitor] not started (psutil unavailable)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="jarvis-sysmon", daemon=True,
        )
        self._thread.start()
        print(f"[SystemMonitor] monitor loop active (every {self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _monitor_loop(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            try:
                h = self.get_system_health()
                if self._db is not None:
                    self._db.log_metrics(
                        cpu_percent=h.cpu_percent,
                        ram_percent=h.ram_percent,
                        disk_percent=h.disk_percent,
                        cpu_temp=h.cpu_temp,
                        battery_percent=h.battery_percent,
                        network_mb=round(
                            h.network_upload_mb + h.network_download_mb, 1
                        ),
                    )
                self._check_thresholds(h)

                # Auto memory relief above 85% RAM (per spec).
                if h.ram_percent >= 85:
                    self.memory_cleanup()

                # Prune old metric rows + trim breach state once an hour.
                ticks += 1
                if ticks % max(1, 3600 // self._interval) == 0 and self._db:
                    self._db.prune_metrics(14)
            except Exception as exc:  # noqa: BLE001
                print(f"[SystemMonitor] monitor tick failed: {exc}")
            # Sleep in small slices so stop() is responsive.
            self._stop.wait(self._interval)

    def _check_thresholds(self, h: SystemHealth) -> None:
        t = self._thresholds
        # cpu_temp is None on macOS — format it safely so building the
        # message list below never hits f"{None:.0f}" (the condition that
        # uses it is False there, but the f-string is evaluated eagerly).
        temp_str = f"{h.cpu_temp:.0f}" if h.cpu_temp is not None else "—"
        # (key, condition, severity, spoken message)
        checks: list[tuple[str, bool, str, str]] = [
            ("cpu_crit", h.cpu_percent >= t["cpu_critical"], "CRITICAL",
             f"MacBook CPU Auslastung kritisch: {h.cpu_percent:.0f} Prozent."),
            ("ram_crit", h.ram_percent >= t["ram_critical"], "CRITICAL",
             f"RAM fast voll: {h.ram_used_gb:.1f} von {h.ram_total_gb:.0f} "
             f"Gigabyte belegt."),
            ("disk_crit", h.disk_percent >= t["disk_critical"], "CRITICAL",
             f"Festplatte fast voll: nur noch {h.disk_free_gb:.0f} Gigabyte frei."),
            ("temp_crit",
             h.cpu_temp is not None and h.cpu_temp >= t["temp_critical"],
             "CRITICAL",
             f"MacBook überhitzt: {temp_str} Grad!"),
            ("batt_crit",
             h.battery_percent is not None and not h.battery_charging
             and h.battery_percent <= t["battery_critical"],
             "CRITICAL",
             f"Akku kritisch niedrig: {h.battery_percent} Prozent."),
        ]
        for key, breached, severity, message in checks:
            if breached and key not in self._breached:
                self._breached.add(key)
                self._fire_alert(message, severity)
                if self._db is not None:
                    self._db.log_event(
                        event_type="system_threshold",
                        severity=severity,
                        source="system_monitor",
                        description=message,
                    )
            elif not breached:
                self._breached.discard(key)

    def _fire_alert(self, message: str, severity: str) -> None:
        print(f"[SystemMonitor] {severity}: {message}")
        if self._alert is not None:
            try:
                self._alert(message, severity)
            except Exception as exc:  # noqa: BLE001
                print(f"[SystemMonitor] alert handler failed: {exc}")
