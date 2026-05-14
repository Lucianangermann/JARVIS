"""Convenience wrappers around ``command_guard.execute``.

The brain routes Claude's ``system_command`` tool_use calls straight into
``command_guard.execute(name, args)``. This file exposes the same actions
as plain Python functions so other code paths (tests, REPLs, hotkeys) can
trigger them without going through Claude.

If you want to add a new system action, *add it to* ``command_guard.py``
— the whitelist there is the security boundary. Don't shell out from
here.
"""
from __future__ import annotations

from typing import Any

from .. import command_guard


def run(command: str, **args: Any) -> str:
    """Run a whitelisted command. Raises ValueError if denied."""
    return command_guard.execute(command, args)


def open_url(url: str) -> str:
    return run("open_url", url=url)


def show_time() -> str:
    return run("show_time")


def show_date() -> str:
    return run("show_date")


def volume(direction: str) -> str:
    return run("volume", direction=direction)


def music(action: str) -> str:
    return run("music", action=action)
