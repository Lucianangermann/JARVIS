"""SQLite store for the JARVIS security layer (``data/security.db``).

Deliberately kept separate from ``data/jarvis.db``: the security tables
have their own retention, audit, and privacy rules, and we never want a
corrupt productivity write to take the audit log down with it (or vice
versa). One :class:`SecurityDB` instance is created by
:class:`~server.security.security_manager.SecurityManager` and shared by
every sub-component, mirroring how the productivity layer threads a
single ``jarvis.db`` handle through TaskManager / FocusManager / …

Connection conventions match the rest of the codebase: a single
``check_same_thread=False`` connection in WAL mode with ``Row`` factory,
guarded by a lock because the background monitor loop, the FastAPI
request handlers, and the voice thread all write concurrently.

Every method is best-effort: a failed insert prints and returns a falsy
value rather than raising, because a security log write must NEVER crash
JARVIS (see the hard rules in tasks/todo.md).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# ── Schema ──────────────────────────────────────────────────────────────── #
# Five tables per the spec. `id` + `timestamp` (epoch seconds, REAL) on
# every row so the daily-summary / retention queries are uniform.

_CREATE_SECURITY_EVENTS = """
CREATE TABLE IF NOT EXISTS security_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL NOT NULL,
    event_type    TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'INFO',
    source        TEXT,
    description   TEXT,
    resolved      INTEGER NOT NULL DEFAULT 0,
    snapshot_path TEXT
)
"""

_CREATE_ACCESS_LOG = """
CREATE TABLE IF NOT EXISTS access_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        REAL NOT NULL,
    user             TEXT,
    command          TEXT,
    ip_address       TEXT,
    voice_confidence REAL,
    permission_level TEXT,
    allowed          INTEGER NOT NULL DEFAULT 0,
    reason           TEXT
)
"""

_CREATE_CAMERA_EVENTS = """
CREATE TABLE IF NOT EXISTS camera_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL NOT NULL,
    camera_id      INTEGER,
    detection_type TEXT,
    confidence     REAL,
    snapshot_path  TEXT,
    alerted        INTEGER NOT NULL DEFAULT 0,
    description    TEXT
)
"""

_CREATE_SYSTEM_METRICS = """
CREATE TABLE IF NOT EXISTS system_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    cpu_percent     REAL,
    ram_percent     REAL,
    disk_percent    REAL,
    cpu_temp        REAL,
    battery_percent INTEGER,
    network_mb      REAL
)
"""

_CREATE_KNOWN_DEVICES = """
CREATE TABLE IF NOT EXISTS known_devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mac_address TEXT UNIQUE,
    hostname    TEXT,
    ip_address  TEXT,
    first_seen  REAL,
    last_seen   REAL,
    trusted     INTEGER NOT NULL DEFAULT 0,
    device_type TEXT
)
"""

# Indices the hot queries actually use: time-ordered scans per table and
# the dedup lookup on MAC address.
_CREATE_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_sec_events_ts   ON security_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_access_log_ts   ON access_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_cam_events_ts   ON camera_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_sys_metrics_ts  ON system_metrics(timestamp)",
]


class SecurityDB:
    """Thread-safe SQLite wrapper for the security layer."""

    def __init__(self, db_path: Path | str = "data/security.db") -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            for stmt in (
                _CREATE_SECURITY_EVENTS,
                _CREATE_ACCESS_LOG,
                _CREATE_CAMERA_EVENTS,
                _CREATE_SYSTEM_METRICS,
                _CREATE_KNOWN_DEVICES,
            ):
                self._conn.execute(stmt)
            for idx in _CREATE_INDICES:
                self._conn.execute(idx)
            self._conn.commit()
            print(f"[SecurityDB] ready at {self._db_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityDB] init failed: {exc}")

    # ── low-level helpers ──────────────────────────────────────────────── #

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int | None:
        """Run a write statement, return lastrowid (or None on failure)."""
        if self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityDB] write failed: {exc}")
            return None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a read statement, return a list of dict rows ([] on failure)."""
        if self._conn is None:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"[SecurityDB] query failed: {exc}")
            return []

    # ── security_events ────────────────────────────────────────────────── #

    def log_event(
        self,
        event_type: str,
        severity: str = "INFO",
        source: str | None = None,
        description: str | None = None,
        snapshot_path: str | None = None,
        resolved: bool = False,
    ) -> int | None:
        return self._execute(
            """INSERT INTO security_events
               (timestamp, event_type, severity, source, description,
                resolved, snapshot_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), event_type, severity, source, description,
             int(resolved), snapshot_path),
        )

    def recent_events(
        self, since_ts: float | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if since_ts is None:
            return self.query(
                "SELECT * FROM security_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        return self.query(
            """SELECT * FROM security_events WHERE timestamp >= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (since_ts, limit),
        )

    # ── access_log ─────────────────────────────────────────────────────── #

    def log_access(
        self,
        user: str | None,
        command: str | None,
        ip_address: str | None,
        voice_confidence: float | None,
        permission_level: str | None,
        allowed: bool,
        reason: str | None = None,
    ) -> int | None:
        return self._execute(
            """INSERT INTO access_log
               (timestamp, user, command, ip_address, voice_confidence,
                permission_level, allowed, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user, command, ip_address, voice_confidence,
             permission_level, int(allowed), reason),
        )

    def recent_access(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM access_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    # ── camera_events ──────────────────────────────────────────────────── #

    def log_camera_event(
        self,
        camera_id: int,
        detection_type: str,
        confidence: float,
        snapshot_path: str | None = None,
        alerted: bool = False,
        description: str | None = None,
    ) -> int | None:
        return self._execute(
            """INSERT INTO camera_events
               (timestamp, camera_id, detection_type, confidence,
                snapshot_path, alerted, description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), camera_id, detection_type, confidence,
             snapshot_path, int(alerted), description),
        )

    def camera_events_since(self, since_ts: float) -> list[dict[str, Any]]:
        return self.query(
            """SELECT * FROM camera_events WHERE timestamp >= ?
               ORDER BY timestamp DESC""",
            (since_ts,),
        )

    # ── system_metrics ─────────────────────────────────────────────────── #

    def log_metrics(
        self,
        cpu_percent: float,
        ram_percent: float,
        disk_percent: float,
        cpu_temp: float | None,
        battery_percent: int | None,
        network_mb: float,
    ) -> int | None:
        return self._execute(
            """INSERT INTO system_metrics
               (timestamp, cpu_percent, ram_percent, disk_percent,
                cpu_temp, battery_percent, network_mb)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), cpu_percent, ram_percent, disk_percent,
             cpu_temp, battery_percent, network_mb),
        )

    def prune_metrics(self, older_than_days: int = 14) -> None:
        cutoff = time.time() - older_than_days * 86400
        self._execute(
            "DELETE FROM system_metrics WHERE timestamp < ?", (cutoff,)
        )

    # ── known_devices ──────────────────────────────────────────────────── #

    def upsert_device(
        self,
        mac_address: str,
        hostname: str | None,
        ip_address: str | None,
        device_type: str | None = None,
        trusted: bool = False,
    ) -> None:
        """Insert a freshly-seen device or bump its last_seen timestamp."""
        now = time.time()
        existing = self.query(
            "SELECT id, trusted FROM known_devices WHERE mac_address = ?",
            (mac_address,),
        )
        if existing:
            self._execute(
                """UPDATE known_devices
                   SET last_seen = ?, hostname = COALESCE(?, hostname),
                       ip_address = COALESCE(?, ip_address)
                   WHERE mac_address = ?""",
                (now, hostname, ip_address, mac_address),
            )
        else:
            self._execute(
                """INSERT INTO known_devices
                   (mac_address, hostname, ip_address, first_seen, last_seen,
                    trusted, device_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (mac_address, hostname, ip_address, now, now,
                 int(trusted), device_type),
            )

    def list_devices(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM known_devices ORDER BY last_seen DESC")

    def is_known_device(self, mac_address: str) -> bool:
        return bool(
            self.query(
                "SELECT 1 FROM known_devices WHERE mac_address = ?",
                (mac_address,),
            )
        )

    def trust_device(self, mac_address: str, trusted: bool = True) -> None:
        self._execute(
            "UPDATE known_devices SET trusted = ? WHERE mac_address = ?",
            (int(trusted), mac_address),
        )

    # ── lifecycle ──────────────────────────────────────────────────────── #

    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[SecurityDB] close failed: {exc}")
            finally:
                self._conn = None
