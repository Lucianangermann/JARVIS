"""JARVIS communication: extended email (multi-account, templates, attachments)."""
from __future__ import annotations

from .email_manager import ExtendedEmailManager
from .email_templates import EmailTemplateManager
from .email_analyzer import EmailAnalyzer

__all__ = ["ExtendedEmailManager", "EmailTemplateManager", "EmailAnalyzer"]
