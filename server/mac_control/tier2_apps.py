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

# Safe factory defaults. The user can extend the *effective* allowlist
# at runtime via the Tier-4 ``add_allowed_app`` action — those additions
# land in ``data/allowed_apps.json`` and are merged at call time by
# ``current_allowed_apps()``. Defaults can't be removed at runtime; edit
# this set by hand if you really need to.
DEFAULT_ALLOWED_APPS: frozenset[str] = frozenset({
    "Music", "Spotify",
    "Safari", "Google Chrome",
    "Terminal", "Visual Studio Code",
    "Finder", "Notes", "Reminders",
})
# Hard block. These handle secrets or expose a huge automation surface,
# so they're refused regardless of what the persistent allowlist says.
# This is the security floor — never relax it at runtime.
BLOCKED_APPS: frozenset[str] = frozenset({
    "Keychain Access", "1Password", "Bitwarden", "Mail", "Messages",
    "System Settings", "System Preferences", "Console", "Activity Monitor",
})


def current_allowed_apps() -> set[str]:
    """Effective allowlist at this moment: factory defaults plus any
    persistent additions, minus anything on the hard block. Read on
    every check so updates take effect without a restart."""
    from . import allowlist as _al

    return (set(DEFAULT_ALLOWED_APPS) | _al.load_extras()) - set(BLOCKED_APPS)

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

# Common German → AppleScript/bundle-name aliases. macOS AppleScript
# only knows apps by their bundle name ("Notes"), not their localised
# UI label ("Notizen") — and `tell application "Notizen" to activate`
# hangs the script for ~8 s while macOS shows an internal "where is X?"
# dialog. Mapping here so the user can speak naturally without us
# guessing per call.
_APP_ALIASES: dict[str, str] = {
    "notizen":      "Notes",
    "erinnerungen": "Reminders",
    "kalender":     "Calendar",
    "kontakte":     "Contacts",
    "rechner":      "Calculator",
    "vorschau":     "Preview",
    "musik":        "Music",
    "nachrichten":  "Messages",
    "fotos":        "Photos",
    "karten":       "Maps",
    "einstellungen": "System Settings",
    "systemeinstellungen": "System Settings",
}

# Notes treats `body` as HTML — newlines need to become <br>, and any
# raw markup in user content must be escaped so it doesn't render as
# formatting. We do that escaping Python-side before passing through
# argv so the AppleScript body itself stays a fixed string constant.
_TR_NEW_NOTE = """
on run argv
    set theTitle to item 1 of argv
    set theBody to item 2 of argv
    tell application "Notes"
        make new note with properties {name:theTitle, body:theBody}
    end tell
end run
"""

# Edit an existing Note. Finds the target by exact title first, then by
# `contains` match as a fallback (so "Einkauf" matches "Einkauf Mai 2026").
# Modes: replace (overwrite body), append (add at end), prepend (add at start).
_TR_EDIT_NOTE = """
on run argv
    set theTitle to item 1 of argv
    set theBody to item 2 of argv
    set theMode to item 3 of argv

    tell application "Notes"
        try
            set theNote to first note whose name is theTitle
        on error
            try
                set theNote to first note whose name contains theTitle
            on error
                error "Notiz mit Titel '" & theTitle & "' nicht gefunden."
            end try
        end try

        if theMode is "replace" then
            set body of theNote to theBody
        else if theMode is "append" then
            set body of theNote to (body of theNote) & "<br>" & theBody
        else if theMode is "prepend" then
            set body of theNote to theBody & "<br>" & (body of theNote)
        else
            error "Unbekannter mode: " & theMode
        end if
        return name of theNote
    end tell
end run
"""

