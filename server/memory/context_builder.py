"""Assemble the full JARVIS system prompt from every memory layer.

The brain's :meth:`_run_tool_loop` previously used the static
``settings.SYSTEM_PROMPT`` as the system message. With long-term
memory in play, that prompt becomes dynamic — we splice in the user
profile, the most relevant past conversations, known-issue warnings,
and a recency block. Everything is plain text so prompt caching
still works (Anthropic caches by exact-string match on the system
block).

Section layout (kept stable so cache hits stay frequent — only the
"Relevant Past Context" + "Current Context" sections vary per query):

    You are JARVIS, …                       — static base
    ## User Profile                         — from ProfileManager
    ## Known Issues to Avoid                — from ErrorMemory
    ## Recent Activity (last 7 days)        — from LongTermMemory
    ## Relevant Past Context                — semantic search, query-dependent
    ## Current Context                      — date / time / session #
    ## Instructions                         — static guidance

Each section gracefully reduces to nothing when its source is
unavailable, so the prompt is always usable even when half the
memory subsystems are degraded.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .error_memory import ErrorMemory
    from .long_term import LongTermMemory
    from .profile_manager import ProfileManager
    from .short_term import ShortTermMemory

log = logging.getLogger("jarvis.memory.context")


_BASE = (
    "You are JARVIS, an advanced AI assistant running on macOS. "
    "You have access to durable memory across sessions — past "
    "conversations, learned facts about the user, and a history of "
    "which commands have worked or failed. Use that context to give "
    "more grounded, personalised replies, but never invent facts "
    "you don't have evidence for."
)

_INSTRUCTIONS = (
    "## Instructions\n"
    "- Reference past conversations naturally when relevant. Do NOT "
    "list memories verbatim unless asked.\n"
    "- If a command has previously failed and we have a known fix, "
    "apply the fix immediately rather than retrying the broken path.\n"
    "- Adapt response length and tone to the user's preferences (see "
    "User Profile → response_style).\n"
    "- When the user shares new information about themselves, weave "
    "it into the conversation naturally — confirmation is fine, but "
    "don't make the user feel surveilled.\n"
    "- Always reply in the user's preferred language (User Profile → "
    "language). Mirror code-switching when they switch.\n"
    "- Keep spoken replies under 3 sentences unless they explicitly "
    "ask for detail. Use plain prose — no markdown, no bullet "
    "points, no headings."
)


class ContextBuilder:
    """Stateless assembler. Holds references to the four memory
    layers and produces the system prompt + a few derivative texts
    (session summary, learnings)."""

    def __init__(self,
                 profile: "ProfileManager | None" = None,
                 long_term: "LongTermMemory | None" = None,
                 error_mem: "ErrorMemory | None" = None,
                 short_term: "ShortTermMemory | None" = None) -> None:
        self.profile = profile
        self.long_term = long_term
        self.error_mem = error_mem
        self.short_term = short_term

    # ---- system prompt ---------------------------------------------------

    def build_system_prompt(self, current_query: str = "",
                            *, session_count: int = 0,
                            base_prompt: str | None = None) -> str:
        """Compose the system message as a single string. Convenience
        wrapper over :meth:`build_system_blocks` — pass-through when
        you don't care about the prompt-cache split."""
        blocks = self.build_system_blocks(
            current_query, session_count=session_count, base_prompt=base_prompt,
        )
        return "\n\n".join(b["text"] for b in blocks)

    def build_system_blocks(self, current_query: str = "",
                            *, session_count: int = 0,
                            base_prompt: str | None = None
                            ) -> list[dict[str, Any]]:
        """Two-block system message keyed for Anthropic's prompt cache.

        Block 1 is the **stable prefix** (base + profile + known
        issues + instructions). Within a session these change rarely,
        so we mark it ``cache_control: ephemeral`` — first call this
        session pays the full prompt token cost, every following call
        amortises against the 5-minute cache window.

        Block 2 is the **per-turn suffix** (recent activity + relevant
        past context + current date/time/session#). Always fresh, no
        cache flag.

        Order matters: the cache key is the exact byte prefix up to
        and including the cache_control breakpoint, so the static
        block has to come first."""
        # ---- stable prefix ---- #
        stable: list[str] = [base_prompt or _BASE]
        prof = self._profile_block()
        if prof:
            stable.append(prof)
        issues = self._issues_block()
        if issues:
            stable.append(issues)
        stable.append(_INSTRUCTIONS)

        # ---- per-turn suffix ---- #
        dynamic: list[str] = []
        recent = self._recent_block()
        if recent:
            dynamic.append(recent)
        if current_query:
            past = self._past_context_block(current_query)
            if past:
                dynamic.append(past)
        dynamic.append(self._current_context_block(session_count))

        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "\n\n".join(stable),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        if dynamic:
            blocks.append({"type": "text", "text": "\n\n".join(dynamic)})
        return blocks

    # ---- per-section builders -------------------------------------------

    def _profile_block(self) -> str:
        if self.profile is None or not self.profile.available:
            return ""
        summary = self.profile.get_profile_summary()
        if not summary.strip():
            return ""
        return f"## User Profile\n{summary}"

    def _issues_block(self, *, min_failures: int = 2, limit: int = 5) -> str:
        if self.error_mem is None or not self.error_mem.available:
            return ""
        rows = self.error_mem.get_problematic_commands(min_failures=min_failures,
                                                       limit=limit)
        if not rows:
            return ""
        bullets = []
        for r in rows:
            rate = int(r["success_rate"] * 100)
            bullets.append(
                f"- {r['command']!r}: {r['fail']}/{r['total']} failures "
                f"({rate}% success rate)"
            )
        return "## Known Issues to Avoid\n" + "\n".join(bullets)

    def _recent_block(self, *, days: int = 7, limit: int = 5) -> str:
        if self.long_term is None or not self.long_term.available:
            return ""
        sessions = self.long_term.get_recent_sessions(days=days, limit=limit)
        if not sessions:
            return ""
        bullets = []
        for s in sessions:
            ts = (s.get("metadata") or {}).get("ended_at", 0)
            when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            body = (s.get("document") or "").strip().replace("\n", " ")
            if len(body) > 180:
                body = body[:180] + "…"
            bullets.append(f"- {when}: {body}")
        return f"## Recent Activity (last {days} days)\n" + "\n".join(bullets)

    def _past_context_block(self, query: str, *, limit: int = 4) -> str:
        if self.long_term is None or not self.long_term.available:
            return ""
        hits = self.long_term.search_similar(query, n_results=limit)
        # Distance ≥ 0.85 on cosine space ≈ unrelated — filter out the
        # noise so we don't poison the prompt with irrelevant chunks.
        hits = [h for h in hits if (h.get("distance") or 1.0) < 0.85]
        if not hits:
            return ""
        bullets = []
        for h in hits:
            body = (h.get("document") or "").strip().replace("\n", " ")
            if len(body) > 220:
                body = body[:220] + "…"
            bullets.append(f"- {body}")
        return "## Relevant Past Context\n" + "\n".join(bullets)

    def _current_context_block(self, session_count: int) -> str:
        now = _dt.datetime.now()
        date_str = now.strftime("%Y-%m-%d (%A)")
        time_str = now.strftime("%H:%M")
        return (
            "## Current Context\n"
            f"Date: {date_str}, Time: {time_str}, Session: #{session_count}"
        )

    # ---- derivative builders --------------------------------------------

    def build_session_summary(self, session_id: str,
                              *, max_chars: int = 600) -> str:
        """Produce the text that gets stored in the long-term
        ``conversations`` collection at session end. Cheap (no LLM
        call): wraps short_term.summarise + a header. The model can
        always do a higher-quality summary later if we add an explicit
        "summarise" tool call before flushing."""
        if self.short_term is None:
            return ""
        body = self.short_term.summarise(session_id, max_chars=max_chars)
        if not body:
            return ""
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"[Session {session_id} ended {ts}]\n{body}"

    def extract_learnings(self, session_id: str) -> list[str]:
        """Pull out memory-worthy items from the current session for
        the ``knowledge`` collection. Currently a thin wrapper over
        :func:`profile_manager.extract_facts` on the joined session
        text — same heuristic, no LLM call. Returns plain strings the
        manager can pass to ``long_term.save_knowledge``."""
        if self.short_term is None:
            return []
        text = self.short_term.summarise(session_id, max_chars=4000)
        if not text:
            return []
        try:
            from .profile_manager import extract_facts, redact_secrets
        except Exception:  # noqa: BLE001
            return []
        facts = extract_facts(redact_secrets(text))
        return [f"{f['category']}: {f['value']}" for f in facts]
