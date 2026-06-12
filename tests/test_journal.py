"""Tests for automatic journal + curriculum + persona."""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.productivity.journal import JournalDB
from server.productivity.curriculum import generate_curriculum, _rule_based
from server.intelligence.persona import compute_persona_block, read_today_mood


# ── JournalDB ─────────────────────────────────────────────────────────────── #

@pytest.fixture()
def jdb(tmp_path: Path) -> JournalDB:
    db = JournalDB(tmp_path / "j.db")
    # Create the tables that JournalDB reads from.
    db._conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium',
            created_at REAL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT,
            start_time REAL,
            duration_minutes REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS mood_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            score INTEGER,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS feedback_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            signal_type TEXT,
            user_text TEXT DEFAULT '',
            jarvis_text TEXT DEFAULT ''
        );
    """)
    db._conn.commit()
    yield db
    db.close()


def _today_ts() -> float:
    d = dt.date.today()
    return dt.datetime(d.year, d.month, d.day).timestamp()


def test_journal_available(jdb: JournalDB) -> None:
    assert jdb.available is True


def test_today_entry_empty(jdb: JournalDB) -> None:
    e = jdb.today_entry()
    assert e["tasks_done"] == 0
    assert e["focus_minutes"] == 0
    assert e["mood_score"] is None


def test_today_entry_with_tasks(jdb: JournalDB) -> None:
    now = _today_ts() + 100
    jdb._conn.execute(
        "INSERT INTO tasks (title, status, created_at, completed_at) VALUES (?,?,?,?)",
        ("Test", "done", now, now),
    )
    jdb._conn.commit()
    e = jdb.today_entry()
    assert e["tasks_done"] == 1
    assert e["tasks_added"] == 1


def test_today_entry_focus(jdb: JournalDB) -> None:
    now = _today_ts() + 200
    jdb._conn.execute(
        "INSERT INTO time_entries (project, start_time, duration_minutes) VALUES (?,?,?)",
        ("Work", now, 45),
    )
    jdb._conn.commit()
    e = jdb.today_entry()
    assert e["focus_minutes"] == 45


def test_today_entry_mood(jdb: JournalDB) -> None:
    jdb._conn.execute(
        "INSERT INTO mood_logs (ts, score, note) VALUES (?,?,?)",
        (_today_ts() + 300, 7, "guter tag"),
    )
    jdb._conn.commit()
    e = jdb.today_entry()
    assert e["mood_score"] == 7
    assert e["mood_note"] == "guter tag"


def test_today_entry_feedback_signals(jdb: JournalDB) -> None:
    now = _today_ts() + 400
    jdb._conn.execute(
        "INSERT INTO feedback_signals (ts, signal_type) VALUES (?,?)", (now, "correction")
    )
    jdb._conn.execute(
        "INSERT INTO feedback_signals (ts, signal_type) VALUES (?,?)", (now, "positive")
    )
    jdb._conn.commit()
    e = jdb.today_entry()
    assert e["corrections"] == 1
    assert e["positive_signals"] == 1


def test_spoken_today_empty(jdb: JournalDB) -> None:
    assert "keine" in jdb.spoken_today().lower()


def test_spoken_today_with_data(jdb: JournalDB) -> None:
    now = _today_ts() + 100
    jdb._conn.execute(
        "INSERT INTO tasks (title, status, created_at, completed_at) VALUES (?,?,?,?)",
        ("A", "done", now, now),
    )
    jdb._conn.execute(
        "INSERT INTO time_entries (project, start_time, duration_minutes) VALUES (?,?,?)",
        ("P", now, 30),
    )
    jdb._conn.commit()
    text = jdb.spoken_today()
    assert "Task" in text
    assert "30min" in text


def test_spoken_week_empty(jdb: JournalDB) -> None:
    text = jdb.spoken_week()
    assert "keine" in text.lower()


def test_week_entries_length(jdb: JournalDB) -> None:
    entries = jdb.week_entries(7)
    assert len(entries) == 7


def test_insights_no_client(jdb: JournalDB) -> None:
    # Falls back to spoken_week
    result = jdb.insights(client=None)
    assert isinstance(result, str)


def test_today_mood_method(jdb: JournalDB) -> None:
    assert jdb.today_mood() is None
    jdb._conn.execute(
        "INSERT INTO mood_logs (ts, score) VALUES (?,?)", (_today_ts() + 1, 6)
    )
    jdb._conn.commit()
    assert jdb.today_mood() == 6


# ── Curriculum ────────────────────────────────────────────────────────────── #

def test_curriculum_no_data() -> None:
    result = generate_curriculum()
    assert "kein" in result.lower()


def test_curriculum_rule_based_cards() -> None:
    result = _rule_based(due_cards=5, subjects=[], available_minutes=30)
    assert "Karteikarten" in result
    assert "5" in result


def test_curriculum_rule_based_subjects() -> None:
    subjects = [
        {"display_name": "Statistik", "status": "offen"},
        {"display_name": "Python", "status": "bearbeitet"},
    ]
    result = _rule_based(due_cards=0, subjects=subjects, available_minutes=60)
    assert "Statistik" in result or "Python" in result


def test_curriculum_with_mock_lerntrack() -> None:
    lt = MagicMock()
    lt.list_group.return_value = [{"display_name": "Mathe", "status": "offen",
                                   "subject_group": ""}]
    result = generate_curriculum(lerntrack=lt, available_minutes=45)
    assert "Mathe" in result or "45" in result


def test_curriculum_with_mock_flashcards() -> None:
    fc = MagicMock()
    fc.due_count.return_value = 8
    result = generate_curriculum(flashcard_manager=fc, available_minutes=30)
    assert "8" in result or "Karteikarten" in result


# ── Adaptive Persona ──────────────────────────────────────────────────────── #

def test_persona_empty_midday() -> None:
    # 11:00 — no special case
    block = compute_persona_block(hour=11)
    assert isinstance(block, str)


def test_persona_morning() -> None:
    block = compute_persona_block(hour=6)
    assert "Frühmorgens" in block or block == ""


def test_persona_evening() -> None:
    block = compute_persona_block(hour=20)
    assert "Abend" in block


def test_persona_low_mood() -> None:
    block = compute_persona_block(mood_score=2, hour=10)
    assert "empathisch" in block.lower() or "2/10" in block


def test_persona_high_mood() -> None:
    block = compute_persona_block(mood_score=9, hour=10)
    assert "9/10" in block or "ambitiös" in block.lower() or "ambitioniert" in block.lower()


def test_persona_no_mood() -> None:
    block = compute_persona_block(hour=15)
    # Afternoon has no special case → empty or minimal
    assert isinstance(block, str)


def test_read_today_mood_missing_db(tmp_path: Path) -> None:
    score = read_today_mood(tmp_path / "nonexistent.db")
    assert score is None


def test_read_today_mood_from_db(tmp_path: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE mood_logs (id INTEGER PRIMARY KEY, ts REAL, score INTEGER, note TEXT)")
    conn.execute("INSERT INTO mood_logs (ts, score, note) VALUES (?,?,?)",
                 (_today_ts() + 1, 8, ""))
    conn.commit()
    conn.close()
    score = read_today_mood(tmp_path / "m.db")
    assert score == 8
