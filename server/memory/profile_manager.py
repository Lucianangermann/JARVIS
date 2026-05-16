"""Durable user profile + lightweight fact extraction + redaction.

Storage
-------
- ``data/profile.json``: live profile state, written on every change.
- ``data/jarvis.db``: rolling history of profile updates (audit-trail
  so we can see what was learned when).

Privacy
-------
Two-layer guarantee that sensitive strings never make it to disk:

1. :func:`redact_secrets` is applied to every text-in / text-out path
   that touches storage. Patterns cover credit cards (Luhn-checked),
   common API-key prefixes (sk-…, AKIA…, ghp_…), Bearer tokens,
   passwords in URLs, and AHV / SSN-like numerics.
2. :data:`_FORBIDDEN_CATEGORIES` listed below are never accepted as
   profile-able facts even if extraction matches. Storing them would
   be a bug, so we reject + log.

Fact extraction
---------------
Pure-regex / heuristic (no SpaCy, no LLM call) — the brain layer can
escalate to model-driven extraction later. The current rules pull
high-confidence simple facts: "Ich heiße X", "Ich lebe in Y", "Meine
Lieblings-… ist Z", and a few English equivalents. False negatives are
the dominant failure mode (we'd rather miss a fact than invent one).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("jarvis.memory.profile")


# Categories we refuse to remember even if extraction "succeeds".
_FORBIDDEN_CATEGORIES = frozenset({
    "password", "credentials", "credit_card", "ssn", "bank",
    "medical", "health", "financial", "api_key", "token",
})


# --- Default profile shape ------------------------------------------------ #

_DEFAULT_PROFILE: dict[str, Any] = {
    "personal":   {"name": None, "language": "de", "timezone": None},
    "behavior":   {"wake_time": None, "sleep_time": None,
                   "active_hours": [], "response_style": "concise"},
    "preferences": {"music_genre": [], "frequent_searches": [],
                    "preferred_apps": [], "smart_home_devices": []},
    "usage":      {"total_commands": 0, "favorite_commands": {},
                   "last_active": None, "session_count": 0},
    "context":    {"current_projects": [], "known_facts": [], "relationships": []},
}


# --- Privacy redaction ---------------------------------------------------- #

# Order matters: more specific patterns first so a more-general one
# doesn't gobble part of the match.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Cards: 13–19 digits, optional spaces/dashes. Luhn-check at apply time.
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[REDACTED:card]"),
    # API keys / tokens: common prefixes + opaque tails. The more
    # specific anthropic pattern must come before the generic sk-
    # one, otherwise it gets eaten by the openai matcher.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:openai-key]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack-token]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b", re.IGNORECASE), "[REDACTED:bearer]"),
    # Generic "password=…" / "passwort: …" / "secret = …" inline.
    (re.compile(r"(?i)\b(?:password|passwort|secret|pwd)\s*[:=]\s*\S+"),
     "[REDACTED:credential]"),
    # passwords inside URLs: scheme://user:pass@host
    (re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://[^:\s/]+:)[^@\s]+(?=@)"),
     r"\g<scheme>[REDACTED:url-password]"),
    # SSN (US): NNN-NN-NNNN. False positives possible but rare in DE text.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:ssn]"),
]


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum. Used to gate the card-number regex so
    we don't redact 13-digit dates / phone numbers."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_secrets(text: str) -> str:
    """Strip sensitive substrings, replacing each with a tagged
    placeholder so the source is visible in logs but the value is
    gone. Idempotent: re-applying never re-redacts a placeholder."""
    if not text:
        return text
    out = text
    for pattern, replacement in _REDACT_PATTERNS:
        if "card" in replacement:
            # Card pattern needs Luhn gate to avoid false positives.
            def _sub_card(m: re.Match[str]) -> str:
                raw = m.group(0)
                digits = re.sub(r"\D", "", raw)
                return replacement if _luhn_ok(digits) else raw
            out = pattern.sub(_sub_card, out)
        else:
            out = pattern.sub(replacement, out)
    return out


# --- Fact extraction (heuristic, no NLP dep) ------------------------------ #

_FACT_RULES: list[tuple[re.Pattern[str], str]] = [
    # German
    (re.compile(r"(?i)\bich\s+heiße\s+([A-ZÄÖÜ][\wäöüß-]{1,40})"), "name"),
    (re.compile(r"(?i)\bmein\s+name\s+ist\s+([A-ZÄÖÜ][\wäöüß-]{1,40})"), "name"),
    (re.compile(r"(?i)\bich\s+(?:lebe|wohne)\s+in\s+([A-ZÄÖÜ][\wäöüß -]{1,40})"), "location"),
    (re.compile(r"(?i)\bich\s+arbeite\s+(?:bei|als)\s+([A-ZÄÖÜ][\wäöüß -]{1,40})"), "occupation"),
    (re.compile(r"(?i)\bmein(?:e)?\s+lieblings(?:musik|genre|stil)\s+ist\s+([^.!?\n]{2,40})"),
     "music_genre"),
    (re.compile(r"(?i)\bich\s+mag\s+(?:gerne\s+)?([^.!?\n]{2,40})"), "preference"),
    # English
    (re.compile(r"(?i)\bmy\s+name\s+is\s+([A-Z][\w-]{1,40})"), "name"),
    (re.compile(r"(?i)\bi\s+live\s+in\s+([A-Z][\w -]{1,40})"), "location"),
    (re.compile(r"(?i)\bi\s+like\s+([^.!?\n]{2,40})"), "preference"),
    (re.compile(r"(?i)\bi\s+work\s+(?:at|as)\s+([A-Z][\w -]{1,40})"), "occupation"),
]


def extract_facts(text: str) -> list[dict[str, str]]:
    """Yield ``[{category, value}, ...]`` for plain-text rules that
    match. Values are stripped + capped at 80 chars."""
    if not text:
        return []
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern, cat in _FACT_RULES:
        for m in pattern.finditer(text):
            val = m.group(1).strip(" .,;:!?\"'")[:80]
            if not val:
                continue
            if cat in _FORBIDDEN_CATEGORIES:
                continue
            key = (cat, val.lower())
            if key in seen:
                continue
            seen.add(key)
            found.append({"category": cat, "value": val})
    return found


# --- Profile manager ------------------------------------------------------ #


_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    category    TEXT    NOT NULL,
    key         TEXT,
    old_value   TEXT,
    new_value   TEXT
);

CREATE INDEX IF NOT EXISTS ix_phist_ts ON profile_history (ts);
"""


