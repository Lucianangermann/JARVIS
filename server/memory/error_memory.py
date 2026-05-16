"""SQLite-backed error history + known-fix lookup.

Three tables, all under ``data/jarvis.db``:

errors
    Every failed command + the error class it raised. Tracks how
    many times the same (command, error_type) pair has reappeared
    so we can promote a frequent failure to known_fixes once a
    workaround proves itself.

known_fixes
    Stable mapping ``error_pattern → fix`` with a running success
    rate. The brain queries this BEFORE executing a risky command
    via :func:`get_known_fix`; if there's a high-confidence match it
    can pre-emptively pick the workaround.

command_stats
    Aggregate counters per command_hash — total / success / fail /
    avg duration. Powers ``get_problematic_commands`` which is what
    we surface to the system prompt as "watch out for these".

Lock model
----------
SQLite serialises writes via its own file lock; we add a process
lock around connection use so the brain's thread pool can't trip
over each other on shared cursors. All errors are caught + logged
so memory misfires never bubble into Brain.reply().
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.memory.errors")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    command         TEXT    NOT NULL,
    command_hash    TEXT    NOT NULL,
    error_type      TEXT    NOT NULL,
    error_message   TEXT    NOT NULL,
    category        TEXT    NOT NULL DEFAULT 'other',
    fix_attempted   TEXT,
    fix_worked      INTEGER,            -- nullable: 0/1 or NULL when no fix yet
    retry_count     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_errors_hash ON errors (command_hash, error_type);
CREATE INDEX IF NOT EXISTS ix_errors_ts   ON errors (ts);

CREATE TABLE IF NOT EXISTS known_fixes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    error_pattern   TEXT    NOT NULL,   -- "<command_hash>|<error_type>"
    fix             TEXT    NOT NULL,
    uses            INTEGER NOT NULL DEFAULT 1,
    successes       INTEGER NOT NULL DEFAULT 1,
    last_used       REAL    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_known_fixes_pat ON known_fixes (error_pattern);

CREATE TABLE IF NOT EXISTS command_stats (
    command_hash    TEXT    PRIMARY KEY,
    command         TEXT    NOT NULL,
    total_calls     INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_seen       REAL    NOT NULL,
    avg_duration_ms REAL    NOT NULL DEFAULT 0
);
"""


