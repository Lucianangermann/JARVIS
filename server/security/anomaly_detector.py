"""Learns normal JARVIS usage and flags suspicious deviations.

The detector keeps a lightweight in-memory behavioural baseline — which
hours you're normally active, which command categories are typical — and
scores each incoming command against it. It also does per-IP rate
limiting independent of the global auth rate limiter, because a flood
from one source is itself an anomaly signal.

The baseline is rebuilt from ``access_log`` (security.db) by
:meth:`learn_normal_patterns`, which the SecurityManager schedules
nightly. Until it has run, the detector falls back to conservative
hard-coded heuristics (deep-night activity, low-confidence high-tier
commands, bursts) so it's useful from the first command.

Everything is best-effort and side-effect-light: a flagged command is
*reported*, never auto-blocked here — acting on an anomaly is the
SecurityManager / EmergencySystem's call.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from .access_control import classify_command

# Per-IP rate-limit bands (requests per rolling 60 s).
_RATE_FLAG = 20
_RATE_BLOCK = 50

# Burst detection: more than N commands within this window is "too fast".
_BURST_WINDOW_S = 10.0
_BURST_COUNT = 6

# Hours considered "deep night" by default until a baseline is learned.
_DEFAULT_NIGHT_HOURS = {0, 1, 2, 3, 4, 5}

# High-tier categories where a low voice confidence is especially suspect.
_SENSITIVE_CATEGORIES = {"system", "files", "email"}


class AnomalyDetector:
    """Behavioural baseline + anomaly scoring + per-IP rate limiting."""

    def __init__(self, db: Any = None) -> None:
        self._db = db
        # Recent command timestamps (for burst detection).
        self._recent_cmds: deque[float] = deque(maxlen=64)
        # Per-IP request timestamps (rolling window).
        self._ip_hits: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=128))
        self._blocked_ips: dict[str, float] = {}  # ip -> unblock epoch
        # Learned baseline.
        self._active_hours: set[int] = set()          # empty = not learned yet
        self._common_categories: set[str] = set()
        self._baseline_ready = False
        # Rolling count of failed auth attempts (for pattern detection).
        self._recent_failures: deque[float] = deque(maxlen=32)
        # Reasons from the most recent analyze_command call.
        self.last_reasons: list[str] = []

    # ── per-command analysis ───────────────────────────────────────────── #

    def analyze_command(
        self,
        command: str,
        speaker_confidence: float = 1.0,
        when: datetime | None = None,
    ) -> bool:
        """Score one command. Returns True if it looks anomalous and
        records human-readable reasons in ``last_reasons``."""
        reasons: list[str] = []
        now = time.time()
        ts = when or datetime.now()

        self._recent_cmds.append(now)
        category = classify_command(command)

        # 1. Unusual hour.
        night = self._active_hours and ts.hour not in self._active_hours
        if night or (not self._active_hours and ts.hour in _DEFAULT_NIGHT_HOURS):
            reasons.append(f"ungewöhnliche Uhrzeit ({ts.hour:02d}:00)")

        # 2. Low confidence on a sensitive command.
        if category in _SENSITIVE_CATEGORIES and speaker_confidence < 0.75:
            reasons.append(
                f"niedrige Stimm-Konfidenz ({speaker_confidence:.2f}) "
                f"bei sensiblem Befehl ({category})"
            )

        # 3. Burst — too many commands too fast.
        recent = [t for t in self._recent_cmds if now - t <= _BURST_WINDOW_S]
        if len(recent) >= _BURST_COUNT:
            reasons.append(f"{len(recent)} Befehle in {_BURST_WINDOW_S:.0f}s")

        # 4. Category never seen in the learned baseline.
        if self._baseline_ready and category not in self._common_categories \
                and category in _SENSITIVE_CATEGORIES:
            reasons.append(f"untypische Befehlskategorie ({category})")

        # 5. Recent auth failures elevate suspicion.
        fails = [t for t in self._recent_failures if now - t <= 600]
        if len(fails) >= 3:
            reasons.append(f"{len(fails)} fehlgeschlagene Auth-Versuche (10 min)")

        self.last_reasons = reasons
        is_anom = bool(reasons)
        if is_anom and self._db is not None:
            self._db.log_event(
                "anomaly", "MEDIUM", "anomaly_detector",
                f"'{command[:60]}' — " + "; ".join(reasons),
            )
        return is_anom

    def record_auth_failure(self) -> None:
        """Feed a failed authentication into the failure window."""
        self._recent_failures.append(time.time())

    # ── pattern report ─────────────────────────────────────────────────── #

    def detect_unusual_patterns(self, lookback_hours: int = 24) -> list[dict[str, Any]]:
        """Scan recent access_log rows for suspicious patterns."""
        patterns: list[dict[str, Any]] = []
        if self._db is None:
            return patterns
        since = time.time() - lookback_hours * 3600
        rows = self._db.query(
            "SELECT * FROM access_log WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        )
        if not rows:
            return patterns

        # Deep-night activity.
        night_rows = [
            r for r in rows
            if datetime.fromtimestamp(r["timestamp"]).hour in _DEFAULT_NIGHT_HOURS
        ]
        if night_rows:
            patterns.append({
                "type": "night_activity",
                "count": len(night_rows),
                "detail": f"{len(night_rows)} Befehle nachts (0–6 Uhr)",
            })

        # Repeated denials (possible probing).
        denied = [r for r in rows if not r["allowed"]]
        if len(denied) >= 3:
            patterns.append({
                "type": "repeated_denials",
                "count": len(denied),
                "detail": f"{len(denied)} abgelehnte Befehle",
            })

        # Low-confidence high-tier attempts.
        risky = [
            r for r in rows
            if (r["voice_confidence"] is not None and r["voice_confidence"] < 0.7)
            and (r["permission_level"] in ("high", "critical"))
        ]
        if risky:
            patterns.append({
                "type": "low_confidence_high_tier",
                "count": len(risky),
                "detail": f"{len(risky)} risikoreiche Befehle mit niedriger Konfidenz",
            })

        return patterns

    # ── per-IP rate limiting ───────────────────────────────────────────── #

    def rate_limit_check(self, ip: str) -> bool:
        """Return True if the request should be ALLOWED, False if it should
        be blocked. Flags at >20/min, hard-blocks (5 min) at >50/min."""
        now = time.time()

        # Currently in a block window?
        unblock = self._blocked_ips.get(ip)
        if unblock is not None:
            if now < unblock:
                return False
            del self._blocked_ips[ip]

        hits = self._ip_hits[ip]
        hits.append(now)
        recent = [t for t in hits if now - t <= 60]

        if len(recent) > _RATE_BLOCK:
            self._blocked_ips[ip] = now + 300  # 5-minute block
            if self._db is not None:
                self._db.log_event(
                    "ip_blocked", "HIGH", "anomaly_detector",
                    f"IP {ip} blocked: {len(recent)} req/min",
                )
            print(f"[AnomalyDetector] IP {ip} blocked ({len(recent)} req/min)")
            return False

        if len(recent) > _RATE_FLAG:
            if self._db is not None:
                self._db.log_event(
                    "rate_flag", "LOW", "anomaly_detector",
                    f"IP {ip} elevated: {len(recent)} req/min",
                )
        return True

    # ── baseline learning ──────────────────────────────────────────────── #

    def learn_normal_patterns(self, days: int = 14) -> dict[str, Any]:
        """Rebuild the behavioural baseline from access_log history. Meant
        to run nightly. Returns a small summary for logging."""
        if self._db is None:
            return {"ok": False, "reason": "no db"}
        since = time.time() - days * 86400
        rows = self._db.query(
            "SELECT timestamp, command FROM access_log "
            "WHERE timestamp >= ? AND allowed = 1",
            (since,),
        )
        if len(rows) < 20:
            # Not enough data — keep falling back to heuristics.
            return {"ok": False, "reason": f"only {len(rows)} samples"}

        hour_counts: dict[int, int] = defaultdict(int)
        cat_counts: dict[str, int] = defaultdict(int)
        for r in rows:
            hour_counts[datetime.fromtimestamp(r["timestamp"]).hour] += 1
            if r["command"]:
                cat_counts[classify_command(r["command"])] += 1

        # Active hours = any hour with at least 2% of total traffic.
        total = sum(hour_counts.values())
        self._active_hours = {
            h for h, c in hour_counts.items() if c >= max(1, total * 0.02)
        }
        # Common categories = top categories covering the bulk of traffic.
        self._common_categories = {
            cat for cat, c in cat_counts.items() if c >= max(1, total * 0.01)
        }
        self._baseline_ready = True

        summary = {
            "ok": True,
            "samples": len(rows),
            "active_hours": sorted(self._active_hours),
            "common_categories": sorted(self._common_categories),
        }
        if self._db is not None:
            self._db.log_event(
                "baseline_learned", "INFO", "anomaly_detector",
                f"{len(rows)} samples, {len(self._active_hours)} active hours",
            )
        print(f"[AnomalyDetector] baseline learned from {len(rows)} samples")
        return summary
