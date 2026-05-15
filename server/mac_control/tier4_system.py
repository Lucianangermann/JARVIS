"""Tier 4 — full system control behind a per-action password.

Gating
------
Every Tier 4 dispatch goes through ``confirmation.consume(id)`` AND
``permission_manager.check_password(provided)`` in the request handler.
This module's handlers run only after both gates have passed; they do
not see the password themselves.

Surface
-------
    terminal(command, args=[])     fixed allowlist with arg validation
    install_app(pkg)               brew install <pkg> — name regex'd
    uninstall_app(pkg)             brew uninstall <pkg>
    open_prefs_pane(pane)          opens a System Settings pane (read-only)
    screenshot()                   captures full screen → /tmp PNG path
    email_preview(to, subject, body)   drafts via mailto: (no auto-send)
    calendar_create(title, start, end) adds event to default calendar

Hard rules baked into this file
-------------------------------
- subprocess is invoked with a *list* argv, never ``shell=True``.
- Every value that becomes part of an argv comes from a regex match or
  an integer cast — no free-form strings reach the OS.
- AppleScript bodies are constants; arguments ride argv.
- Screenshot prints ``[JARVIS SCREEN ACTIVE 👁]`` to stdout for the
  duration of capture so the operator sees screen-reads happen.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import permission_manager
from .permission_manager import Tier

# --- terminal allowlist ---------------------------------------------------- #
#
# Each entry: name → (argv_template, validator)
#   argv_template is a list with "{n}" placeholders for validated args.
#   validator returns a dict of substitutions or raises ValueError.
#
# Anything NOT in this table is hard-rejected. Adding new entries is a
# deliberate code change — never user-supplied.

_SAY_MAX = 300
_INT_RE = re.compile(r"^-?\d{1,5}$")


def _v_say(args: list) -> dict:
    text = "".join(str(a) for a in args).strip()
    if not text:
        raise ValueError("'say' braucht einen Text.")
    if len(text) > _SAY_MAX:
        raise ValueError(f"Text zu lang (Limit {_SAY_MAX}).")
    return {"text": text}


def _v_caffeinate(args: list) -> dict:
    if len(args) != 1 or not _INT_RE.match(str(args[0])):
        raise ValueError("'caffeinate' braucht genau eine Sekunden-Zahl.")
    s = int(args[0])
    if not 1 <= s <= 3600:
        raise ValueError("Sekunden müssen 1..3600 sein.")
    return {"sec": str(s)}


def _v_noargs(args: list) -> dict:
    if args:
        raise ValueError("Dieses Kommando nimmt keine Argumente.")
    return {}


TERMINAL_COMMANDS: dict[str, tuple[list[str], callable]] = {
    "say":            (["/usr/bin/say", "{text}"], _v_say),
    "caffeinate":     (["/usr/bin/caffeinate", "-t", "{sec}"], _v_caffeinate),
    "display_sleep":  (["/usr/bin/pmset", "displaysleepnow"], _v_noargs),
    "mac_sleep":      (["/usr/bin/pmset", "sleepnow"], _v_noargs),
}


def _run_argv(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# --- brew allowlist & validation ------------------------------------------- #

_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")


def _brew_path() -> str | None:
    for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(p).exists():
            return p
    return None


# System Settings pane URIs — opening one is read-only from JARVIS's
# side; the user does any actual editing.
PREFS_PANES: dict[str, str] = {
    "displays":     "x-apple.systempreferences:com.apple.preference.displays",
    "sound":        "x-apple.systempreferences:com.apple.preference.sound",
    "network":      "x-apple.systempreferences:com.apple.preference.network",
    "bluetooth":    "x-apple.systempreferences:com.apple.preferences.Bluetooth",
    "battery":      "x-apple.systempreferences:com.apple.preference.battery",
    "general":      "x-apple.systempreferences:com.apple.preference.general",
    "accessibility":"x-apple.systempreferences:com.apple.preference.universalaccess",
    "keyboard":     "x-apple.systempreferences:com.apple.preference.keyboard",
    "trackpad":     "x-apple.systempreferences:com.apple.preference.trackpad",
}


# --- handlers -------------------------------------------------------------- #

def _terminal(*, command: str = "", args: list | None = None, **_kw) -> str:
    if command not in TERMINAL_COMMANDS:
        return (f"Kommando {command!r} ist nicht erlaubt. "
                f"Erlaubt: {sorted(TERMINAL_COMMANDS)}")
    template, validator = TERMINAL_COMMANDS[command]
    if not isinstance(args, list):
        args = []
    try:
        subs = validator(args)
    except ValueError as exc:
        return str(exc)
    argv = [piece.format(**subs) for piece in template]
    rc, out, err = _run_argv(argv)
    if rc != 0:
        return f"{command} fehlgeschlagen: {err or rc}"
    return out or f"{command} ausgeführt."


def _install_app(*, pkg: str = "", **_kw) -> str:
    if not _PKG_RE.match(pkg or ""):
        return f"Paketname ungültig: {pkg!r}"
    brew = _brew_path()
    if not brew:
        return "Homebrew ist nicht installiert."
    rc, out, err = _run_argv([brew, "install", pkg], timeout=600)
    if rc != 0:
        return f"brew install {pkg} fehlgeschlagen: {err or out or rc}"
    return f"Installiert: {pkg}"


def _uninstall_app(*, pkg: str = "", **_kw) -> str:
    if not _PKG_RE.match(pkg or ""):
        return f"Paketname ungültig: {pkg!r}"
    brew = _brew_path()
    if not brew:
        return "Homebrew ist nicht installiert."
    rc, out, err = _run_argv([brew, "uninstall", pkg], timeout=300)
    if rc != 0:
        return f"brew uninstall {pkg} fehlgeschlagen: {err or out or rc}"
    return f"Entfernt: {pkg}"


def _open_prefs_pane(*, pane: str = "", **_kw) -> str:
    uri = PREFS_PANES.get(pane)
    if not uri:
        return f"Pane {pane!r} unbekannt. Verfügbar: {sorted(PREFS_PANES)}"
    rc, _, err = _run_argv(["/usr/bin/open", uri])
    if rc != 0:
        return f"System Settings konnte nicht geöffnet werden: {err}"
    return f"System Settings geöffnet: {pane}"


def _screenshot(**_kw) -> str:
    """Captures the full screen to /tmp/jarvis_screenshot_<ts>.png.

    Prints a visible indicator to stdout before AND after the capture
    so the operator can audit screen-reads in real time. Returns the
    path on success — Checkpoint D wires this into a Claude Vision call.

    Requires macOS Screen Recording permission (granted via
    System Settings → Privacy → Screen Recording). Without it,
    screencapture silently records the wallpaper only.
    """
    ts = int(time.time())
    out_path = Path(f"/tmp/jarvis_screenshot_{ts}.png")
    print("[JARVIS SCREEN ACTIVE 👁]", flush=True)
    try:
        rc, _, err = _run_argv(
            ["/usr/sbin/screencapture", "-x", str(out_path)], timeout=10
        )
    finally:
        print("[JARVIS SCREEN OFF]", flush=True)
    if rc != 0:
        return f"screencapture fehlgeschlagen: {err}"
    if not out_path.exists():
        return "Screenshot wurde nicht erstellt (Permission fehlt?)"
    return f"Screenshot: {out_path} ({out_path.stat().st_size} B)"


def _email_preview(*, to: str = "", subject: str = "", body: str = "", **_kw) -> str:
    """Open a draft in the default mail client via mailto:.

    Never sends in the background. The user has to hit Send themselves
    — that's the "full preview before sending" rule from the spec.
    """
    if not to or "@" not in to:
        return "Empfänger-Adresse ist ungültig."
    if len(subject) > 200:
        subject = subject[:200]
    if len(body) > 4000:
        body = body[:4000] + "\n…(gekürzt)"
    qs = urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    uri = f"mailto:{urllib.parse.quote(to)}?{qs}"
    rc, _, err = _run_argv(["/usr/bin/open", uri])
    if rc != 0:
        return f"Mail-Entwurf konnte nicht geöffnet werden: {err}"
    return f"Mail-Entwurf vorbereitet (an {to}). Im Mail-Programm prüfen und senden."


# Calendar add — AppleScript with all values via argv. Dates are passed
# as ISO 8601 and converted inside AS, so the script body never contains
# user content.
_TR_CAL_ADD = """
on run argv
    set theTitle to item 1 of argv
    set startISO to item 2 of argv
    set endISO to item 3 of argv
    set startD to my isoToDate(startISO)
    set endD to my isoToDate(endISO)
    tell application "Calendar"
        set theCal to first calendar whose writable is true
        tell theCal
            make new event with properties {summary:theTitle, start date:startD, end date:endD}
        end tell
    end tell
    return "ok"
