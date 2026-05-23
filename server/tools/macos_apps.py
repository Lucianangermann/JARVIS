"""macOS app control — open, close, list running apps.

Uses `open -a` for launching and AppleScript `quit` for closing.
Permission checks run against app_permissions before any action.
"""
from __future__ import annotations

import asyncio
import subprocess


def _run_script(script: str, timeout: float = 5.0) -> tuple[str, bool]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return proc.stderr.strip() or "AppleScript error", True
        return proc.stdout.strip(), False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung bei AppleScript", True
    except Exception as exc:  # noqa: BLE001
        return str(exc), True


async def open_app(name: str) -> tuple[str, bool]:
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        return err or f"'{name}' konnte nicht geöffnet werden.", True
    return f"'{name}' wird geöffnet.", False


async def close_app(name: str) -> tuple[str, bool]:
    return await asyncio.to_thread(
        _run_script, f'tell application "{name}" to quit'
    )


async def list_running() -> list[str]:
    out, err = await asyncio.to_thread(
        _run_script,
        'tell application "System Events" to get name of every process '
        'whose background only is false',
    )
    if err:
        return []
    return sorted(a.strip() for a in out.split(",") if a.strip())


async def app_is_running(name: str) -> bool:
    apps = await list_running()
    return name in apps
