"""Claude integration with per-session memory and a small tool surface.

Design notes
------------
- One ``Anthropic`` client per process; cheap to keep around.
- Conversation state is per-session-id (typically the auth token, since this
  is a single-user assistant). History is trimmed to MAX_HISTORY_TURNS to
  keep latency and cost flat over long conversations.
- The system prompt is sent as a cached text block (``cache_control``) so
  repeat turns pay ~0.1× for the prompt instead of full price. Tool
  definitions ride along in the cached prefix because they render before
  messages — see shared/prompt-caching.md in the claude-api skill.
- Tools exposed to Claude:
    1. ``system_command`` — proxies into ``command_guard.execute``. Every
       call passes through the whitelist; Claude cannot run arbitrary code.
    2. ``web_search`` — built-in server-side tool from Anthropic.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from anthropic import Anthropic
from anthropic.types import Message

from . import command_guard
from .config import settings


def _system_command_tool() -> dict[str, Any]:
    """Single tool schema describing every whitelisted command at once."""
    command_names = list(command_guard.ALLOWED_COMMANDS.keys())
    return {
        "name": "system_command",
        "description": (
            "Run a whitelisted system command on the user's machine. "
            "Only the names listed in the enum are allowed; any other "
            "command is rejected. Pass arguments matching the command's "
            "schema (see descriptions). Available commands:\n"
            "- open_url(url): open an http(s) URL in the default browser.\n"
            "- show_time(): say the current local time.\n"
            "- show_date(): say today's date.\n"
            "- volume(direction): direction ∈ {up, down, mute, unmute} (macOS only).\n"
            "- music(action): action ∈ {play, pause, next, previous} (macOS only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": command_names},
                "args": {
                    "type": "object",
                    "description": (
                        "Arguments dict for the command. {} if the command "
                        "takes no parameters."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    }


class Brain:
    """Conversation manager + agentic tool loop around Claude Haiku."""

    def __init__(self) -> None:
        self.client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()  # FastAPI runs handlers in a threadpool

        self._tools: list[dict[str, Any]] = [
            _system_command_tool(),
            # Built-in Anthropic web search. Free tier on Haiku is generous.
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
        ]

    # -- Public API -------------------------------------------------------- #

    def reply(self, session_id: str, user_text: str) -> str:
        """Return Claude's spoken-ready reply to ``user_text``."""
        user_text = user_text.strip()[: settings.MAX_INPUT_LENGTH]
        if not user_text:
            return "I didn't catch that."

        with self._lock:
            history = self._histories.setdefault(session_id, [])
            history.append({"role": "user", "content": user_text})
            try:
                final_text = self._run_tool_loop(history)
            except Exception as exc:  # noqa: BLE001 — surface to user
                # Roll back the user turn so a retry doesn't double it up.
                history.pop()
                return f"Sorry — something went wrong contacting Claude: {exc}"

            history.append({"role": "assistant", "content": final_text})
            self._trim(history)
            return final_text

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._histories.pop(session_id, None)

    # -- Internals --------------------------------------------------------- #

    def _trim(self, history: list[dict[str, Any]]) -> None:
        # Each turn is user+assistant; keep the last N pairs.
        max_messages = settings.MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            del history[: len(history) - max_messages]

    def _run_tool_loop(self, history: list[dict[str, Any]]) -> str:
        """Manual agentic loop: call Claude, run any tools, feed results back."""
        for _ in range(8):  # generous bound; tools are cheap
            resp: Message = self.client.messages.create(
                model=settings.MODEL,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": settings.SYSTEM_PROMPT,
                        # Cache the (static) system prompt + tool list. Tools
                        # render before messages, so this breakpoint covers
                        # both. Saves ~90% on prompt tokens after the first
                        # call in a session.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=self._tools,
                messages=history,
            )

            # Stop conditions: normal end_turn, or pause_turn (server-side
            # tool wants another round-trip — just resend with the assistant
            # turn appended).
            if resp.stop_reason in ("end_turn", "stop_sequence", "max_tokens"):
                history.append({"role": "assistant", "content": resp.content})
                return _join_text(resp)

            if resp.stop_reason == "pause_turn":
                history.append({"role": "assistant", "content": resp.content})
                continue

            if resp.stop_reason == "tool_use":
                history.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "system_command":
                        result, is_error = self._exec_system_command(block.input)
                    else:
                        result = f"Unknown tool {block.name!r}."
                        is_error = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        }
                    )
                history.append({"role": "user", "content": tool_results})
                continue

            # Refusal or anything else unexpected → stop gracefully.
            history.append({"role": "assistant", "content": resp.content})
            return _join_text(resp) or "I can't help with that."

        return "I'm spinning on tool calls — try rephrasing."

    def _exec_system_command(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch a `system_command` tool_use to the whitelist."""
        command = (tool_input or {}).get("command", "")
        args = (tool_input or {}).get("args") or {}
        if not isinstance(args, dict):
            return ("`args` must be an object.", True)
        try:
            return (command_guard.execute(command, args), False)
        except ValueError as exc:
            return (str(exc), True)


def _join_text(resp: Message) -> str:
    """Concatenate every text block in the response — ignore tool_use blocks."""
    return "".join(
        block.text for block in resp.content if block.type == "text"
    ).strip()
