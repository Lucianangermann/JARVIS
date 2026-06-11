"""Centralised logging for JARVIS.

The codebase is split between modules that use the ``logging`` module
(memory, smarthome, mac_control) and a large body of ``print()``-based
status output ("[Tag] …"). Rewriting 400+ prints is risky churn, so
instead :func:`configure_logging`:

  1. configures the ``logging`` root with a rotating file handler
     (``logs/jarvis.log``) + a console handler, so the logging-module
     callers persist to disk; and
  2. tees ``sys.stdout`` / ``sys.stderr`` so every ``print()`` line is ALSO
     captured into the same rotating log — without touching the call
     sites. The terminal still shows everything as before.

New code should prefer ``get_logger(__name__)`` over ``print()``.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TextIO

_configured = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class _Tee:
    """Wrap a stream so writes go to the original AND, line-by-line, to a
    logger (which routes into the rotating file handler). Lets us capture
    print() output to disk with zero call-site changes."""

    def __init__(self, original: TextIO, logger: logging.Logger,
                 level: int) -> None:
        self._orig = original
        self._logger = logger
        self._level = level
        self._buf = ""
        self._in_log = False  # reentrancy guard (logging must not recurse)

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
        except Exception:  # noqa: BLE001
            pass
        if self._in_log:
            return len(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._in_log = True
                try:
                    self._logger.log(self._level, line)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._in_log = False
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return getattr(self._orig, "isatty", lambda: False)()

    def fileno(self) -> int:
        return self._orig.fileno()


def configure_logging(log_dir: Path | str = "logs", *,
                      level: int = logging.INFO,
                      tee_prints: bool = True) -> None:
    """Idempotent. Sets up the rotating file + console handlers and (by
    default) tees print output into the log file."""
    global _configured
    if _configured:
        return
    _configured = True

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "jarvis.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level)
    # Console handler for logging-module callers (prints already hit the
    # terminal via the tee; logging-module records need their own console
    # output). Keep it terse.
    console = logging.StreamHandler(sys.__stderr__)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    root.addHandler(console)

    if tee_prints:
        # A dedicated logger that writes ONLY to the file handler (no console
        # propagation, or print lines would double on screen).
        console_logger = logging.getLogger("jarvis.console")
        console_logger.setLevel(level)
        console_logger.propagate = False
        console_logger.addHandler(file_handler)
        sys.stdout = _Tee(sys.stdout, console_logger, logging.INFO)
        sys.stderr = _Tee(sys.stderr, console_logger, logging.WARNING)
