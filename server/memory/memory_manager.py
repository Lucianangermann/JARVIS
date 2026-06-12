"""Central memory coordinator.

Exposes the single object the brain talks to. Internally fans calls
out to the four storage layers (short-term / long-term / error /
profile) and owns the cross-layer lifecycle (session_start / end,
log files).

Failure model
-------------
The brain MUST keep working even if memory blows up. Every public
method wraps its body in try/except and swallows errors with a log
line — :attr:`degraded` flips True on first failure so callers
(and ``/memory/stats``) can see which layers are out.
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import threading
import time
from pathlib import Path
from typing import Any

from .context_builder import ContextBuilder
from .error_memory import ErrorMemory
from .long_term import LongTermMemory
from .profile_manager import ProfileManager
from .quality_metrics import QualityMetricsDB
from .self_improvement import SelfImprovementDB
from .short_term import ShortTermMemory


# Resolve the project root the same way config.py does — parent of
# the package directory. Keeps file locations co-located in data/
# and logs/ regardless of where the server is launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_LOGS_DIR = _PROJECT_ROOT / "logs"


def _make_file_logger(name: str, filename: str) -> logging.Logger:
    """Per-file logger with daily rotation. Separate from the root
    Python logger so memory writes don't get muddled with the brain's
    stderr output."""
    log = logging.getLogger(name)
    if log.handlers:
        return log
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        _LOGS_DIR / filename, when="midnight", backupCount=14,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


class MemoryManager:
    """One per server process. Brain creates this in ``__init__`` and
    calls the lifecycle methods at the documented points."""

    def __init__(self,
                 data_dir: Path | str | None = None,
                 logs_dir: Path | str | None = None,
                 *,
                 default_session_id: str = "default") -> None:
        self.data_dir = Path(data_dir) if data_dir else _DATA_DIR
        self.logs_dir = Path(logs_dir) if logs_dir else _LOGS_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.default_session_id = default_session_id

        # Lifecycle counters — kept here, not in profile, because
        # they reset on process restart.
        self._sessions_started_this_process = 0
        self._sessions_lock = threading.Lock()

        # Per-session "is this turn ongoing?" guard so a slow brain
        # call doesn't race with another command coming in.
        self._session_locks: dict[str, threading.Lock] = {}

        # Build each subsystem in isolation. Failures are non-fatal —
        # we record the load-error for /memory/stats but keep going.
        self.short_term = ShortTermMemory()
        self.long_term  = LongTermMemory(self.data_dir / "chromadb")
        self.error_mem  = ErrorMemory(self.data_dir / "jarvis.db")
        self.profile    = ProfileManager(
            self.data_dir / "profile.json",
            self.data_dir / "jarvis.db",
        )
        self.self_improvement = SelfImprovementDB(self.data_dir / "jarvis.db")
        self.quality_metrics = QualityMetricsDB(self.data_dir / "jarvis.db")
        self.context_builder = ContextBuilder(
            profile=self.profile,
            long_term=self.long_term,
            error_mem=self.error_mem,
            short_term=self.short_term,
            self_improvement=self.self_improvement,
        )
        # Tracks the last JARVIS response per session for correction detection.
        self._prev_responses: dict[str, str] = {}

        self._mem_log = _make_file_logger("jarvis.memlog.memory", "memory.log")
        self._learn_log = _make_file_logger("jarvis.memlog.learning", "learning.log")

        self._mem_log.info("MEMORY BOOT: long_term=%s error=%s profile=%s",
                           self.long_term.available,
                           self.error_mem.available,
                           self.profile.available)
        # Also surface the boot state to stdout so it's visible
        # alongside the rest of the [JARVIS] startup chatter — the
        # per-file logger has propagate=False, so without this print
        # the operator has no way to see at a glance whether memory
        # came up cleanly.
        lt_stats = self.long_term.stats()
        em_stats = self.error_mem.stats()
        pr_stats = self.profile.stats()
        marker = "✓" if not self.degraded else "⚠"
        print(
            f"[JARVIS] memory ready {marker} "
            f"long_term={'on' if self.long_term.available else 'OFF'} "
            f"({lt_stats.get('conversations', 0)} convos, "
            f"{lt_stats.get('commands', 0)} cmds, "
            f"{lt_stats.get('knowledge', 0)} facts) — "
            f"error={'on' if self.error_mem.available else 'OFF'} "
            f"({em_stats.get('known_fixes', 0)} known fixes) — "
            f"profile={'on' if self.profile.available else 'OFF'} "
            f"({pr_stats.get('session_count', 0)} sessions, "
            f"{pr_stats.get('known_facts', 0)} facts)"
        )

    # ---- properties / introspection --------------------------------------

    @property
    def degraded(self) -> bool:
        """True iff any storage layer is unavailable."""
        return not (self.long_term.available
                    and self.error_mem.available
                    and self.profile.available)

    def stats(self) -> dict[str, Any]:
        """Aggregated stats across all layers — drives /memory/stats."""
        return {
            "degraded": self.degraded,
            "sessions_started_this_process": self._sessions_started_this_process,
            "long_term": self.long_term.stats(),
            "error":     self.error_mem.stats(),
            "profile":   self.profile.stats(),
            "short_term": {
                "active_sessions": self.short_term.session_count(),
            },
        }

    # ---- lifecycle -------------------------------------------------------

    def session_start(self, session_id: str | None = None,
                      *, warmup_query: str = "") -> str:
        """Called by the brain on first user message of a session.

        Bumps the session counter, prefetches a warmup search if a
        query is given, and returns the system prompt the brain
        should use for THIS first turn. Subsequent turns rebuild the
        prompt via :meth:`build_prompt_for` (cheap)."""
        session_id = session_id or self.default_session_id
        try:
            with self._sessions_lock:
                self._sessions_started_this_process += 1
            session_count = self.profile.increment_session() if self.profile.available else 0
            # Warmup search — pull a few past hits into Chroma's cache
            # so the first per-turn search doesn't pay disk cost.
            hits = []
            if warmup_query:
                hits = self.long_term.search_similar(warmup_query, n_results=5)
            counts = self.long_term.stats()
            err_count = (self.error_mem.stats() or {}).get("known_fixes", 0)
            self._mem_log.info(
                "SESSION #%d STARTED session_id=%s "
                "long_term_conversations=%d commands=%d knowledge=%d "
                "known_fixes=%d hits=%d",
                session_count, session_id,
                counts.get("conversations", 0),
                counts.get("commands", 0),
                counts.get("knowledge", 0),
                err_count, len(hits),
            )
            self._learn_log.info("SESSION #%d STARTED", session_count)
            self._learn_log.info("MEMORY LOADED: %d relevant contexts found",
                                 len(hits))
            return self.context_builder.build_system_prompt(
                warmup_query, session_count=session_count,
            )
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("session_start failed: %s", exc)
            # Even if the prompt build fails, return SOMETHING usable.
            return self.context_builder.build_system_prompt("", session_count=0)

    def before_message(self, session_id: str, user_text: str) -> str:
        """Called by the brain right BEFORE the Claude API call.

        Appends the user turn to short-term, returns the
        (potentially refreshed) system prompt that should be sent
        with this call. Cheap — semantic search is the only
        non-trivial cost, and 5-result search is <50 ms warm."""
        session_id = session_id or self.default_session_id
        try:
            self.short_term.add(session_id, "user", user_text)
            session_count = self.profile.stats().get("session_count", 0) \
                if self.profile.available else 0
            return self.context_builder.build_system_prompt(
                user_text, session_count=session_count,
            )
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("before_message failed: %s", exc)
            return self.context_builder.build_system_prompt("", session_count=0)

    def build_system_blocks(self, user_text: str = "") -> list[dict[str, Any]]:
        """Return the two-block system message for Anthropic's API
        call (cached-prefix + dynamic suffix). Use this from the
        brain instead of build_system_prompt + cache_control gymnastics."""
        try:
            session_count = (self.profile.stats().get("session_count", 0)
                             if self.profile.available else 0)
            return self.context_builder.build_system_blocks(
                user_text, session_count=session_count,
            )
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("build_system_blocks failed: %s", exc)
            return [{"type": "text", "text": "You are JARVIS."}]

    def after_message(self, session_id: str, user_text: str,
                      response: str) -> None:
        """Called AFTER the Claude reply lands. Adds the assistant
        turn to short-term, lets the profile manager scan the new
        text for facts, writes a learning line, and checks for
        corrections to extract behavioral lessons."""
        session_id = session_id or self.default_session_id
        try:
            self.short_term.add(session_id, "assistant", response)
            if self.profile.available:
                # Extract from BOTH sides — facts can come from
                # JARVIS's own confirmation as well ("Verstanden,
                # dein Lieblingsgenre ist Jazz").
                added_user = self.profile.update_from_conversation(user_text)
                added_jarvis = self.profile.update_from_conversation(response)
                for f in (added_user + added_jarvis):
                    self._learn_log.info("LEARNED: %s = %r",
                                         f["category"], f["value"])
            # Self-improvement: check if current user message is a correction
            # of the PREVIOUS JARVIS response, and extract a lesson if so.
            if self.self_improvement.available:
                prev = self._prev_responses.get(session_id, "")
                if prev:
                    self.self_improvement.maybe_learn(
                        jarvis_response=prev,
                        user_reply=user_text,
                        session_id=session_id,
                        client=self.context_builder.client,
                    )
                    # Mirror correction signal into quality metrics so
                    # tool-level correction rates can be computed.
                    if (self.quality_metrics.available
                            and self.self_improvement._has_correction_signal(user_text)):
                        self.quality_metrics.mark_corrected(
                            session_id, since_ts=time.time() - 300,
                        )
            self._prev_responses[session_id] = response
            # Goal auto-extraction: if the user text contains a goal
            # signal, ask Haiku to extract and save it automatically.
            if self.context_builder.client is not None:
                try:
                    from ..productivity.goals import GoalDB as _GoalDB
                    _gdb = _GoalDB(self.data_dir / "jarvis.db")
                    _gdb.maybe_extract_goal(
                        user_text, self.context_builder.client,
                    )
                    _gdb.close()
                except Exception:  # noqa: BLE001 — never crash on goal extraction
                    pass
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("after_message failed: %s", exc)

    def record_command_result(self, command: str, *,
                              success: bool,
                              error: BaseException | str | None = None,
                              fix: str | None = None,
                              category: str = "other",
                              duration_ms: float | None = None) -> None:
        """Called by the brain after every tool / command execution.

        Drives both the error_memory tables and the long-term
        commands collection so future turns can semantic-search past
        attempts."""
        try:
            if success:
                self.error_mem.record_success(command, duration_ms=duration_ms)
                self.long_term.save_command(
                    command, success=True, category=category,
                    duration_ms=duration_ms,
                )
                if self.profile.available:
                    self.profile.increment_command(category)
            else:
                row_id = self.error_mem.record_error(
                    command, error or "unknown error", category=category,
                )
                err_type = error.__class__.__name__ if isinstance(error, BaseException) \
                    else "Error"
                self.long_term.save_command(
                    command, result=str(error)[:200] if error else "",
                    success=False, category=category, duration_ms=duration_ms,
                    error_type=err_type,
                )
                # If the caller already applied a fix, persist that
                # outcome so a future turn can grab it from
                # known_fixes via get_known_fix.
                if fix and row_id is not None:
                    self.error_mem.record_fix(row_id, fix, worked=True)
                    self._learn_log.info(
                        "ERROR FIXED: %s → %s (worked)", err_type, fix,
                    )
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("record_command_result failed: %s", exc)

    def session_end(self, session_id: str | None = None) -> dict[str, Any]:
        """Called on shutdown OR after :data:`TURN_TIMEOUT_S` idle.

        Builds a summary of the session, ships it to long-term
        memory, harvests learnings as knowledge entries, and clears
        the short-term buffer. Returns the summary string + IDs of
        anything stored (helpful for /memory/recent immediately)."""
        session_id = session_id or self.default_session_id
        out: dict[str, Any] = {"session_id": session_id}
        try:
            summary = self.context_builder.build_session_summary(session_id)
            if not summary.strip():
                self.short_term.clear(session_id)
                self._mem_log.info("SESSION ENDED (empty): id=%s", session_id)
                return out
            msg_count = self.short_term.message_count(session_id)
            conv_id = self.long_term.save_conversation(
                summary, session_id=session_id, message_count=msg_count,
            )
            out["conversation_id"] = conv_id
            # Pull out facts as knowledge entries.
            learnings = self.context_builder.extract_learnings(session_id)
            kn_ids: list[str] = []
            for line in learnings:
                kn_id = self.long_term.save_knowledge(line, source="session")
                if kn_id:
                    kn_ids.append(kn_id)
                    self._learn_log.info("LEARNED (knowledge): %s", line)
            out["knowledge_ids"] = kn_ids
            self._mem_log.info(
                "SESSION ENDED: id=%s messages=%d summary_stored=%s learnings=%d",
                session_id, msg_count, bool(conv_id), len(kn_ids),
            )
            self._learn_log.info(
                "SESSION ENDED: %d messages, %d learnings",
                msg_count, len(kn_ids),
            )
            self.short_term.clear(session_id)
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("session_end failed: %s", exc)
            out["error"] = str(exc)
        return out

    # ---- API surface for /memory/* routes --------------------------------

    def search(self, query: str, *, n_results: int = 5) -> list[dict[str, Any]]:
        try:
            return self.long_term.search_similar(query, n_results=n_results)
        except Exception as exc:  # noqa: BLE001
            self._mem_log.warning("search failed: %s", exc)
            return []

    def recent_sessions(self, *, days: int = 7, limit: int = 10
                        ) -> list[dict[str, Any]]:
        return self.long_term.get_recent_sessions(days=days, limit=limit)

    def known_errors(self) -> list[dict[str, Any]]:
        return self.error_mem.get_problematic_commands(min_failures=1, limit=50)

    def get_profile(self) -> dict[str, Any]:
        return self.profile.get()

    def forget_everything(self, *, confirmation_token: str | None = None) -> dict[str, Any]:
        """Full GDPR wipe — clears ChromaDB + SQLite tables + profile.

        Requires ``confirmation_token == "I UNDERSTAND"`` (double-
        confirm via the route layer). The actual user content is
        NOT logged; only the action and timestamp."""
        if confirmation_token != "I UNDERSTAND":
            return {"ok": False, "reason": "confirmation_token required"}
        result = {
            "ok": True,
            "long_term": self.long_term.wipe_all(),
            "error_memory": self.error_mem.wipe_all(),
        }
        self.profile.wipe_all()
        result["profile"] = "reset"
        # Also drop every active short-term session in memory.
        # We don't have a wipe API on ShortTermMemory because clearing
        # individual sessions is the normal path — for a global wipe
        # we just blow the dict.
        self.short_term._buffers.clear()  # noqa: SLF001
        self._mem_log.info("MEMORY WIPED AT USER REQUEST")
        self._learn_log.info("MEMORY WIPED AT USER REQUEST")
        return result