def _hash(command: str) -> str:
    """Stable short hash of a command's normalised form. Used as the
    join key everywhere — different exact phrasings of the same
    intent collapse to one row only if the normalisation collapses
    them, which for now means lowercasing + collapsing whitespace.
    Coarse enough that "Open Safari" and "open  safari" share stats."""
    norm = " ".join((command or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


class ErrorMemory:
    """Durable error / fix / stats store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.available = False
        try:
            self._open()
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("error memory disabled: %s", exc)

    # ---- setup -----------------------------------------------------------

    def _open(self) -> None:
        # check_same_thread=False because FastAPI's threadpool will
        # rotate workers. We serialise access via self._lock.
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    # ---- public API ------------------------------------------------------

    def record_error(self, command: str, error: BaseException | str, *,
                     category: str = "other") -> int | None:
        """Insert one error row, or bump retry_count if the same
        (command_hash, error_type) is the most recent existing entry.
        Returns the row id or None on degraded mode."""
        if not self.available:
            return None
        err_type, err_msg = _classify(error)
        cmd_hash = _hash(command)
        try:
            with self._lock:
                cur = self._conn.cursor()
                # Look for an open (no-fix-yet) recent matching row to
                # increment instead of inserting a duplicate.
                cur.execute(
                    "SELECT id, retry_count FROM errors "
                    "WHERE command_hash=? AND error_type=? AND fix_worked IS NULL "
                    "ORDER BY ts DESC LIMIT 1",
                    (cmd_hash, err_type),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        "UPDATE errors SET retry_count=?, ts=? WHERE id=?",
                        (existing[1] + 1, time.time(), existing[0]),
                    )
                    row_id = existing[0]
                else:
                    cur.execute(
                        "INSERT INTO errors (ts, command, command_hash, "
                        "error_type, error_message, category) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), command, cmd_hash, err_type, err_msg, category),
                    )
                    row_id = cur.lastrowid
                # Update aggregate stats — failure counter.
                self._touch_stats(cmd_hash, command, success=False)
                log.info("err recorded: cmd=%r type=%s row=%d", command[:60], err_type, row_id)
                return row_id
        except Exception as exc:  # noqa: BLE001
            log.warning("record_error failed: %s", exc)
            return None

    def record_success(self, command: str, *, duration_ms: float | None = None) -> None:
        """Bump the success counter for a command. Called from the
        manager after every clean tool run. Cheap — keeps
        ``get_problematic_commands`` accurate."""
        if not self.available:
            return
        cmd_hash = _hash(command)
        try:
            with self._lock:
                self._touch_stats(cmd_hash, command, success=True, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001
            log.warning("record_success failed: %s", exc)

    def record_fix(self, error_id: int, fix: str, *, worked: bool) -> None:
        """Mark an error row with the attempted fix + outcome. Promotes
        successful fixes to ``known_fixes`` (or bumps the existing
        row's success rate)."""
        if not self.available:
            return
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute(
                    "UPDATE errors SET fix_attempted=?, fix_worked=? WHERE id=?",
                    (fix, 1 if worked else 0, error_id),
                )
                cur.execute(
                    "SELECT command_hash, error_type FROM errors WHERE id=?", (error_id,),
                )
                row = cur.fetchone()
                if not row:
                    return
                pattern = f"{row[0]}|{row[1]}"
                now = time.time()
                if worked:
                    # Promote / refresh in known_fixes. INSERT OR IGNORE +
                    # UPDATE so we always end with one row.
                    cur.execute(
                        "INSERT OR IGNORE INTO known_fixes "
                        "(error_pattern, fix, uses, successes, last_used) "
                        "VALUES (?, ?, 1, 1, ?)",
                        (pattern, fix, now),
                    )
                    cur.execute(
                        "UPDATE known_fixes SET fix=?, uses=uses+1, "
                        "successes=successes+1, last_used=? "
                        "WHERE error_pattern=? AND fix<>?",
                        (fix, now, pattern, fix),
                    )
                    cur.execute(
                        "UPDATE known_fixes SET uses=uses+1, successes=successes+1, last_used=? "
                        "WHERE error_pattern=? AND fix=?",
                        (now, pattern, fix),
                    )
                else:
                    cur.execute(
                        "UPDATE known_fixes SET uses=uses+1, last_used=? "
                        "WHERE error_pattern=? AND fix=?",
                        (now, pattern, fix),
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("record_fix failed: %s", exc)

    def get_known_fix(self, command: str, error_type: str | None = None
                      ) -> dict[str, Any] | None:
        """Return a previously-successful fix for this command (and
        optionally a specific error_type). Returns
        ``{fix, success_rate, uses}`` or None."""
        if not self.available:
            return None
        cmd_hash = _hash(command)
        try:
            with self._lock:
                cur = self._conn.cursor()
                if error_type:
                    pattern = f"{cmd_hash}|{error_type}"
                    cur.execute(
                        "SELECT fix, uses, successes FROM known_fixes "
                        "WHERE error_pattern=? ORDER BY last_used DESC LIMIT 1",
                        (pattern,),
                    )
                else:
                    # Best fix for any error type on this command.
                    cur.execute(
                        "SELECT fix, uses, successes FROM known_fixes "
                        "WHERE error_pattern LIKE ? "
                        "ORDER BY (CAST(successes AS REAL) / NULLIF(uses, 0)) DESC, "
                        "         last_used DESC LIMIT 1",
                        (f"{cmd_hash}|%",),
                    )
                row = cur.fetchone()
                if not row:
                    return None
                fix, uses, successes = row
                return {"fix": fix, "uses": uses, "successes": successes,
                        "success_rate": successes / uses if uses else 0.0}
        except Exception as exc:  # noqa: BLE001
            log.warning("get_known_fix failed: %s", exc)
            return None

    def get_problematic_commands(self, *, min_failures: int = 2,
                                 limit: int = 10) -> list[dict[str, Any]]:
        """Commands with at least ``min_failures`` failures. Surfaced
        in the system prompt as warnings so the brain can pick a
        gentler tactic ("be conservative when the user asks X")."""
        if not self.available:
            return []
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT command, total_calls, success_count, fail_count, last_seen "
                    "FROM command_stats WHERE fail_count >= ? "
                    "ORDER BY fail_count DESC, last_seen DESC LIMIT ?",
                    (min_failures, limit),
                )
                rows = cur.fetchall()
            return [
                {"command": r[0], "total": r[1], "success": r[2],
                 "fail": r[3], "last_seen": r[4],
                 "success_rate": (r[2] / r[1]) if r[1] else 0.0}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("get_problematic_commands failed: %s", exc)
            return []

    def auto_retry_strategy(self, command: str) -> dict[str, Any]:
        """Suggest a retry policy based on this command's history.

        Simple heuristic: more past failures → more retries + longer
        delay. Returns ``{retries, delay, fallback}``. ``fallback`` is
        the most recent successful fix text (so the brain can swap to
        it without re-trying the original)."""
        default = {"retries": 1, "delay": 0.5, "fallback": None}
        if not self.available:
            return default
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT success_count, fail_count FROM command_stats "
                    "WHERE command_hash=?", (_hash(command),),
                )
                stats = cur.fetchone() or (0, 0)
            fixes = self.get_known_fix(command)
            success, fail = stats
            if fail >= 4:
                strat = {"retries": 3, "delay": 2.0, "fallback": None}
            elif fail >= 2:
                strat = {"retries": 2, "delay": 1.0, "fallback": None}
            else:
                strat = default.copy()
            if fixes and fixes["success_rate"] >= 0.5:
                strat["fallback"] = fixes["fix"]
            return strat
        except Exception as exc:  # noqa: BLE001
            log.warning("auto_retry_strategy failed: %s", exc)
            return default

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": self.available, "db_path": str(self.db_path)}
        if not self.available:
            return out
        try:
            with self._lock:
                cur = self._conn.cursor()
                for tbl in ("errors", "known_fixes", "command_stats"):
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    out[tbl] = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
        return out

    def wipe_all(self) -> dict[str, int]:
        """Full wipe of all three tables. Returns rowcount per table."""
        wiped = {"errors": 0, "known_fixes": 0, "command_stats": 0}
        if not self.available:
            return wiped
        try:
            with self._lock:
                cur = self._conn.cursor()
                for tbl in wiped:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    wiped[tbl] = cur.fetchone()[0]
                    cur.execute(f"DELETE FROM {tbl}")
        except Exception as exc:  # noqa: BLE001
            log.warning("wipe_all failed: %s", exc)
        return wiped

    # ---- internals -------------------------------------------------------

    def _touch_stats(self, cmd_hash: str, command: str, *,
                     success: bool, duration_ms: float | None = None) -> None:
        """Upsert a command_stats row. Caller holds self._lock."""
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO command_stats (command_hash, command, total_calls, "
            "success_count, fail_count, last_seen, avg_duration_ms) "
            "VALUES (?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(command_hash) DO UPDATE SET "
            "  total_calls   = command_stats.total_calls + 1, "
            "  success_count = command_stats.success_count + ?, "
            "  fail_count    = command_stats.fail_count + ?, "
            "  last_seen     = excluded.last_seen, "
            "  avg_duration_ms = "
            "    CASE WHEN ? IS NULL THEN command_stats.avg_duration_ms "
            "    ELSE (command_stats.avg_duration_ms * command_stats.total_calls + ?) "
            "         / (command_stats.total_calls + 1) END",
            (cmd_hash, command, 1 if success else 0, 0 if success else 1,
             time.time(), duration_ms or 0.0,
             1 if success else 0, 0 if success else 1,
             duration_ms, duration_ms or 0.0),
        )


def _classify(error: BaseException | str) -> tuple[str, str]:
    """Reduce an exception or error string to ``(type_name, message)``.

    type_name is the exception class name, or a leading bracketed
    tag in a string ("[ERROR] HomeKit timeout" → "ERROR"). Used as
    the join key for known-fix lookups so phrasing changes in the
    message don't fragment the index."""
    if isinstance(error, BaseException):
        return error.__class__.__name__, str(error)[:400]
    s = str(error)
    m = re.match(r"\s*\[([A-Za-z_][A-Za-z_0-9 ]{0,30})\]", s)
    return (m.group(1).strip() if m else "Error"), s[:400]
