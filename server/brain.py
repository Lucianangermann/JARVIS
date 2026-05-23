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
import re
import threading
from typing import Any

from anthropic import Anthropic
from anthropic.types import Message

from . import command_guard
from .config import settings
from .mac_control import dispatcher as mac_dispatcher
from .mac_control import permission_manager as mac_pm
from .mac_control.permission_manager import Tier as MacTier
from .memory import MemoryManager


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
            "  create_note(title, body)              # writes a new note into Apple Notes.app\n"
            "  edit_note(title, body, mode?)         # edit existing note. Matches title\n"
            "    exact-first then by `contains`. mode ∈ {replace (default), append, prepend}.\n"
            "    Use append/prepend when user says 'add to my note X', use replace when\n"
            "    they say 'change the content of X to Y'.\n"
            "  create_reminder(title, body?, due?, list?)\n"
            "    — creates a reminder in Apple Reminders.app. due is ISO 8601\n"
            "    (YYYY-MM-DDTHH:MM). list selects a specific Reminders list;\n"
            "    omit to use the default list.\n"
            "  open_app(name)                        # any installed macOS app.\n"
            "    Use this for plain 'launch X' requests — it's a single fast call,\n"
            "    handles localised names (Notizen→Notes, Erinnerungen→Reminders),\n"
            "    and fails cleanly if the app isn't installed. DO NOT also call\n"
            "    run_applescript('tell application X to activate') — that's\n"
            "    redundant and triggers a second Tier-4 password prompt.\n"
            "  close_app(name, force?)               # quit an app. force=True\n"
            "    uses SIGKILL instead of the polite Quit event — only use that\n"
            "    when the user explicitly asks for force or the polite quit\n"
            "    already failed. Honours the same German aliases as open_app.\n"
            "  run_applescript(script)               # Tier 4, password each time.\n"
            "    Execute arbitrary AppleScript — full operate-any-app capability\n"
            "    via macOS' scripting bridge, or use System Events for keystrokes\n"
            "    and clicks on apps that lack a scripting dictionary. Output is\n"
            "    capped at 1000 chars in the return value. Only use this when\n"
            "    open_app or a more specific action (create_note, music_transport,\n"
            "    …) doesn't fit.\n"
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


# Trigger phrases that route to an intelligence-layer routine
# assembler instead of through Claude. Kept short + literal so they
# can't easily fire from a normal sentence. Free-form variants
# ("kannst du mir kurz das Briefing geben") fall through to Claude
# which can route them via a future briefing tool if we add one.
#
# Map → routine name accepted by IntelligenceManager.run_routine.
_BRIEFING_TRIGGERS: dict[str, str] = {
    # ── morning briefing (default ─ "briefing" without qualifier) ──
    "briefing":                "morning",
    "brief mich":              "morning",
    "morgenbriefing":          "morning",
    "morgen-briefing":         "morning",
    "morgens-briefing":        "morning",
    "tagesbriefing":           "morning",
    "morning briefing":        "morning",
    "guten morgen jarvis":     "morning",
    "was steht an":            "morning",
    "was steht heute an":      "morning",
    # ── work-start ────────────────────────────────────────────────
    "arbeitsstart":            "work_start",
    "arbeitsstart-briefing":   "work_start",
    "work start":              "work_start",
    "work start briefing":     "work_start",
    "los geht's":              "work_start",
    # ── lunch ────────────────────────────────────────────────────
    "mittagsbriefing":         "lunch",
    "mittagspause":            "lunch",
    "lunch briefing":          "lunch",
    # ── evening ──────────────────────────────────────────────────
    "abendbriefing":           "evening",
    "feierabend-briefing":     "evening",
    "feierabend":              "evening",
    "evening briefing":        "evening",
    "tagesabschluss":          "evening",
}


def _briefing_routine_for(text: str) -> str | None:
    """Return the intelligence routine name (eg ``"morning"``) for a
    trigger phrase, or None if the text isn't a known trigger."""
    return _BRIEFING_TRIGGERS.get(text.lower().strip().strip(".!?,").strip())


