"""Tier 2 — apps & media via AppleScript templates.

Safety model
------------
Every AppleScript in this module is a fixed string constant. The only
parameters that vary at call time are passed to ``osascript`` as
**positional arguments** (osascript's ``on run argv`` handler), never
interpolated into the script body. That means user/LLM input cannot
inject AppleScript syntax — the AppleScript parser sees the script
text once at compile time, and our Python-controlled values become
plain string variables at run time.

App allowlist
-------------
Only the apps in ``ALLOWED_APPS`` can be opened or controlled. Anything
else — Mail, Messages, Keychain Access, System Settings, password
managers — is hard-rejected before reaching osascript.

Registered actions
------------------
    music_transport(player, action)   play / pause / next / previous
    open_url(url)                     open http(s) URL in Safari
    set_volume(level)                 0..100
    volume_up / volume_down / volume_mute / volume_unmute
    send_notification(title, body)
    open_app(name)                    allowlist only
"""
from __future__ import annotations

import re
import subprocess
from urllib.parse import urlparse

from . import permission_manager
from .permission_manager import Tier

# Apps we'll touch. Anything else: REJECTED.
ALLOWED_APPS: frozenset[str] = frozenset({
    "Music", "Spotify",
    "Safari", "Google Chrome",
    "Terminal", "Visual Studio Code",
    "Finder",
})
# Apps that are explicitly blocked even if someone adds them to ALLOWED_APPS
# by mistake later — these handle secrets or have a huge automation surface.
BLOCKED_APPS: frozenset[str] = frozenset({
    "Keychain Access", "1Password", "Bitwarden", "Mail", "Messages",
    "System Settings", "System Preferences", "Console", "Activity Monitor",
})

# Music transport works for Music.app and Spotify.app — same vocabulary.
ALLOWED_PLAYERS: frozenset[str] = frozenset({"Music", "Spotify"})
ALLOWED_TRANSPORT: frozenset[str] = frozenset({"play", "pause", "next", "previous"})

# A safe URL (http/https only, no control chars).
_URL_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")
_NOTIF_MAX = 200


# --- osascript helper ------------------------------------------------------ #

class _ASError(Exception):
    pass


def _osa(script: str, *args: str, timeout: float = 8.0) -> str:
    """Run ``script`` via osascript, passing ``args`` as argv to its
    ``on run argv`` handler. Args are passed positionally so AppleScript
    injection is structurally impossible.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-", *(str(a) for a in args)],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise _ASError(str(exc)) from exc
    if proc.returncode != 0:
        raise _ASError(proc.stderr.strip() or f"exit {proc.returncode}")
    return proc.stdout.strip()


# --- AppleScript templates ------------------------------------------------- #

_TR_TRANSPORT = """
on run argv
    set appName to item 1 of argv
    set theCommand to item 2 of argv
    tell application appName
        if theCommand is "play" then
            play
        else if theCommand is "pause" then
            pause
        else if theCommand is "next" then
            next track
        else if theCommand is "previous" then
            previous track
        end if
    end tell
end run
"""

_TR_OPEN_URL = """
on run argv
    set theURL to item 1 of argv
    tell application "Safari"
        activate
        if (count of windows) = 0 then
            make new document
        end if
        set URL of current tab of front window to theURL
    end tell
end run
"""

_TR_SET_VOLUME = """
on run argv
    set theVol to (item 1 of argv) as integer
    if theVol < 0 then set theVol to 0
    if theVol > 100 then set theVol to 100
    set volume output volume theVol
    if theVol > 0 then
        set volume without output muted
    end if
end run
"""

_TR_VOLUME_NUDGE = """
on run argv
    set delta to (item 1 of argv) as integer
    set v to output volume of (get volume settings)
    set v to v + delta
    if v < 0 then set v to 0
    if v > 100 then set v to 100
    set volume output volume v
    if v > 0 then set volume without output muted
    return v as string
end run
"""

_TR_MUTE = """
on run argv
    set m to (item 1 of argv)
    if m is "true" then
        set volume with output muted
    else
        set volume without output muted
    end if
end run
"""

_TR_NOTIFY = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
"""

_TR_OPEN_APP = """
on run argv
    tell application (item 1 of argv) to activate
end run
"""


# --- handlers -------------------------------------------------------------- #

