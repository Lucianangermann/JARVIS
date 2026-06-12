"""Tests for the four system optimizations:
- Async ChromaDB write buffer (LongTermMemory)
- Knowledge deduplication (save_knowledge)
- Goal-Task linkage (GoalDB + TaskManager)
- Adaptive prompt compression (PromptCompressor)
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _tmp_db() -> Path:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Path(f.name)


def _tmp_dir() -> Path:
    import tempfile as _tmp
    d = _tmp.mkdtemp()
    return Path(d)


# ════════════════════════════════════════════════════════════════════════ #
# Async write buffer
# ════════════════════════════════════════════════════════════════════════ #

needs_embeddings = pytest.mark.skipif(
    not __import__("server.memory.embeddings", fromlist=["is_available"]).is_available(),
    reason="embedding model not available",
)


@needs_embeddings
def test_async_write_returns_id_immediately():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        # save_conversation must return a non-None ID synchronously.
        entry_id = lt.save_conversation("quick save test")
        assert entry_id is not None
        assert entry_id.startswith("sess-")
        lt.flush()
        lt.close()


@needs_embeddings
def test_flush_waits_for_writes():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        lt.save_conversation("flush test A")
        lt.save_conversation("flush test B")
        lt.flush()
        # After flush, both entries must be visible.
        stats = lt.stats()
        assert stats["conversations"] == 2
        lt.close()


@needs_embeddings
def test_close_drains_queue():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        lt.save_conversation("close drain test")
        lt.close()  # should wait for the pending write
        # Re-open and check the entry survived.
        lt2 = LongTermMemory(Path(d) / "chroma")
        results = lt2.search_similar("drain test")
        assert results and "drain" in results[0]["document"].lower()
        lt2.close()


@needs_embeddings
def test_async_save_knowledge_returns_id():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        eid = lt.save_knowledge("Async write test fact", category="idea")
        assert eid is not None
        lt.flush()
        hits = lt.search_knowledge("async test")
        assert hits
        lt.close()


# ════════════════════════════════════════════════════════════════════════ #
# Knowledge deduplication
# ════════════════════════════════════════════════════════════════════════ #

@needs_embeddings
def test_dedup_merges_identical_fact():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        id1 = lt.save_knowledge("User lebt in Berlin.")
        lt.flush()
        # Exact same text — should return the same id.
        id2 = lt.save_knowledge("User lebt in Berlin.")
        lt.flush()
        assert id1 == id2, "Identical fact must return same entry id"
        assert lt.stats()["knowledge"] == 1
        lt.close()


@needs_embeddings
def test_dedup_keeps_distinct_facts():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        id1 = lt.save_knowledge("User bevorzugt dunkles UI-Theme.")
        lt.flush()
        id2 = lt.save_knowledge("Idee: Finanz-Layer mit Beleg-Scan aus der Mail-App.")
        lt.flush()
        assert id1 != id2, "Distinct facts must have different ids"
        assert lt.stats()["knowledge"] == 2
        lt.close()


@needs_embeddings
def test_dedup_updates_document_on_merge():
    from server.memory.long_term import LongTermMemory
    with tempfile.TemporaryDirectory() as d:
        lt = LongTermMemory(Path(d) / "chroma")
        lt.save_knowledge("Nutzer lebt in Berlin.")
        lt.flush()
        # Near-identical phrasing that could be a duplicate:
        id2 = lt.save_knowledge("Nutzer lebt in Berlin.")
        lt.flush()
        results = lt.search_knowledge("wo wohnt der nutzer")
        assert results
        lt.close()


# ════════════════════════════════════════════════════════════════════════ #
# Goal-Task linkage
# ════════════════════════════════════════════════════════════════════════ #

class TestGoalTaskLinkage:
    def setup_method(self):
        from server.productivity.goals import GoalDB
        self.db_path = _tmp_db()
        self.gdb = GoalDB(self.db_path)

    def teardown_method(self):
        self.gdb.close()
        self.db_path.unlink(missing_ok=True)

    def test_auto_link_no_goals(self):
        result = self.gdb.auto_link_task("Lernkarten erstellen")
        assert result is None

    def test_auto_link_matching_goal(self):
        gid = self.gdb.add("Statistik Prüfung vorbereiten")
        matched = self.gdb.auto_link_task("Statistik Übungsaufgaben lösen")
        assert matched == gid

    def test_auto_link_no_match_below_threshold(self):
        self.gdb.add("Fitness Training")
        matched = self.gdb.auto_link_task("Steuererklärung abgeben")
        assert matched is None

    def test_auto_link_picks_best_goal(self):
        gid1 = self.gdb.add("Python lernen")
        gid2 = self.gdb.add("Fitness verbessern")
        matched = self.gdb.auto_link_task("Python Übungen machen")
        assert matched == gid1

    def test_auto_link_empty_title(self):
        self.gdb.add("Irgendein Ziel")
        result = self.gdb.auto_link_task("")
        assert result is None

    def test_link_summary_no_tasks(self):
        gid = self.gdb.add("Mein Ziel")
        summary = self.gdb.link_summary(gid)
        assert summary["linked"] == 0
        assert summary["pct"] is None

    def test_link_summary_counts_tasks(self):
        gid = self.gdb.add("Coding")
        # Insert tasks directly (tasks table in same DB).
        self.gdb._conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY, title TEXT, status TEXT DEFAULT 'todo', "
            "goal_id INTEGER)"
        )
        self.gdb._conn.execute(
            "INSERT INTO tasks (title, status, goal_id) VALUES ('T1', 'done', ?)", (gid,)
        )
        self.gdb._conn.execute(
            "INSERT INTO tasks (title, status, goal_id) VALUES ('T2', 'todo', ?)", (gid,)
        )
        self.gdb._conn.commit()
        summary = self.gdb.link_summary(gid)
        assert summary["linked"] == 2
        assert summary["done"] == 1
        assert summary["pct"] == 50

    def test_update_progress_from_tasks(self):
        gid = self.gdb.add("Workout plan")
        self.gdb._conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY, title TEXT, status TEXT DEFAULT 'todo', "
            "goal_id INTEGER)"
        )
        for i in range(3):
            self.gdb._conn.execute(
                "INSERT INTO tasks (title, status, goal_id) VALUES (?, 'done', ?)",
                (f"Task {i}", gid),
            )
        self.gdb._conn.commit()
        ok = self.gdb.update_progress_from_tasks(gid)
        assert ok
        goal = next(g for g in self.gdb.get_active() if g["id"] == gid)
        assert goal["progress_pct"] == 100


class TestTaskManagerGoalId:
    def setup_method(self):
        from server.productivity.task_manager import TaskManager
        self.db_path = _tmp_db()
        self.tm = TaskManager(self.db_path)

    def teardown_method(self):
        self.tm._conn.close()
        self.db_path.unlink(missing_ok=True)

    def test_add_task_with_goal_id(self):
        tid = self.tm.add_task("Test task", goal_id=42)
        assert tid > 0
        row = self.tm._conn.execute(
            "SELECT goal_id FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == 42

    def test_add_task_without_goal_id(self):
        tid = self.tm.add_task("No goal task")
        assert tid > 0
        row = self.tm._conn.execute(
            "SELECT goal_id FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] is None

    def test_goal_id_column_exists(self):
        info = self.tm._conn.execute(
            "PRAGMA table_info(tasks)"
        ).fetchall()
        columns = [r[1] for r in info]
        assert "goal_id" in columns


# ════════════════════════════════════════════════════════════════════════ #
# Prompt compressor
# ════════════════════════════════════════════════════════════════════════ #

class TestPromptCompressor:
    def setup_method(self):
        from server.memory.prompt_compressor import PromptCompressor
        self.pc = PromptCompressor()

    def _mock_profile(self, facts: list[str]):
        profile = MagicMock()
        profile.available = True
        profile.get.return_value = {"context": {"known_facts": facts}}
        profile._lock = __import__("threading").RLock()
        profile._profile = {"context": {"known_facts": facts}}
        profile._save = MagicMock()
        return profile

    def test_compress_profile_too_few_facts(self):
        profile = self._mock_profile(["User lebt in Berlin", "User mag Kaffee"])
        client = MagicMock()
        result = self.pc.compress_profile_facts(profile, client)
        assert "noch nicht genug" in result.lower() or "genug" in result.lower()
        client.messages.create.assert_not_called()

    def test_compress_profile_unavailable(self):
        profile = MagicMock()
        profile.available = False
        result = self.pc.compress_profile_facts(profile, MagicMock())
        assert "nicht verfügbar" in result.lower()

    def test_compress_profile_calls_haiku(self):
        facts = [f"Fakt {i}" for i in range(6)]
        profile = self._mock_profile(facts)
        client = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Fakt A\nFakt B\nFakt C"
        client.messages.create.return_value.content = [mock_block]

        result = self.pc.compress_profile_facts(profile, client)
        client.messages.create.assert_called_once()
        assert "6" in result or "komprimiert" in result.lower()

    def test_compress_profile_updates_facts(self):
        facts = [f"Fakt {i}" for i in range(6)]
        profile = self._mock_profile(facts)
        client = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Merged Fakt 1\nMerged Fakt 2"
        client.messages.create.return_value.content = [mock_block]

        self.pc.compress_profile_facts(profile, client)
        assert profile._profile["context"]["known_facts"] == [
            "Merged Fakt 1", "Merged Fakt 2"
        ]
        profile._save.assert_called_once()

    def test_compress_profile_empty_haiku_response(self):
        facts = [f"Fakt {i}" for i in range(6)]
        profile = self._mock_profile(facts)
        client = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = ""
        client.messages.create.return_value.content = [mock_block]

        result = self.pc.compress_profile_facts(profile, client)
        assert "keine antwort" in result.lower() or "nicht" in result.lower()
        profile._save.assert_not_called()

    def test_compress_lessons_delegates_to_si(self):
        si = MagicMock()
        si.available = True
        si.consolidate_lessons.return_value = "Konsolidiert: 5→3."
        result = self.pc.compress_lessons(si, client=MagicMock())
        assert "konsolidiert" in result.lower() or "5" in result

    def test_compress_lessons_unavailable(self):
        si = MagicMock()
        si.available = False
        result = self.pc.compress_lessons(si, client=MagicMock())
        assert "nicht verfügbar" in result.lower()

    def test_run_combines_both(self):
        facts = [f"F{i}" for i in range(6)]
        profile = self._mock_profile(facts)
        si = MagicMock()
        si.available = True
        si.consolidate_lessons.return_value = "OK"
        client = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "A\nB\nC"
        client.messages.create.return_value.content = [mock_block]

        result = self.pc.run(profile, si, client)
        assert "profil" in result.lower()
        assert "regel" in result.lower()

    def test_compress_profile_haiku_error_graceful(self):
        facts = [f"F{i}" for i in range(6)]
        profile = self._mock_profile(facts)
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API error")

        result = self.pc.compress_profile_facts(profile, client)
        assert "fehlgeschlagen" in result.lower()
        profile._save.assert_not_called()
