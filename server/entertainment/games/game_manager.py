"""Coordinator for all voice games, with score persistence."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .trivia import TriviaGame
from .twenty_questions import TwentyQuestions
from .word_games import WordGames

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS game_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_type TEXT NOT NULL,
    score INTEGER DEFAULT 0,
    max_score INTEGER DEFAULT 0,
    played_at REAL NOT NULL,
    details TEXT DEFAULT ''
)
"""


class GameManager:
    """Routes voice commands to the appropriate game and saves scores."""

    def __init__(self, db_path: Path, client) -> None:
        try:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_SQL)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[GAMES] DB init failed: {exc}")
            self._conn = None  # type: ignore[assignment]

        self.trivia = TriviaGame(client)
        self.twenty_q = TwentyQuestions(client)
        self.word = WordGames(client)

    def active_game(self) -> str | None:
        """Return which game is currently active, if any."""
        if self.trivia.is_active():
            return "trivia"
        if self.twenty_q.is_active():
            return "twenty_q"
        return None

    def handle_command(self, command: str) -> tuple[str, bool] | None:
        """Route a voice command to the appropriate game handler.

        Returns None if the command didn't match any game trigger.
        """
        cmd = command.lower().strip()

        # Stop active game
        if "spiel beenden" in cmd or "stop game" in cmd:
            active = self.active_game()
            if active == "trivia":
                return self.trivia.stop()
            if active == "twenty_q":
                return self.twenty_q.stop()
            return "Kein aktives Spiel.", False

        # Game starters (check before active-game routing)
        if "trivia" in cmd or "quiz" in cmd:
            category = "Allgemeinwissen"
            difficulty = "mittel"
            # Extract category if present
            if "thema" in cmd:
                parts = cmd.split("thema")
                if len(parts) > 1:
                    category = parts[1].strip().split()[0] if parts[1].strip() else category
            return self.trivia.start(category=category, difficulty=difficulty)

        if "20 fragen" in cmd or "zwanzig fragen" in cmd:
            return self.twenty_q.start_jarvis_thinks()

        if "witz" in cmd or "joke" in cmd:
            return self.word.tell_joke()

        if "auflösung" in cmd:
            return self.word.reveal_riddle_answer()

        if "rätsel" in cmd:
            return self.word.get_riddle()

        if "fakt" in cmd or "fact" in cmd:
            topic = ""
            if "über" in cmd:
                parts = cmd.split("über", 1)
                if len(parts) > 1:
                    topic = parts[1].strip()
            return self.word.random_fact(topic=topic)

        if "geschichte" in cmd or "story" in cmd:
            return self.word.story_starter()

        # Active game routing
        active = self.active_game()
        if active == "trivia":
            return self.trivia.answer(command)

        if active == "twenty_q":
            # Guess check
            if any(cmd.startswith(p) for p in ("ist es", "rate", "ist das")):
                return self.twenty_q.make_guess(command)
            # Otherwise treat as a question
            return self.twenty_q.answer_question(command)

        return None

    def save_score(
        self,
        game_type: str,
        score: int,
        max_score: int,
        details: str = "",
    ) -> None:
        """Persist a game result to the database."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO game_scores (game_type, score, max_score, played_at, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (game_type, score, max_score, time.time(), details),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[GAMES] save_score failed: {exc}")
