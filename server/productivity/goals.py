"""Long-term goal tracker — extracted from conversations or manually set.

Tracks goals with deadlines, progress percentages, and milestone
check-ins. Completely separate from the task system: tasks are
daily work items, goals are multi-week/month outcomes they contribute to.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    title                TEXT    NOT NULL,
    description          TEXT    DEFAULT '',
    deadline             TEXT    DEFAULT NULL,   -- YYYY-MM-DD
    status               TEXT    DEFAULT 'active',
    progress_pct         INTEGER DEFAULT 0,
    created_at           REAL    NOT NULL,
    updated_at           REAL    NOT NULL,
    achieved_at          REAL    DEFAULT NULL,
    next_review_at       REAL    DEFAULT NULL,
    review_interval_days INTEGER DEFAULT 3
);
CREATE INDEX IF NOT EXISTS ix_goals_status ON goals(status);

CREATE TABLE IF NOT EXISTS goal_checkpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      INTEGER NOT NULL REFERENCES goals(id),
    ts           REAL    NOT NULL,
    note         TEXT    DEFAULT '',
    progress_pct INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gcp_goal ON goal_checkpoints(goal_id);
"""

_EXTRACT_PROMPT = """\
Der Nutzer hat folgendes gesagt:
"{text}"

Steckt darin ein langfristiges persönliches Ziel (Prüfung, Sport, Projekt, Gewicht, Habit)?
Falls ja, antworte NUR mit diesem JSON (kein Markdown):
{{"title": "<kurzer Ziel-Titel, max 60 Zeichen>", "deadline": "<YYYY-MM-DD oder null>"}}
Falls nein: KEIN_ZIEL"""


