"""Central entertainment layer coordinator."""
from __future__ import annotations

from pathlib import Path

from .mood_music import play_mood
from .watchlist import Watchlist
from .games.game_manager import GameManager
from .gaming_mode import GamingMode
from . import birthdays


class EntertainmentManager:
    """Wires mood music, watchlist, games, gaming mode, and birthdays."""

    def __init__(self, db_path: Path, client, smarthome=None) -> None:
        self.watchlist = Watchlist(db_path)
        self.games = GameManager(db_path, client)
        self.gaming = GamingMode(db_path, smarthome)
        self._client = client
        self._smarthome = smarthome

    def start(self) -> None:
        print("[ENTERTAINMENT] ready")

    def stop(self) -> None:
        """Close sub-manager SQLite connections at shutdown (WAL flush).
        GamingMode's smart-home threads are daemons and self-terminate."""
        for sub in (self.watchlist, self.games, self.gaming):
            conn = getattr(sub, "_conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def morning_brief_addon(self) -> str:
        """Return a birthday reminder string for the morning briefing."""
        try:
            bd = birthdays.check_todays_birthdays()
            if bd:
                return f"Heute hat {bd} Geburtstag!"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def process_command(self, command: str) -> tuple[str, bool] | None:  # noqa: PLR0912, PLR0911
        """Route a natural language command to the appropriate sub-system.

        Returns (message, is_error) or None if no match.
        """
        cmd = command.lower().strip()

        # ── Mood music ────────────────────────────────────────────────────
        mood_triggers = (
            "stimmung", "mood", "entspann", "konzentrier", "glücklich",
            "traurig", "sport", "party", "schlaf", "romantisch", "morgen",
            "gaming musik",
        )
        if any(t in cmd for t in mood_triggers):
            return play_mood(command)

        # ── Watchlist management ──────────────────────────────────────────
        if "watchlist hinzufügen" in cmd or "merke film" in cmd or "merke serie" in cmd:
            title = ""
            for trigger in ("watchlist hinzufügen", "merke film", "merke serie"):
                if trigger in cmd:
                    title = command[command.lower().index(trigger) + len(trigger):].strip()
                    break
            media_type = "show" if "serie" in cmd else "movie" if "film" in cmd else "unknown"
            return self.watchlist.add(title, type=media_type)

        if "watchlist" in cmd or "was soll ich schauen" in cmd:
            return self.watchlist.what_to_watch()

        if "als gesehen" in cmd or "gesehen markieren" in cmd:
            title = ""
            for trigger in ("als gesehen", "gesehen markieren"):
                if trigger in cmd:
                    title = command[: command.lower().index(trigger)].strip()
                    break
            if not title:
                title = cmd.replace("als gesehen", "").replace("gesehen markieren", "").strip()
            return self.watchlist.mark_watched(title)

        # ── Gaming mode ───────────────────────────────────────────────────
        if ("gaming" in cmd or "spielen" in cmd or "game mode" in cmd) and (
            "stop" in cmd or "beenden" in cmd
        ):
            return self.gaming.stop()

        if "gaming modus" in cmd or "gaming mode" in cmd or "spiel modus" in cmd:
            game_name = "Unbekanntes Spiel"
            # Try to extract game name from command
            for trigger in ("gaming modus", "gaming mode", "spiel modus"):
                if trigger in cmd:
                    after = command[command.lower().index(trigger) + len(trigger):].strip()
                    if after:
                        game_name = after
                    break
            return self.gaming.start(game_name)

        if "gaming stats" in cmd or "wie lange gespielt" in cmd:
            return self.gaming.get_stats()

        # ── Birthdays ─────────────────────────────────────────────────────
        if "geburtstag" in cmd:
            return birthdays.get_upcoming_birthdays()

        # ── Games fallthrough ─────────────────────────────────────────────
        result = self.games.handle_command(command)
        if result is not None:
            return result

        return None
