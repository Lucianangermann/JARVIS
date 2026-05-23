"""macOS app control — open, close, list running apps."""
from __future__ import annotations

import subprocess


def _script(code: str, timeout: float = 5.0) -> tuple[str, bool]:
    try:
        p = subprocess.run(["osascript", "-e", code],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return p.stderr.strip() or "AppleScript-Fehler", True
        return p.stdout.strip(), False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung", True
    except Exception as exc:  # noqa: BLE001
        return str(exc), True


def open_app(name: str) -> tuple[str, bool]:
    try:
        p = subprocess.run(["open", "-a", name],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return p.stderr.strip() or f"'{name}' konnte nicht geöffnet werden.", True
        return f"'{name}' wird geöffnet.", False
    except Exception as exc:  # noqa: BLE001
        return str(exc), True


def close_app(name: str) -> tuple[str, bool]:
    return _script(f'tell application "{name}" to quit')


def list_running() -> list[str]:
    out, err = _script(
        'tell application "System Events" to get name of every process '
        'whose background only is false',
    )
    if err:
        return []
    return sorted(a.strip() for a in out.split(",") if a.strip())
