"""Conversation topic co-occurrence graph in SQLite.

Nodes = topic tags (same keywords extracted by _extract_cluster_tags).
Edges = co-occurrence: tags A and B appeared in the same knowledge note
or conversation summary → weight += 1.

This lets search_memory / recall_knowledge surface unexpected thematic
connections: "Every time you discuss Python it also involves your
Statistik exam" — without any LLM call on the write path.

Tables live in jarvis.db (same WAL connection as everything else).
"""
from __future__ import annotations

import itertools
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_nodes (
    tag         TEXT PRIMARY KEY,
    total_count INTEGER DEFAULT 0,
    last_seen   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_edges (
    tag_a       TEXT NOT NULL,
    tag_b       TEXT NOT NULL,
    weight      INTEGER DEFAULT 1,
    last_seen   REAL    NOT NULL,
    PRIMARY KEY (tag_a, tag_b)
);
CREATE INDEX IF NOT EXISTS ix_te_a ON topic_edges(tag_a);
CREATE INDEX IF NOT EXISTS ix_te_b ON topic_edges(tag_b);
"""


class TopicGraph:
    """Lightweight tag co-occurrence graph backed by jarvis.db."""

    def __init__(self, db_path: str | Path) -> None:
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
            print(f"[TopicGraph] init failed: {exc}")

    # ── write ─────────────────────────────────────────────────────────── #

    def record_tags(self, tags: list[str]) -> None:
        """Upsert nodes and all pairwise co-occurrence edges for `tags`.

        Idempotent-ish: repeated calls just increment weights.
        """
        if not self.available:
            return
        clean = list({t.strip().lower() for t in tags if t and t.strip()})
        if not clean:
            return
        now = time.time()
        try:
            for tag in clean:
                self._conn.execute(
                    "INSERT INTO topic_nodes (tag, total_count, last_seen) "
                    "VALUES (?, 1, ?) "
                    "ON CONFLICT(tag) DO UPDATE SET "
                    "total_count=total_count+1, last_seen=excluded.last_seen",
                    (tag, now),
                )
            for a, b in itertools.combinations(sorted(set(clean)), 2):
                self._conn.execute(
                    "INSERT INTO topic_edges (tag_a, tag_b, weight, last_seen) "
                    "VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(tag_a, tag_b) DO UPDATE SET "
                    "weight=weight+1, last_seen=excluded.last_seen",
                    (a, b, now),
                )
            self._conn.commit()
        except Exception as exc:
            print(f"[TopicGraph] record_tags failed: {exc}")

    # ── read ──────────────────────────────────────────────────────────── #

    def related_tags(self, tag: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return tags most frequently co-occurring with `tag`.

        Each result: {tag, weight}.
        """
        if not self.available:
            return []
        tag = tag.strip().lower()
        try:
            rows = self._conn.execute(
                "SELECT CASE WHEN tag_a=? THEN tag_b ELSE tag_a END AS other, "
                "weight FROM topic_edges "
                "WHERE tag_a=? OR tag_b=? "
                "ORDER BY weight DESC LIMIT ?",
                (tag, tag, tag, limit),
            ).fetchall()
            return [{"tag": r["other"], "weight": r["weight"]} for r in rows]
        except Exception:
            return []

    def top_nodes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most frequent topic tags.

        Each result: {tag, total_count}.
        """
        if not self.available:
            return []
        try:
            rows = self._conn.execute(
                "SELECT tag, total_count FROM topic_nodes "
                "ORDER BY total_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"tag": r["tag"], "count": r["total_count"]} for r in rows]
        except Exception:
            return []

    def topic_bridge(self, query_tags: list[str], n: int = 3) -> list[dict[str, Any]]:
        """Find tags strongly connected to query_tags but unexpected.

        Returns up to `n` {tag, weight, via} entries — tags reachable
        from query_tags via edges but NOT in query_tags themselves. Useful
        for surfacing thematic patterns: "whenever you ask about Python
        it co-occurs with Statistik."
        """
        if not self.available or not query_tags:
            return []
        clean = {t.strip().lower() for t in query_tags if t.strip()}
        candidates: dict[str, int] = {}
        for tag in clean:
            for rel in self.related_tags(tag, limit=10):
                other = rel["tag"]
                if other not in clean:
                    candidates[other] = candidates.get(other, 0) + rel["weight"]
        sorted_c = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
        return [{"tag": t, "weight": w} for t, w in sorted_c[:n]]

    def cluster_map(self, limit: int = 8) -> dict[str, Any]:
        """Return top nodes and their strongest single connection."""
        nodes = self.top_nodes(limit=limit)
        result = []
        for n in nodes:
            related = self.related_tags(n["tag"], limit=1)
            entry: dict[str, Any] = {"tag": n["tag"], "count": n["count"]}
            if related:
                entry["strongest_link"] = related[0]["tag"]
                entry["link_weight"] = related[0]["weight"]
            result.append(entry)
        return {"nodes": result, "total_nodes": len(self.top_nodes(limit=1000))}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