# Reminder body is plain text (Reminders.app doesn't parse HTML in body).
# Due date is optional; we parse YYYY-MM-DDTHH:MM in AppleScript so the
# script body stays a constant. List name is optional — empty string
# means "default list".
_TR_NEW_REMINDER = """
on run argv
    set theTitle to item 1 of argv
    set theBody to item 2 of argv
    set theDueIso to item 3 of argv
    set theListName to item 4 of argv

    set theProps to {name:theTitle}
    if theBody is not "" then
        set theProps to theProps & {body:theBody}
    end if
    if theDueIso is not "" then
        set dueDate to my isoToDate(theDueIso)
        set theProps to theProps & {due date:dueDate, remind me date:dueDate}
    end if

    tell application "Reminders"
        if theListName is "" then
            tell default list
                make new reminder with properties theProps
            end tell
        else
            try
                set targetList to first list whose name is theListName
            on error
                error "Reminders-Liste " & theListName & " nicht gefunden."
            end try
            tell targetList
                make new reminder with properties theProps
            end tell
        end if
    end tell
    return "ok"
end run

on isoToDate(iso)
    set y to text 1 thru 4 of iso as integer
    set mo to text 6 thru 7 of iso as integer
    set d to text 9 thru 10 of iso as integer
    set hh to text 12 thru 13 of iso as integer
    set mm to text 15 thru 16 of iso as integer
    set theDate to current date
    set year of theDate to y
    set month of theDate to mo
    set day of theDate to d
    set hours of theDate to hh
    set minutes of theDate to mm
    set seconds of theDate to 0
    return theDate
end isoToDate
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


_NOTE_TITLE_MAX = 200
_NOTE_BODY_MAX = 8000
_REMINDER_TITLE_MAX = 200
_REMINDER_BODY_MAX = 2000
_REMINDER_LIST_MAX = 80
_ISO_RE_REM = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _create_reminder(
    *, title: str = "", body: str = "", due: str = "", list: str = "", **_kw
) -> str:
    if not isinstance(title, str) or not title.strip():
        return "title fehlt."
    title = title.strip()[:_REMINDER_TITLE_MAX]
    if not isinstance(body, str):
        body = str(body)
    body = body[:_REMINDER_BODY_MAX]
    if due and not _ISO_RE_REM.match(due):
        return "due muss ISO 8601 sein: YYYY-MM-DDTHH:MM (z. B. 2026-05-20T14:30)"
    if not isinstance(list, str):
        list = ""
    list_name = list.strip()[:_REMINDER_LIST_MAX]
    try:
        _osa(_TR_NEW_REMINDER, title, body, due or "", list_name)
    except _ASError as exc:
        return f"Reminder erstellen fehlgeschlagen: {exc}"
    parts = [f"Reminder erstellt: {title}"]
    if due:
        parts.append(f"fällig {due}")
    if list_name:
        parts.append(f"in {list_name!r}")
    return ", ".join(parts) + "."


_EDIT_MODES = ("replace", "append", "prepend")


def _edit_note(*, title: str = "", body: str = "", mode: str = "replace", **_kw) -> str:
    """Edit an existing note in Apple Notes by title.

    mode='replace' (default) overwrites the body. 'append' adds at the
    end (separated by a <br>). 'prepend' adds at the start.
    """
    if not isinstance(title, str) or not title.strip():
        return "title fehlt."
    title = title.strip()[:_NOTE_TITLE_MAX]
    if mode not in _EDIT_MODES:
        return f"mode muss {list(_EDIT_MODES)} sein (war: {mode!r})."
    if not isinstance(body, str):
        body = str(body)
    truncated = len(body) > _NOTE_BODY_MAX
    if truncated:
        body = body[:_NOTE_BODY_MAX]
    body_html = (
        body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
    )
    try:
        found = _osa(_TR_EDIT_NOTE, title, body_html, mode)
    except _ASError as exc:
        return f"Notiz bearbeiten fehlgeschlagen: {exc}"
    verb = {"replace": "ersetzt", "append": "ergänzt", "prepend": "vorangestellt"}[mode]
    suffix = " (Body gekürzt)" if truncated else ""
    return f"Notiz {found!r} {verb}{suffix}."


def _create_note(*, title: str = "", body: str = "", **_kw) -> str:
    if not isinstance(title, str) or not title.strip():
        return "title fehlt."
    if not isinstance(body, str):
        body = str(body)
    title = title.strip()[:_NOTE_TITLE_MAX]
    truncated = len(body) > _NOTE_BODY_MAX
    if truncated:
        body = body[:_NOTE_BODY_MAX]
    # Escape HTML, then turn line breaks into <br> so the note renders
    # the way the user wrote it. Notes parses `body` as HTML.
    body_html = (
        body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
    )
    try:
        _osa(_TR_NEW_NOTE, title, body_html)
    except _ASError as exc:
        return f"Notiz erstellen fehlgeschlagen: {exc}"
    suffix = " (Body gekürzt)" if truncated else ""
    return f"Notiz erstellt: {title}{suffix}"


# Polite-quit template. We check the running process list via System
# Events first so `tell app X to quit` doesn't accidentally *launch* X
# in order to send it a quit event.
_TR_QUIT_APP = """
on run argv
    set appName to item 1 of argv
    tell application "System Events"
        set theProcs to (every process whose name is appName)
    end tell
    if (count of theProcs) is 0 then
        return "not running"
    end if
    tell application appName to quit
    return "quit"
