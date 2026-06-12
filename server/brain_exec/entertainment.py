"""EntertainmentExecMixin — entertainment tool handler.

Mixed into Brain. All self.* attributes are satisfied by Brain.__init__.
"""
from __future__ import annotations

from typing import Any


class EntertainmentExecMixin:
    """Exec method for mood music, watchlist, games, gaming mode,
    birthdays, and news briefing."""

    def _exec_entertainment(
        self, tool_name: str, inp: dict[str, Any],
    ) -> tuple[str, bool]:
        """Dispatch entertainment tool_use to the EntertainmentManager."""
        try:
            if self._entertainment is None:  # type: ignore[attr-defined]
                from pathlib import Path as _Path
                from ..entertainment.entertainment_manager import EntertainmentManager as _EM
                _db = _Path(__file__).resolve().parents[2] / "data" / "jarvis.db"
                self._entertainment = _EM(  # type: ignore[attr-defined]
                    _db, self.client, self.smarthome)  # type: ignore[attr-defined]

            ent = self._entertainment  # type: ignore[attr-defined]
            inp = inp or {}

            if tool_name == "play_mood_music":
                from ..entertainment.mood_music import play_mood
                return play_mood(inp.get("mood", "entspannt"))

            if tool_name == "manage_watchlist":
                action = inp.get("action", "")
                if action == "add":
                    title = inp.get("title", "")
                    if not title:
                        return "title ist erforderlich.", True
                    return ent.watchlist.add(
                        title,
                        type=inp.get("type", "unknown"),
                    )
                if action == "list":
                    return ent.watchlist.spoken_list()
                if action == "mark_watched":
                    title = inp.get("title", "")
                    if not title:
                        return "title ist erforderlich.", True
                    return ent.watchlist.mark_watched(title, inp.get("rating"))
                if action == "what_to_watch":
                    return ent.watchlist.what_to_watch()
                return f"Unbekannte Watchlist-Aktion: {action}", True

            if tool_name == "play_game":
                action = inp.get("action", "")
                text = inp.get("text", "")
                if action == "joke":
                    return ent.games.word.tell_joke(inp.get("category", "general"))
                if action == "riddle":
                    return ent.games.word.get_riddle()
                if action == "riddle_answer":
                    return ent.games.word.reveal_riddle_answer()
                if action == "fact":
                    return ent.games.word.random_fact(topic=text)
                if action == "story":
                    return ent.games.word.story_starter()
                if action == "trivia_start":
                    return ent.games.trivia.start(
                        category=inp.get("category", "Allgemeinwissen"),
                        difficulty=inp.get("difficulty", "mittel"),
                    )
                if action == "twenty_questions":
                    return ent.games.twenty_q.start_jarvis_thinks()
                if action == "stop_game":
                    active = ent.games.active_game()
                    if active == "trivia":
                        return ent.games.trivia.stop()
                    if active == "twenty_q":
                        return ent.games.twenty_q.stop()
                    return "Kein aktives Spiel.", False
                if action == "answer":
                    result = ent.games.handle_command(text or "")
                    if result is not None:
                        return result
                    return "Kein aktives Spiel für diese Antwort.", False
                return f"Unbekannte Spiel-Aktion: {action}", True

            if tool_name == "manage_gaming_mode":
                action = inp.get("action", "")
                if action == "start":
                    return ent.gaming.start(inp.get("game_name", "Unbekanntes Spiel"))
                if action == "stop":
                    return ent.gaming.stop()
                if action == "stats":
                    return ent.gaming.get_stats()
                return f"Unbekannte Gaming-Aktion: {action}", True

            if tool_name == "get_birthdays":
                from ..entertainment import birthdays as _bd
                return _bd.get_upcoming_birthdays(inp.get("days_ahead", 7))

            if tool_name == "get_news_briefing":
                from ..tools.news import get_headlines
                n = max(1, min(int(inp.get("items", 5)), 10))
                headlines = get_headlines(n=n)
                if not headlines:
                    return "Keine Nachrichten verfügbar.", True
                news_text = "\n".join(
                    f"- {h.title} ({h.source})" for h in headlines
                )
                prompt = (
                    "Fasse diese Nachrichten als gesprochenes Briefing auf Deutsch zusammen, "
                    "natürlicher Stil, keine Aufzählungen:\n\n" + news_text
                )
                msg = self.client.messages.create(  # type: ignore[attr-defined]
                    model="claude-haiku-4-5-20251001",
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                return msg.content[0].text.strip(), False

            return f"Unbekanntes Entertainment-Tool: {tool_name}", True

        except Exception as exc:  # noqa: BLE001
            return f"Entertainment-Fehler: {exc}", True
