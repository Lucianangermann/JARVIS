"""Claude-powered word games: jokes, riddles, facts, story starters."""
from __future__ import annotations

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 300


class WordGames:
    """Voice-friendly word games powered by Claude."""

    def __init__(self, client) -> None:
        self._client = client
        self._last_riddle_answer: str = ""

    def _ask(self, prompt: str) -> str:
        """Call Claude and return the text response."""
        msg = self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def tell_joke(self, category: str = "general") -> tuple[str, bool]:
        """Ask Claude for a short German joke."""
        try:
            prompt = (
                f"Erzähle mir einen kurzen, lustigen Witz auf Deutsch. "
                f"Kategorie: {category}. "
                f"Antworte NUR mit dem Witz, kein Kommentar davor oder danach."
            )
            joke = self._ask(prompt)
            return joke, False
        except Exception as exc:  # noqa: BLE001
            return f"Witz-Fehler: {exc}", True

    def get_riddle(self) -> tuple[str, bool]:
        """Ask Claude for a German riddle and store the answer."""
        try:
            prompt = (
                "Erstelle ein kurzes Rätsel auf Deutsch. "
                "Format: 'Rätsel: [frage]\\nAuflösung: [antwort]'. "
                "Antworte NUR im angegebenen Format."
            )
            raw = self._ask(prompt)
            # Parse question and answer
            question = ""
            answer = ""
            for line in raw.splitlines():
                if line.lower().startswith("rätsel:"):
                    question = line[len("rätsel:"):].strip()
                elif line.lower().startswith("auflösung:"):
                    answer = line[len("auflösung:"):].strip()
            if not question:
                question = raw
            self._last_riddle_answer = answer
            return (
                f"Rätsel: {question} ... Sag 'Auflösung' für die Antwort.",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Rätsel-Fehler: {exc}", True

    def reveal_riddle_answer(self) -> tuple[str, bool]:
        """Reveal the answer to the last riddle."""
        if not self._last_riddle_answer:
            return "Kein aktives Rätsel.", True
        return f"Die Auflösung: {self._last_riddle_answer}", False

    def random_fact(self, topic: str = "") -> tuple[str, bool]:
        """Get a surprising fact from Claude."""
        try:
            topic_part = f" über {topic}" if topic else ""
            prompt = (
                f"Nenne mir einen überraschenden, interessanten Fakt"
                f"{topic_part}. Auf Deutsch, maximal 2 Sätze."
            )
            fact = self._ask(prompt)
            return fact, False
        except Exception as exc:  # noqa: BLE001
            return f"Fakt-Fehler: {exc}", True

    def story_starter(self) -> tuple[str, bool]:
        """Get a cliffhanger story opener from Claude."""
        try:
            prompt = (
                "Beginne eine kurze, spannende Geschichte auf Deutsch "
                "mit genau 2-3 Sätzen. Höre mittendrin auf um Spannung zu erzeugen."
            )
            story = self._ask(prompt)
            return story, False
        except Exception as exc:  # noqa: BLE001
            return f"Geschichte-Fehler: {exc}", True