end run
"""


def _close_app(*, name: str = "", force: bool = False, **_kw) -> str:
    """Quit an app politely (force=True → SIGKILL via pkill).

    Polite quit goes through AppleScript's standard Quit event. Apps
    with unsaved work may show a save dialog — that's normal macOS
    behaviour, JARVIS doesn't dismiss it. Use force=True only when
    a polite quit has already failed or for unresponsive apps.
    """
    if not isinstance(name, str) or not name:
        return "App-Name fehlt."
    name = name.strip()
    if any(ch in name for ch in "/\\:\x00\n\r\t"):
        return f"App-Name enthält unzulässige Zeichen: {name!r}"
    if len(name) > 80:
        return "App-Name zu lang."

    canonical = _APP_ALIASES.get(name.lower(), name)

    if force:
        # SIGKILL via pkill -x (exact process-name match). Fast,
        # bypasses save prompts. Use sparingly.
        try:
            proc = subprocess.run(
                ["/usr/bin/pkill", "-9", "-x", canonical],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"Force-Quit fehlgeschlagen: {exc}"
        if proc.returncode == 0:
            return f"{canonical} mit Gewalt beendet."
        if proc.returncode == 1:
            return f"{canonical} war nicht offen."
        return f"pkill returncode {proc.returncode}: {proc.stderr.strip()}"

    try:
        result = _osa(_TR_QUIT_APP, canonical)
    except _ASError as exc:
        return f"App-Beenden fehlgeschlagen: {exc}"
    if result == "not running":
        return f"{canonical} war nicht offen."
    return f"{canonical} beendet."


def _open_app(*, name: str = "", **_kw) -> str:
    """Open any installed macOS app by name.

    Uses ``/usr/bin/open -a`` rather than ``tell application … to
    activate``. ``open`` returns immediately with a non-zero exit code
    when the app isn't installed, whereas the AppleScript path hangs
    for seconds while macOS shows an internal "where is this app?"
    dialog — that's the timeout the user hit with "Notizen".

    Localised German app names ("Notizen", "Erinnerungen") are mapped
    to their AppleScript bundle names ("Notes", "Reminders") via
    ``_APP_ALIASES`` so the user can speak naturally.

    No allowlist enforcement — the user explicitly opted into open
    access ("alle Apps … keine Einschränkungen"). The validation here
    is only structural: reject path separators / control chars to
    prevent injection through a crafted name.
    """
    if not isinstance(name, str) or not name:
        return "App-Name fehlt."
    name = name.strip()
    if any(ch in name for ch in "/\\:\x00\n\r\t"):
        return f"App-Name enthält unzulässige Zeichen: {name!r}"
    if len(name) > 80:
        return "App-Name zu lang."

    # Map common German display names to their AppleScript-friendly form.
    canonical = _APP_ALIASES.get(name.lower(), name)

    try:
        proc = subprocess.run(
            ["/usr/bin/open", "-a", canonical],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"App-Start fehlgeschlagen: {exc}"
    if proc.returncode != 0:
        # `open -a Foo` exits 1 with "Unable to find application…" on
        # missing apps — surface that cleanly.
        msg = proc.stderr.strip() or f"exit {proc.returncode}"
        return f"App {canonical!r} nicht gefunden: {msg}"
    display = canonical if canonical == name else f"{canonical} (für {name!r})"
    return f"{display} gestartet."


# --- registry -------------------------------------------------------------- #
#
# Tier classification per user spec ("volle Kontrolle Apps zu öffnen
# und wieder zu schließen, nur wenn ich etwas in den Apps ändern will,
# soll ich das bestätigen müssen"):
#
#   _TIER1_APP_LIFECYCLE  — open / close. Launching or quitting an app
#       changes nothing INSIDE the app, so we put these in Tier INFO
#       (run inline, no confirmation gate). Kill-switch still applies
#       only to non-INFO tiers, so these stay reachable in emergencies
#       which is actually useful (eg. kill a misbehaving app while
#       Tier 2 is locked).
#
#   _TIER2_APP_ACTIONS    — everything that mutates state INSIDE a
#       running app (open a URL, play music, change volume, write a
#       note, create a reminder, send a notification). All Tier APPS —
#       confirmation per action via /confirm.

_TIER1_APP_LIFECYCLE: tuple[tuple[str, callable, callable], ...] = (
    ("open_app",  _open_app,  lambda **p: f"App öffnen: {p.get('name','?')}"),
    ("close_app", _close_app, lambda **p: (
        f"App schließen: {p.get('name','?')}{' (force)' if p.get('force') else ''}"
    )),
)

_TIER2_APP_ACTIONS: tuple[tuple[str, callable, callable], ...] = (
    ("music_transport",   _music_transport,   lambda **p: f"{p.get('player','Spotify')}: {p.get('action','play')}"),
    ("open_url",          _open_url,          lambda **p: f"URL öffnen in Safari: {p.get('url','')}"),
    ("set_volume",        _set_volume,        lambda **p: f"Lautstärke auf {p.get('level','?')}"),
    ("volume_up",         _volume_up,         lambda **_: "Lautstärke +10"),
    ("volume_down",       _volume_down,       lambda **_: "Lautstärke -10"),
    ("volume_mute",       _volume_mute,       lambda **_: "Stummschalten"),
    ("volume_unmute",     _volume_unmute,     lambda **_: "Stummschaltung aufheben"),
    ("send_notification", _send_notification, lambda **p: f"Notification: {p.get('title','JARVIS')} — {p.get('body','')[:60]}"),
    ("create_note",       _create_note,       lambda **p: f"Notiz in Notes.app erstellen: {p.get('title','')[:80]}"),
    ("edit_note",         _edit_note,         lambda **p: (
        f"Notiz bearbeiten ({p.get('mode','replace')}): {p.get('title','')[:60]}"
    )),
    ("create_reminder",   _create_reminder,   lambda **p: (
        f"Reminder erstellen: {p.get('title','')[:60]}"
        + (f" (fällig {p.get('due','')})" if p.get('due') else "")
        + (f" in {p.get('list','')!r}" if p.get('list') else "")
    )),
)


def register_all() -> None:
    for name, handler, summary in _TIER1_APP_LIFECYCLE:
        permission_manager.register(name, Tier.INFO, handler, summary)
    for name, handler, summary in _TIER2_APP_ACTIONS:
        permission_manager.register(name, Tier.APPS, handler, summary)


# Registration only — Tier 2 actions don't run until the dispatcher (built
# in Checkpoint D) checks tier2_is_unlocked() at call time.
register_all()
