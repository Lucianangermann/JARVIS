"""Back-compat shim — the canonical AppleScript runner now lives in
``server.common.applescript``. Re-exported here so existing
``from ..applescript import osa, ASError`` imports in the communication
layer keep working.
"""
from __future__ import annotations

from ..common.applescript import ASError, osa

__all__ = ["osa", "ASError"]
