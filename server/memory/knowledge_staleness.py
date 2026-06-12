"""Knowledge staleness manager — detects and archives outdated ChromaDB notes.

Staleness score = weighted combination of:
  - Age factor    (40%): how old is the note (saturates at 90 days)
  - Access factor (40%): how recently was it retrieved (saturates at 60 days)
  - Frequency     (20%): inverse of access count (never accessed = stale)

Entries with score >= 0.70 are "stale candidates" and queued for
Haiku review. Haiku decides YES (still relevant) / NO (archive).
Archived entries are deleted from ChromaDB — they do NOT move anywhere;
they were noise. Access logging is in jarvis.db so it survives ChromaDB
resets.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .long_term import LongTermMemory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_access_log (
    doc_id  TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kal_doc  ON knowledge_access_log(doc_id);
CREATE INDEX IF NOT EXISTS ix_kal_ts   ON knowledge_access_log(ts);
"""

_REVIEW_PROMPT = """\
Eine gespeicherte Wissensnotiz lautet:
"{text}"

Ist dieser Eintrag noch relevant für eine aktive persönliche KI (Stand: heute)?
Antworte NUR mit JA oder NEIN."""


class KnowledgeStalenessDB:
    """Track access frequency for ChromaDB knowledge entries and archive stale ones."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self.available = False
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self.available = True
        except Exception as exc:
            print(f"[KnowledgeStaleness] init failed: {exc}")

    # ── access logging ──────────────────────────────────────────────── #

    def record_access(self, doc_id: str) -> None:
        if not self.available or not doc_id:
            return
        try:
            self._conn.execute(
                "INSERT INTO knowledge_access_log (doc_id, ts) VALUES (?, ?)",
                (doc_id, time.time()),
            )
            self._conn.commit()
        except Exception:
            pass

    def _access_stats(self, doc_ids: list[str]) -> dict[str, dict[str, float]]:
        """Return {doc_id: {count, last_ts}} for a list of doc IDs."""
        if not doc_ids or not self.available:
            return {}
        placeholders = ",".join("?" * len(doc_ids))
        rows = self._conn.execute(
            f"SELECT doc_id, COUNT(*) AS cnt, MAX(ts) AS last_ts "
            f"FROM knowledge_access_log WHERE doc_id IN ({placeholders}) "
            f"GROUP BY doc_id",
            doc_ids,
        ).fetchall()
        return {r[0]: {"count": r[1], "last_ts": r[2]} for r in rows}

    # ── staleness scoring ────────────────────────────────────────────── #

    @staticmethod
    def _score(created_ts: float, last_access_ts: float | None,
               access_count: int) -> float:
        now = time.time()
        age_days = (now - created_ts) / 86400
        age_factor = min(1.0, age_days / 90)

        if last_access_ts is None:
            access_factor = 1.0
        else:
            access_days = (now - last_access_ts) / 86400
            access_factor = min(1.0, access_days / 60)

        freq_factor = 1.0 / (access_count + 1)

        return round(0.40 * age_factor + 0.40 * access_factor + 0.20 * freq_factor, 3)

    def stale_candidates(
        self, long_term: "LongTermMemory",
        threshold: float = 0.70,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` ChromaDB knowledge entries that appear stale.

        Each entry: {doc_id, text, score, created_ts}
        """
        if not long_term.available:
            return []
        try:
            with long_term._lock:
                results = long_term._kn.get(
                    include=["documents", "metadatas"],
                    limit=200,
                )
        except Exception as exc:
            print(f"[KnowledgeStaleness] chroma scan failed: {exc}")
            return []

        ids = results.get("ids") or []
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []

        stats = self._access_stats(ids)
        candidates: list[dict[str, Any]] = []

        for doc_id, text, meta in zip(ids, docs, metas):
            created_ts = float((meta or {}).get("ts", time.time() - 86400 * 30))
            st = stats.get(doc_id, {})
            score = self._score(
                created_ts,
                st.get("last_ts"),
                int(st.get("count", 0)),
            )
            if score >= threshold:
                candidates.append({
                    "doc_id": doc_id,
                    "text": text or "",
                    "score": score,
                    "created_ts": created_ts,
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    # ── review + archive ─────────────────────────────────────────────── #

    def review_and_clean(
        self,
        candidates: list[dict[str, Any]],
        client: Any,
        long_term: "LongTermMemory",
    ) -> dict[str, int]:
        """Ask Haiku about each candidate. Archive those flagged NO.

        Returns {reviewed, archived}.
        """
        archived = 0
        for entry in candidates:
            text = entry["text"][:400].replace('"', "'")
            prompt = _REVIEW_PROMPT.format(text=text)
            try:
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}],
                )
                answer = ""
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        answer = (block.text or "").strip().upper()
                if "NEIN" in answer or answer.startswith("NO"):
                    self._archive(entry["doc_id"], long_term)
                    archived += 1
            except Exception as exc:
                print(f"[KnowledgeStaleness] review failed for {entry['doc_id']}: {exc}")
        return {"reviewed": len(candidates), "archived": archived}

    def _archive(self, doc_id: str, long_term: "LongTermMemory") -> None:
        try:
            with long_term._lock:
                long_term._kn.delete(ids=[doc_id])
            # Remove from access log too so score resets if re-added.
            self._conn.execute(
                "DELETE FROM knowledge_access_log WHERE doc_id=?", (doc_id,)
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[KnowledgeStaleness] archive failed for {doc_id}: {exc}")

    # ── weekly maintenance ───────────────────────────────────────────── #

    def run_weekly_review(
        self, long_term: "LongTermMemory", client: Any,
        threshold: float = 0.70, limit: int = 10,
    ) -> str:
        """Top-level: find candidates, review, return spoken summary."""
        candidates = self.stale_candidates(long_term, threshold=threshold, limit=limit)
        if not candidates:
            return "Kein veraltetes Wissen gefunden."
        result = self.review_and_clean(candidates, client, long_term)
        reviewed = result["reviewed"]
        archived = result["archived"]
        kept = reviewed - archived
        return (
            f"{reviewed} ältere Wissenseinträge geprüft: "
            f"{archived} archiviert, {kept} weiterhin relevant."
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