class GoalDB:
    """Long-term goal store backed by jarvis.db."""

    # SR review intervals (days): 3 → 7 → 14 → 30 → 30 (cap)
    _SR_INTERVALS = [3, 7, 14, 30]

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self.available = False
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            # Migrate: add SR columns if they don't exist yet.
            for col, defn in [
                ("next_review_at", "REAL DEFAULT NULL"),
                ("review_interval_days", "INTEGER DEFAULT 3"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE goals ADD COLUMN {col} {defn}")
                except Exception:
                    pass  # column already exists
            self._conn.commit()
            self.available = True
        except Exception as exc:
            print(f"[Goals] init failed: {exc}")

    # ── write ─────────────────────────────────────────────────────────── #

    def add(self, title: str, description: str = "",
            deadline: str | None = None) -> int | None:
        title = title.strip()[:100]
        if not title:
            return None
        now = time.time()
        try:
            cur = self._conn.execute(
                "INSERT INTO goals (title, description, deadline, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, description.strip()[:300], deadline, now, now),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[Goals] add failed: {exc}")
            return None

    def update_progress(self, goal_id: int, pct: int,
                        note: str = "") -> bool:
        pct = max(0, min(100, pct))
        now = time.time()
        try:
            self._conn.execute(
                "UPDATE goals SET progress_pct=?, updated_at=? WHERE id=? AND status='active'",
                (pct, now, goal_id),
            )
            self._conn.execute(
                "INSERT INTO goal_checkpoints (goal_id, ts, note, progress_pct) "
                "VALUES (?, ?, ?, ?)",
                (goal_id, now, note.strip()[:200], pct),
            )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[Goals] update_progress failed: {exc}")
            return False

    def achieve(self, goal_id: int) -> bool:
        now = time.time()
        try:
            self._conn.execute(
                "UPDATE goals SET status='achieved', progress_pct=100, "
                "achieved_at=?, updated_at=? WHERE id=?",
                (now, now, goal_id),
            )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[Goals] achieve failed: {exc}")
            return False

    def abandon(self, goal_id: int) -> bool:
        try:
            self._conn.execute(
                "UPDATE goals SET status='abandoned', updated_at=? WHERE id=?",
                (time.time(), goal_id),
            )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[Goals] abandon failed: {exc}")
            return False

    # ── read ──────────────────────────────────────────────────────────── #

    def get_active(self) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE status='active' "
                "ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline, id"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[Goals] get_active failed: {exc}")
            return []

    def get_all(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            return []

    def checkpoints(self, goal_id: int, limit: int = 5) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM goal_checkpoints WHERE goal_id=? "
                "ORDER BY ts DESC LIMIT ?",
                (goal_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── spoken output ─────────────────────────────────────────────────── #

    def spoken_status(self) -> str:
        goals = self.get_active()
        if not goals:
            return "Keine aktiven Langzeit-Ziele gesetzt."
        today = dt.date.today()
        lines = []
        for g in goals:
            line = f"• {g['title']} — {g['progress_pct']}%"
            if g.get("deadline"):
                try:
                    dl = dt.date.fromisoformat(g["deadline"])
                    days_left = (dl - today).days
                    if days_left < 0:
                        line += f" (ÜBERFÄLLIG um {-days_left}d)"
                    elif days_left == 0:
                        line += " (Deadline heute!)"
                    else:
                        line += f" (noch {days_left}d)"
                except ValueError:
                    line += f" (bis {g['deadline']})"
            ts = self.link_summary(g["id"])
            if ts["linked"] > 0:
                line += f" [{ts['done']}/{ts['linked']} Tasks erledigt]"
            lines.append(line)
        n = len(goals)
        return f"{n} aktive{'s' if n == 1 else ''} Ziel{'e' if n != 1 else ''}:\n" + "\n".join(lines)

    def weekly_spoken(self) -> str:
        """Short version for the weekly recap."""
        goals = self.get_active()
        if not goals:
            return ""
        near = [g for g in goals if g.get("deadline") and
                0 <= (dt.date.fromisoformat(g["deadline"]) - dt.date.today()).days <= 14
                if g.get("deadline")]
        if near:
            g = near[0]
            days = (dt.date.fromisoformat(g["deadline"]) - dt.date.today()).days
            return (f"Ziel '{g['title']}': {g['progress_pct']}% — "
                    f"noch {days} Tag{'e' if days != 1 else ''}.")
        top = goals[0]
        return f"Ziel '{top['title']}': {top['progress_pct']}% erreicht."

    # ── spaced-repetition review ──────────────────────────────────────── #

    def due_for_review(self) -> list[dict]:
        """Return active goals whose next_review_at is in the past (or null)."""
        now = time.time()
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE status='active' "
                "AND (next_review_at IS NULL OR next_review_at <= ?) "
                "ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline",
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def record_review(self, goal_id: int, pct_update: int | None = None) -> bool:
        """Mark a goal as reviewed and advance its SR interval.

        Optionally also updates progress_pct. Returns True on success."""
        now = time.time()
        try:
            row = self._conn.execute(
                "SELECT review_interval_days FROM goals WHERE id=? AND status='active'",
                (goal_id,),
            ).fetchone()
            if not row:
                return False
            current_interval = int(row[0] or 3)
            # Advance to next interval (cap at 30 days)
            intervals = self._SR_INTERVALS
            try:
                idx = intervals.index(current_interval)
                next_interval = intervals[min(idx + 1, len(intervals) - 1)]
            except ValueError:
                next_interval = min(current_interval * 2, 30)
            next_review = now + next_interval * 86400
            if pct_update is not None:
                pct_update = max(0, min(100, pct_update))
                self._conn.execute(
                    "UPDATE goals SET next_review_at=?, review_interval_days=?, "
                    "progress_pct=?, updated_at=? WHERE id=?",
                    (next_review, next_interval, pct_update, now, goal_id),
                )
            else:
                self._conn.execute(
                    "UPDATE goals SET next_review_at=?, review_interval_days=?, "
                    "updated_at=? WHERE id=?",
                    (next_review, next_interval, now, goal_id),
                )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[Goals] record_review failed: {exc}")
            return False

    def review_summary(self) -> str:
        """German one-liner: goals due for review right now."""
        due = self.due_for_review()
        if not due:
            return ""
        if len(due) == 1:
            return f"Ziel '{due[0]['title'][:40]}' wartet auf dein Update."
        return f"{len(due)} Ziele warten auf ein Fortschritts-Update."

    # ── goal-task linkage ─────────────────────────────────────────────── #

    def auto_link_task(self, task_title: str) -> int | None:
        """Return goal_id of the best-matching active goal for a task title.

        Uses word-Jaccard overlap. Returns None if no goal scores > 0.15
        or if there are no active goals. Never raises.
        """
        if not self.available or not task_title.strip():
            return None
        goals = self.get_active()
        if not goals:
            return None
        task_words = {w.lower() for w in task_title.split() if len(w) > 2}
        best_id: int | None = None
        best_score = 0.15  # minimum threshold
        for g in goals:
            goal_words = {w.lower() for w in g["title"].split() if len(w) > 2}
            if not goal_words:
                continue
            union = task_words | goal_words
            if not union:
                continue
            score = len(task_words & goal_words) / len(union)
            if score > best_score:
                best_score = score
                best_id = g["id"]
        return best_id

    def link_summary(self, goal_id: int) -> dict:
        """Return {linked, done, pct} for tasks linked to this goal.

        Queries the tasks table in the same jarvis.db connection.
        pct is None when no tasks are linked yet.
        """
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
                "FROM tasks WHERE goal_id=?",
                (goal_id,),
            ).fetchone()
            total = int(row[0] or 0)
            done = int(row[1] or 0)
            pct = int(done / total * 100) if total > 0 else None
            return {"linked": total, "done": done, "pct": pct}
        except Exception:
            return {"linked": 0, "done": 0, "pct": None}

    def update_progress_from_tasks(self, goal_id: int) -> bool:
        """Auto-compute progress from linked tasks and update the goal."""
        summary = self.link_summary(goal_id)
        if summary["linked"] == 0 or summary["pct"] is None:
            return False
        return self.update_progress(goal_id, summary["pct"],
                                    note="auto-calculated from linked tasks")

    # ── auto-extraction ───────────────────────────────────────────────── #

    @staticmethod
    def _has_goal_signal(text: str) -> bool:
        lower = text.lower()
        signals = ["ich will", "mein ziel", "bis zum", "bis ende", "prüfung am",
                   "prüfung in", "abnehmen", "zunehmen", "trainieren", "lernen bis",
                   "fertig bis", "abschließen bis", "schaffen bis"]
        return any(s in lower for s in signals)

    def maybe_extract_goal(self, user_text: str, client: Any) -> str | None:
        """If the user text contains a goal signal, extract and save it.

        Returns the goal title if a new goal was stored, else None."""
        if not self.available or not self._has_goal_signal(user_text):
            return None
        if client is None or len(user_text) > 300:
            return None
        import json
        prompt = _EXTRACT_PROMPT.format(text=user_text[:280].replace('"', "'"))
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    raw = (block.text or "").strip()
                    if raw == "KEIN_ZIEL":
                        return None
                    data = json.loads(raw)
                    title = str(data.get("title", "")).strip()[:100]
                    deadline = data.get("deadline") or None
                    if title:
                        gid = self.add(title, deadline=deadline)
                        if gid:
                            print(f"[Goals] auto-extracted goal #{gid}: {title}")
                            return title
        except Exception as exc:
            print(f"[Goals] maybe_extract_goal failed: {exc}")
        return None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