# Vision short-circuits. Like the briefing map above: each key is a
# normalised lowercase phrase (no trailing punctuation), each value
# is one of the action keys handled by ``_run_vision_action`` below.
# The set is deliberately tight — fuzzy matches fall through to
# Claude, which can call vision_tools.* via tool_use if it wants.
_VISION_TRIGGERS: dict[str, str] = {
    # ── screen describe ──────────────────────────────────────────
    "was ist auf meinem bildschirm":   "screen_describe",
    "was siehst du":                   "screen_describe",
    "was siehst du auf meinem bildschirm": "screen_describe",
    "was ist auf dem bildschirm":      "screen_describe",
    "beschreibe meinen bildschirm":    "screen_describe",
    "what's on my screen":             "screen_describe",
    "describe my screen":              "screen_describe",
    # ── error / problem ──────────────────────────────────────────
    "was ist das problem":             "screen_error",
    "gibt es einen fehler":            "screen_error",
    "siehst du einen fehler":          "screen_error",
    "what's wrong":                    "screen_error",
    "any errors":                      "screen_error",
    # ── read / OCR via screen ────────────────────────────────────
    "lies das":                        "screen_read",
    "lies das mal":                    "screen_read",
    "lies das vor":                    "screen_read",
    "read this":                       "screen_read",
    "read the screen":                 "screen_read",
    # ── code explanation ─────────────────────────────────────────
    "erkläre diesen code":             "screen_code",
    "erkläre den code":                "screen_code",
    "was macht der code":              "screen_code",
    "explain this code":               "screen_code",
    "explain the code":                "screen_code",
    # ── snapshot for later compare ────────────────────────────────
    "merk dir den bildschirm":         "screen_snapshot",
    "speicher den bildschirm":         "screen_snapshot",
    "remember this screen":            "screen_snapshot",
    # ── compare with last snapshot ───────────────────────────────
    "was hat sich verändert":          "screen_compare",
    "was hat sich geändert":           "screen_compare",
    "was ist anders":                  "screen_compare",
    "what changed":                    "screen_compare",
    # ── camera ───────────────────────────────────────────────────
    "ist jemand da":                   "camera_snapshot",
    "guck mal nach":                   "camera_snapshot",
    "schau in die kamera":             "camera_snapshot",
    "is anyone there":                 "camera_snapshot",
    # ── motion monitor ───────────────────────────────────────────
    "beobachte die tür":               "motion_start",
    "beobachte die tuer":              "motion_start",
    "überwache die kamera":            "motion_start",
    "watch the door":                  "motion_start",
    "start watching":                  "motion_start",
    "hör auf zu beobachten":           "motion_stop",
    "hoer auf zu beobachten":          "motion_stop",
    "stop watching":                   "motion_stop",
}


def _vision_action_for(text: str) -> str | None:
    """Return the vision action key for a trigger phrase, or None if
    the text isn't a known trigger. Same normalisation as the briefing
    matcher so ``"Lies das."`` works."""
    return _VISION_TRIGGERS.get(text.lower().strip().strip(".!?,").strip())


