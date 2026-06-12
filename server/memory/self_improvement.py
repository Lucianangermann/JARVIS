"""Self-improvement engine — JARVIS learns behavioral rules from corrections.

Flow
----
1. After every turn, ``maybe_learn()`` checks whether the user's message
   looks like a correction of the previous JARVIS reply.
2. If a correction signal is found, Claude Haiku decides:
   - Is this really a correction?
   - If so: extract a concise, generalised German rule sentence.
3. The lesson is stored in ``learned_lessons`` (SQLite, jarvis.db).
4. ``ContextBuilder`` injects active lessons into the system prompt as
   ``## Learned Behaviors`` so they immediately affect future replies.

Security note: raw user text NEVER lands in the system prompt — Haiku
always reformulates it into a rule sentence, which acts as a sanitisation
layer against prompt-injection via "corrections".
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


_CORRECTION_KEYWORDS = frozenset([
    "nein", "falsch", "stimmt nicht", "nicht richtig", "falsch verstanden",
    "ich meinte", "eigentlich", "nicht so gemeint", "das war nicht",
    "du hast", "hör auf", "lass das", "nicht so", "besser wäre",
    "das wollte ich nicht", "hast du falsch", "verstanden",
])

_POSITIVE_KEYWORDS = frozenset([
    "genau", "perfekt", "super", "richtig", "stimmt", "exakt", "top",
    "gut gemacht", "ja genau", "korrekt", "sehr gut", "danke",
])

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_lessons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    lesson     TEXT    NOT NULL,
    source     TEXT    DEFAULT 'correction',
    confidence REAL    DEFAULT 0.8,
    active     INTEGER DEFAULT 1,
    session_id TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ll_ts     ON learned_lessons (ts);
CREATE INDEX IF NOT EXISTS ix_ll_active ON learned_lessons (active);

CREATE TABLE IF NOT EXISTS feedback_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    session_id  TEXT    DEFAULT '',
    signal_type TEXT    NOT NULL,
    user_text   TEXT    DEFAULT '',
    jarvis_text TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fs_ts ON feedback_signals (ts);
"""

_EXTRACT_PROMPT = """\
JARVIS hat folgendes gesagt:
"{jarvis}"

Der Nutzer hat danach folgendes gesagt:
"{user}"

Ist das eine Korrektur oder ein Hinweis, dass JARVIS etwas falsch gemacht hat?
Falls ja: Schreib EINE kurze Verhaltensregel (1 Satz, Deutsch, max 80 Zeichen), \
die JARVIS ab jetzt befolgen soll. Nur die Regel, kein Kommentar.
Falls nein: Antworte nur mit KEIN_HINWEIS"""


class SelfImprovementDB:
    """Lesson store + correction detector. Lives in the shared jarvis.db."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        self.available = False
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self.available = True
        except Exception as exc:
            print(f"[SelfImprovement] init failed: {exc}")

    # ── correction detection ──────────────────────────────────────── #

    @staticmethod
    def _has_correction_signal(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _CORRECTION_KEYWORDS)

    @staticmethod
    def _has_positive_signal(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _POSITIVE_KEYWORDS)

    def maybe_learn(
        self,
        jarvis_response: str,
        user_reply: str,
        *,
        session_id: str = "",
        client: Any = None,
    ) -> str | None:
        """Core hook — call this after every turn.

        If ``user_reply`` looks like a correction of ``jarvis_response``,
        extract a lesson via Haiku and persist it. Returns the lesson
        text if one was created, None otherwise."""
        if not self.available or not jarvis_response or not user_reply:
            return None

        signal = "positive" if self._has_positive_signal(user_reply) else None
        if self._has_correction_signal(user_reply):
            signal = "correction"

        if signal:
            self._record_signal(signal, user_reply[:300], jarvis_response[:300], session_id)

        if signal != "correction" or client is None:
            return None

        # Short user replies are more likely to be corrections than long ones.
        if len(user_reply) > 200:
            return None

        lesson = self._extract_lesson(jarvis_response, user_reply, client)
        if lesson and lesson != "KEIN_HINWEIS":
            lid = self.add_lesson(lesson, source="correction", session_id=session_id)
            if lid:
                print(f"[SelfImprovement] new lesson #{lid}: {lesson}")
                return lesson
        return None

    def _extract_lesson(self, jarvis: str, user: str, client: Any) -> str | None:
        prompt = _EXTRACT_PROMPT.format(
            jarvis=jarvis[:400].replace('"', "'"),
            user=user[:200].replace('"', "'"),
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    text = (block.text or "").strip()
                    if text and text != "KEIN_HINWEIS" and len(text) < 120:
                        return text
        except Exception as exc:
            print(f"[SelfImprovement] extract_lesson failed: {exc}")
        return None

    # ── storage ───────────────────────────────────────────────────── #

    def _record_signal(
        self, signal_type: str, user_text: str,
        jarvis_text: str, session_id: str,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO feedback_signals "
                "(ts, session_id, signal_type, user_text, jarvis_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), session_id, signal_type, user_text, jarvis_text),
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[SelfImprovement] record_signal failed: {exc}")

    def add_lesson(
        self,
        lesson: str,
        *,
        source: str = "manual",
        confidence: float = 0.8,
        session_id: str = "",
    ) -> int | None:
        lesson = lesson.strip()[:120]
        if not lesson:
            return None
        # Dedup: skip if a very similar lesson already exists (simple exact check).
        try:
            existing = self._conn.execute(
                "SELECT id FROM learned_lessons WHERE active=1 AND lesson=?",
                (lesson,),
            ).fetchone()
            if existing:
                return None
            cur = self._conn.execute(
                "INSERT INTO learned_lessons (ts, lesson, source, confidence, session_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), lesson, source, confidence, session_id),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[SelfImprovement] add_lesson failed: {exc}")
            return None

    def get_active_lessons(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT id, lesson, source, confidence, ts FROM learned_lessons "
                "WHERE active=1 ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[SelfImprovement] get_active_lessons failed: {exc}")
            return []

    def deactivate_lesson(self, lesson_id: int) -> bool:
        try:
            self._conn.execute(
                "UPDATE learned_lessons SET active=0 WHERE id=?", (lesson_id,)
            )
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[SelfImprovement] deactivate_lesson failed: {exc}")
            return False

    def spoken_summary(self) -> str:
        lessons = self.get_active_lessons()
        n = len(lessons)
        if not n:
            return "Noch keine Verhaltensregeln gelernt."
        try:
            signals = self._conn.execute(
                "SELECT signal_type, COUNT(*) AS n FROM feedback_signals "
                "GROUP BY signal_type"
            ).fetchall()
            corrections = next(
                (r["n"] for r in signals if r["signal_type"] == "correction"), 0
            )
            positives = next(
                (r["n"] for r in signals if r["signal_type"] == "positive"), 0
            )
            return (
                f"{n} aktive Regel{'n' if n != 1 else ''} gelernt. "
                f"{corrections} Korrekturen, {positives} positive Signale empfangen."
            )
        except Exception:
            return f"{n} aktive Verhaltensregel{'n' if n != 1 else ''} gelernt."

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
