"""ContextEngine — what kind of moment is it, right now?

Aggregates a handful of cheap signals into a single "current
context" struct that the brain can use to adjust its responses:

  • Time bucket (early_morning → late_night)
  • Activity (working, relaxing, in_meeting, focused, sleeping)
  • Stress level (low / medium / high) — derived from command
    frequency over a short rolling window
  • Day type (weekday / weekend)
  • Energy level (high / medium / low) — heuristic on time of day
  • Location (home / away / unknown) — best-effort network probe

All detections are deliberately heuristic; none of them block on
network calls, none can crash. Missing or ambiguous inputs map to
"unknown" or sensible defaults so prompt_block() always returns a
usable string.
"""
from __future__ import annotations

import os
import socket
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

# Time buckets — half-open intervals on the local-clock hour. Edges
# chosen to match the spec; the buckets feed into the activity +
# energy estimators below.
_TIME_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (5,   7, "early_morning"),
    (7,   9, "morning"),
    (9,  12, "work_morning"),
    (12, 13, "lunch"),
    (13, 17, "work_afternoon"),
    (17, 21, "evening"),
    (21, 23, "night"),
)
# Anything that falls through is "late_night" (23:00–05:00).

# Cheap keyword signals to classify the most recent command. Order
# matters — first match wins so a sentence like "play song while
# coding" lands as relaxing (the music intent dominates).
_RELAXING_HINTS = (
    "spiel", "play", "musik", "music", "song", "lied", "spotify",
    "podcast", "youtube", "netflix", "film", "movie", "serie",
    "show",
)
_WORKING_HINTS = (
    "code", "build", "deploy", "commit", "merge", "diff",
    "kompilier", "compile", "debug", "stack trace", "error",
    "test", "ssh ", "git ", "npm ", "pip ", "docker ", "kubectl",
)

# Command-frequency thresholds for stress detection. Counts the
# number of commands in the trailing window — fast bursts read as
# "high stress" / context-switch fatigue.
_STRESS_WINDOW = timedelta(minutes=10)
_STRESS_HIGH = 8       # ≥ 8 commands in 10 min → high
_STRESS_MED = 4        # ≥ 4 commands → medium

# Pause that pushes us back to a "focused" reading. People doing
# deep work tend to either ignore the assistant entirely or come
# back to it after long stretches.
_FOCUSED_GAP = timedelta(minutes=30)

# Sleeping = no command for ≥ 6 h AND it's outside daytime.
_SLEEP_GAP = timedelta(hours=6)


def _local_now() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _classify_time(now: datetime) -> str:
    h = now.hour
    for start, end, label in _TIME_BUCKETS:
        if start <= h < end:
            return label
    return "late_night"


def _classify_keywords(text: str) -> str | None:
    t = text.lower()
    if any(k in t for k in _RELAXING_HINTS):
        return "relaxing"
    if any(k in t for k in _WORKING_HINTS):
        return "working"
    return None


def _energy(time_label: str) -> str:
    if time_label in ("work_morning", "morning"):
        return "high"
    if time_label in ("work_afternoon", "lunch"):
        return "medium"
    if time_label in ("evening", "early_morning"):
        return "medium"
    return "low"  # night, late_night


def _is_at_home() -> str:
    """Naive home/away detection from network interfaces.

    Returns "home" if any local interface's IP matches the prefix in
    HOME_NETWORK_PREFIX (default unset → always "unknown"). We
    deliberately don't claim "away" — there are too many false
    positives (Tailscale interfaces, VPNs, captive portals) and the
    cost of guessing wrong is making the briefing feel weird.
    """
    prefix = (os.getenv("HOME_NETWORK_PREFIX") or "").strip()
    if not prefix:
        return "unknown"
    try:
        # Loop over IPs bound to the machine. socket.getaddrinfo
        # returns more than just IPv4, hence the filter.
        infos = socket.getaddrinfo(socket.gethostname(), None)
        for fam, _typ, _proto, _cname, addr in infos:
            if fam == socket.AF_INET and addr[0].startswith(prefix):
                return "home"
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


