"""Claude-powered 20 questions game."""
from __future__ import annotations

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 100


class TwentyQuestions:
    """JARVIS thinks of something; user asks yes/no questions to guess it."""

    def __init__(self, client) -> None:
        self._client = client
        self._active = False
        self._secret = ""
        self._questions_asked = 0
        self._max_questions = 20
        self._answers_so_far: list[str] = []

    def _ask(self, prompt: str) -> str:
        msg = self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def start_jarvis_thinks(self) -> tuple[str, bool]:
        """JARVIS picks a secret word and game begins."""
        try:
            prompt = (
                "Denke dir einen bekannten Begriff (Person, Tier, Gegenstand oder Ort) aus. "
                "Antworte NUR mit dem Begriff, ohne Erklärung."
            )
            secret = self._ask(prompt)
            self._secret = secret
            self._active = True
            self._questions_asked = 0
            self._answers_so_far = []
            return "Ich habe mir etwas gedacht! Du hast 20 Fragen. Stelle Ja/Nein-Fragen!", False
        except Exception as exc:  # noqa: BLE001
            return f"Spielstart-Fehler: {exc}", True

    def answer_question(self, question: str) -> tuple[str, bool]:
        """Answer a yes/no question about the secret word."""
        if not self._active:
            return "Kein aktives Spiel.", True
        try:
            self._questions_asked += 1
            prompt = (
                f"Du denkst an: '{self._secret}'. "
                f"Beantworte diese Ja/Nein-Frage: '{question}'. "
                "Antworte NUR mit 'Ja', 'Nein' oder 'Manchmal'."
            )
            answer = self._ask(prompt)
            self._answers_so_far.append(f"Q: {question} A: {answer}")
            remaining = self._max_questions - self._questions_asked
            if remaining <= 0:
                self._active = False
                return f"Keine Fragen mehr! Das Wort war: {self._secret}", False
            return f"{answer}. Noch {remaining} Fragen.", False
        except Exception as exc:  # noqa: BLE001
            return f"Antwort-Fehler: {exc}", True

    def make_guess(self, guess: str) -> tuple[str, bool]:
        """User makes a guess."""
        if not self._active:
            return "Kein aktives Spiel.", True
        guess_clean = guess.lower().strip()
        secret_clean = self._secret.lower().strip()
        if guess_clean in secret_clean or secret_clean in guess_clean:
            self._active = False
            return f"Richtig! Das war '{self._secret}'. Glückwunsch!", False
        remaining = self._max_questions - self._questions_asked
        return f"Nein, das ist falsch. Noch {remaining} Fragen.", False

    def stop(self) -> tuple[str, bool]:
        """End the game and reveal the secret."""
        secret = self._secret or "noch nicht gestartet"
        self._active = False
        self._secret = ""
        self._questions_asked = 0
        self._answers_so_far = []
        return f"Das gesuchte Wort war: {secret}.", False

    def is_active(self) -> bool:
        return self._active
