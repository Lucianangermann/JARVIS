"""Automatic journal — aggregates daily signals from existing DB tables.

No new table needed: reads tasks, time_entries, mood_logs, and
feedback_signals which already live in jarvis.db.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any


class JournalDB:
    """Aggregates daily / weekly metrics from jarvis.db into readable summaries."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self.available = False
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self.available = True
        except Exception as exc:
            print(f"[Journal] init failed: {exc}")

    # ── day-level entry ─────────────────────────────────────────────── #

    def today_entry(self) -> dict[str, Any]:
        """Compile today's metrics from all available tables."""
        today = dt.date.today()
        start_ts = dt.datetime(today.year, today.month, today.day).timestamp()
        end_ts = start_ts + 86400

        entry: dict[str, Any] = {
            "date": today.isoformat(),
            "tasks_done": 0,
            "tasks_added": 0,
            "focus_minutes": 0,
            "mood_score": None,
            "mood_note": "",
            "corrections": 0,
            "positive_signals": 0,
        }
        if not self.available:
            return entry

        try:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status='done' "
                "AND completed_at >= ? AND completed_at < ?",
                (start_ts, end_ts),
            ).fetchone()
            entry["tasks_done"] = r["n"] if r else 0

            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ? AND created_at < ?",
                (start_ts, end_ts),
            ).fetchone()
            entry["tasks_added"] = r["n"] if r else 0

            r = self._conn.execute(
                "SELECT COALESCE(SUM(duration_minutes), 0) AS m FROM time_entries "
                "WHERE start_time >= ? AND start_time < ?",
                (start_ts, end_ts),
            ).fetchone()
            entry["focus_minutes"] = int(r["m"] if r else 0)
        except Exception:
            pass

        try:
            r = self._conn.execute(
                "SELECT score, note FROM mood_logs "
                "WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (start_ts, end_ts),
            ).fetchone()
            if r:
                entry["mood_score"] = r["score"]
                entry["mood_note"] = r["note"] or ""
        except Exception:
            pass

        try:
            for row in self._conn.execute(
                "SELECT signal_type, COUNT(*) AS n FROM feedback_signals "
                "WHERE ts >= ? AND ts < ? GROUP BY signal_type",
                (start_ts, end_ts),
            ).fetchall():
                if row["signal_type"] == "correction":
                    entry["corrections"] = row["n"]
                elif row["signal_type"] == "positive":
                    entry["positive_signals"] = row["n"]
        except Exception:
            pass

        return entry

    def week_entries(self, days: int = 7) -> list[dict[str, Any]]:
        """One compact entry per day for the last N days (newest first)."""
        entries = []
        today = dt.date.today()
        for i in range(days):
            day = today - dt.timedelta(days=i)
            start_ts = dt.datetime(day.year, day.month, day.day).timestamp()
            end_ts = start_ts + 86400
            e: dict[str, Any] = {
                "date": day.isoformat(),
                "tasks_done": 0,
                "focus_minutes": 0,
                "mood_score": None,
            }
            if self.available:
                try:
                    r = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM tasks WHERE status='done' "
                        "AND completed_at >= ? AND completed_at < ?",
                        (start_ts, end_ts),
                    ).fetchone()
                    e["tasks_done"] = r["n"] if r else 0

                    r = self._conn.execute(
                        "SELECT COALESCE(SUM(duration_minutes),0) AS m FROM time_entries "
                        "WHERE start_time >= ? AND start_time < ?",
                        (start_ts, end_ts),
                    ).fetchone()
                    e["focus_minutes"] = int(r["m"] if r else 0)

                    r = self._conn.execute(
                        "SELECT score FROM mood_logs WHERE ts >= ? AND ts < ? "
                        "ORDER BY ts DESC LIMIT 1",
                        (start_ts, end_ts),
                    ).fetchone()
                    e["mood_score"] = r["score"] if r else None
                except Exception:
                    pass
            entries.append(e)
        return entries

    # ── spoken output ─────────────────────────────────────────────────── #

    def spoken_today(self) -> str:
        e = self.today_entry()
        parts = []
        if e["tasks_done"]:
            s = "s" if e["tasks_done"] != 1 else ""
            parts.append(f"{e['tasks_done']} Task{s} erledigt")
        if e["focus_minutes"]:
            h, m = e["focus_minutes"] // 60, e["focus_minutes"] % 60
            lbl = f"{h}h {m}min" if h else f"{m}min"
            parts.append(f"{lbl} fokussiert")
        if e["mood_score"] is not None:
            parts.append(f"Stimmung {e['mood_score']}/10")
        if not parts:
            return "Heute noch keine Aktivitäten erfasst."
        return "Heute: " + ", ".join(parts) + "."

    def spoken_week(self) -> str:
        entries = self.week_entries(7)
        total_tasks = sum(e["tasks_done"] for e in entries)
        total_focus = sum(e["focus_minutes"] for e in entries)
        moods = [e["mood_score"] for e in entries if e["mood_score"] is not None]
        avg_mood = sum(moods) / len(moods) if moods else None

        parts = []
        if total_tasks:
            parts.append(f"{total_tasks} Tasks abgeschlossen")
        if total_focus:
            h = total_focus // 60
            parts.append(f"{h}h Fokuszeit")
        if avg_mood is not None:
            parts.append(f"Ø Stimmung {avg_mood:.1f}/10")
        if not parts:
            return "Diese Woche noch keine Aktivitäten erfasst."
        return "Diese Woche: " + ", ".join(parts) + "."

    def insights(self, *, client: Any = None) -> str:
        """AI-generated insights from this week's data, falling back to spoken_week."""
        entries = self.week_entries(7)

        if client is None:
            return self.spoken_week()

        data_lines = []
        for e in reversed(entries):
            line = f"{e['date']}: {e['tasks_done']} Tasks, {e['focus_minutes']}min Fokus"
            if e["mood_score"] is not None:
                line += f", Stimmung {e['mood_score']}/10"
            data_lines.append(line)

        prompt = (
            "Analysiere diese Wochendaten von JARVIS und gib 2-3 kurze, "
            "konkrete Einblicke auf Deutsch. Was lief gut? Was kann verbessert werden? "
            "Kein Markdown, nur Fließtext.\n\n"
            + "\n".join(data_lines)
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text" and block.text:
                    return block.text.strip()
        except Exception as exc:
            print(f"[Journal] insights LLM failed: {exc}")
        return self.spoken_week()

    def today_mood(self) -> int | None:
        """Return today's most recent mood score, or None."""
        e = self.today_entry()
        return e.get("mood_score")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