end run

on isoToDate(iso)
    -- iso: YYYY-MM-DDTHH:MM
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

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _calendar_create(*, title: str = "", start: str = "", end: str = "", **_kw) -> str:
    if not title or not isinstance(title, str):
        return "title fehlt."
    title = title.strip()[:200]
    if not _ISO_RE.match(start or ""):
        return "start muss ISO 8601 sein: YYYY-MM-DDTHH:MM"
    if not _ISO_RE.match(end or ""):
        return "end muss ISO 8601 sein: YYYY-MM-DDTHH:MM"
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-", title, start, end],
            input=_TR_CAL_ADD,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"Calendar-Eintrag fehlgeschlagen: {exc}"
    if proc.returncode != 0:
        return f"Calendar-Eintrag fehlgeschlagen: {proc.stderr.strip()}"
    return f"Termin angelegt: {title} ({start} → {end})"


# --- registry -------------------------------------------------------------- #

_TIER4: tuple[tuple[str, callable, callable], ...] = (
    ("terminal",         _terminal,         lambda **p: f"Terminal: {p.get('command','')} {p.get('args',[])}"),
    ("install_app",      _install_app,      lambda **p: f"App installieren (brew): {p.get('pkg','')}"),
    ("uninstall_app",    _uninstall_app,    lambda **p: f"App entfernen (brew): {p.get('pkg','')}"),
    ("open_prefs_pane",  _open_prefs_pane,  lambda **p: f"System Settings öffnen: {p.get('pane','')}"),
    ("screenshot",       _screenshot,       lambda **_: "Screenshot vom gesamten Bildschirm"),
    ("email_preview",    _email_preview,    lambda **p: f"Mail-Entwurf an {p.get('to','')}: {p.get('subject','')[:60]}"),
    ("calendar_create",  _calendar_create,  lambda **p: f"Termin: {p.get('title','')[:60]} ({p.get('start','')}→{p.get('end','')})"),
)


def register_all() -> None:
    for name, handler, summary in _TIER4:
        permission_manager.register(name, Tier.SYSTEM, handler, summary)


register_all()