class ContextEngine:
    """Aggregates current-state heuristics for prompt injection.

    Stateful: holds a ring buffer of recent command timestamps so
    we can derive frequency-based signals (stress, focused, idle).
    Thread-safe enough for our needs — append/iter on a deque is
    atomic under the GIL and we never iterate while mutating from
    another thread.
    """

    def __init__(self, history_size: int = 50) -> None:
        # (timestamp, text) tuples — newest at the right end.
        self._history: deque[tuple[datetime, str]] = deque(maxlen=history_size)

    # --- ingestion ------------------------------------------------------ #

    def record_command(self, text: str) -> None:
        """Brain calls this on every user turn. We log timestamp +
        a short prefix of the text (enough for keyword classification
        without filling the buffer with novels)."""
        if not text:
            return
        snippet = text.strip()[:120]
        self._history.append((_local_now(), snippet))

    # --- detection ------------------------------------------------------ #

    def _last_command_time(self) -> datetime | None:
        return self._history[-1][0] if self._history else None

    def stress_level(self) -> str:
        """Rolling count of commands in the last ``_STRESS_WINDOW``."""
        if not self._history:
            return "low"
        cutoff = _local_now() - _STRESS_WINDOW
        n = sum(1 for ts, _ in self._history if ts >= cutoff)
        if n >= _STRESS_HIGH:
            return "high"
        if n >= _STRESS_MED:
            return "medium"
        return "low"

    def activity(self, *, calendar_busy: bool = False) -> str:
        """Best-effort activity classification.

        ``calendar_busy`` is plumbed in by the manager — it knows
        about the calendar tool and we don't want context.py to
        import tools/ directly (it'd create a circular import once
        chainer enters the picture in slice 8).
        """
        now = _local_now()
        last = self._last_command_time()
        time_label = _classify_time(now)

        # Calendar wins — a meeting is unambiguous.
        if calendar_busy:
            return "in_meeting"

        # No activity for hours + outside daytime → assume sleeping.
        if last is None and time_label in ("late_night", "night"):
            return "sleeping"
        if last is not None and (now - last) >= _SLEEP_GAP \
           and time_label in ("late_night", "night"):
            return "sleeping"

        # Very recent command? Inspect its text for working/relaxing
        # hints. This is the strongest signal we have aside from the
        # calendar.
        if last is not None and (now - last) < timedelta(minutes=15):
            kw = _classify_keywords(self._history[-1][1])
            if kw is not None:
                return kw

        # Long pause = focused work the assistant isn't seeing.
        if last is not None and (now - last) >= _FOCUSED_GAP:
            return "focused"

        # Otherwise fall back to time-of-day heuristic.
        if time_label in ("work_morning", "work_afternoon"):
            return "working"
        if time_label in ("evening", "late_night", "night"):
            return "relaxing"
        return "working"

    def day_type(self) -> str:
        return "weekend" if _local_now().weekday() >= 5 else "weekday"

    def location(self) -> str:
        return _is_at_home()

    # --- snapshot ------------------------------------------------------- #

    def current(self, *, calendar_busy: bool = False) -> dict[str, str]:
        """Single struct with every field — useful for debugging
        and the future /intelligence/context API route."""
        now = _local_now()
        time_label = _classify_time(now)
        return {
            "time_context": time_label,
            "activity":     self.activity(calendar_busy=calendar_busy),
            "stress_level": self.stress_level(),
            "day_type":     self.day_type(),
            "energy_level": _energy(time_label),
            "location":     self.location(),
        }

    def prompt_block(self, *, calendar_busy: bool = False) -> str:
        """German prose summary for injection into Claude's system
        prompt. Designed to read like an internal note from a
        thoughtful assistant ("Nutzer wirkt fokussiert, halte dich
        kurz") rather than a status dump."""
        c = self.current(calendar_busy=calendar_busy)
        # Translate codes to natural German with a touch of guidance.
        wd = "Wochenende" if c["day_type"] == "weekend" else "Werktag"
        time_de = {
            "early_morning":  "früher Morgen",
            "morning":        "Vormittag",
            "work_morning":   "Arbeitszeit, Vormittag",
            "lunch":          "Mittagszeit",
            "work_afternoon": "Arbeitszeit, Nachmittag",
            "evening":        "Abend",
            "night":          "Abend, spät",
            "late_night":     "tiefe Nacht",
        }.get(c["time_context"], c["time_context"])

        sentences: list[str] = [
            f"Kontext: {wd}, {time_de}."
        ]

        # Activity drives style — surface it for Claude with a hint
        # how to respond. We're not gagging the model, just nudging.
        style_hint = {
            "in_meeting": "Nutzer ist in einem Meeting. Maximal ein "
                          "Satz, möglichst flüsterleise im Ton.",
            "working":    "Nutzer arbeitet konzentriert. Knapp und "
                          "effizient antworten, höchstens zwei Sätze.",
            "focused":    "Nutzer war länger im Tiefenfokus. Knapp "
                          "antworten und keine unnötigen Rückfragen "
                          "stellen.",
            "relaxing":   "Nutzer entspannt sich. Antwort darf "
                          "etwas länger und lockerer sein.",
            "sleeping":   "Es ist Schlafenszeit. Nur antworten wenn "
                          "wirklich nötig, ansonsten extrem knapp.",
        }.get(c["activity"], "")
        sentences.append(f"Aktivität: {c['activity']}.")
        if style_hint:
            sentences.append(style_hint)

        if c["stress_level"] != "low":
            sentences.append(
                "Hinweis: Nutzer hat in den letzten Minuten viele "
                "Befehle gegeben — wirkt unter Druck."
            )

        if c["location"] == "home":
            sentences.append("Standort: zu Hause.")
        # We deliberately omit "Standort: unbekannt" — noisy and
        # adds nothing to Claude's decision-making.

        return " ".join(sentences)
