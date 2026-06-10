"""Flashcards with SM-2 spaced repetition (data/knowledge.db).

Each card carries the SM-2 scheduling state (ease factor, interval,
repetitions, next-review date). Reviewing a card with a quality grade
(0–5) reschedules it: poor recall resets the interval, good recall pushes
it further out, so you review what you're about to forget and skip what
you know cold.

Cards can be authored directly or generated from saved knowledge facts
via Claude. SQLite-backed and thread-safe (the brain worker thread, the
scheduler, and FastAPI all touch it). Best-effort throughout — a failed
write prints and returns a falsy value rather than crashing JARVIS.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings

_CREATE = """
CREATE TABLE IF NOT EXISTS flashcards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    front         TEXT NOT NULL,
    back          TEXT NOT NULL,
    category      TEXT DEFAULT 'general',
    source        TEXT DEFAULT 'manual',
    created_at    REAL NOT NULL,
    ease_factor   REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    repetitions   INTEGER NOT NULL DEFAULT 0,
    next_review   REAL NOT NULL,
    last_reviewed REAL,
    review_count  INTEGER NOT NULL DEFAULT 0
)
"""

_MIN_EF = 1.3  # SM-2 lower bound on the ease factor


class FlashcardManager:
    """SM-2 spaced-repetition flashcard store."""

    def __init__(self, db_path: Path | str = "data/knowledge.db",
                 client: Any = None) -> None:
        self._db_path = Path(db_path)
        self._client = client
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(_CREATE)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_due ON flashcards(next_review)")
            self._conn.commit()
            print(f"[Flashcards] ready at {self._db_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Flashcards] init failed: {exc}")

    # ── low-level ──────────────────────────────────────────────────────── #

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> int | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            print(f"[Flashcards] write failed: {exc}")
            return None

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"[Flashcards] query failed: {exc}")
            return []

    # ── authoring ──────────────────────────────────────────────────────── #

    def add_card(self, front: str, back: str, category: str = "general",
                 source: str = "manual") -> int | None:
        if not front.strip() or not back.strip():
            return None
        now = time.time()
        return self._write(
            """INSERT INTO flashcards
               (front, back, category, source, created_at, next_review)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (front.strip(), back.strip(), category, source, now, now),
        )

    def delete_card(self, card_id: int) -> bool:
        return self._write("DELETE FROM flashcards WHERE id=?", (card_id,)) is not None

    def get_card(self, card_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM flashcards WHERE id=?", (card_id,))
        return rows[0] if rows else None

    def list_cards(self, category: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        if category:
            return self._query(
                "SELECT * FROM flashcards WHERE category=? "
                "ORDER BY created_at DESC LIMIT ?", (category, limit))
        return self._query(
            "SELECT * FROM flashcards ORDER BY created_at DESC LIMIT ?", (limit,))

    # ── review (SM-2) ──────────────────────────────────────────────────── #

    def due_cards(self, limit: int = 20, now: float | None = None) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        return self._query(
            "SELECT * FROM flashcards WHERE next_review <= ? "
            "ORDER BY next_review ASC LIMIT ?", (now, limit))

    def due_count(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        rows = self._query(
            "SELECT COUNT(*) AS n FROM flashcards WHERE next_review <= ?", (now,))
        return rows[0]["n"] if rows else 0

    def review_card(self, card_id: int, quality: int,
                    now: float | None = None) -> dict[str, Any]:
        """Apply an SM-2 review. ``quality`` 0–5 (0=blackout, 5=perfect).
        Returns the new schedule, or {} if the card is gone."""
        card = self.get_card(card_id)
        if card is None:
            return {}
        now = now if now is not None else time.time()
        q = max(0, min(5, int(quality)))

        ef = float(card["ease_factor"])
        reps = int(card["repetitions"])
        interval = float(card["interval_days"])

        if q < 3:
            # Failed recall — relearn from the start (review again tomorrow).
            reps = 0
            interval = 1.0
        else:
            if reps == 0:
                interval = 1.0
            elif reps == 1:
                interval = 6.0
            else:
                interval = round(interval * ef, 2)
            reps += 1
            # SM-2 ease-factor update.
            ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
            ef = max(_MIN_EF, ef)

        next_review = now + interval * 86400
        self._write(
            """UPDATE flashcards
               SET ease_factor=?, interval_days=?, repetitions=?,
                   next_review=?, last_reviewed=?, review_count=review_count+1
               WHERE id=?""",
            (round(ef, 3), interval, reps, next_review, now, card_id),
        )
        return {"card_id": card_id, "interval_days": interval,
                "repetitions": reps, "ease_factor": round(ef, 3),
                "next_review": next_review}

    @staticmethod
    def quality_from_feedback(feedback: str) -> int:
        """Map spoken self-grading to an SM-2 quality. 'falsch'→1, 'schwer'
        →3, 'richtig'/'gut'→4, 'einfach'→5."""
        f = (feedback or "").lower()
        if any(w in f for w in ("falsch", "wrong", "nochmal", "again", "weiß nicht")):
            return 1
        if any(w in f for w in ("schwer", "hard", "knapp")):
            return 3
        if any(w in f for w in ("einfach", "easy", "leicht", "sicher")):
            return 5
        return 4  # default "richtig / good"

    # ── generation from knowledge (Claude) ─────────────────────────────── #

    def generate_from_text(self, text: str, category: str = "learning",
                           max_cards: int = 5) -> list[int]:
        """Turn a chunk of knowledge into Q&A flashcards via Claude."""
        if self._client is None or not text.strip():
            return []
        import json
        prompt = (
            f"Create up to {max_cards} concise flashcards from the text "
            "below. Return ONLY JSON: "
            '{"cards":[{"front":"question","back":"answer"}]}. '
            "Questions and answers in the same language as the text.\n\n"
            + text[:4000])
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=1024,
                messages=[{"role": "user", "content": prompt}])
            raw = ""
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    raw = (b.text or "").strip()
                    break
            data = self._parse_cards(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[Flashcards] generate failed: {exc}")
            return []
        ids: list[int] = []
        for c in data[:max_cards]:
            front, back = c.get("front", ""), c.get("back", "")
            cid = self.add_card(front, back, category=category, source="generated")
            if cid:
                ids.append(cid)
        return ids

    @staticmethod
    def _parse_cards(raw: str) -> list[dict[str, str]]:
        import json
        text = raw.strip()
        if "```" in text:
            for p in text.split("```"):
                p = p.strip()
                if p.startswith("{") or p.startswith("json"):
                    text = p[4:].strip() if p.startswith("json") else p
                    break
        if not text.startswith("{"):
            i, j = text.find("{"), text.rfind("}")
            if i != -1 and j != -1:
                text = text[i:j + 1]
        try:
            data = json.loads(text)
            cards = data.get("cards", []) if isinstance(data, dict) else []
            return [c for c in cards if isinstance(c, dict) and c.get("front")]
        except Exception:  # noqa: BLE001
            return []

    # ── stats / spoken ─────────────────────────────────────────────────── #

    def stats(self) -> dict[str, Any]:
        total = self._query("SELECT COUNT(*) AS n FROM flashcards")
        return {"total": total[0]["n"] if total else 0, "due": self.due_count()}

    def spoken_due(self) -> str:
        n = self.due_count()
        if n == 0:
            return "Keine fälligen Karteikarten. Gut gelernt!"
        return f"Du hast {n} fällige Karteikarte{'n' if n != 1 else ''} zum Wiederholen."

    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._conn = None
