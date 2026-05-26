"""Claude-powered trivia game."""
from __future__ import annotations

import json

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1024


class TriviaGame:
    """Voice trivia game — Claude generates questions and judges answers."""

    def __init__(self, client) -> None:
        self._client = client
        self._active = False
        self._score = 0
        self._total = 0
        self._questions: list[dict] = []
        self._current_idx = 0
        self._current_answer = ""

    def _ask(self, prompt: str, max_tokens: int = _MAX_TOKENS) -> str:
        msg = self._client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def start(
        self,
        category: str = "Allgemeinwissen",
        difficulty: str = "mittel",
        count: int = 5,
    ) -> tuple[str, bool]:
        """Start a new trivia game."""
        if self._active:
            return "Spiel läuft bereits.", True
        try:
            prompt = (
                f"Erstelle {count} Trivia-Fragen auf Deutsch zum Thema {category}, "
                f"Schwierigkeit: {difficulty}. "
                'Antworte NUR als JSON-Array: [{"frage": "...", "antwort": "...", "erklaerung": "..."}]'
            )
            raw = self._ask(prompt)
            # Strip markdown code fences if present
            if raw.startswith("```"):
                lines = raw.splitlines()
                raw = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()
            self._questions = json.loads(raw)
            if not self._questions:
                return "Keine Fragen generiert.", True
            self._active = True
            self._score = 0
            self._total = len(self._questions)
            self._current_idx = 0
            self._current_answer = self._questions[0].get("antwort", "")
            first_q = self._questions[0].get("frage", "?")
            return (
                f"Trivia gestartet! Frage 1 von {self._total}: {first_q}",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Trivia-Fehler: {exc}", True

    def answer(self, user_answer: str) -> tuple[str, bool]:
        """Judge the user's answer for the current question."""
        if not self._active:
            return "Kein aktives Spiel.", True
        try:
            question = self._questions[self._current_idx].get("frage", "?")
            expected = self._current_answer
            prompt = (
                f"Ist '{user_answer}' eine korrekte Antwort auf '{question}'? "
                f"Die erwartete Antwort ist '{expected}'. "
                "Antworte NUR mit 'JA' oder 'NEIN'."
            )
            verdict = self._ask(prompt, max_tokens=10).upper().strip()
            correct = "JA" in verdict

            if correct:
                self._score += 1
                feedback = "Richtig!"
            else:
                feedback = f"Falsch. Die Antwort ist: {expected}"

            self._current_idx += 1

            if self._current_idx < self._total:
                next_q = self._questions[self._current_idx].get("frage", "?")
                self._current_answer = self._questions[self._current_idx].get("antwort", "")
                n = self._current_idx + 1
                return f"{feedback} Frage {n}: {next_q}", False
            else:
                self._active = False
                return (
                    f"{feedback} Spiel beendet! Du hast {self._score} von "
                    f"{self._total} Fragen richtig beantwortet.",
                    False,
                )
        except Exception as exc:  # noqa: BLE001
            return f"Antwort-Fehler: {exc}", True

    def stop(self) -> tuple[str, bool]:
        """End the current trivia game."""
        self._active = False
        self._questions = []
        self._current_idx = 0
        self._score = 0
        self._total = 0
        self._current_answer = ""
        return "Trivia beendet.", False

    def is_active(self) -> bool:
        return self._active
