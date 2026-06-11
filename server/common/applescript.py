"""Canonical injection-safe AppleScript runner for JARVIS.

The script is piped to ``osascript -`` and every call-time parameter is
passed as a **positional argv** to the script's ``on run argv`` handler —
never string-interpolated into the script body. That makes AppleScript
injection structurally impossible, which matters wherever free text
(message bodies, contact names, file paths) reaches AppleScript.

New code should import ``osa``/``ASError`` from here. The communication
layer's ``applescript`` module re-exports these for back-compat. The older
``tools/*`` and ``mac_control`` helpers predate this and build scripts with
``osascript -e`` + escaped interpolation; migrating them to ``osa`` is a
per-script rewrite best done with a Mac to verify each AppleScript.
"""
from __future__ import annotations

import subprocess


class ASError(Exception):
    """An osascript invocation failed (non-zero exit, timeout, missing)."""


def osa(script: str, *args: str, timeout: float = 10.0) -> str:
    """Run ``script`` via osascript with ``args`` as positional argv.

    Raises :class:`ASError` on any failure; callers translate that into a
    best-effort fallback rather than letting it crash JARVIS.
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
        raise ASError(str(exc)) from exc
    if proc.returncode != 0:
        raise ASError(proc.stderr.strip() or f"exit {proc.returncode}")
    return proc.stdout.strip()
