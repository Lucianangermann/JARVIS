"""Persistent, runtime-mutable app allowlist.

The built-in ``DEFAULT_ALLOWED_APPS`` in ``tier2_apps`` provides the safe
factory defaults. This module lets a Tier-4 action append entries to a
JSON file on disk so JARVIS can extend its own automation surface
without a code change. On every ``open_app`` / ``music_transport`` call,
``tier2_apps.current_allowed_apps()`` returns the union of defaults and
this file.

Hard rules
----------
- ``BLOCKED_APPS`` in ``tier2_apps`` is the floor. Anything on that list
  cannot be added here — defense in depth against prompt injection.
- Adding an app is a Tier 4 action: per-action password required, logged
  to ``confirmations.log`` and ``actions.log``.
- Removing only removes from the persistent layer; the code defaults
  remain. To remove a default, edit ``tier2_apps.DEFAULT_ALLOWED_APPS``
  by hand.

Storage
-------
JSON list at ``<project_root>/data/allowed_apps.json``. The directory is
created on first write. The file is git-ignored.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from ..config import settings

_lock = threading.Lock()


def _store_path() -> Path:
    return settings.LOG_DIR.parent / "data" / "allowed_apps.json"


def _read_locked() -> set[str]:
    p = _store_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return set()
    return {x for x in data if isinstance(x, str)}


def _write_locked(names: Iterable[str]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sorted(set(names)), ensure_ascii=False, indent=2) + "\n"
    p.write_text(payload, encoding="utf-8")


# --- public API ------------------------------------------------------------ #

def load_extras() -> set[str]:
    """Names the user added at runtime. Excludes the code defaults."""
    with _lock:
        return _read_locked()


def _validate_name(name: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "App-Name fehlt."
    if len(name) > 60:
        return False, "App-Name zu lang (max 60 Zeichen)."
    bad = set(name) & set("/\\:\x00\n\r\t")
    if bad:
        return False, f"App-Name enthält unzulässige Zeichen: {sorted(bad)}"
    return True, name


def add(name: str) -> tuple[bool, str]:
    """Append ``name`` to the persistent allowlist. Returns (ok, message)."""
    ok, msg_or_name = _validate_name(name)
    if not ok:
        return False, msg_or_name
    name = msg_or_name

    # Lazy import avoids a circular dependency at module load time.
    from .tier2_apps import BLOCKED_APPS, DEFAULT_ALLOWED_APPS

    if name in BLOCKED_APPS:
        return False, (f"{name!r} steht auf der Code-Blockliste — "
                       "kann zur Laufzeit nicht freigegeben werden.")
    if name in DEFAULT_ALLOWED_APPS:
        return False, f"{name!r} ist schon per Default erlaubt — nichts zu tun."

    with _lock:
        current = _read_locked()
        if name in current:
            return False, f"{name!r} ist bereits in der Allowlist."
        current.add(name)
        _write_locked(current)
    return True, f"{name!r} zur Allowlist hinzugefügt."


def remove(name: str) -> tuple[bool, str]:
    """Drop ``name`` from the persistent layer. Cannot remove defaults."""
    ok, msg_or_name = _validate_name(name)
    if not ok:
        return False, msg_or_name
    name = msg_or_name

    with _lock:
        current = _read_locked()
        if name not in current:
            return False, (f"{name!r} ist nicht in der Persistent-Allowlist "
                          "(Code-Defaults können hier nicht entfernt werden).")
        current.discard(name)
        _write_locked(current)
    return True, f"{name!r} aus der Allowlist entfernt."


def list_all() -> list[str]:
    """Effective allowlist = defaults ∪ extras − blocked. Used by the
    Tier-1 ``list_allowed_apps`` action."""
    from .tier2_apps import BLOCKED_APPS, DEFAULT_ALLOWED_APPS

    with _lock:
        extras = _read_locked()
    return sorted((set(DEFAULT_ALLOWED_APPS) | extras) - set(BLOCKED_APPS))
