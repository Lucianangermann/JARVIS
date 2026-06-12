"""Proactive behavioral anomaly detection for JARVIS.

Reads existing SQLite tables (jarvis.db) and surfaces patterns that
the user might not notice themselves:
  - Mood gap: no mood log for ≥ 3 days
  - Focus gap: no tracked focus time this calendar week
  - Overdue spike: > 5 tasks past their due date
  - Stale goal: active goal not updated in > 14 days
  - Tool drift: any tool correction rate > 30 % in past 7 days

All checks are purely additive — they never write anything. Each check
silently degrades to "no anomaly" if the relevant table is missing or
the query errors. Designed to run in session_greeting's hot path: no
LLM calls, no ChromaDB.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_SEC = 1.0
_DAY = 86_400.0
_WEEK = 7 * _DAY


class AnomalyDetector:
    """Stateless detector — every call opens its own read connection."""

    # ── individual checks ───────────────────────────────────────────── #

    @staticmethod
    def _check_mood_gap(conn: sqlite3.Connection) -> dict[str, Any] | None:
        try:
            row = conn.execute(
                "SELECT MAX(ts) AS last FROM mood_logs"
            ).fetchone()
            if row and row[0]:
                gap_days = (time.time() - float(row[0])) / _DAY
                if gap_days >= 3:
                    return {
                        "type": "mood_gap",
                        "severity": "medium",
                        "message": (
                            f"Kein Mood-Log seit {int(gap_days)} Tagen."
                        ),
                    }
        except Exception:
            pass
        return None

    @staticmethod
    def _check_focus_gap(conn: sqlite3.Connection) -> dict[str, Any] | None:
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(duration_minutes), 0) AS m "
                "FROM time_entries "
                "WHERE start_time >= strftime('%s', 'now', 'weekday 0', '-7 days')"
            ).fetchone()
            if row and (row[0] or 0) == 0:
                return {
                    "type": "focus_gap",
                    "severity": "low",
                    "message": "Diese Woche noch keine Fokussession gestartet.",
                }
        except Exception:
            pass
        return None

    @staticmethod
    def _check_overdue_spike(conn: sqlite3.Connection) -> dict[str, Any] | None:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE status IN ('todo','in_progress') AND due_date < date('now')"
            ).fetchone()
            n = row[0] if row else 0
            if n > 5:
                return {
                    "type": "overdue_spike",
                    "severity": "high",
                    "message": f"{n} überfällige Tasks aufgelaufen.",
                }
        except Exception:
            pass
        return None

    @staticmethod
    def _check_stale_goal(conn: sqlite3.Connection) -> dict[str, Any] | None:
        try:
            row = conn.execute(
                "SELECT title, (strftime('%s','now') - updated_at) / 86400.0 AS days_stale "
                "FROM goals WHERE status='active' "
                "ORDER BY days_stale DESC LIMIT 1"
            ).fetchone()
            if row and row[1] is not None and float(row[1]) > 14:
                return {
                    "type": "stale_goal",
                    "severity": "medium",
                    "message": (
                        f"Ziel '{row[0][:40]}' seit "
                        f"{int(row[1])} Tagen nicht aktualisiert."
                    ),
                }
        except Exception:
            pass
        return None

    @staticmethod
    def _check_tool_drift(db_path: str) -> dict[str, Any] | None:
        """Check tool_quality table for correction rate spikes."""
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            since = time.time() - 7 * _DAY
            rows = conn.execute(
                "SELECT tool_name, COUNT(*) AS total, "
                "SUM(CASE WHEN corrected=1 THEN 1 ELSE 0 END) AS errors "
                "FROM tool_quality WHERE ts >= ? "
                "GROUP BY tool_name HAVING total >= 3",
                (since,),
            ).fetchall()
            conn.close()
            worst = None
            worst_rate = 0.0
            for r in rows:
                rate = r["errors"] / r["total"]
                if rate > worst_rate:
                    worst_rate = rate
                    worst = r["tool_name"]
            if worst and worst_rate > 0.30:
                pct = int(worst_rate * 100)
                return {
                    "type": "tool_drift",
                    "severity": "medium",
                    "message": (
                        f"Tool '{worst}' hat {pct}% Korrekturrate diese Woche."
                    ),
                }
        except Exception:
            pass
        return None

    # ── public API ───────────────────────────────────────────────────── #

    @classmethod
    def detect(cls, db_path: str | Path) -> list[dict[str, Any]]:
        """Run all checks and return list of anomalies found.

        Never raises — each check degrades silently on error.
        """
        path = str(db_path)
        anomalies: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for check in (
                cls._check_mood_gap,
                cls._check_focus_gap,
                cls._check_overdue_spike,
                cls._check_stale_goal,
            ):
                result = check(conn)
                if result:
                    anomalies.append(result)
            conn.close()
        except Exception:
            pass

        drift = cls._check_tool_drift(path)
        if drift:
            anomalies.append(drift)

        return anomalies

    @classmethod
    def spoken_anomalies(cls, db_path: str | Path) -> str:
        """Return a German one-liner summary of detected anomalies, or ''."""
        anomalies = cls.detect(db_path)
        if not anomalies:
            return ""
        high = [a for a in anomalies if a["severity"] == "high"]
        if high:
            return high[0]["message"]
        return anomalies[0]["message"]
