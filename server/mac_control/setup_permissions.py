"""macOS TCC permission checker for JARVIS mac_control.

Run::

    .venv/bin/python -m server.mac_control.setup_permissions

Reports the state of each permission JARVIS needs and tells you exactly
which System Settings pane to open if anything is missing. We can't grant
permissions programmatically — TCC is user-driven by design — but we can
probe each one and surface the problem so the first run isn't a guessing
game.

What we probe
-------------
- Automation (Finder, Music, Spotify, Calendar, Safari) — try a no-op
  AppleScript against each and see whether macOS rejects it.
- Screen Recording — take a tiny screencapture into a tempfile. Without
  the permission, macOS silently writes only the wallpaper; we compare
  against a known-tiny size threshold as a heuristic.
- mac_control's own env — MAC_CONTROL_ENABLED, JARVIS_SUDO_PASSWORD set?
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import settings

# --- ANSI helpers ---------------------------------------------------------- #

_OK = "\033[32m✓\033[0m"
_NO = "\033[31m✗\033[0m"
_WARN = "\033[33m!\033[0m"


def _osa(script: str, *args: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Run an AppleScript; return (success, message-or-error-text)."""
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
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"exit {proc.returncode}"
    return True, proc.stdout.strip()


# --- probes ---------------------------------------------------------------- #

def check_automation_target(app: str) -> tuple[bool, str]:
    """Probe AppleScript automation against ``app``.

    ``tell application X to name`` is the most portable no-op — every
    scriptable app responds to ``name``, whereas ``count of windows``
    isn't in some app dictionaries (Spotify, for one).
    """
    script = (
        'on run argv\n'
        '  set appName to item 1 of argv\n'
        '  tell application appName to get its name\n'
        '  return "ok"\n'
        'end run\n'
    )
    # First-time prompts can take 8+ s while the user clicks Allow.
    ok, msg = _osa(script, app, timeout=12.0)
    if ok:
        return True, "automation granted"
    low = msg.lower()
    if "not authorized" in low or "1743" in low or "not allowed" in low:
        return False, "Automation permission DENIED — System Settings → Privacy & Security → Automation"
    if "can't find" in low or "(-1728)" in msg:
        return True, "app not installed / not running (no permission needed yet)"
    if "timed out" in low:
        return False, "timed out — accept the macOS permission prompt, then re-run"
    return False, msg


def check_screen_recording() -> tuple[bool, str]:
    """A screencapture without permission writes a wallpaper-only PNG
    that's typically <100 KB on a modern Retina display. A real screen
    capture comes back several MB. The 100 KB heuristic is good enough
    for a binary 'permission yes/no' check."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
        path = Path(fp.name)
    try:
        proc = subprocess.run(
            ["/usr/sbin/screencapture", "-x", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return False, proc.stderr.strip()
        size = path.stat().st_size
        if size < 100_000:
            return False, (f"capture only {size} B — usually means Screen Recording is denied. "
                           "System Settings → Privacy & Security → Screen Recording, add your terminal.")
        return True, f"{size//1024} KB captured"
    finally:
        path.unlink(missing_ok=True)


def check_env() -> list[tuple[bool, str, str]]:
    """Report on the .env-driven mac_control toggles."""
    rows: list[tuple[bool, str, str]] = []
    rows.append((
        settings.MAC_CONTROL_ENABLED,
        "MAC_CONTROL_ENABLED",
        "1 (active)" if settings.MAC_CONTROL_ENABLED else "0 (mac_control disabled — set to 1 in .env)",
    ))
    pw_set = bool(getattr(settings, "JARVIS_SUDO_PASSWORD", ""))
    rows.append((
        pw_set,
        "JARVIS_SUDO_PASSWORD",
        "set (Tier 4 available)" if pw_set else "empty (Tier 4 disabled)",
    ))
    return rows


# --- main ------------------------------------------------------------------ #

def main() -> int:
    print("JARVIS mac_control — permission check\n")

    print("== .env ==")
    failures = 0
    for ok, name, msg in check_env():
        mark = _OK if ok else _WARN
        print(f"  {mark} {name}: {msg}")

    print("\n== Automation (per app) ==")
    for app in ("Finder", "Music", "Spotify", "Calendar", "Safari", "Notes"):
        ok, msg = check_automation_target(app)
        mark = _OK if ok else _NO
        if not ok:
            failures += 1
        print(f"  {mark} {app:9s}: {msg}")

    print("\n== Screen Recording ==")
    ok, msg = check_screen_recording()
    mark = _OK if ok else _NO
    if not ok:
        failures += 1
    print(f"  {mark} screencapture: {msg}")

    print("\n== Hints ==")
    print(
        "  • Automation prompts appear the first time JARVIS touches each app.\n"
        "    If you accidentally clicked 'Don't Allow', open\n"
        "      System Settings → Privacy & Security → Automation\n"
        "    and toggle the relevant row back on.\n"
        "  • Screen Recording must be granted to the *terminal / app* that runs\n"
        "    the python server (Terminal.app, iTerm, VS Code, …). After enabling,\n"
        "    quit and reopen that app — macOS won't honour the change live.\n"
        "  • mac_control itself only goes live once MAC_CONTROL_ENABLED=1.\n"
    )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
