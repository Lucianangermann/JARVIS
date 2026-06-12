"""Productivity score and insights derived from SQLite data."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def _today_epoch() -> float:
    d = date.today()
    return float(datetime(d.year, d.month, d.day).timestamp())


class ProductivityAnalytics:
    """Compute daily and weekly productivity metrics from jarvis.db."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception as exc:
            print(f"[ProductivityAnalytics] DB init failed: {exc}")

    # ── Core counters ─────────────────────────────────────────────────────── #

    def get_focus_minutes_today(self) -> float:
        try:
            today_start = _today_epoch()
            cur = self._conn.execute(
                """SELECT COALESCE(SUM(duration_minutes), 0)
                   FROM time_entries
                   WHERE start_time >= ? AND duration_minutes IS NOT NULL""",
                (today_start,),
            )
            return float(cur.fetchone()[0])
        except Exception as exc:
            print(f"[ProductivityAnalytics] get_focus_minutes_today failed: {exc}")
            return 0.0

    def get_tasks_done_today(self) -> int:
        try:
            today_start = _today_epoch()
            cur = self._conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE status='done' AND completed_at >= ?""",
                (today_start,),
            )
            return int(cur.fetchone()[0])
        except Exception as exc:
            print(f"[ProductivityAnalytics] get_tasks_done_today failed: {exc}")
            return 0

    def _get_tasks_planned_today(self) -> int:
        try:
            today = date.today().isoformat()
            cur = self._conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE due_date = ? OR status IN ('todo','in_progress')""",
                (today,),
            )
            return max(1, int(cur.fetchone()[0]))
        except Exception as exc:
            print(f"[ProductivityAnalytics] _get_tasks_planned_today failed: {exc}")
            return 1

    # ── Score ─────────────────────────────────────────────────────────────── #

    def daily_score(self) -> dict[str, Any]:
        try:
            done = self.get_tasks_done_today()
            planned = self._get_tasks_planned_today()
            focus = self.get_focus_minutes_today()

            task_score = (done / max(planned, 1)) * 5
            focus_score = min(focus / 120, 1.0) * 3
            base = 2.0
            raw = task_score + focus_score + base
            score = round(min(raw, 10.0), 1)

            return {
                "score":        score,
                "tasks_done":   done,
                "tasks_planned": planned,
                "focus_minutes": round(focus, 1),
                "breakdown": {
                    "task_component":  round(task_score, 2),
                    "focus_component": round(focus_score, 2),
                    "base":            base,
                },
            }
        except Exception as exc:
            print(f"[ProductivityAnalytics] daily_score failed: {exc}")
            return {"score": 0.0, "tasks_done": 0, "tasks_planned": 0,
                    "focus_minutes": 0.0, "breakdown": {}}

    def deep_work_blocks(self, since_ts: float | None = None) -> list[dict[str, Any]]:
        """Time entries with duration >= 25 min (deep work threshold)."""
        try:
            if since_ts is None:
                since_ts = _today_epoch()
            cur = self._conn.execute(
                """SELECT start_time, duration_minutes,
                          COALESCE(project, task_title, 'Sonstiges') as label
                   FROM time_entries
                   WHERE start_time >= ? AND duration_minutes >= 25
                   ORDER BY start_time DESC""",
                (since_ts,),
            )
            return [{"start": r[0], "minutes": float(r[1]), "label": r[2]}
                    for r in cur.fetchall()]
        except Exception as exc:
            print(f"[ProductivityAnalytics] deep_work_blocks failed: {exc}")
            return []

    def deadline_risk_score(self) -> dict[str, Any]:
        """Tasks due within 3 days; risk score 0-100 (25 per at-risk task)."""
        try:
            cutoff = (date.today() + timedelta(days=3)).isoformat()
            cur = self._conn.execute(
                """SELECT id, title, due_date, status FROM tasks
                   WHERE due_date IS NOT NULL AND due_date <= ?
                   AND status NOT IN ('done', 'cancelled')
                   ORDER BY due_date""",
                (cutoff,),
            )
            rows = cur.fetchall()
            tasks = [{"title": r["title"] or str(r["id"]),
                      "due_date": r["due_date"],
                      "status": r["status"]} for r in rows]
            return {"score": min(len(tasks) * 25, 100), "at_risk": tasks}
        except Exception as exc:
            print(f"[ProductivityAnalytics] deadline_risk_score failed: {exc}")
            return {"score": 0, "at_risk": []}

    def project_time_distribution(self, since_ts: float | None = None) -> list[dict[str, Any]]:
        """Total focus minutes per project label for the given period (default: last 7 days)."""
        try:
            if since_ts is None:
                today = date.today()
                since_ts = (datetime(today.year, today.month, today.day)
                            - timedelta(days=6)).timestamp()
            cur = self._conn.execute(
                """SELECT COALESCE(project, task_title, 'Sonstiges') as label,
                          COALESCE(SUM(duration_minutes), 0) as minutes
                   FROM time_entries
                   WHERE start_time >= ? AND duration_minutes IS NOT NULL
                   GROUP BY label ORDER BY minutes DESC""",
                (since_ts,),
            )
            return [{"label": r[0], "minutes": float(r[1])} for r in cur.fetchall()]
        except Exception as exc:
            print(f"[ProductivityAnalytics] project_time_distribution failed: {exc}")
            return []

    def spoken_deep_work_summary(self, since_ts: float | None = None) -> str:
        blocks = self.deep_work_blocks(since_ts)
        if not blocks:
            return "Noch kein Deep-Work-Block."
        total = sum(b["minutes"] for b in blocks)
        h, m = int(total // 60), int(total % 60)
        label = f"{h}h {m}min" if h else f"{m}min"
        n = len(blocks)
        return f"{n} Deep-Work-Block{'s' if n > 1 else ''}, gesamt {label}."

    def spoken_daily_score(self) -> str:
        try:
            d = self.daily_score()
            score = d["score"]
            done = d["tasks_done"]
            planned = d["tasks_planned"]
            focus = int(d["focus_minutes"])
            return (
                f"Produktivitätsscore heute: {score} von 10. "
                f"Du hast {done} von {planned} Tasks erledigt "
                f"und {focus} Minuten fokussiert gearbeitet."
            )
        except Exception as exc:
            print(f"[ProductivityAnalytics] spoken_daily_score failed: {exc}")
            return "Produktivitätsscore momentan nicht verfügbar."

    # ── Weekly summary ────────────────────────────────────────────────────── #

    def weekly_summary(self) -> str:
        try:
            today = date.today()
            week_start = datetime(today.year, today.month, today.day) - timedelta(days=6)
            week_start_ts = week_start.timestamp()

            done_cur = self._conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE status='done' AND completed_at >= ?""",
                (week_start_ts,),
            )
            total_done = int(done_cur.fetchone()[0])

            focus_cur = self._conn.execute(
                """SELECT COALESCE(SUM(duration_minutes), 0)
                   FROM time_entries
                   WHERE start_time >= ? AND duration_minutes IS NOT NULL""",
                (week_start_ts,),
            )
            total_focus = float(focus_cur.fetchone()[0])
            focus_h = int(total_focus // 60)
            focus_m = int(total_focus % 60)

            # Most productive day (most tasks done)
            day_cur = self._conn.execute(
                """SELECT DATE(completed_at, 'unixepoch', 'localtime') as day,
                          COUNT(*) as cnt
                   FROM tasks
                   WHERE status='done' AND completed_at >= ?
                   GROUP BY day ORDER BY cnt DESC LIMIT 1""",
                (week_start_ts,),
            )
            best_row = day_cur.fetchone()
            best_day_str = ""
            if best_row and best_row["day"]:
                try:
                    d_obj = date.fromisoformat(best_row["day"])
                    _WEEKDAYS = ["Montag", "Dienstag", "Mittwoch",
                                 "Donnerstag", "Freitag", "Samstag", "Sonntag"]
                    best_day_str = (
                        f" Produktivster Tag: {_WEEKDAYS[d_obj.weekday()]} "
                        f"({best_row['cnt']} Tasks)."
                    )
                except Exception:
                    pass

            focus_label = f"{focus_h}h {focus_m}min" if focus_h else f"{focus_m}min"
            return (
                f"Diese Woche: {total_done} Tasks erledigt, "
                f"{focus_label} fokussiert gearbeitet.{best_day_str}"
            )
        except Exception as exc:
            print(f"[ProductivityAnalytics] weekly_summary failed: {exc}")
            return "Wochenübersicht momentan nicht verfügbar."