def _music_transport(*, player: str = "Spotify", action: str = "play", **_kw) -> str:
    if player not in ALLOWED_PLAYERS:
        return f"Player {player!r} ist nicht erlaubt."
    if action not in ALLOWED_TRANSPORT:
        return f"Aktion {action!r} ist nicht erlaubt."
    try:
        _osa(_TR_TRANSPORT, player, action)
    except _ASError as exc:
        return f"{player}: {exc}"
    verb = {"play": "spielt", "pause": "pausiert",
            "next": "Skip nach vorn", "previous": "Skip zurück"}[action]
    return f"{player} {verb}."


def _open_url(*, url: str = "", **_kw) -> str:
    if not isinstance(url, str) or not _URL_RE.match(url):
        return "URL enthält ungültige Zeichen."
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "Nur http(s)-URLs sind erlaubt."
    try:
        _osa(_TR_OPEN_URL, url)
    except _ASError as exc:
        return f"Safari: {exc}"
    return f"Geöffnet: {url}"


def _set_volume(*, level: int | str = 50, **_kw) -> str:
    try:
        v = int(level)
    except (TypeError, ValueError):
        return "Lautstärke muss eine Zahl 0–100 sein."
    if not 0 <= v <= 100:
        return "Lautstärke muss zwischen 0 und 100 liegen."
    try:
        _osa(_TR_SET_VOLUME, str(v))
    except _ASError as exc:
        return f"Lautstärke: {exc}"
    return f"Lautstärke auf {v}."


def _volume_nudge(delta: int) -> str:
    try:
        new_v = _osa(_TR_VOLUME_NUDGE, str(delta))
    except _ASError as exc:
        return f"Lautstärke: {exc}"
    return f"Lautstärke auf {new_v}."


def _volume_up(**_kw) -> str:
    return _volume_nudge(+10)


def _volume_down(**_kw) -> str:
    return _volume_nudge(-10)


def _volume_mute(**_kw) -> str:
    try:
        _osa(_TR_MUTE, "true")
    except _ASError as exc:
        return f"Mute: {exc}"
    return "Stummgeschaltet."


def _volume_unmute(**_kw) -> str:
    try:
        _osa(_TR_MUTE, "false")
    except _ASError as exc:
        return f"Unmute: {exc}"
    return "Ton wieder an."


def _send_notification(*, title: str = "JARVIS", body: str = "", **_kw) -> str:
    if not isinstance(title, str) or not isinstance(body, str):
        return "Titel und Text müssen Strings sein."
    title = title.strip()[:_NOTIF_MAX] or "JARVIS"
    body = body.strip()[:_NOTIF_MAX]
    if not body:
        return "Benachrichtigung braucht einen Text."
    try:
        _osa(_TR_NOTIFY, title, body)
    except _ASError as exc:
        return f"Notification: {exc}"
    return f"Notification angezeigt: {title} — {body}"


def _open_app(*, name: str = "", **_kw) -> str:
    if not isinstance(name, str) or not name:
        return "App-Name fehlt."
    name = name.strip()
    if name in BLOCKED_APPS:
        return f"{name!r} steht auf der Blockliste."
    if name not in ALLOWED_APPS:
        return f"{name!r} ist nicht in der Allowlist: {sorted(ALLOWED_APPS)}."
    try:
        _osa(_TR_OPEN_APP, name)
    except _ASError as exc:
        return f"App-Start fehlgeschlagen: {exc}"
    return f"{name} gestartet."


# --- registry -------------------------------------------------------------- #

_TIER2: tuple[tuple[str, callable, callable], ...] = (
    ("music_transport",   _music_transport,   lambda **p: f"{p.get('player','Spotify')}: {p.get('action','play')}"),
    ("open_url",          _open_url,          lambda **p: f"URL öffnen in Safari: {p.get('url','')}"),
    ("set_volume",        _set_volume,        lambda **p: f"Lautstärke auf {p.get('level','?')}"),
    ("volume_up",         _volume_up,         lambda **_: "Lautstärke +10"),
    ("volume_down",       _volume_down,       lambda **_: "Lautstärke -10"),
    ("volume_mute",       _volume_mute,       lambda **_: "Stummschalten"),
    ("volume_unmute",     _volume_unmute,     lambda **_: "Stummschaltung aufheben"),
    ("send_notification", _send_notification, lambda **p: f"Notification: {p.get('title','JARVIS')} — {p.get('body','')[:60]}"),
    ("open_app",          _open_app,          lambda **p: f"App öffnen: {p.get('name','?')}"),
)


def register_all() -> None:
    for name, handler, summary in _TIER2:
        permission_manager.register(name, Tier.APPS, handler, summary)


# Registration only — Tier 2 actions don't run until the dispatcher (built
# in Checkpoint D) checks tier2_is_unlocked() at call time.
register_all()
