"""Central coordinator for the JARVIS productivity layer."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .task_manager import TaskManager
from .focus_manager import FocusManager
from .analytics import ProductivityAnalytics
from .meeting_assistant import MeetingAssistant


class ProductivityManager:
    """Ties TaskManager, FocusManager, ProductivityAnalytics, and the
    MeetingAssistant together."""

    def __init__(self, db_path: Path | str, client: Any = None) -> None:
        self._db_path = Path(db_path)
        self.tasks = TaskManager(self._db_path)
        self.focus = FocusManager(self._db_path)
        self.analytics = ProductivityAnalytics(self._db_path)
        # Meeting assistant reuses the task manager (action items → tasks)
        # and the brain's Claude client (summarisation). client may be None
        # when lazily constructed; the brain sets it on the meeting object.
        self.meeting = MeetingAssistant(task_manager=self.tasks, client=client)

    def start(self) -> None:
        print("[PRODUCTIVITY] ready")

    def stop(self) -> None:
        """Close the sub-managers' SQLite connections (WAL flush) at
        shutdown. The pomodoro thread is a daemon and self-terminates."""
        try:
            if getattr(self.focus, "is_running", lambda: False)():
                self.focus.stop_pomodoro()
        except Exception:  # noqa: BLE001
            pass
        for sub in (self.tasks, self.focus, self.analytics):
            conn = getattr(sub, "_conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── Briefing addons ───────────────────────────────────────────────────── #

    def morning_brief_addon(self) -> str:
        try:
            top = self.tasks.get_top3()
            today_all = self.tasks.get_today_tasks()
            total_open = len(today_all)
            if not top:
                return f"Offene Tasks gesamt: {total_open}."
            parts = [f"{i+1}. {t['title']}" for i, t in enumerate(top)]
            return (
                "Deine Top 3 Tasks: " + ", ".join(parts) + ". "
                f"Offene Tasks gesamt: {total_open}."
            )
        except Exception as exc:
            print(f"[ProductivityManager] morning_brief_addon failed: {exc}")
            return ""

    def evening_brief_addon(self) -> str:
        try:
            score_text = self.analytics.spoken_daily_score()
            # Tomorrow's tasks: anything with no due date or future due dates
            # We just report the total still-open count as a forward look.
            overdue = self.tasks.get_overdue()
            overdue_str = (
                f" {len(overdue)} überfällige Tasks." if overdue else ""
            )
            return f"{score_text}{overdue_str}"
        except Exception as exc:
            print(f"[ProductivityManager] evening_brief_addon failed: {exc}")
            return ""

    # ── Natural-language command routing ──────────────────────────────────── #

    def process_command(self, command: str) -> str | None:
        """Route productivity natural-language commands.

        Returns None if the command is not matched, so the caller can
        fall through to Claude.
        """
        try:
            c = command.lower().strip()

            if any(k in c for k in ("top 3", "top drei", "wichtigste aufgaben")):
                return self.tasks.spoken_top3()

            if any(k in c for k in ("pomodoro starten", "starte pomodoro",
                                     "pomodoro", "fokus starten",
                                     "starte fokus", "fokus")):
                return self.focus.start_pomodoro()

            if any(k in c for k in ("timer stopp", "stopp timer",
                                     "timer stop", "stop timer",
                                     "pomodoro stop", "stopp pomodoro")):
                if self.focus.is_running():
                    return self.focus.stop_pomodoro()
                return self.focus.stop_timer()

            if any(k in c for k in ("score", "produktivität heute",
                                     "wie produktiv", "mein score")):
                return self.analytics.spoken_daily_score()

            if any(k in c for k in ("zeit heute", "wie viel zeit",
                                     "wie lange", "zeiterfassung")):
                return self.focus.get_time_today()

            if any(k in c for k in ("überfällig", "overdue",
                                     "was ist überfällig", "fällige tasks")):
                overdue = self.tasks.get_overdue()
                if not overdue:
                    return "Keine überfälligen Tasks."
                parts = [t["title"] for t in overdue[:5]]
                suffix = f" (und {len(overdue)-5} weitere)" if len(overdue) > 5 else ""
                return "Überfällige Tasks: " + ", ".join(parts) + suffix + "."

            return None
        except Exception as exc:
            print(f"[ProductivityManager] process_command failed: {exc}")
            return None
