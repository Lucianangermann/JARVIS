"""AppleAppsExecMixin — macOS & Apple app tool handlers.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from typing import Any


class AppleAppsExecMixin:
    """Exec methods for macOS app control, Reminders, Music, Notes,
    and Calendar."""

    def _exec_macos_app(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.app_permissions import is_approved, approve_app, revoke_app, APP_ALIASES
        from ..tools.macos_apps import open_app, close_app, list_running
        inp = tool_input or {}
        action = inp.get("action", "")
        app = inp.get("app_name", "")
        app = APP_ALIASES.get(app.lower(), app)
        if action == "list_running":
            apps = list_running()
            return ", ".join(apps) if apps else "Keine Apps im Vordergrund.", False
        if action == "approve":
            if not app:
                return "app_name ist erforderlich.", True
            pw = inp.get("password", "")
            ok, msg = approve_app(app, pw)
            return msg, not ok
        if action == "revoke":
            if not app:
                return "app_name ist erforderlich.", True
            return revoke_app(app), False
        if not app:
            return "app_name ist erforderlich.", True
        if not is_approved(app):
            return (
                f"'{app}' ist nicht freigegeben. Bitte bestätige mit deinem JARVIS-App-Passwort. "
                f"Rufe dann macos_app mit action='approve', app_name='{app}' und dem Passwort auf.",
                True,
            )
        if action == "open":
            return open_app(app)
        if action == "close":
            return close_app(app)
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_reminders(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.reminders_tool import (
            list_reminders, create_reminder, complete_reminder, list_reminder_lists,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list":
            return list_reminders(inp.get("list_name"))
        if action == "list_lists":
            return list_reminder_lists()
        if action == "create":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return create_reminder(title, inp.get("list_name"), inp.get("due_date"))
        if action == "complete":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return complete_reminder(title, inp.get("list_name"))
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_music(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.music_tool import (
            play, pause, next_track, previous_track, current_track,
            set_volume, play_by_name, toggle_shuffle, player_state,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "play":
            return play()
        if action == "pause":
            return pause()
        if action == "next":
            return next_track()
        if action == "previous":
            return previous_track()
        if action == "current":
            return current_track()
        if action == "state":
            return player_state()
        if action == "volume":
            level = inp.get("level")
            if level is None:
                return "level (0–100) ist erforderlich.", True
            return set_volume(int(level))
        if action == "play_by_name":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return play_by_name(query)
        if action == "shuffle_on":
            return toggle_shuffle(True)
        if action == "shuffle_off":
            return toggle_shuffle(False)
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_notes(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from ..tools.notes_tool import (
            list_notes, read_note, create_note, search_notes, append_to_note,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list":
            return list_notes(inp.get("folder"))
        if action == "read":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return read_note(title)
        if action == "create":
            title = inp.get("title", "")
            content = inp.get("content", "")
            if not title:
                return "title ist erforderlich.", True
            return create_note(title, content, inp.get("folder"))
        if action == "search":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return search_notes(query)
        if action == "append":
            title = inp.get("title", "")
            content = inp.get("content", "")
            if not title or not content:
                return "title und content sind erforderlich.", True
            return append_to_note(title, content)
        return f"Unbekannte Aktion: {action}", True

    def _exec_get_calendar(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Read calendar events — no confirmation, no password, purely local."""
        from ..tools.calendar_tool import get_today_events, get_next_event, get_events
        from datetime import datetime, timedelta
        inp = tool_input or {}
        action = inp.get("action", "today")
        try:
            if action == "today":
                events = get_today_events()
            elif action == "next":
                ev = get_next_event()
                events = [ev] if ev else []
            elif action == "range":
                from ..tools.calendar_tool import _LOCAL_TZ
                df = inp.get("date_from", "")
                dt = inp.get("date_to", "")
                if not df:
                    return "date_from ist erforderlich für range.", True
                start = datetime.fromisoformat(df).replace(tzinfo=_LOCAL_TZ)
                end   = (datetime.fromisoformat(dt).replace(tzinfo=_LOCAL_TZ)
                         if dt else start + timedelta(days=7))
                events = get_events(start, end)
            else:
                return f"Unbekannte Aktion: {action}", True
        except Exception as exc:  # noqa: BLE001
            return f"Kalender-Zugriff fehlgeschlagen: {exc}", True
        if not events:
            return "Keine Termine gefunden.", False
        lines = []
        for ev in events:
            start_str = ev.start.strftime("%A %d.%m. %H:%M")
            end_str   = ev.end.strftime("%H:%M")
            loc = f" ({ev.location})" if ev.location else ""
            lines.append(f"{start_str}–{end_str}: {ev.title}{loc}")
        return "\n".join(lines), False