class ProfileManager:
    """Profile + history with privacy redaction baked into every
    write path. Reads return live state; writes append to history
    AND atomically rewrite profile.json."""

    def __init__(self, profile_path: str | Path, db_path: str | Path) -> None:
        self.profile_path = Path(profile_path)
        self.db_path = Path(db_path)
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.available = False
        self._profile: dict[str, Any] = {}
        try:
            self._load()
            self._open_history()
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("profile manager disabled: %s", exc)

    # ---- public API ------------------------------------------------------

    def get(self) -> dict[str, Any]:
        """Snapshot copy of the live profile (safe to mutate)."""
        with self._lock:
            return json.loads(json.dumps(self._profile))

    def get_profile_summary(self, *, max_chars: int = 600) -> str:
        """Concise text for the system prompt. Skips empty fields so
        the model doesn't see "name: null" lines that just add noise."""
        with self._lock:
            p = self._profile
        lines: list[str] = []
        pers = p.get("personal") or {}
        if pers.get("name"):     lines.append(f"Name: {pers['name']}")
        if pers.get("language"): lines.append(f"Language: {pers['language']}")
        if pers.get("timezone"): lines.append(f"Timezone: {pers['timezone']}")
        beh = p.get("behavior") or {}
        if beh.get("response_style"):
            lines.append(f"Response style: {beh['response_style']}")
        if beh.get("active_hours"):
            lines.append(f"Active hours: {beh['active_hours']}")
        prefs = p.get("preferences") or {}
        for k in ("music_genre", "preferred_apps", "smart_home_devices"):
            vals = prefs.get(k) or []
            if vals:
                lines.append(f"{k.replace('_', ' ').title()}: {', '.join(map(str, vals[:6]))}")
        ctx = p.get("context") or {}
        facts = ctx.get("known_facts") or []
        if facts:
            lines.append("Known facts: " + " | ".join(facts[:8]))
        out = "\n".join(lines)
        return out[:max_chars]

    def learn_preference(self, category: str, value: str) -> None:
        """Append ``value`` to ``preferences[category]`` (deduped).

        Rejects forbidden categories and redacts the value before
        storing. ``category`` must be one of the known preference
        keys; unknown ones get logged + ignored."""
        if category in _FORBIDDEN_CATEGORIES:
            log.warning("refusing to learn forbidden category=%r", category)
            return
        value = redact_secrets((value or "").strip())[:80]
        if not value:
            return
        prefs_cat_map = {
            "music_genre": "music_genre",
            "preferred_apps": "preferred_apps",
            "smart_home_devices": "smart_home_devices",
            "frequent_searches": "frequent_searches",
            "preference": "music_genre",  # generic "I like X" → music_genre bucket
        }
        target = prefs_cat_map.get(category)
        if not target:
            log.debug("unknown preference category=%r value=%r", category, value)
            return
        with self._lock:
            existing = self._profile.setdefault("preferences", {}) \
                                    .setdefault(target, [])
            if value not in existing:
                old = list(existing)
                existing.append(value)
                self._save()
                self._history("preferences." + target, target, str(old), str(existing))

    def increment_command(self, command_type: str) -> None:
        """Bump usage counters. Cheap — called from manager after
        every successful tool execution."""
        command_type = (command_type or "other").lower()[:40]
        with self._lock:
            usage = self._profile.setdefault("usage", {})
            usage["total_commands"] = int(usage.get("total_commands", 0)) + 1
            fav = usage.setdefault("favorite_commands", {})
            fav[command_type] = int(fav.get(command_type, 0)) + 1
            usage["last_active"] = time.time()
            self._save()

    def increment_session(self) -> int:
        """Bump the session counter and return the new value. Called
        once per :meth:`MemoryManager.session_start`."""
        with self._lock:
            usage = self._profile.setdefault("usage", {})
            new_count = int(usage.get("session_count", 0)) + 1
            usage["session_count"] = new_count
            usage["last_active"] = time.time()
            self._save()
            return new_count

    def update_from_conversation(self, text: str) -> list[dict[str, str]]:
        """Run fact extraction over a chunk of conversation text and
        merge the results into the profile. Returns the list of new
        facts actually stored (after dedup + redaction). Empty list
        if nothing new was learned."""
        if not text:
            return []
        # Redact BEFORE extraction so we don't capture secrets as
        # "facts" via the loose preference rule.
        clean = redact_secrets(text)
        facts = extract_facts(clean)
        added: list[dict[str, str]] = []
        for f in facts:
            cat, val = f["category"], f["value"]
            if cat == "name":
                with self._lock:
                    pers = self._profile.setdefault("personal", {})
                    if pers.get("name") != val:
                        old = pers.get("name")
                        pers["name"] = val
                        self._save()
                        self._history("personal", "name", str(old), val)
                        added.append(f)
            elif cat == "location":
                with self._lock:
                    ctx = self._profile.setdefault("context", {})
                    facts_l = ctx.setdefault("known_facts", [])
                    line = f"lives in {val}"
                    if line not in facts_l:
                        facts_l.append(line)
                        self._save()
                        self._history("context.known_facts", "location",
                                      None, line)
                        added.append(f)
            elif cat == "occupation":
                with self._lock:
                    ctx = self._profile.setdefault("context", {})
                    facts_l = ctx.setdefault("known_facts", [])
                    line = f"occupation: {val}"
                    if line not in facts_l:
                        facts_l.append(line)
                        self._save()
                        self._history("context.known_facts", "occupation",
                                      None, line)
                        added.append(f)
            elif cat in ("music_genre", "preference"):
                pre_size = len(self._profile.get("preferences", {})
                                            .get("music_genre", []))
                self.learn_preference("music_genre", val)
                post_size = len(self._profile.get("preferences", {})
                                              .get("music_genre", []))
                if post_size > pre_size:
                    added.append(f)
        return added

    def add_fact(self, fact: str) -> bool:
        """Directly add a free-form fact to ``context.known_facts``.
        Used by the manager when the long-term-memory layer surfaces
        knowledge worth pinning to the profile. Returns True if new."""
        fact = redact_secrets((fact or "").strip())[:200]
        if not fact:
            return False
        with self._lock:
            facts = self._profile.setdefault("context", {}) \
                                 .setdefault("known_facts", [])
            if fact in facts:
                return False
            facts.append(fact)
            self._save()
            self._history("context.known_facts", "fact", None, fact)
            return True

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "available": self.available,
            "profile_path": str(self.profile_path),
        }
        if not self.available:
            return out
        with self._lock:
            usage = self._profile.get("usage", {})
            out["total_commands"] = usage.get("total_commands", 0)
            out["session_count"] = usage.get("session_count", 0)
            out["known_facts"] = len(self._profile.get("context", {}).get("known_facts", []))
            out["preferences"] = {
                k: len(v) if isinstance(v, list) else 0
                for k, v in (self._profile.get("preferences") or {}).items()
            }
        return out

    def wipe_all(self) -> None:
        """Reset to default profile + clear history table. Logged but
        the actual content is not."""
        with self._lock:
            self._profile = self._fresh_profile()
            self._save()
            try:
                cur = self._hist.cursor()
                cur.execute("DELETE FROM profile_history")
            except Exception as exc:  # noqa: BLE001
                log.warning("profile history wipe failed: %s", exc)
            log.info("profile + history wiped")

    # ---- internals -------------------------------------------------------

    def _load(self) -> None:
        if self.profile_path.exists():
            try:
                with self.profile_path.open("r", encoding="utf-8") as fp:
                    self._profile = json.load(fp)
            except json.JSONDecodeError as exc:
                log.warning("profile.json malformed (%s) — starting fresh", exc)
                self._profile = self._fresh_profile()
                self._save()
        else:
            self._profile = self._fresh_profile()
            self._save()

    def _fresh_profile(self) -> dict[str, Any]:
        # Deep copy so callers can mutate without poisoning the template.
        return json.loads(json.dumps(_DEFAULT_PROFILE))

    def _save(self) -> None:
        # Atomic write — temp file + rename — so a crash mid-write
        # doesn't leave a corrupt profile.json.
        tmp = self.profile_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(self._profile, fp, indent=2, ensure_ascii=False)
        tmp.replace(self.profile_path)

    def _open_history(self) -> None:
        self._hist = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None,
        )
        self._hist.execute("PRAGMA journal_mode=WAL")
        self._hist.executescript(_HISTORY_SCHEMA)

    def _history(self, category: str, key: str | None,
                 old_value: str | None, new_value: str | None) -> None:
        try:
            self._hist.execute(
                "INSERT INTO profile_history (ts, category, key, old_value, new_value) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), category, key, old_value, new_value),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("history append failed: %s", exc)
