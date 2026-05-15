"""mac_control — staged-permission macOS automation for JARVIS.

Public surface
--------------
- ``Tier`` — IntEnum with the four permission tiers.
- ``permission_manager`` — action registry and unlock state.
- ``confirmation`` — pending-action store for Tier 3+ confirmation flow.
- ``kill_switch`` — process-wide emergency stop.
- ``action_logger`` — three rotating audit logs (actions / rejected / confirmations).
- ``dispatch(action, params, *, password=None)`` — single entry point used by the
  brain. Looks up the intrinsic tier, applies the right gate, runs and logs.

Tier of every action is fixed at registration in the tierN_*.py module — no
caller can request a different tier for an existing action.

See ``README_MAC_CONTROL.md`` for the full model, setup, and hard rules.
"""
from __future__ import annotations

from . import action_logger, confirmation, kill_switch, permission_manager
from .permission_manager import Tier

# Import tier modules so their register_all() runs at package load.
from . import tier1_info, tier2_apps, tier3_files, tier4_system  # noqa: F401
# Import dispatcher last — it depends on the registry being populated.
from . import dispatcher  # noqa: F401

__all__ = [
    "Tier",
    "action_logger",
    "confirmation",
    "dispatcher",
    "kill_switch",
    "permission_manager",
]
