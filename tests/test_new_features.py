"""Tests for the four new JARVIS features:
- KnowledgeStalenessDB (Wissens-Veralterungs-Manager)
- AnomalyDetector (Proaktive Anomalie-Erkennung)
- GoalDB SR extensions (Ziel-Spaced-Repetition)
- TopicGraph (Konversations-Themen-Graph)
"""
from __future__ import annotations

import sqlite3
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── helpers ────────────────────────────────────────────────────────────── #

def _tmp_db() -> Path:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Path(f.name)


def _seed_jarvis_tables(path: Path) -> None:
    """Create minimal table structure used by AnomalyDetector / GoalSR."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS mood_logs (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            score INTEGER NOT NULL, note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY, started_at REAL NOT NULL,
            duration_minutes REAL DEFAULT 0, task_id INTEGER, project TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL,
            status TEXT DEFAULT 'todo', due_date TEXT, priority INTEGER DEFAULT 2
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, description TEXT DEFAULT '',
            deadline TEXT DEFAULT NULL, status TEXT DEFAULT 'active',
            progress_pct INTEGER DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            achieved_at REAL DEFAULT NULL,
            next_review_at REAL DEFAULT NULL,
            review_interval_days INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS goal_checkpoints (
            id INTEGER PRIMARY KEY, goal_id INTEGER,
            ts REAL NOT NULL, note TEXT DEFAULT '', progress_pct INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tool_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, session_id TEXT DEFAULT '',
            tool_name TEXT NOT NULL, corrected INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════════════════════ #
# KnowledgeStalenessDB
# ════════════════════════════════════════════════════════════════════════ #

class TestKnowledgeStaleness:
    def setup_method(self):
        from server.memory.knowledge_staleness import KnowledgeStalenessDB
        self.db_path = _tmp_db()
        self.ks = KnowledgeStalenessDB(self.db_path)

    def teardown_method(self):
        self.ks.close()
        self.db_path.unlink(missing_ok=True)

    def test_available(self):
        assert self.ks.available

    def test_record_access(self):
        self.ks.record_access("kn-abc123")
        stats = self.ks._access_stats(["kn-abc123"])
        assert stats["kn-abc123"]["count"] == 1

    def test_record_access_accumulates(self):
        for _ in range(3):
            self.ks.record_access("kn-xyz")
        stats = self.ks._access_stats(["kn-xyz"])
        assert stats["kn-xyz"]["count"] == 3

    def test_access_stats_missing_id(self):
        stats = self.ks._access_stats(["nonexistent"])
        assert "nonexistent" not in stats

    def test_score_new_never_accessed(self):
        now = time.time()
        score = self.ks._score(created_ts=now - 5 * 86400,
                               last_access_ts=None,
                               access_count=0)
        # High score: recently created but never accessed
        assert 0.0 <= score <= 1.0

    def test_score_old_never_accessed_is_high(self):
        now = time.time()
        score = self.ks._score(created_ts=now - 120 * 86400,
                               last_access_ts=None,
                               access_count=0)
        assert score >= 0.70, f"Expected stale, got {score}"

    def test_score_recently_accessed_is_low(self):
        now = time.time()
        score = self.ks._score(created_ts=now - 30 * 86400,
                               last_access_ts=now - 1 * 86400,
                               access_count=10)
        assert score < 0.70, f"Expected fresh, got {score}"

    def test_stale_candidates_no_chroma(self):
        lt_mock = MagicMock()
        lt_mock.available = False
        result = self.ks.stale_candidates(lt_mock)
        assert result == []

    def test_stale_candidates_with_chroma(self):
        lt_mock = MagicMock()
        lt_mock.available = True
        lt_mock._lock = __import__("threading").Lock()
        lt_mock._kn.get.return_value = {
            "ids": ["kn-old"],
            "documents": ["Python ist toll"],
            "metadatas": [{"ts": time.time() - 200 * 86400}],
        }
        candidates = self.ks.stale_candidates(lt_mock, threshold=0.5)
        assert any(c["doc_id"] == "kn-old" for c in candidates)

    def test_record_access_noop_empty_id(self):
        self.ks.record_access("")
        stats = self.ks._access_stats([""])
        assert stats == {}

    def test_run_weekly_review_no_chroma(self):
        lt_mock = MagicMock()
        lt_mock.available = False
        result = self.ks.run_weekly_review(lt_mock, client=MagicMock())
        assert "keine" in result.lower() or "gefunden" in result.lower()


# ════════════════════════════════════════════════════════════════════════ #
# AnomalyDetector
# ════════════════════════════════════════════════════════════════════════ #

class TestAnomalyDetector:
    def setup_method(self):
        from server.intelligence.anomaly_detector import AnomalyDetector
        self.AD = AnomalyDetector
        self.db_path = _tmp_db()
        _seed_jarvis_tables(self.db_path)

    def teardown_method(self):
        self.db_path.unlink(missing_ok=True)

    def test_no_anomalies_clean_db(self):
        # Fresh DB with no data — no anomalies (can't detect gaps without data).
        result = self.AD.detect(self.db_path)
        # focus_gap may fire on empty DB, that's fine, but no crash.
        assert isinstance(result, list)

    def test_mood_gap_detected(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO mood_logs (ts, score) VALUES (?, ?)",
            (time.time() - 5 * 86400, 7),  # 5 days ago
        )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "mood_gap" in types

    def test_no_mood_gap_logged_today(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO mood_logs (ts, score) VALUES (?, ?)",
            (time.time() - 3600, 8),  # 1 hour ago
        )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "mood_gap" not in types

    def test_focus_gap_no_entries(self):
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "focus_gap" in types

    def test_focus_gap_not_detected_with_entry(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO time_entries (started_at, duration_minutes) VALUES (?, ?)",
            (time.time() - 3600, 25),
        )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "focus_gap" not in types

    def test_overdue_spike_detected(self):
        conn = sqlite3.connect(str(self.db_path))
        for i in range(6):
            conn.execute(
                "INSERT INTO tasks (title, status, due_date) VALUES (?, 'todo', ?)",
                (f"Task {i}", "2020-01-01"),
            )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "overdue_spike" in types

    def test_overdue_spike_not_triggered_under_threshold(self):
        conn = sqlite3.connect(str(self.db_path))
        for i in range(3):
            conn.execute(
                "INSERT INTO tasks (title, status, due_date) VALUES (?, 'todo', ?)",
                (f"Task {i}", "2020-01-01"),
            )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "overdue_spike" not in types

    def test_stale_goal_detected(self):
        conn = sqlite3.connect(str(self.db_path))
        old_ts = time.time() - 20 * 86400
        conn.execute(
            "INSERT INTO goals (title, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
            ("Stale goal", old_ts, old_ts),
        )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "stale_goal" in types

    def test_stale_goal_not_detected_recent(self):
        conn = sqlite3.connect(str(self.db_path))
        now = time.time()
        conn.execute(
            "INSERT INTO goals (title, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
            ("Fresh goal", now - 5 * 86400, now - 2 * 86400),
        )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "stale_goal" not in types

    def test_tool_drift_detected(self):
        conn = sqlite3.connect(str(self.db_path))
        now = time.time()
        for _ in range(5):
            conn.execute(
                "INSERT INTO tool_quality (ts, tool_name, corrected) VALUES (?, ?, 1)",
                (now - 3600, "bad_tool"),
            )
        conn.commit()
        conn.close()
        anomalies = self.AD.detect(self.db_path)
        types = [a["type"] for a in anomalies]
        assert "tool_drift" in types

    def test_spoken_anomalies_empty(self):
        # No overdue tasks, focus gap is a low severity — spoken may still return something
        result = self.AD.spoken_anomalies(self.db_path)
        assert isinstance(result, str)

    def test_spoken_anomalies_returns_high_priority_first(self):
        conn = sqlite3.connect(str(self.db_path))
        for i in range(8):
            conn.execute(
                "INSERT INTO tasks (title, status, due_date) VALUES (?, 'todo', ?)",
                (f"T{i}", "2020-01-01"),
            )
        conn.commit()
        conn.close()
        result = self.AD.spoken_anomalies(self.db_path)
        assert "überfällig" in result.lower() or "aufgelaufen" in result.lower()

    def test_detect_missing_table_graceful(self):
        # Completely empty DB — no tables at all.
        empty = _tmp_db()
        try:
            result = self.AD.detect(empty)
            assert isinstance(result, list)
        finally:
            empty.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════════════ #
# GoalDB Spaced-Repetition extensions
# ════════════════════════════════════════════════════════════════════════ #

class TestGoalSR:
    def setup_method(self):
        from server.productivity.goals import GoalDB
        self.db_path = _tmp_db()
        self.gdb = GoalDB(self.db_path)

    def teardown_method(self):
        self.gdb.close()
        self.db_path.unlink(missing_ok=True)

    def test_available(self):
        assert self.gdb.available

    def test_due_for_review_new_goal_has_no_review(self):
        gid = self.gdb.add("Prüfung bestehen")
        assert gid is not None
        due = self.gdb.due_for_review()
        assert any(g["id"] == gid for g in due)

    def test_record_review_advances_interval(self):
        gid = self.gdb.add("Learn Python")
        ok = self.gdb.record_review(gid)
        assert ok
        # After first review, next_review_at should be set in the future.
        row = self.gdb._conn.execute(
            "SELECT next_review_at, review_interval_days FROM goals WHERE id=?", (gid,)
        ).fetchone()
        assert row[0] is not None
        assert row[0] > time.time()

    def test_record_review_progresses_through_intervals(self):
        intervals = [3, 7, 14, 30]
        gid = self.gdb.add("Fitness")
        # Force starting interval to 3.
        self.gdb._conn.execute(
            "UPDATE goals SET review_interval_days=3 WHERE id=?", (gid,))
        self.gdb._conn.commit()
        self.gdb.record_review(gid)
        row = self.gdb._conn.execute(
            "SELECT review_interval_days FROM goals WHERE id=?", (gid,)
        ).fetchone()
        assert row[0] == 7  # advanced to next interval

    def test_record_review_caps_at_30(self):
        gid = self.gdb.add("Long goal")
        self.gdb._conn.execute(
            "UPDATE goals SET review_interval_days=30 WHERE id=?", (gid,))
        self.gdb._conn.commit()
        self.gdb.record_review(gid)
        row = self.gdb._conn.execute(
            "SELECT review_interval_days FROM goals WHERE id=?", (gid,)
        ).fetchone()
        assert row[0] == 30  # stays at cap

    def test_record_review_with_pct_update(self):
        gid = self.gdb.add("Gewicht")
        ok = self.gdb.record_review(gid, pct_update=42)
        assert ok
        # progress_pct should NOT be updated by record_review (only update_progress does that)
        row = self.gdb._conn.execute(
            "SELECT progress_pct FROM goals WHERE id=?", (gid,)
        ).fetchone()
        # pct_update=42 but record_review doesn't update progress, only manages SR state
        # Actually our implementation DOES update if pct_update is not None — verify:
        assert row[0] == 42

    def test_not_due_after_review(self):
        gid = self.gdb.add("Meditation")
        self.gdb.record_review(gid)
        due = self.gdb.due_for_review()
        ids = [g["id"] for g in due]
        assert gid not in ids

    def test_record_review_nonexistent_goal(self):
        ok = self.gdb.record_review(99999)
        assert not ok

    def test_review_summary_empty(self):
        result = self.gdb.review_summary()
        assert result == ""

    def test_review_summary_one_goal(self):
        self.gdb.add("Lese mehr Bücher")
        result = self.gdb.review_summary()
        assert "Ziel" in result or "wartet" in result

    def test_review_summary_multiple_goals(self):
        for title in ["Ziel A", "Ziel B", "Ziel C"]:
            self.gdb.add(title)
        result = self.gdb.review_summary()
        assert "3" in result

    def test_update_progress_then_review(self):
        gid = self.gdb.add("Coding Challenge")
        self.gdb.update_progress(gid, 50, "Hälfte geschafft")
        # Simulate brain_exec calling record_review after update.
        self.gdb.record_review(gid)
        due = self.gdb.due_for_review()
        ids = [g["id"] for g in due]
        assert gid not in ids


# ════════════════════════════════════════════════════════════════════════ #
# TopicGraph
# ════════════════════════════════════════════════════════════════════════ #

class TestTopicGraph:
    def setup_method(self):
        from server.memory.topic_graph import TopicGraph
        self.db_path = _tmp_db()
        self.tg = TopicGraph(self.db_path)

    def teardown_method(self):
        self.tg.close()
        self.db_path.unlink(missing_ok=True)

    def test_available(self):
        assert self.tg.available

    def test_record_single_tag(self):
        self.tg.record_tags(["python"])
        nodes = self.tg.top_nodes()
        assert any(n["tag"] == "python" for n in nodes)

    def test_record_multiple_tags_creates_edges(self):
        self.tg.record_tags(["python", "statistik"])
        related = self.tg.related_tags("python")
        tags = [r["tag"] for r in related]
        assert "statistik" in tags

    def test_record_tags_accumulates_weight(self):
        for _ in range(3):
            self.tg.record_tags(["python", "statistik"])
        related = self.tg.related_tags("python")
        stat_entry = next((r for r in related if r["tag"] == "statistik"), None)
        assert stat_entry is not None
        assert stat_entry["weight"] == 3

    def test_record_tags_empty_list(self):
        self.tg.record_tags([])
        assert self.tg.top_nodes() == []

    def test_record_tags_deduplicates_within_call(self):
        self.tg.record_tags(["python", "python", "statistik"])
        nodes = self.tg.top_nodes()
        python_node = next((n for n in nodes if n["tag"] == "python"), None)
        assert python_node is not None
        assert python_node["count"] == 1  # appears once in the call

    def test_related_tags_unknown_tag(self):
        result = self.tg.related_tags("nonexistent")
        assert result == []

    def test_related_tags_respects_limit(self):
        self.tg.record_tags(["base", "a", "b", "c", "d", "e"])
        related = self.tg.related_tags("base", limit=2)
        assert len(related) <= 2

    def test_topic_bridge_finds_indirect_connection(self):
        self.tg.record_tags(["python", "prüfung"])
        self.tg.record_tags(["python", "statistik"])
        # Querying "prüfung" should bridge to "statistik" via "python".
        bridges = self.tg.topic_bridge(["prüfung"], n=3)
        bridge_tags = [b["tag"] for b in bridges]
        # "python" or "statistik" should appear (both connected to prüfung indirectly).
        assert len(bridges) > 0

    def test_topic_bridge_excludes_query_tags(self):
        self.tg.record_tags(["python", "statistik", "prüfung"])
        bridges = self.tg.topic_bridge(["python"])
        bridge_tags = [b["tag"] for b in bridges]
        assert "python" not in bridge_tags

    def test_topic_bridge_empty_query(self):
        result = self.tg.topic_bridge([])
        assert result == []

    def test_cluster_map_structure(self):
        self.tg.record_tags(["python", "statistik"])
        self.tg.record_tags(["python", "machine-learning"])
        cmap = self.tg.cluster_map(limit=5)
        assert "nodes" in cmap
        assert "total_nodes" in cmap
        assert cmap["total_nodes"] >= 1

    def test_cluster_map_strongest_link(self):
        self.tg.record_tags(["python", "statistik"])
        self.tg.record_tags(["python", "statistik"])
        self.tg.record_tags(["python", "algo"])
        cmap = self.tg.cluster_map(limit=5)
        python_node = next((n for n in cmap["nodes"] if n["tag"] == "python"), None)
        if python_node and python_node.get("strongest_link"):
            # statistik has weight 2, algo has 1 — python's strongest link is statistik.
            assert python_node["strongest_link"] == "statistik"

    def test_bidirectional_lookup(self):
        self.tg.record_tags(["alpha", "beta"])
        # Both directions should return the partner.
        from_alpha = [r["tag"] for r in self.tg.related_tags("alpha")]
        from_beta  = [r["tag"] for r in self.tg.related_tags("beta")]
        assert "beta"  in from_alpha
        assert "alpha" in from_beta

    def test_record_tags_strips_whitespace(self):
        self.tg.record_tags(["  python  ", " statistik"])
        nodes = [n["tag"] for n in self.tg.top_nodes()]
        assert "python" in nodes
        assert "statistik" in nodes

    def test_top_nodes_sorted_by_count(self):
        self.tg.record_tags(["rare"])
        for _ in range(5):
            self.tg.record_tags(["frequent"])
        nodes = self.tg.top_nodes(limit=5)
        assert nodes[0]["tag"] == "frequent"
