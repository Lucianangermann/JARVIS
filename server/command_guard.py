"""Whitelisted system command runner.

The brain may ask to run a "system command" via tool use. Instead of letting
Claude pick an arbitrary shell string, every action is keyed by a short name
(e.g. ``open_url``) that maps to a hand-written, parameter-validated handler.

Anything not in ALLOWED_COMMANDS is rejected and logged to
``logs/rejected.log`` so we can audit attempted abuse.
"""
from __future__ import annotations

import datetime as _dt
import os
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


def _spotify_installed() -> bool:
    """Return True if the Spotify .app bundle is present in standard locations."""
    candidates = [
        "/Applications/Spotify.app",
        os.path.expanduser("~/Applications/Spotify.app"),
    ]
    return any(os.path.isdir(p) for p in candidates)


# spotify:track:abc123, spotify:playlist:abc123, spotify:album:abc123, …
_SPOTIFY_URI_RE = re.compile(r"^spotify:(track|playlist|album|artist):[A-Za-z0-9]+$")


def _spotify_launch_if_needed() -> None:
    subprocess.run(
        ["open", "-a", "Spotify"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript string, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False, capture_output=True, text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _music(action: str, query: str = "") -> str:
    """Control Spotify on macOS, including search-and-play by name."""
    if platform.system() != "Darwin":
        return "Music control is only wired up for macOS in this build."
    valid = {"play", "pause", "next", "previous", "play_track", "play_playlist"}
    if action not in valid:
        raise ValueError(f"action must be one of {sorted(valid)}")

    if not _spotify_installed():
        return ("Spotify ist auf diesem Mac nicht installiert. "
                "Installiere es von https://spotify.com/download oder aus dem App Store.")

    # --- Search-and-play paths --------------------------------------------- #
    if action in {"play_track", "play_playlist"}:
        if not query or not query.strip():
            raise ValueError(f"{action} braucht ein 'query'-Argument.")
        from . import spotify as _spotify  # late import: avoids requests at startup

        kind = "track" if action == "play_track" else "playlist"
        try:
            hit = _spotify.search(query, kind=kind)
        except _spotify.SpotifyConfigError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"Spotify-Suche fehlgeschlagen: {exc}"

        if not hit or not hit.get("uri"):
            return f'Konnte keinen passenden {kind} zu "{query}" finden.'

        uri = hit["uri"]
        # Sanity-check the URI before splicing it into AppleScript.
        if not _SPOTIFY_URI_RE.match(uri):
            return f"Spotify lieferte eine unerwartete URI: {uri!r}."

        _spotify_launch_if_needed()
        rc, _out, err = _osascript(f'tell application "Spotify" to play track "{uri}"')
        if rc != 0:
            return ("Spotify konnte den Befehl nicht ausführen: "
                    f"{err or 'keine Details verfügbar'}")

        label = hit["name"] or query
        if kind == "track" and hit.get("artists"):
            label = f"{label} von {hit['artists']}"
        elif kind == "playlist" and hit.get("owner"):
            label = f"{label} (von {hit['owner']})"
        return f"Spotify spielt: {label}"

    # --- Transport controls ------------------------------------------------ #
    if action == "play":
        _spotify_launch_if_needed()

    cmd_map = {
        "play": 'tell application "Spotify" to play',
        "pause": 'tell application "Spotify" to pause',
        "next": 'tell application "Spotify" to next track',
        "previous": 'tell application "Spotify" to previous track',
    }
    rc, _out, err = _osascript(cmd_map[action])
    if rc != 0:
        return ("Spotify konnte den Befehl nicht ausführen: "
                f"{err or 'keine Details verfügbar'}")
    return f"Spotify: {action}."


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
                    "enum": [
                        "play", "pause", "next", "previous",
                        "play_track", "play_playlist",
                    ],
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Song title (for play_track) or playlist name "
                        "(for play_playlist). Required for those actions, "
                        "ignored otherwise."
                    ),
                },
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
