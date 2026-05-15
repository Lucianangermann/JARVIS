"""Three rotating audit logs for every mac_control dispatch.

Streams
-------
    logs/actions.log        every dispatch attempt + outcome
    logs/rejected.log       refused by tier / sandbox / kill switch
    logs/confirmations.log  Tier 3+ confirmation prompts and responses

Format
------
    %(asctime)s [tier=%(tier)s] [%(action)s] [STATUS] message

STATUS is one of SUCCESS, REJECTED, CANCELLED, TIMEOUT, FAILED, PENDING,
CONFIRMED, TRIGGERED, RESUMED.

Rotation
--------
10 MB per file, 5 backups kept. JARVIS itself never deletes logs.

Safety
------
``_PasswordRedactor`` strips any literal occurrence of the Tier-4 password
from log messages before they are written to disk. Code should never pass
the password into a log call in the first place — this is defense in depth.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import Any

from ..config import settings

_LOG_FMT = "%(asctime)s [tier=%(tier)s] [%(action)s] %(message)s"
_ROTATE_BYTES = 10 * 1024 * 1024
_ROTATE_BACKUPS = 5


class _PasswordRedactor(logging.Filter):
    """Replace any literal copy of JARVIS_SUDO_PASSWORD with ***REDACTED***."""

    def filter(self, record: logging.LogRecord) -> bool:
        pw = getattr(settings, "JARVIS_SUDO_PASSWORD", "") or ""
        if not pw:
            return True
        msg = record.getMessage()
        if pw in msg:
            record.msg = msg.replace(pw, "***REDACTED***")
            record.args = ()
        return True


def _build(name: str, filename: str) -> logging.Logger:
    log = logging.getLogger(f"jarvis.mac.{name}")
    # Re-import safe: skip if already configured.
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False  # do not bubble up into the root logger
    handler = RotatingFileHandler(
        settings.LOG_DIR / filename,
        maxBytes=_ROTATE_BYTES,
        backupCount=_ROTATE_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FMT))
    handler.addFilter(_PasswordRedactor())
    log.addHandler(handler)
    return log


_actions = _build("actions", "actions.log")
_rejected = _build("rejected", "rejected.log")
_confirmations = _build("confirmations", "confirmations.log")


def _write(logger: logging.Logger, tier: Any, action: str, status: str, message: str) -> None:
    logger.info(f"[{status}] {message}", extra={"tier": str(tier), "action": action})


def log_action(tier: Any, action: str, status: str, message: str = "") -> None:
    """Audit entry for any dispatch attempt — success or otherwise."""
    _write(_actions, tier, action, status, message)


def log_rejected(tier: Any, action: str, reason: str) -> None:
    """An action was refused before it could run (tier / sandbox / killswitch)."""
    _write(_rejected, tier, action, "REJECTED", reason)


def log_confirmation(tier: Any, action: str, status: str, message: str = "") -> None:
    """A confirmation prompt was issued, confirmed, denied, or timed out."""
    _write(_confirmations, tier, action, status, message)


def tail_actions(n: int = 10) -> list[str]:
    """Return the last ``n`` lines of actions.log — used by the
    "show last actions" command. Cheap because the file is bounded."""
    path = settings.LOG_DIR / "actions.log"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        lines = fp.readlines()
    return [line.rstrip("\n") for line in lines[-n:]]
