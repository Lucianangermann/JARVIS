"""JARVIS communication layer.

Phased build (see tasks/todo.md):
  P1 — db, notification_center, translator            [done]
  P2 — imessage, telegram_bot, messaging_manager
  P3 — call_manager, email (templates/analyzer/manager)
  P4 — comm_automation, social_manager
  P6 — communication_manager + integration

Imports are guarded so a missing optional dependency in one component
never blocks the others — or the rest of JARVIS — from loading.
"""
from __future__ import annotations

from .db import CommunicationDB
from .notifications import NotificationCenter
from .translation import CommunicationTranslator
from .communication_manager import CommunicationManager

__all__ = [
    "CommunicationDB",
    "NotificationCenter",
    "CommunicationTranslator",
    "CommunicationManager",
]
