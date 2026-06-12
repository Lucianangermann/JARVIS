"""Self-improvement engine — JARVIS learns behavioral rules from corrections.

Four-layer loop
---------------
1. **Detection** — keyword heuristics classify each user message as
   'correction', 'positive', or neutral after every turn.
2. **Extraction** — Claude Haiku decides if the signal is genuine and
   extracts a generalised German rule + a type label (stil / fakt /
   tool / präferenz).  Raw user text never becomes a prompt instruction.
3. **Semantic dedup** — before storing, Haiku checks the new rule against
   existing ones; if semantically identical it updates confidence instead
   of inserting.  Conflicting rules (same topic, contradictory advice)
   are weakened automatically.
4. **Injection** — ContextBuilder splits lessons by type: stil/präferenz
   go into the stable cached block; fakt/tool go into the per-turn
   dynamic block filtered by Jaccard relevance to the current query.

Lifecycle
---------
* **Reinforcement**: positive feedback raises the matching lesson's
  confidence (capped at 1.0).
* **Decay**: on startup, lessons untouched for >30 days lose 0.05
  confidence; any that drop below 0.3 are deactivated automatically.
* **Consolidation**: on demand (self_reflect action='consolidate'),
  Haiku merges redundant lessons and resolves conflicts.
"""
from __future__ import annotations

import json
import sqlite3
import string as _string
import time
from pathlib import Path
from typing import Any


# ── signal vocabularies ────────────────────────────────────────────────── #

_CORRECTION_KEYWORDS = frozenset([
    "nein", "falsch", "stimmt nicht", "nicht richtig", "falsch verstanden",
    "ich meinte", "eigentlich", "nicht so gemeint", "das war nicht",
    "du hast", "hör auf", "lass das", "nicht so", "besser wäre",
    "das wollte ich nicht", "hast du falsch", "falsch gemacht",
])

_POSITIVE_KEYWORDS = frozenset([
    "genau", "perfekt", "super", "richtig", "stimmt", "exakt", "top",
    "gut gemacht", "ja genau", "korrekt", "sehr gut", "danke", "toll",
])

# Stop-words filtered out before Jaccard scoring.
_STOP_DE = frozenset([
    "der", "die", "das", "und", "oder", "in", "zu", "von", "für", "mit",
    "auf", "ist", "sind", "nicht", "alle", "immer", "beim", "bei", "wenn",
    "ein", "eine", "einen", "eines", "einer", "dem", "den", "des", "als",
    "an", "am", "im", "nach", "nur", "noch", "schon", "auch", "aber",
])

# ── prompts ────────────────────────────────────────────────────────────── #

_EXTRACT_PROMPT = """\
JARVIS hat folgendes gesagt:
"{jarvis}"

Der Nutzer hat danach folgendes gesagt:
"{user}"

Ist das eine Korrektur oder ein Hinweis, dass JARVIS etwas falsch gemacht hat?

Falls ja: Antworte AUSSCHLIESSLICH mit diesem JSON (kein Markdown, kein Kommentar):
{{"rule": "<1 Satz, Deutsch, max 80 Zeichen, allgemeine Verhaltensregel>", "type": "<stil|fakt|tool|präferenz>"}}
- stil      = wie JARVIS antwortet (Ton, Länge, Format, Sprache)
- fakt      = JARVIS hat eine falsche Information gegeben
- tool      = JARVIS hat das falsche Tool oder die falsche Aktion gewählt
- präferenz = persönliche Vorliebe oder Gewohnheit des Nutzers

Falls nein: Antworte nur mit KEIN_HINWEIS"""

_DEDUP_PROMPT = """\
Neue Regel: "{new}"

Bestehende Regeln:
{existing}

Gibt es eine bestehende Regel, die semantisch dasselbe bedeutet wie die neue?
Antworte nur mit der Zahl (ID) der passenden Regel oder KEINE."""

