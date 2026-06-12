"""Conversation quality metrics — tracks tool call correction rates.

Every tool call is recorded. When the user sends a correction signal
shortly after, the preceding calls are marked as corrected. Weekly
audit surfaces which tools are most frequently corrected so the
self-improvement loop can address root causes.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_quality (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    session_id  TEXT    DEFAULT '',
    tool_name   TEXT    NOT NULL,
    corrected   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tq_ts      ON tool_quality(ts);
CREATE INDEX IF NOT EXISTS ix_tq_tool    ON tool_quality(tool_name);
CREATE INDEX IF NOT EXISTS ix_tq_session ON tool_quality(session_id);
"""

_AUDIT_PROMPT = """\
Tool-Nutzungsstatistik der letzten 7 Tage:
{stats}

Erstelle einen kurzen Qualitätsbericht (3-4 Sätze, Deutsch):
- Welche Tools werden am häufigsten korrigiert?
- Was könnte die Ursache sein?
- Welche Maßnahmen könnten helfen?
Kein Markdown, nur Fließtext."""


class QualityMetricsDB:
    """Tool call quality tracker backed by jarvis.db."""

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
            print(f"[QualityMetrics] init failed: {exc}")

    # ── recording ─────────────────────────────────────────────────────── #

    def record_call(self, tool_name: str, session_id: str = "") -> int | None:
        """Record a tool call. Returns the row id for later marking."""
        try:
            cur = self._conn.execute(
                "INSERT INTO tool_quality (ts, session_id, tool_name) VALUES (?,?,?)",
                (time.time(), session_id, tool_name),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as exc:
            print(f"[QualityMetrics] record_call failed: {exc}")
            return None

    def mark_corrected(self, session_id: str, since_ts: float) -> None:
        """Mark recent tool calls in this session as corrected.

        Called when a correction signal is detected in `after_message`.
        Marks calls from the last N seconds to avoid false attribution."""
        try:
            self._conn.execute(
                "UPDATE tool_quality SET corrected=1 "
                "WHERE session_id=? AND ts >= ? AND corrected=0",
                (session_id, since_ts),
            )
            self._conn.commit()
        except Exception as exc:
            print(f"[QualityMetrics] mark_corrected failed: {exc}")

    # ── analysis ──────────────────────────────────────────────────────── #

    def correction_rates(self, days: int = 7) -> list[dict[str, Any]]:
        """Return correction rate per tool for the last N days."""
        cutoff = time.time() - days * 86400
        try:
            rows = self._conn.execute(
                "SELECT tool_name, COUNT(*) AS total, "
                "SUM(corrected) AS corrections "
                "FROM tool_quality WHERE ts >= ? "
                "GROUP BY tool_name "
                "ORDER BY CAST(SUM(corrected) AS REAL) / COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
            return [
                {
                    "tool": r["tool_name"],
                    "total": r["total"],
                    "corrections": r["corrections"] or 0,
                    "rate": round((r["corrections"] or 0) / r["total"], 2),
                }
                for r in rows
            ]
        except Exception as exc:
            print(f"[QualityMetrics] correction_rates failed: {exc}")
            return []

    def weekly_report(self) -> str:
        """Plain-text weekly quality summary."""
        rates = self.correction_rates(7)
        if not rates:
            return "Keine Tool-Nutzungsdaten der letzten Woche."
        total_calls = sum(r["total"] for r in rates)
        total_corrections = sum(r["corrections"] for r in rates)
        overall_rate = round(total_corrections / total_calls, 2) if total_calls else 0

        lines = [
            f"Tool-Qualität (7 Tage): {total_calls} Aufrufe, "
            f"{total_corrections} Korrekturen ({overall_rate:.0%} Korrekturrate)."
        ]
        worst = [r for r in rates if r["rate"] > 0.1 and r["total"] >= 3]
        if worst:
            top = worst[0]
            lines.append(
                f"Häufigste Korrekturen bei '{top['tool']}': "
                f"{top['corrections']}/{top['total']} ({top['rate']:.0%})."
            )
        return " ".join(lines)

    def audit_report(self, *, client: Any = None) -> str:
        """LLM-enhanced quality audit, falls back to weekly_report."""
        rates = self.correction_rates(7)
        if not rates:
            return self.weekly_report()
        if client is None:
            return self.weekly_report()

        stats_lines = []
        for r in rates[:8]:
            stats_lines.append(
                f"  {r['tool']}: {r['corrections']}/{r['total']} Korrekturen "
                f"({r['rate']:.0%})"
            )
        prompt = _AUDIT_PROMPT.format(stats="\n".join(stats_lines))
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text" and block.text:
                    return block.text.strip()
        except Exception as exc:
            print(f"[QualityMetrics] audit_report LLM failed: {exc}")
        return self.weekly_report()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
