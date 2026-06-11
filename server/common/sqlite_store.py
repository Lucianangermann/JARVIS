"""Shared thread-safe SQLite base for the JARVIS data layers.

security.db, communication.db, finance.db, and knowledge.db all opened a
``check_same_thread=False`` connection in WAL mode with a ``Row`` factory,
guarded the same ``threading.Lock``, and re-implemented the identical
``_execute`` / ``query`` / ``close`` trio. That boilerplate is now here
once, so a WAL/locking/busy-timeout change lands in a single place.

Subclasses override :meth:`_init_schema` (create tables + indices on the
passed connection) and optionally :meth:`_on_ready` (e.g. a boot prune).
Every write/read is best-effort: a failure prints and returns a falsy
value rather than raising — a data-layer hiccup must never crash JARVIS.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class ThreadSafeDB:
    """One locked WAL connection with best-effort execute/query helpers."""

    def __init__(self, db_path: Path | str, *, label: str = "DB") -> None:
        self._db_path = Path(db_path)
        self._label = label
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._init_schema(self._conn)   # no lock needed pre-concurrency
            self._conn.commit()
            self._on_ready()
            print(f"[{label}] ready at {self._db_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] init failed: {exc}")

    # ── subclass hooks ─────────────────────────────────────────────────── #

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + indices. Override in the subclass."""
        raise NotImplementedError

    def _on_ready(self) -> None:
        """Optional post-init hook (e.g. retention prune). Default no-op."""

    # ── core helpers ───────────────────────────────────────────────────── #

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            print(f"[{self._label}] write failed: {exc}")
            return None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"[{self._label}] query failed: {exc}")
            return []

    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[{self._label}] close failed: {exc}")
            finally:
                self._conn = None