_CONSOLIDATE_PROMPT = """\
Hier sind alle aktiven Verhaltensregeln von JARVIS:
{rules}

Deine Aufgabe:
1. Fasse semantisch identische oder sehr ähnliche Regeln zusammen.
2. Löse Konflikte auf (wähle die neuere / präzisere Regel).
3. Behalte maximal 12 Regeln.

Antworte AUSSCHLIESSLICH mit einem JSON-Array (kein Markdown):
[{{"rule": "<Text>", "type": "<stil|fakt|tool|präferenz>", "confidence": <0.5–1.0>}}, ...]"""

# ── schema ─────────────────────────────────────────────────────────────── #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_lessons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    lesson           TEXT    NOT NULL,
    lesson_type      TEXT    DEFAULT 'general',
    source           TEXT    DEFAULT 'correction',
    confidence       REAL    DEFAULT 0.8,
    reinforced_count INTEGER DEFAULT 0,
    last_reinforced  REAL    DEFAULT 0,
    active           INTEGER DEFAULT 1,
    session_id       TEXT    DEFAULT ''
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

_MIGRATIONS = [
    ("lesson_type",      "TEXT    DEFAULT 'general'"),
    ("reinforced_count", "INTEGER DEFAULT 0"),
    ("last_reinforced",  "REAL    DEFAULT 0"),
]


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
            self._migrate()
            self._conn.commit()
            self.available = True
            self._maybe_decay()
        except Exception as exc:
            print(f"[SelfImprovement] init failed: {exc}")

    def _migrate(self) -> None:
        for col, defn in _MIGRATIONS:
            try:
                self._conn.execute(
                    f"ALTER TABLE learned_lessons ADD COLUMN {col} {defn}"
                )
            except Exception:
                pass

    # ── signal detection ──────────────────────────────────────────── #

    @staticmethod
    def _has_correction_signal(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _CORRECTION_KEYWORDS)

    @staticmethod
    def _has_positive_signal(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _POSITIVE_KEYWORDS)

    # ── Jaccard relevance ─────────────────────────────────────────── #

    @staticmethod
    def _words(text: str) -> frozenset[str]:
        words: set[str] = set()
        for raw in text.lower().split():
            w = raw.strip(_string.punctuation)
            if w and w not in _STOP_DE and len(w) > 2:
                words.add(w)
        return frozenset(words)

    def _jaccard(self, a: str, b: str) -> float:
        wa, wb = self._words(a), self._words(b)
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    # ── core learning hook ────────────────────────────────────────── #

    def maybe_learn(
        self,
        jarvis_response: str,
        user_reply: str,
        *,
        session_id: str = "",
        client: Any = None,
    ) -> str | None:
        """Call this after every turn.

        Returns the lesson text if a new rule was created or updated,
        None otherwise."""
        if not self.available or not jarvis_response or not user_reply:
            return None

        is_correction = self._has_correction_signal(user_reply)
        is_positive = self._has_positive_signal(user_reply)
        signal = "correction" if is_correction else ("positive" if is_positive else None)

        if signal:
            self._record_signal(signal, user_reply[:300], jarvis_response[:300], session_id)

        # Positive feedback → reinforce the most relevant existing lesson.
        if signal == "positive":
            self._reinforce_best_match(jarvis_response)
            return None

        # Correction → extract lesson and store/update.
        if signal != "correction" or client is None:
            return None
        if len(user_reply) > 200:
            return None

        extracted = self._extract_lesson_with_type(jarvis_response, user_reply, client)
        if not extracted:
            return None
        rule, lesson_type = extracted

        # Semantic dedup: update existing similar lesson instead of inserting.
        existing_id = self._find_similar_lesson_id(rule, client)
        if existing_id is not None:
            self._update_lesson(existing_id, rule, lesson_type, confidence_delta=+0.1)
            print(f"[SelfImprovement] updated lesson #{existing_id}: {rule}")
            return rule

        # Weaken any lessons on the same topic before inserting.
        self._weaken_conflicting(rule)

        lid = self.add_lesson(rule, source="correction", session_id=session_id,
                              lesson_type=lesson_type)
        if lid:
            print(f"[SelfImprovement] new lesson #{lid} ({lesson_type}): {rule}")
            return rule
        return None

    # ── extraction ────────────────────────────────────────────────── #

    def _extract_lesson_with_type(
        self, jarvis: str, user: str, client: Any,
    ) -> tuple[str, str] | None:
        prompt = _EXTRACT_PROMPT.format(
            jarvis=jarvis[:400].replace('"', "'"),
            user=user[:200].replace('"', "'"),
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    text = (block.text or "").strip()
                    if text == "KEIN_HINWEIS":
                        return None
                    # Strip potential markdown fences.
                    if text.startswith("```"):
                        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    data = json.loads(text)
                    rule = str(data.get("rule", "")).strip()[:100]
                    ltype = str(data.get("type", "general")).strip().lower()
                    if ltype not in {"stil", "fakt", "tool", "präferenz"}:
                        ltype = "general"
                    if rule:
                        return rule, ltype
        except Exception as exc:
            print(f"[SelfImprovement] extract_lesson failed: {exc}")
        return None

    # ── semantic dedup ────────────────────────────────────────────── #

    def _find_similar_lesson_id(self, new_lesson: str, client: Any) -> int | None:
        existing = self.get_active_lessons(limit=15)
        if not existing:
            return None

        # Cheap pre-filter: only call Haiku when Jaccard > 0.25.
        candidates = [
            r for r in existing
            if self._jaccard(new_lesson, r["lesson"]) > 0.25
        ]
        if not candidates:
            return None

        lines = "\n".join(f"[{r['id']}] {r['lesson']}" for r in candidates)
        prompt = _DEDUP_PROMPT.format(
            new=new_lesson.replace('"', "'"),
            existing=lines,
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    text = (block.text or "").strip()
                    if text.isdigit():
                        return int(text)
        except Exception as exc:
            print(f"[SelfImprovement] dedup check failed: {exc}")
        return None

    # ── consolidation ─────────────────────────────────────────────── #

    def consolidate_lessons(self, client: Any) -> str:
        """Weekly maintenance: Haiku merges redundant lessons, resolves
        conflicts, caps the list at 12. Returns a human-readable summary."""
        lessons = self.get_active_lessons(limit=30)
        if len(lessons) < 2:
            return "Zu wenige Regeln für Konsolidierung."
        lines = "\n".join(
            f"[{r['id']}] ({r['lesson_type']}, conf={r['confidence']:.1f}) {r['lesson']}"
            for r in lessons
        )
        prompt = _CONSOLIDATE_PROMPT.format(rules=lines)
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    raw = (block.text or "").strip()
                    break
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            consolidated: list[dict] = json.loads(raw)
        except Exception as exc:
            return f"Konsolidierung fehlgeschlagen: {exc}"

        # Deactivate all existing lessons, then re-insert consolidated set.
        try:
            self._conn.execute("UPDATE learned_lessons SET active=0")
            for entry in consolidated:
                rule = str(entry.get("rule", "")).strip()[:100]
                ltype = str(entry.get("type", "general")).lower()
                conf = float(entry.get("confidence", 0.8))
                if rule:
                    self._conn.execute(
                        "INSERT INTO learned_lessons "
                        "(ts, lesson, lesson_type, source, confidence, active) "
                        "VALUES (?, ?, ?, 'synthesis', ?, 1)",
                        (time.time(), rule, ltype, conf),
                    )
            self._conn.commit()
            n = len([e for e in consolidated if e.get("rule")])
            return f"Konsolidiert: {len(lessons)} → {n} Regeln."
        except Exception as exc:
            return f"Konsolidierung DB-Fehler: {exc}"

    # ── reinforcement ─────────────────────────────────────────────── #

    def _reinforce_best_match(self, jarvis_response: str) -> None:
        """Find the lesson most textually similar to the confirmed response
        and increase its confidence."""
        lessons = self.get_active_lessons(limit=15)
        if not lessons:
            return
        best = max(lessons, key=lambda r: self._jaccard(r["lesson"], jarvis_response))
        if self._jaccard(best["lesson"], jarvis_response) > 0.1:
            self._update_lesson(best["id"], best["lesson"], best.get("lesson_type", "general"),
                                confidence_delta=+0.08)

    def _update_lesson(
        self, lesson_id: int, rule: str, lesson_type: str,
        confidence_delta: float = 0.0,
    ) -> None:
        try:
            row = self._conn.execute(
                "SELECT confidence FROM learned_lessons WHERE id=?", (lesson_id,)
            ).fetchone()
            if not row:
                return
            new_conf = round(min(1.0, max(0.1, row["confidence"] + confidence_delta)), 3)
            self._conn.execute(
                "UPDATE learned_lessons "
                "SET lesson=?, lesson_type=?, confidence=?, reinforced_count=reinforced_count+1, "
                "last_reinforced=?, ts=? "
                "WHERE id=?",
                (rule, lesson_type, new_conf, time.time(), time.time(), lesson_id),
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[SelfImprovement] update_lesson failed: {exc}")

    def _weaken_conflicting(self, new_lesson: str) -> None:
        """Reduce confidence of existing lessons that overlap with ``new_lesson``
        (same topic, possibly outdated advice). Deactivates those below 0.3."""
        lessons = self.get_active_lessons(limit=15)
        for r in lessons:
            if self._jaccard(new_lesson, r["lesson"]) > 0.35:
                new_conf = round(r["confidence"] - 0.15, 3)
                if new_conf < 0.3:
                    self._conn.execute(
                        "UPDATE learned_lessons SET active=0 WHERE id=?", (r["id"],)
                    )
                else:
                    self._conn.execute(
                        "UPDATE learned_lessons SET confidence=? WHERE id=?",
                        (new_conf, r["id"]),
                    )
        self._conn.commit()

    # ── decay ─────────────────────────────────────────────────────── #

    def _maybe_decay(self) -> None:
        """Reduce confidence of lessons untouched for >30 days. Runs on
        startup — server restarts are infrequent enough that this is safe."""
        cutoff = time.time() - 30 * 86400
        try:
            self._conn.execute(
                "UPDATE learned_lessons "
                "SET confidence = ROUND(confidence - 0.05, 3) "
                "WHERE active=1 AND last_reinforced < ? AND ts < ?",
                (cutoff, cutoff),
            )
            self._conn.execute(
                "UPDATE learned_lessons SET active=0 "
                "WHERE active=1 AND confidence < 0.3"
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[SelfImprovement] decay failed: {exc}")

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
        lesson_type: str = "general",
    ) -> int | None:
        lesson = lesson.strip()[:120]
        if not lesson:
            return None
        try:
            existing = self._conn.execute(
                "SELECT id FROM learned_lessons WHERE active=1 AND lesson=?",
                (lesson,),
            ).fetchone()
            if existing:
                return None
            cur = self._conn.execute(
                "INSERT INTO learned_lessons "
                "(ts, lesson, lesson_type, source, confidence, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), lesson, lesson_type, source, confidence, session_id),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[SelfImprovement] add_lesson failed: {exc}")
            return None

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

    # ── retrieval ─────────────────────────────────────────────────── #

    def get_active_lessons(
        self, limit: int = 20, *, lesson_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            if lesson_types:
                placeholders = ",".join("?" * len(lesson_types))
                rows = self._conn.execute(
                    f"SELECT id, lesson, lesson_type, source, confidence, ts "
                    f"FROM learned_lessons "
                    f"WHERE active=1 AND lesson_type IN ({placeholders}) "
                    f"ORDER BY confidence DESC, ts DESC LIMIT ?",
                    (*lesson_types, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, lesson, lesson_type, source, confidence, ts "
                    "FROM learned_lessons "
                    "WHERE active=1 ORDER BY confidence DESC, ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[SelfImprovement] get_active_lessons failed: {exc}")
            return []

    def get_lessons_for_prompt(
        self,
        query: str = "",
        limit: int = 5,
        *,
        lesson_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return lessons sorted by relevance to ``query`` × confidence.

        When ``query`` is empty, falls back to pure confidence ordering."""
        all_lessons = self.get_active_lessons(limit=limit * 3, lesson_types=lesson_types)
        if not query or not all_lessons:
            return all_lessons[:limit]
        scored = sorted(
            all_lessons,
            key=lambda r: (self._jaccard(r["lesson"], query) + 0.01) * r["confidence"],
            reverse=True,
        )
        return scored[:limit]

    # ── spoken summaries ──────────────────────────────────────────── #

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
