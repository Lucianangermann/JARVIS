"""Adaptive persona — adjusts JARVIS's tone based on time of day and mood.

Returns a short prompt block that is injected into the dynamic (per-turn)
system message so it stays outside the prompt-cache boundary and reflects
the current moment. The block intentionally stays under 5 lines so it
doesn't eat into the effective context budget.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any


def compute_persona_block(
    *,
    mood_score: int | None = None,
    hour: int | None = None,
) -> str:
    """Return a one-paragraph persona hint for the current moment.

    When ``mood_score`` is None the persona only adjusts for time of day.
    When the user has a logged mood for today it adds empathy / ambition hints.
    Returns an empty string if nothing noteworthy applies.
    """
    if hour is None:
        hour = dt.datetime.now().hour

    lines: list[str] = []

    # Time-of-day tone
    if 5 <= hour < 9:
        lines.append("Frühmorgens: sei direkt und energetisch, keine langen Erklärungen.")
    elif 9 <= hour < 12:
        lines.append("Vormittag: konzentriert und produktiv — Fokus auf Tasks und Ziele.")
    elif 12 <= hour < 14:
        lines.append("Mittagszeit: entspannter Ton, kurze Antworten bevorzugen.")
    elif 18 <= hour < 22:
        lines.append("Abend: ruhiger Ton, weniger aufgaben-fokussiert.")
    elif hour >= 22 or hour < 5:
        lines.append("Späte Stunde: sehr kurze Antworten, keinen Druck auf Produktivität ausüben.")

    # Mood adjustment
    if mood_score is not None:
        if mood_score <= 3:
            lines.append(
                f"Nutzer-Stimmung heute: {mood_score}/10 — besonders empathisch sein, "
                "keinen Aufgaben-Druck ausüben, einfühlsam reagieren."
            )
        elif mood_score <= 5:
            lines.append(
                f"Nutzer-Stimmung heute: {mood_score}/10 — sanfter Ton, keine überladenen Antworten."
            )
        elif mood_score >= 9:
            lines.append(
                f"Nutzer-Stimmung heute: {mood_score}/10 — ambitioniertere Ziele dürfen angesprochen werden."
            )

    if not lines:
        return ""
    return "## Aktuelle Persona\n" + " ".join(lines)


def read_today_mood(db_path: str | Path) -> int | None:
    """Quick single-value read of today's mood score from jarvis.db.

    Returns None when the table doesn't exist, the day has no entry, or
    any error occurs — always safe to call."""
    import sqlite3
    import time as _t
    today_start = _t.time() - (_t.time() % 86400)  # rough UTC midnight
    import datetime as _dt
    d = _dt.date.today()
    day_ts = _dt.datetime(d.year, d.month, d.day).timestamp()
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT score FROM mood_logs WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
            (day_ts,),
        ).fetchone()
        conn.close()
        return row["score"] if row else None
    except Exception:
        return None
