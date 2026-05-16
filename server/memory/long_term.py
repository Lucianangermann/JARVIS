"""Long-term semantic memory via ChromaDB.

Storage layout
--------------
Three collections in ``data/chromadb/``:

- ``conversations`` — one entry per ended session. Document body is
  the rollup from :meth:`ShortTermMemory.summarise`. Metadata: session
  id, message count, ended_at, optional topic tags.
- ``commands`` — one entry per executed command. Document: the
  user-visible command text. Metadata: success/fail, error class,
  category, duration_ms, ts.
- ``knowledge`` — facts learned ABOUT the user / their environment
  ("user prefers German", "lives in Berlin", "Spotify Premium"). One
  fact per entry. Source-attributed in metadata.

Why ChromaDB
------------
- Local persistent client — files in ``data/chromadb/`` only, no
  daemon. Survives restarts.
- Bring-your-own embeddings (we hand it our 384-dim vectors from
  ``embeddings.py``; Chroma doesn't try to load its own model).
- Cosine similarity by default — matches normalised
  all-MiniLM-L6-v2 output.

Graceful degradation
--------------------
If ChromaDB or the embedding model fail to load, :class:`LongTermMemory`
sets ``self.available = False`` and every method returns a benign
empty / no-op result. The brain keeps running on short-term memory
alone — JARVIS just won't recall older context. Failure reasons are
logged once at startup.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import embeddings

log = logging.getLogger("jarvis.memory.long_term")


class LongTermMemory:
    """Persistent vector store wrapping three Chroma collections.

    All writes are best-effort and never raise into the brain's hot
    path — failures are logged and swallowed."""

    def __init__(self, persist_dir: str | Path) -> None:
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        self.available = False
        self._client = None
        self._conv = None
        self._cmd = None
        self._kn = None

        try:
            self._open()
            # Force the embedding model to load now — otherwise the
            # first session_start() takes ~10s to warm up.
            if embeddings.is_available():
                embeddings.encode("warmup")
                self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("long-term memory disabled: %s", exc)
            self.available = False

    # ---- setup -----------------------------------------------------------

    def _open(self) -> None:
        import chromadb
        from chromadb.config import Settings

        # PersistentClient writes everything under persist_dir. We turn
        # off telemetry so chromadb doesn't phone home.
        self._client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        # Three collections, all using our externally-provided embeddings
        # (we never let chromadb run its own model — we already own the
        # one in embeddings.py). metadata={"hnsw:space": "cosine"} matches
        # the normalised embeddings the model emits.
        meta = {"hnsw:space": "cosine"}
        self._conv = self._client.get_or_create_collection("conversations", metadata=meta)
        self._cmd  = self._client.get_or_create_collection("commands",      metadata=meta)
        self._kn   = self._client.get_or_create_collection("knowledge",     metadata=meta)
        log.info("ChromaDB ready at %s "
                 "(conversations=%d, commands=%d, knowledge=%d)",
                 self.persist_dir,
                 self._conv.count(), self._cmd.count(), self._kn.count())

    # ---- writes ----------------------------------------------------------

    def save_conversation(self, summary: str, *,
                          session_id: str | None = None,
                          message_count: int = 0,
                          tags: list[str] | None = None) -> str | None:
        """Store a session summary. Returns the new entry's id, or
        None if storage is unavailable / summary is empty."""
        if not self.available or not summary.strip():
            return None
        entry_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        try:
            vec = embeddings.encode(summary)
            meta = {
                "ended_at": time.time(),
                "message_count": int(message_count),
                "tags": ",".join(tags or []),
            }
            with self._lock:
                self._conv.upsert(
                    ids=[entry_id],
                    embeddings=[vec],
                    documents=[summary],
                    metadatas=[meta],
                )
            return entry_id
        except Exception as exc:  # noqa: BLE001
            log.warning("save_conversation failed: %s", exc)
            return None

    def save_command(self, command: str, *,
                     result: str = "",
                     success: bool = True,
                     category: str = "other",
                     duration_ms: float | None = None,
                     error_type: str | None = None) -> str | None:
        """Store one executed command + outcome. Used later for
        ``search_similar_commands`` to learn from past attempts."""
        if not self.available or not command.strip():
            return None
        entry_id = f"cmd-{uuid.uuid4().hex[:12]}"
        try:
            vec = embeddings.encode(command)
            meta: dict[str, Any] = {
                "ts": time.time(),
                "success": bool(success),
                "category": category,
            }
            if duration_ms is not None:
                meta["duration_ms"] = float(duration_ms)
            if error_type:
                meta["error_type"] = str(error_type)[:80]
            # Combine command + result text in the document so search
            # can match against either ("how did the brightness command
            # go last time" finds it via the result).
            doc = command if not result else f"{command}\n→ {result[:400]}"
            with self._lock:
                self._cmd.upsert(
                    ids=[entry_id],
                    embeddings=[vec],
                    documents=[doc],
                    metadatas=[meta],
                )
            return entry_id
        except Exception as exc:  # noqa: BLE001
            log.warning("save_command failed: %s", exc)
            return None

    def save_knowledge(self, fact: str, *,
                       source: str = "conversation",
                       category: str = "general") -> str | None:
        """Store a single fact about the user / environment."""
        if not self.available or not fact.strip():
            return None
        entry_id = f"kn-{uuid.uuid4().hex[:12]}"
        try:
            vec = embeddings.encode(fact)
            meta = {"ts": time.time(), "source": source, "category": category}
            with self._lock:
                self._kn.upsert(
                    ids=[entry_id],
                    embeddings=[vec],
                    documents=[fact],
                    metadatas=[meta],
                )
            return entry_id
        except Exception as exc:  # noqa: BLE001
            log.warning("save_knowledge failed: %s", exc)
            return None

    # ---- search ----------------------------------------------------------

    def search_similar(self, query: str, n_results: int = 5) -> list[dict[str, Any]]:
        """Semantic search across the conversations collection.

        Returns ``[{id, document, metadata, distance}, ...]`` sorted
        most-similar-first. Empty list on unavailable / empty store.
        Target: under 200 ms warm."""
        if not self.available or not query.strip():
            return []
        try:
            vec = embeddings.encode(query)
            with self._lock:
                res = self._conv.query(
                    query_embeddings=[vec],
                    n_results=min(n_results, max(1, self._conv.count())),
                )
            return self._unpack(res)
        except Exception as exc:  # noqa: BLE001
            log.warning("search_similar failed: %s", exc)
            return []

    def search_similar_commands(self, command: str,
                                n_results: int = 3) -> list[dict[str, Any]]:
        """Find past commands that resemble ``command`` along with their
        success/fail metadata — used to predict best execution
        strategy + warn about historically-broken paths."""
        if not self.available or not command.strip():
            return []
        try:
            vec = embeddings.encode(command)
            with self._lock:
                count = self._cmd.count()
                if count == 0:
                    return []
                res = self._cmd.query(
                    query_embeddings=[vec],
                    n_results=min(n_results, count),
                )
            return self._unpack(res)
        except Exception as exc:  # noqa: BLE001
            log.warning("search_similar_commands failed: %s", exc)
            return []

    def search_knowledge(self, query: str, n_results: int = 5) -> list[dict[str, Any]]:
        if not self.available or not query.strip():
            return []
        try:
            vec = embeddings.encode(query)
            with self._lock:
                count = self._kn.count()
                if count == 0:
                    return []
                res = self._kn.query(
                    query_embeddings=[vec],
                    n_results=min(n_results, count),
                )
            return self._unpack(res)
        except Exception as exc:  # noqa: BLE001
            log.warning("search_knowledge failed: %s", exc)
            return []

    def get_recent_sessions(self, *, days: int = 7,
                            limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent session summaries within the window.

        Chroma doesn't natively sort by metadata, so we fetch all
        entries (cheap for the small N we keep) and sort in Python."""
        if not self.available:
            return []
        try:
            cutoff = time.time() - days * 86400
            with self._lock:
                got = self._conv.get(include=["documents", "metadatas"])
            ids = got.get("ids") or []
            docs = got.get("documents") or []
            metas = got.get("metadatas") or []
            rows = []
            for i, m in zip(ids, metas):
                ts = (m or {}).get("ended_at", 0)
                if ts < cutoff:
                    continue
                rows.append({"id": i, "metadata": m, "ts": ts})
            rows.sort(key=lambda r: r["ts"], reverse=True)
            rows = rows[:limit]
            # Attach the documents back so callers don't have to round-trip.
            doc_by_id = dict(zip(ids, docs))
            for r in rows:
                r["document"] = doc_by_id.get(r["id"], "")
            return rows
        except Exception as exc:  # noqa: BLE001
            log.warning("get_recent_sessions failed: %s", exc)
            return []

    # ---- maintenance -----------------------------------------------------

    def forget(self, *, older_than_days: int = 90,
               protect_knowledge: bool = True) -> dict[str, int]:
        """Best-effort prune of conversations + commands older than
        ``older_than_days``. Knowledge is always preserved unless the
        caller explicitly overrides — the spec rule is "never delete
        error memories or profile data" so we err on the side of keep.

        Returns a count dict: ``{conversations: N, commands: M}``.
        """
        deleted = {"conversations": 0, "commands": 0}
        if not self.available:
            return deleted
        cutoff = time.time() - older_than_days * 86400
        try:
            for coll, key in ((self._conv, "ended_at"), (self._cmd, "ts")):
                with self._lock:
                    got = coll.get(include=["metadatas"])
                ids = got.get("ids") or []
                metas = got.get("metadatas") or []
                to_delete = [i for i, m in zip(ids, metas)
                             if (m or {}).get(key, time.time()) < cutoff]
                if to_delete:
                    with self._lock:
                        coll.delete(ids=to_delete)
                    coll_name = "conversations" if coll is self._conv else "commands"
                    deleted[coll_name] = len(to_delete)
                    log.info("forget(): pruned %d entries from %s", len(to_delete), coll_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("forget failed: %s", exc)
        return deleted

    def wipe_all(self) -> dict[str, int]:
        """GDPR-style full wipe of all three collections. Returns the
        number of entries removed per collection."""
        wiped = {"conversations": 0, "commands": 0, "knowledge": 0}
        if not self.available:
            return wiped
        try:
            for coll, key in (
                (self._conv, "conversations"),
                (self._cmd, "commands"),
                (self._kn, "knowledge"),
            ):
                with self._lock:
                    got = coll.get()
                ids = got.get("ids") or []
                if ids:
                    with self._lock:
                        coll.delete(ids=ids)
                    wiped[key] = len(ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("wipe_all failed: %s", exc)
        return wiped

    def stats(self) -> dict[str, Any]:
        """Quick state snapshot for /memory/stats."""
        out: dict[str, Any] = {
            "available": self.available,
            "persist_dir": str(self.persist_dir),
        }
        if not self.available:
            return out
        try:
            with self._lock:
                out["conversations"] = self._conv.count()
                out["commands"]      = self._cmd.count()
                out["knowledge"]     = self._kn.count()
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
        return out

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _unpack(res: dict[str, Any]) -> list[dict[str, Any]]:
        """Chroma query results come back as parallel lists wrapped in
        a per-query outer list (we only ever send one query). Flatten
        into ``[{id, document, metadata, distance}, ...]``."""
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = []
        for i, doc, meta, dist in zip(ids, docs, metas, dists):
            out.append({"id": i, "document": doc, "metadata": meta or {},
                        "distance": float(dist) if dist is not None else None})
        return out