def _apple_tools() -> list[dict[str, Any]]:
    """Tool schemas for macOS app control, Apple apps, and Safari."""
    return [
        {
            "name": "macos_app",
            "description": (
                "Open or close any macOS application, list currently running apps, "
                "or approve a new third-party app with the user's password. "
                "Apple first-party apps (Calendar, Mail, Music, Notes, Safari, "
                "Reminders, Messages, etc.) are always allowed. Third-party apps "
                "require password approval before first use — call action='approve' "
                "with the password the user just provided. Once approved, the app "
                "is remembered permanently."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "close", "list_running", "approve", "revoke"],
                        "description": "open/close an app, list running apps, or approve/revoke a third-party app",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Exact macOS application name (e.g. 'Spotify', 'Notion'). Required for open/close/approve/revoke.",
                    },
                    "password": {
                        "type": "string",
                        "description": "User's JARVIS app password. Required only for action='approve'.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "apple_reminders",
            "description": (
                "Read and manage Apple Reminders. List open reminders (optionally "
                "filtered by list), create new reminders, and mark them complete."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "list_lists", "create", "complete"],
                    },
                    "title": {"type": "string", "description": "Reminder title (for create/complete)"},
                    "list_name": {"type": "string", "description": "Reminders list name (optional filter)"},
                    "due_date": {"type": "string", "description": "ISO datetime string e.g. '2026-05-25T09:00' (optional, for create)"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "apple_music",
            "description": (
                "Control Apple Music: play, pause, skip tracks, set volume, "
                "search and play by song/artist name, toggle shuffle, get current track."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous", "current",
                                 "volume", "play_by_name", "shuffle_on", "shuffle_off", "state"],
                    },
                    "query": {"type": "string", "description": "Search query for play_by_name"},
                    "level": {"type": "integer", "description": "Volume 0–100 (for action='volume')"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "apple_notes",
            "description": (
                "Read and create Apple Notes. List notes, read a note by title, "
                "create a new note, search note content, or append text to an existing note."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "create", "search", "append"],
                    },
                    "title": {"type": "string", "description": "Note title (for read/create/append)"},
                    "content": {"type": "string", "description": "Note body (for create/append)"},
                    "query": {"type": "string", "description": "Search term (for search)"},
                    "folder": {"type": "string", "description": "Notes folder name (optional)"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "apple_mail",
            "description": (
                "Read and send emails via Apple Mail. List unread messages, "
                "read a specific message by subject fragment, send an email, "
                "or get the unread count."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_unread", "read", "send", "unread_count"],
                    },
                    "to": {"type": "string", "description": "Recipient email address (for send)"},
                    "subject": {"type": "string", "description": "Email subject (for send or read filter)"},
                    "body": {"type": "string", "description": "Email body text (for send)"},
                    "mailbox": {"type": "string", "description": "Mailbox name, default 'INBOX'"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "safari_control",
            "description": (
                "Control Safari: open a URL, search the web (opens DuckDuckGo), "
                "read the current page title/URL/text, navigate back/forward, "
                "or open a new tab."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open_url", "search", "current_url", "current_title",
                                 "read_page", "back", "forward", "new_tab"],
                    },
                    "url": {"type": "string", "description": "URL to open (for open_url/new_tab)"},
                    "query": {"type": "string", "description": "Search query (for action='search')"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    ]


class Brain:
    """Conversation manager + agentic tool loop around Claude Haiku."""

    def __init__(self) -> None:
        self.client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()  # FastAPI runs handlers in a threadpool

        # Optional intelligence layer. The server wires this in
        # main.py's lifespan; brain works fine with it set to None
        # (no briefing short-circuit, no context injection).
        self.intelligence = None  # type: ignore[assignment]

        # Optional vision layer (Phase 5 of the slice plan). Same
        # pattern as intelligence: lifespan attaches it after init,
        # every call-site None-guards. Set to None means the trigger
        # short-circuit below falls through to Claude.
        self.vision = None  # type: ignore[assignment]

        # Optional smart home layer. Attached by main.py's lifespan
        # after SmartHomeManager.start() completes. None = no devices.
        self.smarthome = None  # type: ignore[assignment]

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

        # Vision tools (Phase 4 of the vision slice). Registered
        # unconditionally — when ``self.vision`` is None at runtime
        # (deps missing, init failed) the dispatcher returns a clean
        # error string so Claude can still finish its reply gracefully
        # instead of crashing the turn.
        from .tools.vision_tools import vision_tools
        self._tools.extend(vision_tools())

        # Smart Home tool. Registered unconditionally; the dispatcher
        # returns a clean error when smarthome is None so Claude can
        # still finish the turn gracefully.
        from .smarthome.tools.smarthome_tools import smarthome_tool
        self._tools.append(smarthome_tool())

        # macOS app control + Apple app integrations (Calendar, Reminders,
        # Music, Notes, Mail, Safari). Registered unconditionally.
        self._tools.extend(_apple_tools())

        # Long-term memory + self-learning. Owns short-term history (was
        # _histories), error history, profile, and the dynamic system-
        # prompt builder. Falls back to in-memory only if storage can't
        # be opened — the brain keeps working either way.
        self.memory = MemoryManager()
        self._started_sessions: set[str] = set()

    # -- Public API -------------------------------------------------------- #

    def reply(self, session_id: str, user_text: str,
              *, speak_locally: bool = True) -> str:
        """Return Claude's spoken-ready reply to ``user_text``.

        ``speak_locally``: if False, suppress the per-sentence
        ``tts_ref.speak()`` calls that would otherwise pipe audio
        through the Mac's speakers. The streaming HUD events
        (``jarvis_partial`` / ``jarvis_reply``) still fire, so remote
        clients (PWA, etc.) can do their own playback. voice_loop —
        the path triggered by the local wake word — keeps the default
        (True) so the user gets a voice answer when speaking to the
        Mac directly.
        """
        user_text = user_text.strip()[: settings.MAX_INPUT_LENGTH]
        if not user_text:
            return "I didn't catch that."

        # Slice 3: feed every turn into the ContextEngine so it can
        # track command frequency, recency, and intent keywords —
        # used to populate activity / stress / style hints in the
        # next prompt build. Done BEFORE the briefing short-circuit
        # so even briefing requests count toward "user is interacting".
        if self.intelligence is not None:
            try:
                self.intelligence.record_command(user_text)
            except Exception:  # noqa: BLE001 — never crash on telemetry
                pass

        # Briefing short-circuit: if the user typed/said one of the
        # known trigger phrases, hand the matching routine's output
        # back directly instead of routing through Claude. Saves a
        # full API round-trip and keeps the response deterministic
        # — the briefing is already polished spoken text.
        if self.intelligence is not None:
            routine_name = _briefing_routine_for(user_text)
            if routine_name is not None:
                text = self.intelligence.run_routine(routine_name)
                if text:
                    # The streaming-Claude path speaks each sentence via
                    # flush_sentence() as it's assembled; voice_loop
                    # therefore assumes by the time reply() returns the
                    # audio is already in flight and does NOT call
                    # tts.speak() on the return value. The briefing
                    # short-circuit skips that streaming path entirely,
                    # so without this block the briefing comes back as
                    # text-only and the Mac stays silent. We also emit a
                    # jarvis_partial event so the HUD can render the
                    # text bubble the same way it does for normal replies.
                    try:
                        from . import events as _events
                        _events.publish({"type": "jarvis_partial", "text": text})
                    except Exception:  # noqa: BLE001
                        pass
                    if speak_locally:
                        try:
                            from . import voice_loop as _vl
                            tts_ref = getattr(_vl, "_tts_ref", None)
                            if tts_ref is not None:
                                tts_ref.speak(text)
                        except Exception:  # noqa: BLE001
                            pass
                    return text
                # Unknown routine or assembly failure — fall through
                # to Claude rather than returning empty/error string.

        # Vision short-circuit. Same pattern as the briefing block:
        # tight trigger-phrase match, route to a vision call, deliver
        # the reply through the same partial+TTS plumbing the
        # streaming Claude path uses so voice_loop doesn't go silent.
        # Free-form vision questions (eg "siehst du was am Fenster")
        # fall through to Claude, which can call the vision_tools
        # below if appropriate.
        if self.vision is not None:
            action = _vision_action_for(user_text)
            if action is not None:
                text = self._run_vision_action(action)
                if text:
                    self._emit_short_circuit_reply(text, speak_locally)
                    return text
                # No usable result (capture failed, deps missing, …)
                # — fall through to Claude rather than returning a
                # bare error string to the user.

        # Clear any leftover /interrupt flag from a previous turn.
        # voice_loop's wake-word path already clears it before
        # spawning its brain thread; the WS (PWA) path didn't, so a
        # STOP press would leave brain_cancel set and every subsequent
        # WS reply would short-circuit to an empty string on entry.
        # Doing it once here covers both call sites.
        try:
            from . import voice_loop as _vl
            ev = getattr(_vl, "_brain_cancel_ref", None)
            if ev is not None and ev.is_set():
                ev.clear()
        except Exception:  # noqa: BLE001 — voice loop not running, ignore
            pass

        with self._lock:
            # First message of a session triggers the memory warmup
            # (bumps session counter, semantic-searches the user's
            # query against past sessions, primes the prompt cache).
            if session_id not in self._started_sessions:
                self.memory.session_start(session_id, warmup_query=user_text)
                self._started_sessions.add(session_id)
            # Append to short-term + rebuild the system blocks. The
            # returned prompt isn't actually used here (we re-fetch
            # blocks inside the tool loop), but the side-effect of
            # short-term.add is required so the next iteration sees
            # the user turn.
            self.memory.before_message(session_id, user_text)

            history = self._histories.setdefault(session_id, [])
            history.append({"role": "user", "content": user_text})
            try:
                final_text = self._run_tool_loop(history, session_id=session_id,
                                                  user_text=user_text,
                                                  speak_locally=speak_locally)
            except Exception as exc:  # noqa: BLE001 — surface to user
                # Roll back the user turn so a retry doesn't double it up.
                history.pop()
                return f"Sorry — something went wrong contacting Claude: {exc}"

            # If a /interrupt fired during streaming the reply we
            # have here is a fragment — don't pollute conversation
            # history or memory with it. _brain_work / the WS caller
            # already discards the return path via brain_cancel.
            if self._cancel_check():
                return final_text
            history.append({"role": "assistant", "content": final_text})
            self._trim(history)
            # Fact-extraction + learning hooks. Done outside the model
            # call's critical path so memory writes can't block latency.
            try:
                self.memory.after_message(session_id, user_text, final_text)
            except Exception:  # noqa: BLE001
                pass  # memory layer already logs its own failures
            return final_text

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._histories.pop(session_id, None)
            # Flush short-term + summary so the session_end hooks
            # actually persist the conversation to long-term memory
            # instead of being silently dropped. memory.session_end
            # is best-effort + logged on failure.
            try:
                self.memory.session_end(session_id)
            except Exception:  # noqa: BLE001
                pass
            self._started_sessions.discard(session_id)

    # -- Internals --------------------------------------------------------- #

    def _trim(self, history: list[dict[str, Any]]) -> None:
        # Each turn is user+assistant; keep the last N pairs.
        max_messages = settings.MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            del history[: len(history) - max_messages]

    def _run_tool_loop(self, history: list[dict[str, Any]],
                       *, session_id: str = "",
                       user_text: str = "",
                       speak_locally: bool = True) -> str:
        """Manual agentic loop: call Claude, run any tools, feed results back.

        ``user_text`` is the current user turn — drives the memory
        layer's semantic search for the per-turn "Relevant Past
        Context" block in the system prompt.

        Streaming: each per-iteration call uses ``messages.stream()``
        so text deltas reach the TTS queue + HUD as they arrive,
        instead of after the whole turn lands. The existing
        ``stop_reason`` switch downstream is fed the final message
        from ``stream.get_final_message()`` so the tool_use branch is
        unchanged. tts.speak() is queue-based and feeds into the
        Speex AEC via voice_loop's full-duplex callback — we
        deliberately do NOT shell out to /usr/bin/say because that
        would bypass AEC and the mic would re-ingest JARVIS' own
        voice (the same failure mode that killed barge-in).
        """
        for _ in range(8):  # generous bound; tools are cheap
            # Cancel-aware: if a /interrupt fired between turns we
            # don't want to start a new Claude call. Cheap check.
            if self._cancel_check():
                return ""
            # Build a fresh system message each iteration. The cached
            # prefix (base + profile + known issues + instructions)
            # stays byte-stable so Anthropic's prompt cache hits;
            # only the dynamic suffix (recent activity + relevant past
            # + current date/time) is regenerated per call.
            system_blocks = self.memory.build_system_blocks(user_text)

            # Intelligence-layer context (local time, next calendar
            # event, …) goes in its own trailing text block so it
            # stays OUTSIDE the cache-control breakpoint on block 1.
            # If it shared a block with the stable prefix, every turn
            # would invalidate the prompt cache; if it shared the
            # dynamic block from memory, the memory layer would have
            # to know about intelligence, which we want to avoid.
            if self.intelligence is not None:
                try:
                    intel_ctx = self.intelligence.get_context_for_brain()
                except Exception as exc:  # noqa: BLE001
                    intel_ctx = ""
                    print(f"[brain] intelligence context failed: {exc}")
                if intel_ctx:
                    system_blocks = system_blocks + [
                        {"type": "text", "text": intel_ctx},
                    ]

            resp = self._stream_one_turn(history, system_blocks,
                                          speak_locally=speak_locally)

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
                    elif block.name in {"analyze_screen",
                                         "check_screen_for_errors",
                                         "read_screen_text"}:
                        result, is_error = self._exec_vision_tool(
                            block.name, block.input,
                        )
                    elif block.name == "smarthome_control":
                        result, is_error = self._exec_smarthome_tool(block.input)
                    elif block.name == "macos_app":
                        result, is_error = self._exec_macos_app(block.input)
                    elif block.name == "apple_reminders":
                        result, is_error = self._exec_apple_reminders(block.input)
                    elif block.name == "apple_music":
                        result, is_error = self._exec_apple_music(block.input)
                    elif block.name == "apple_notes":
                        result, is_error = self._exec_apple_notes(block.input)
                    elif block.name == "apple_mail":
                        result, is_error = self._exec_apple_mail(block.input)
                    elif block.name == "safari_control":
                        result, is_error = self._exec_safari_control(block.input)
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
                    # Persist the outcome to memory so future turns
                    # can semantic-search past attempts + the error
                    # memory can promote a working fix. We log a
                    # human-readable command string (tool name +
                    # input summary) rather than the raw block so
                    # ChromaDB embeddings stay meaningful.
                    self._record_tool_result(block, result, is_error)
                history.append({"role": "user", "content": tool_results})
                continue

            # Refusal or anything else unexpected → stop gracefully.
            history.append({"role": "assistant", "content": resp.content})
            return _join_text(resp) or "I can't help with that."

        return "I'm spinning on tool calls — try rephrasing."

    def _cancel_check(self) -> bool:
        """True iff a /interrupt is currently armed. Reads through to
        voice_loop's module-level cancel Event so the brain stays
        loosely coupled (no direct import dependency in __init__)."""
        try:
            from . import voice_loop as _vl
        except Exception:  # noqa: BLE001
            return False
        ev = getattr(_vl, "_brain_cancel_ref", None)
        return ev is not None and ev.is_set()

    # Sentence-end punctuation. The detector looks at the rstripped
    # buffer's trailing char — "..." is treated as a single boundary
    # because the regex below normalises consecutive dots.
    _SENTENCE_ENDERS = frozenset(".!?:")
    # Minimum sentence length below which we don't ship to TTS yet —
    # avoids speaking fragments like "Ja." or stray numbered list
    # entries ("1.") before the next clause arrives.
    _MIN_SPEAKABLE_LEN = 4

    # ── short-circuit helpers shared by briefing + vision paths ────── #

    def _emit_short_circuit_reply(
        self, text: str, speak_locally: bool,
    ) -> None:
        """Deliver a non-streaming reply (briefing, vision result, …)
        through the same HUD+TTS pair the streaming Claude path uses.

        Why: ``flush_sentence()`` inside _stream_one_turn() does the
        per-sentence TTS + jarvis_partial publish for normal replies,
        so voice_loop assumes by the time reply() returns the audio is
        already in flight and never calls tts.speak() on the return
        value. Short-circuits skip that streaming path entirely, so
        without this helper they come back text-only and the Mac
        stays silent. Mirroring the two side-effects keeps short-
        circuit replies indistinguishable from Claude-streamed ones
        as far as the rest of the stack is concerned.
        """
        try:
            from . import events as _events
            _events.publish({"type": "jarvis_partial", "text": text})
        except Exception:  # noqa: BLE001
            pass
        if speak_locally:
            try:
                from . import voice_loop as _vl
                tts_ref = getattr(_vl, "_tts_ref", None)
                if tts_ref is not None:
                    tts_ref.speak(text)
            except Exception:  # noqa: BLE001
                pass

    def _run_vision_action(self, action: str) -> str | None:
        """Execute one vision short-circuit action and return a German
        speakable reply, or None if the action couldn't produce useful
        output (failed capture, deps missing, no snapshot stored).

        The map below is intentionally lightweight — each entry is the
        smallest amount of glue between the trigger phrase and the
        underlying VisionManager subcomponent. Anything richer (custom
        prompts, parameter passing) belongs in the vision_tools.py
        tool_use surface, not here.
        """
        vision = self.vision
        if vision is None:
            return None

        try:
            if action == "screen_describe":
                return vision.screen.analyze_screen("describe")
            if action == "screen_error":
                # detect_error_on_screen is the same call with the
                # "error" preset; using it explicitly so future tweaks
                # (eg auto-suggest a fix) land in one place.
                return vision.screen.detect_error_on_screen()
            if action == "screen_read":
                return vision.screen.analyze_screen("read")
            if action == "screen_code":
                return vision.screen.analyze_screen("code")

            if action == "screen_snapshot":
                ok = vision.comparator.snapshot_screen()
                return ("Bildschirm gespeichert. Sag mir später "
                        "'was hat sich verändert', um zu vergleichen.") \
                    if ok else None
            if action == "screen_compare":
                result = vision.comparator.compare_with_snapshot()
                if result is None:
                    return ("Ich habe keinen gespeicherten Bildschirm "
                            "zum Vergleichen. Sag 'merk dir den "
                            "Bildschirm' und frag später erneut.")
                if not result.differences:
                    return result.summary
                # Trim the bullet list for the speakable reply — the
                # full list still rides along in the comparator's
                # state for debug/inspection.
                bullets = "; ".join(result.differences[:3])
                return f"{result.summary} Konkret: {bullets}."

            if action == "camera_snapshot":
                analysis = vision.motion.capture_once()
                return analysis or None
            if action == "motion_start":
                ok = vision.motion.start()
                return ("Kamera-Überwachung läuft. Ich melde mich, "
                        "wenn ich Bewegung sehe.") \
                    if ok else ("Ich konnte die Kamera nicht starten "
                                "— vermutlich keine Berechtigung oder "
                                "schon in Benutzung.")
            if action == "motion_stop":
                vision.motion.stop()
                return "Kamera-Überwachung beendet."
        except Exception as exc:  # noqa: BLE001
            print(f"[VISION] short-circuit action {action!r} crashed: {exc}")
            return None
        return None

    def _stream_one_turn(self, history: list[dict[str, Any]],
                         system_blocks: list[dict[str, Any]],
                         *, speak_locally: bool = True) -> "Message":
        """Issue one ``messages.stream()`` call, push completed
        sentences to TTS + HUD as text deltas arrive, then return the
        final Message so the existing tool_use / end_turn switch can
        run unmodified.

        Cancellation: every sentence flush checks the cross-thread
        brain_cancel event (set by /interrupt + Cmd+Shift+J). On
        cancel we close the stream early and let the caller see a
        partial response — the caller's existing cancel check will
        discard it."""
        from . import events
        try:
            from . import voice_loop as _vl
        except Exception:  # noqa: BLE001
            _vl = None

        # State for sentence detection. ``flushed_len`` is the offset
        # into ``accumulated`` past which we haven't yet emitted —
        # everything before that is already in flight to TTS.
        accumulated = ""
        flushed_len = 0

        # Inline alias so the inner loop doesn't pay the import cost
        # on every delta.
        cancel_requested = self._cancel_check

        def flush_sentence(text: str) -> None:
            text = text.strip()
            if len(text) < self._MIN_SPEAKABLE_LEN:
                return
            # 1) HUD: incremental display via a typed event.
            try:
                events.publish({"type": "jarvis_partial", "text": text})
            except Exception:  # noqa: BLE001
                pass
            # 2) TTS: only if voice_loop is actually running AND the
            # caller wants local speech. Remote clients (the PWA) set
            # speak_locally=False so the iPhone speaks via Web Speech
            # and the Mac stays silent — otherwise both speakers fire
            # simultaneously, which is what the user hit.
            if speak_locally:
                tts_ref = getattr(_vl, "_tts_ref", None) if _vl is not None else None
                if tts_ref is not None:
                    try:
                        tts_ref.speak(text)
                    except Exception:  # noqa: BLE001
                        pass

        with self.client.messages.stream(
            model=settings.MODEL,
            max_tokens=1024,
            system=system_blocks,
            tools=self._tools,
            messages=history,
        ) as stream:
            for delta in stream.text_stream:
                if cancel_requested():
                    # Stop pulling deltas. The Anthropic SDK aborts
                    # the underlying connection on context exit.
                    break
                if not delta:
                    continue
                accumulated += delta
                # Look for the next sentence boundary past flushed_len.
                # We scan from the back of the buffer for the latest
                # terminator so we batch as much as possible without
                # holding onto the entire stream.
                tail = accumulated[flushed_len:]
                # Find the LAST sentence-end in the new tail so we
                # flush all complete sentences in one go.
                last_idx = -1
                for i, ch in enumerate(tail):
                    if ch in self._SENTENCE_ENDERS:
                        # Avoid flushing on a decimal point: "3.14",
                        # "v1.0". Crude guard: require the previous
                        # char to be non-digit OR followed by space /
                        # end-of-buffer.
                        prev = tail[i - 1] if i > 0 else " "
                        nxt = tail[i + 1] if i + 1 < len(tail) else " "
                        if prev.isdigit() and nxt.isdigit():
                            continue
                        last_idx = i
                if last_idx >= 0:
                    chunk = tail[: last_idx + 1]
                    flushed_len += len(chunk)
                    flush_sentence(chunk)

        # Tail flush: any remaining buffer past the last sentence
        # boundary (the model often ends a turn without a period when
        # it stopped on max_tokens or a stop_sequence).
        if not cancel_requested():
            remainder = accumulated[flushed_len:].strip()
            if remainder:
                flush_sentence(remainder)

        # Hand back the final Message so the caller's stop_reason /
        # tool_use logic stays exactly as before.
        return stream.get_final_message()

    def _record_tool_result(self, block: Any, result: str, is_error: bool) -> None:
        """Forward a tool execution outcome to the memory layer.

        We don't want this in the hot path of the loop, so failures
        are caught + ignored. The recorded "command" is a stable
        text key (tool name + main parameter) — readable enough that
        semantic search can later match similar requests."""
        try:
            tool_name = block.name
            inp = getattr(block, "input", None) or {}
            # Compose a stable string key for memory. The most
            # informative parameter depends on the tool — fall back
            # to the tool name alone if we don't know the shape.
            if tool_name == "mac_action":
                action = inp.get("action", "?")
                command = f"mac_action:{action}"
                category = action.split("_", 1)[0]
            elif tool_name == "system_command":
                cmd = inp.get("command", "?")
                command = f"system_command:{cmd}"
                category = "system"
            elif tool_name == "confirm_action":
                command = "confirm_action"
                category = "confirm"
            else:
                command = tool_name or "tool"
                category = "other"
            if is_error:
                self.memory.record_command_result(
                    command, success=False,
                    error=result if isinstance(result, str) else str(result),
                    category=category,
                )
            else:
                self.memory.record_command_result(
                    command, success=True, category=category,
                )
        except Exception:  # noqa: BLE001 — memory must never break the brain
            pass

    def _exec_vision_tool(
        self, name: str, tool_input: dict[str, Any],
    ) -> tuple[str, bool]:
        """Dispatch a vision tool_use to the VisionManager.

        Returns ``(text, is_error)`` so the surrounding tool loop can
        decide whether to surface the result as content or as an
        error to the model. We deliberately keep the error case
        non-fatal — Claude can still wrap a "I couldn't see the
        screen" reply around it rather than aborting the turn."""
        if self.vision is None:
            return (
                "vision unavailable (deps missing or init failed)",
                True,
            )
        try:
            if name == "analyze_screen":
                question = (tool_input or {}).get("question") or "describe"
                if not isinstance(question, str):
                    return ("`question` must be a string.", True)
                result = self.vision.screen.analyze_screen(question)
            elif name == "check_screen_for_errors":
                result = self.vision.screen.detect_error_on_screen()
            elif name == "read_screen_text":
                result = self.vision.screen.analyze_screen("read")
            else:
                return (f"Unknown vision tool {name!r}.", True)
        except Exception as exc:  # noqa: BLE001
            return (f"vision tool {name!r} crashed: {exc}", True)

        if not result:
            return (
                "vision call returned no result — likely Screen "
                "Recording permission missing or capture failed",
                True,
            )
        return (result, False)

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


    def _exec_smarthome_tool(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch a smarthome_control tool_use to the SmartHomeManager.

        Brain runs in a worker thread (asyncio.to_thread), so we
        schedule the coroutine on the main event loop via
        run_coroutine_threadsafe and wait for the result synchronously.
        Falls back to asyncio.run() if the main loop isn't captured yet
        (e.g. unit tests)."""
        import asyncio as _aio
        from .smarthome.tools.smarthome_tools import execute_smarthome_tool
        from . import events as _events
        inp = tool_input or {}
        try:
            coro = execute_smarthome_tool(
                self.smarthome,
                action=inp.get("action", ""),
                command=inp.get("command"),
                scene=inp.get("scene"),
                device=inp.get("device"),
                level=inp.get("level"),
                color=inp.get("color"),
            )
            main_loop = _events._loop
            if main_loop is not None and main_loop.is_running():
                future = _aio.run_coroutine_threadsafe(coro, main_loop)
                result = future.result(timeout=15)
            else:
                result = _aio.run(coro)
            return (result, False)
        except Exception as exc:  # noqa: BLE001
            return (f"Smart Home Fehler: {exc}", True)


    # ── Apple app executors ────────────────────────────────────────────── #

    def _exec_macos_app(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.app_permissions import is_approved, approve_app, revoke_app
        from .tools.macos_apps import open_app, close_app, list_running
        inp = tool_input or {}
        action = inp.get("action", "")
        app = inp.get("app_name", "")
        if action == "list_running":
            apps = list_running()
            return ", ".join(apps) if apps else "Keine Apps im Vordergrund.", False
        if action == "approve":
            if not app:
                return "app_name ist erforderlich.", True
            pw = inp.get("password", "")
            ok, msg = approve_app(app, pw)
            return msg, not ok
        if action == "revoke":
            if not app:
                return "app_name ist erforderlich.", True
            return revoke_app(app), False
        if not app:
            return "app_name ist erforderlich.", True
        if not is_approved(app):
            return (
                f"'{app}' ist nicht freigegeben. Bitte bestätige mit deinem JARVIS-App-Passwort. "
                f"Rufe dann macos_app mit action='approve', app_name='{app}' und dem Passwort auf.",
                True,
            )
        if action == "open":
            return open_app(app)
        if action == "close":
            return close_app(app)
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_reminders(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.reminders_tool import (
            list_reminders, create_reminder, complete_reminder, list_reminder_lists,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list":
            return list_reminders(inp.get("list_name"))
        if action == "list_lists":
            return list_reminder_lists()
        if action == "create":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return create_reminder(title, inp.get("list_name"), inp.get("due_date"))
        if action == "complete":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return complete_reminder(title, inp.get("list_name"))
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_music(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.music_tool import (
            play, pause, next_track, previous_track, current_track,
            set_volume, play_by_name, toggle_shuffle, player_state,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "play":
            return play()
        if action == "pause":
            return pause()
        if action == "next":
            return next_track()
        if action == "previous":
            return previous_track()
        if action == "current":
            return current_track()
        if action == "state":
            return player_state()
        if action == "volume":
            level = inp.get("level")
            if level is None:
                return "level (0–100) ist erforderlich.", True
            return set_volume(int(level))
        if action == "play_by_name":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return play_by_name(query)
        if action == "shuffle_on":
            return toggle_shuffle(True)
        if action == "shuffle_off":
            return toggle_shuffle(False)
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_notes(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.notes_tool import list_notes, read_note, create_note, search_notes, append_to_note
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list":
            return list_notes(inp.get("folder"))
        if action == "read":
            title = inp.get("title", "")
            if not title:
                return "title ist erforderlich.", True
            return read_note(title)
        if action == "create":
            title = inp.get("title", "")
            content = inp.get("content", "")
            if not title:
                return "title ist erforderlich.", True
            return create_note(title, content, inp.get("folder"))
        if action == "search":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return search_notes(query)
        if action == "append":
            title = inp.get("title", "")
            content = inp.get("content", "")
            if not title or not content:
                return "title und content sind erforderlich.", True
            return append_to_note(title, content)
        return f"Unbekannte Aktion: {action}", True

    def _exec_apple_mail(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.mail_tool import list_unread, read_message, send_message, get_unread_count
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "list_unread":
            return list_unread(inp.get("mailbox", "INBOX"))
        if action == "read":
            subject = inp.get("subject", "")
            if not subject:
                return "subject ist erforderlich.", True
            return read_message(subject)
        if action == "send":
            to = inp.get("to", "")
            subject = inp.get("subject", "")
            body = inp.get("body", "")
            if not to or not subject:
                return "to und subject sind erforderlich.", True
            return send_message(to, subject, body)
        if action == "unread_count":
            return get_unread_count()
        return f"Unbekannte Aktion: {action}", True

    def _exec_safari_control(self, tool_input: dict[str, Any]) -> tuple[str, bool]:
        from .tools.safari_tool import (
            open_url, search_in_safari, current_url, current_title,
            current_page_text, navigate_back, navigate_forward, open_new_tab,
        )
        inp = tool_input or {}
        action = inp.get("action", "")
        if action == "open_url":
            url = inp.get("url", "")
            if not url:
                return "url ist erforderlich.", True
            return open_url(url)
        if action == "search":
            query = inp.get("query", "")
            if not query:
                return "query ist erforderlich.", True
            return search_in_safari(query)
        if action == "current_url":
            return current_url()
        if action == "current_title":
            return current_title()
        if action == "read_page":
            return current_page_text()
        if action == "back":
            return navigate_back()
        if action == "forward":
            return navigate_forward()
        if action == "new_tab":
            return open_new_tab(inp.get("url"))
        return f"Unbekannte Aktion: {action}", True


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_WS = re.compile(r"\s+")


def _dedupe_paragraphs(text: str) -> str:
    """Drop consecutive identical paragraphs.

    Haiku occasionally emits the same sentence twice when uncertain —
    sometimes as two identical text blocks, sometimes as one block with
    the content duplicated and separated by a blank line. The TTS then
    reads it twice. This collapses adjacent paragraphs whose
    whitespace-normalised form matches.
    """
    if not text:
        return text
    paras = _PARAGRAPH_SPLIT.split(text)
    out: list[str] = []
    last_norm: str | None = None
    for p in paras:
        norm = _WS.sub(" ", p).strip().lower()
        if norm and norm == last_norm:
            continue
        out.append(p)
        last_norm = norm
    return "\n\n".join(out)


def _join_text(resp: Message) -> str:
    """Concatenate every text block in the response — ignore tool_use blocks.

    Multiple text blocks are joined with a paragraph break so the dedup
    pass can recognise identical adjacent blocks (otherwise "X" + "X"
    becomes "XX" and looks like a single weird sentence)."""
    blocks = [b.text for b in resp.content if b.type == "text"]
    joined = "\n\n".join(b.strip() for b in blocks if b.strip())
    return _dedupe_paragraphs(joined).strip()
