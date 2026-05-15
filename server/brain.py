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
from .mac_control import dispatcher as mac_dispatcher
from .mac_control import permission_manager as mac_pm
from .mac_control.permission_manager import Tier as MacTier


def _mac_action_tool() -> dict[str, Any]:
    """Single tool covering every registered mac_control action.

    Claude sees the full action list as an enum and a per-tier
    behaviour summary so it knows when to expect a PENDING response.
    Returning PENDING is fine — Claude should describe the pending
    action to the user and wait for them to confirm; on the next turn,
    Claude calls ``confirm_action`` (Tier 3) or the user authorises in
    the web UI (Tier 4).
    """
    actions = sorted(a.name for a in mac_pm.all_actions())
    by_tier: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    for a in mac_pm.all_actions():
        by_tier[int(a.tier)].append(a.name)
    tier_lines = "\n".join(
        f"  Tier {t}: {', '.join(sorted(names))}"
        for t, names in sorted(by_tier.items()) if names
    )
    return {
        "name": "mac_action",
        "description": (
            "Run a macOS automation action. Each action has a fixed permission "
            "tier — the caller cannot change it.\n\n"
            f"{tier_lines}\n\n"
            "Behaviour by tier:\n"
            "  Tier 1 INFO  — read-only, runs inline.\n"
            "  Tier 2 APPS  — first call per session returns PENDING; once the "
            "user confirms it once, subsequent Tier-2 calls run inline.\n"
            "  Tier 3 FILES — every call returns PENDING; you must then call "
            "confirm_action(id, approve=True) after the user agrees.\n"
            "  Tier 4 SYSTEM — every call returns PENDING; the user authorises "
            "in the web UI (password entry). You DO NOT call confirm_action "
            "for Tier 4 — tell the user to open the web UI and confirm there.\n\n"
            "Params per action are passed as a dict. Common signatures:\n"
            "  get_weather(city)\n"
            "  music_transport(player='Spotify'|'Music', action='play'|'pause'|'next'|'previous')\n"
            "  open_url(url)\n"
            "  set_volume(level=0..100)\n"
            "  send_notification(title, body)\n"
            "  create_note(title, body)              # writes a note into Apple Notes.app\n"
            "  open_app(name)  # allowlist: Music, Spotify, Safari, Google Chrome, Terminal, Visual Studio Code, Finder, Notes\n"
            "  list_dir(path), read_file(path), create_file(path, content),\n"
            "  create_dir(path), rename(path, new_name), move(src, dst), trash(path)\n"
            "    — paths are sandboxed to ~/Desktop, ~/Downloads, ~/Documents.\n"
            "  terminal(command, args)  # command ∈ {say, caffeinate, display_sleep, mac_sleep}\n"
            "  install_app(pkg), uninstall_app(pkg)  # brew package names\n"
            "  open_prefs_pane(pane), screenshot(),\n"
            "  email_preview(to, subject, body), calendar_create(title, start, end)\n"
            "  list_allowed_apps()                   # Tier 1, no params\n"
            "  add_allowed_app(name), remove_allowed_app(name)\n"
            "    — extend / shrink the persistent app allowlist. Tier 4.\n"
            "    Cannot override the hard-coded BLOCKED_APPS list.\n"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": actions},
                "params": {
                    "type": "object",
                    "description": "Arguments dict for the action. {} if none.",
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }


def _confirm_action_tool() -> dict[str, Any]:
    """Confirm or deny a pending Tier-3 action.

    Tier 4 is intentionally NOT supported here — its password must be
    typed by the user in the web UI, never relayed through chat.
    """
    return {
        "name": "confirm_action",
        "description": (
            "Confirm or deny a pending Tier-3 action returned by mac_action. "
            "Call this when the user says yes/ja/ok or no/nein/cancel in "
            "response to your confirmation prompt. The pending id you pass "
            "must come from a previous mac_action tool_result.\n\n"
            "DO NOT use this for Tier-4 actions. Tier 4 requires the user to "
            "enter the JARVIS password in the web UI — if a Tier-4 action "
            "is pending, instruct the user to open the web UI and confirm there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "pending_id from a prior mac_action result"},
                "approve": {"type": "boolean", "description": "True if user agreed, False if denied"},
            },
            "required": ["id", "approve"],
            "additionalProperties": False,
        },
    }


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
            "- music(action, query?): control Spotify on macOS. "
            "action ∈ {play, pause, next, previous, play_track, play_playlist}. "
            "For 'play_track' pass query=<song title> "
            "(e.g. query='Bohemian Rhapsody Queen'). "
            "For 'play_playlist' pass query=<playlist name> "
            "(e.g. query='chill vibes'). "
            "For the transport actions (play/pause/next/previous) omit query. "
            "Use these whenever the user asks to play music, switch songs, "
            "or play a specific track/playlist — Spotify launches automatically."
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
        # mac_control tools are only exposed when explicitly enabled —
        # they expose a large action surface that we don't want live in
        # text-only deployments.
        if settings.MAC_CONTROL_ENABLED:
            self._tools.extend([_mac_action_tool(), _confirm_action_tool()])

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
                    elif block.name == "mac_action":
                        result, is_error = self._exec_mac_action(block.input)
                    elif block.name == "confirm_action":
                        result, is_error = self._exec_confirm_action(block.input)
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

    def _exec_mac_action(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch a `mac_action` tool_use through the staged-permission
        dispatcher and serialise the envelope as JSON so Claude can read
        the ``status`` field and decide what to tell the user."""
        action_name = (tool_input or {}).get("action", "")
        params = (tool_input or {}).get("params") or {}
        if not isinstance(params, dict):
            return ("`params` must be an object.", True)
        envelope = mac_dispatcher.dispatch(action_name, params)
        is_error = envelope.get("status") == "rejected"
        return (json.dumps(envelope, ensure_ascii=False), is_error)

    def _exec_confirm_action(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Confirm or cancel a Tier-3 pending. Tier 4 is refused here —
        the password must come from the web UI, never via chat."""
        pid = (tool_input or {}).get("id", "")
        approve = bool((tool_input or {}).get("approve", False))
        if not isinstance(pid, str) or not pid:
            return ("`id` is required and must be a string.", True)
        # Inspect first so we can refuse Tier-4 before consuming.
        from .mac_control import confirmation as _cf
        peek = _cf.peek(pid)
        if peek is None:
            return (json.dumps({"status": "rejected",
                                "reason": "Pending id unknown or expired."}),
                    True)
        if peek.requires_password:
            return (json.dumps({
                "status": "rejected",
                "tier": peek.tier,
                "reason": ("Tier 4 cannot be confirmed via chat. Ask the user "
                          "to enter the JARVIS password in the web UI."),
            }), True)
        envelope = (mac_dispatcher.consume(pid) if approve
                    else mac_dispatcher.cancel(pid))
        is_error = envelope.get("status") == "rejected"
        return (json.dumps(envelope, ensure_ascii=False), is_error)


def _join_text(resp: Message) -> str:
    """Concatenate every text block in the response — ignore tool_use blocks."""
    return "".join(
        block.text for block in resp.content if block.type == "text"
    ).strip()
