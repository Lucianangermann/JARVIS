"""SQLite-backed task and project manager using the existing data/jarvis.db."""
from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any


_CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    status      TEXT DEFAULT 'active',
    due_date    TEXT,
    color       TEXT DEFAULT '#00d4ff',
    goal        TEXT,
    created_at  REAL NOT NULL
)
"""

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    description        TEXT,
    project_id         INTEGER,
    priority           INTEGER DEFAULT 2,
    status             TEXT DEFAULT 'todo',
    due_date           TEXT,
    created_at         REAL NOT NULL,
    completed_at       REAL,
    estimated_minutes  INTEGER,
    context            TEXT DEFAULT 'work',
    energy_level       TEXT DEFAULT 'medium',
    tags               TEXT DEFAULT ''
)
"""

_CREATE_TIME_ENTRIES = """
CREATE TABLE IF NOT EXISTS time_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project          TEXT NOT NULL,
    task_title       TEXT,
    start_time       REAL NOT NULL,
    end_time         REAL,
    duration_minutes REAL,
    category         TEXT DEFAULT 'work',
    notes            TEXT
)
"""


class TaskManager:
    """SQLite-backed task and project manager."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_PROJECTS)
            self._conn.execute(_CREATE_TASKS)
            self._conn.execute(_CREATE_TIME_ENTRIES)
            self._conn.commit()
        except Exception as exc:
            print(f"[TaskManager] DB init failed: {exc}")

    # ── helpers ──────────────────────────────────────────────────────────── #

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _today_str(self) -> str:
        return date.today().isoformat()

    # ── projects ──────────────────────────────────────────────────────────── #

    def _get_or_create_project(self, name: str) -> int | None:
        try:
            cur = self._conn.execute(
                "SELECT id FROM projects WHERE name = ?", (name,)
            )
            row = cur.fetchone()
            if row:
                return row["id"]
            cur = self._conn.execute(
                "INSERT INTO projects (name, created_at) VALUES (?, ?)",
                (name, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[TaskManager] _get_or_create_project failed: {exc}")
            return None

    def list_projects(self) -> list[dict[str, Any]]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM projects WHERE status='active' ORDER BY name"
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]
        except Exception as exc:
            print(f"[TaskManager] list_projects failed: {exc}")
            return []

    # ── tasks ─────────────────────────────────────────────────────────────── #

    def add_task(
        self,
        title: str,
        priority: int = 2,
        due_date: str | None = None,
        project_name: str | None = None,
        context: str = "work",
        tags: str = "",
        description: str = "",
    ) -> int:
        try:
            project_id: int | None = None
            if project_name:
                project_id = self._get_or_create_project(project_name)
            cur = self._conn.execute(
                """INSERT INTO tasks
                   (title, description, project_id, priority, due_date,
                    created_at, context, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, description, project_id, priority, due_date,
                 time.time(), context, tags),
            )
            self._conn.commit()
            return cur.lastrowid or 0
        except Exception as exc:
            print(f"[TaskManager] add_task failed: {exc}")
            return 0

    def get_today_tasks(self) -> list[dict[str, Any]]:
        try:
            today = self._today_str()
            cur = self._conn.execute(
                """SELECT * FROM tasks
                   WHERE status IN ('todo','in_progress')
                     AND (due_date IS NULL OR due_date <= ?)
                   ORDER BY priority ASC, due_date ASC""",
                (today,),
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]
        except Exception as exc:
            print(f"[TaskManager] get_today_tasks failed: {exc}")
            return []

    def get_top3(self) -> list[dict[str, Any]]:
        return self.get_today_tasks()[:3]

    def complete_task(self, task_id: int) -> bool:
        try:
            self._conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[TaskManager] complete_task failed: {exc}")
            return False

    def get_overdue(self) -> list[dict[str, Any]]:
        try:
            today = self._today_str()
            cur = self._conn.execute(
                """SELECT * FROM tasks
                   WHERE status IN ('todo','in_progress')
                     AND due_date IS NOT NULL
                     AND due_date < ?
                   ORDER BY priority ASC, due_date ASC""",
                (today,),
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]
        except Exception as exc:
            print(f"[TaskManager] get_overdue failed: {exc}")
            return []

    def get_project_status(self, project_name: str) -> dict[str, Any]:
        try:
            today = self._today_str()
            proj_cur = self._conn.execute(
                "SELECT * FROM projects WHERE name=?", (project_name,)
            )
            proj = proj_cur.fetchone()
            if not proj:
                return {"error": f"Projekt '{project_name}' nicht gefunden."}
            pid = proj["id"]

            totals = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id=?", (pid,)
            ).fetchone()[0]
            done = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='done'",
                (pid,),
            ).fetchone()[0]
            in_prog = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='in_progress'",
                (pid,),
            ).fetchone()[0]
            overdue = self._conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE project_id=? AND status IN ('todo','in_progress')
                     AND due_date IS NOT NULL AND due_date < ?""",
                (pid, today),
            ).fetchone()[0]
            next_cur = self._conn.execute(
                """SELECT * FROM tasks
                   WHERE project_id=? AND status IN ('todo','in_progress')
                   ORDER BY priority ASC, due_date ASC LIMIT 3""",
                (pid,),
            )
            next_tasks = [self._row_to_dict(r) for r in next_cur.fetchall()]

            return {
                "name":       project_name,
                "total":      totals,
                "done":       done,
                "in_progress": in_prog,
                "overdue":    overdue,
                "next_tasks": next_tasks,
            }
        except Exception as exc:
            print(f"[TaskManager] get_project_status failed: {exc}")
            return {}

    # ── spoken summaries ──────────────────────────────────────────────────── #

    def spoken_top3(self) -> str:
        try:
            top = self.get_top3()
            if not top:
                return "Keine offenen Tasks für heute."
            parts = [f"{i+1}. {t['title']}" for i, t in enumerate(top)]
            return "Deine Top 3 heute: " + ", ".join(parts) + "."
        except Exception as exc:
            print(f"[TaskManager] spoken_top3 failed: {exc}")
            return "Task-Übersicht momentan nicht verfügbar."

    def spoken_project_status(self, project_name: str) -> str:
        try:
            s = self.get_project_status(project_name)
            if "error" in s:
                return s["error"]
            nxt = s.get("next_tasks", [])
            nxt_str = (
                "Nächste Aufgabe: " + nxt[0]["title"] + "."
                if nxt else "Keine offenen Tasks."
            )
            return (
                f"Projekt {s['name']}: {s['done']} von {s['total']} Tasks erledigt"
                f", {s['in_progress']} in Arbeit"
                f", {s['overdue']} überfällig. {nxt_str}"
            )
        except Exception as exc:
            print(f"[TaskManager] spoken_project_status failed: {exc}")
            return "Projektstatus momentan nicht verfügbar."
