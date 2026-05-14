"""Whitelisted system command runner.

The brain may ask to run a "system command" via tool use. Instead of letting
Claude pick an arbitrary shell string, every action is keyed by a short name
(e.g. ``open_url``) that maps to a hand-written, parameter-validated handler.

Anything not in ALLOWED_COMMANDS is rejected and logged to
``logs/rejected.log`` so we can audit attempted abuse.
"""
from __future__ import annotations

import datetime as _dt
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Callable
from urllib.parse import urlparse

from .config import settings

# A safe URL: http/https only, no shell-meta chars.
_URL_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")


# --- Command handlers ------------------------------------------------------ #

def _open_url(url: str) -> str:
    if not _URL_RE.match(url):
        raise ValueError("URL contains invalid characters.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http(s) URLs are allowed.")
    if not parsed.netloc:
        raise ValueError("URL missing host.")

    system = platform.system()
    if system == "Darwin":
        opener = ["open", url]
    elif system == "Windows":
        # `start` is a cmd builtin, not an exe — must go through cmd /c.
        opener = ["cmd", "/c", "start", "", url]
    else:
        opener = ["xdg-open", url]

    subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opened {url} in the default browser."


def _show_time() -> str:
    return _dt.datetime.now().strftime("It's %H:%M on %A, %B %d.")


def _show_date() -> str:
    return _dt.datetime.now().strftime("Today is %A, %B %d, %Y.")


def _volume(direction: str) -> str:
    """Tweak system volume on macOS (AppleScript) — silently noops elsewhere."""
    if platform.system() != "Darwin":
        return "Volume control is only wired up for macOS in this build."
    if direction not in {"up", "down", "mute", "unmute"}:
        raise ValueError("direction must be up|down|mute|unmute")

    if direction == "mute":
        script = "set volume with output muted"
    elif direction == "unmute":
        script = "set volume without output muted"
    else:
        delta = 10 if direction == "up" else -10
        script = (
            "set v to output volume of (get volume settings)\n"
            f"set volume output volume (v + {delta})"
        )
    subprocess.run(["osascript", "-e", script], check=False)
    return f"Volume {direction}."


def _music(action: str) -> str:
    """Play / pause the system music app on macOS."""
    if platform.system() != "Darwin":
        return "Music control is only wired up for macOS in this build."
    if action not in {"play", "pause", "next", "previous"}:
        raise ValueError("action must be play|pause|next|previous")
    cmd_map = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
    }
    subprocess.run(["osascript", "-e", cmd_map[action]], check=False)
    return f"Music {action}."


# --- Registry -------------------------------------------------------------- #

# Maps command name -> (handler, JSON schema for Claude tool use).
ALLOWED_COMMANDS: dict[str, tuple[Callable[..., str], dict[str, Any]]] = {
    "open_url": (
        _open_url,
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL."}
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    ),
    "show_time": (lambda: _show_time(), {"type": "object", "properties": {}}),
    "show_date": (lambda: _show_date(), {"type": "object", "properties": {}}),
    "volume": (
        _volume,
        {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "mute", "unmute"],
                }
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
    ),
    "music": (
        _music,
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "next", "previous"],
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
}


def list_commands() -> list[dict[str, Any]]:
    """Return the catalogue Claude will be shown — name + parameter schema."""
    return [
        {"name": name, "input_schema": schema}
        for name, (_handler, schema) in ALLOWED_COMMANDS.items()
    ]


def _log_rejected(command: str, reason: str, args: dict[str, Any]) -> None:
    stamp = _dt.datetime.utcnow().isoformat(timespec="seconds")
    line = f"{stamp}Z  command={command!r}  reason={reason}  args={args}\n"
    try:
        with settings.REJECTED_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:  # pragma: no cover — disk full / permissions
        print(f"[JARVIS] could not write rejected.log: {exc}", file=sys.stderr)


def execute(command: str, args: dict[str, Any] | None = None) -> str:
    """Run a whitelisted command. Raises ValueError if denied or invalid."""
    args = args or {}
    if command not in ALLOWED_COMMANDS:
        _log_rejected(command, "not in whitelist", args)
        raise ValueError(f"Command {command!r} is not in the whitelist.")

    handler, _schema = ALLOWED_COMMANDS[command]
    try:
        return handler(**args)
    except TypeError as exc:
        _log_rejected(command, f"bad args: {exc}", args)
        raise ValueError(f"Invalid arguments for {command!r}: {exc}") from exc
    except ValueError as exc:
        _log_rejected(command, str(exc), args)
        raise
