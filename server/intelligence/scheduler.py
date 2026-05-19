"""Tiny in-process scheduler — one background thread, minute-resolution.

The intelligence layer needs to wake up on its own to fire routines
(morning briefing at 08:00, notification checks every minute, …) but
we don't want to take on a real cron library. The contract:

  • Jobs registered with ``every(seconds, fn)`` fire repeatedly with
    a minimum gap of ``seconds`` between calls.
  • Jobs registered with ``daily(hhmm, fn, weekdays=…)`` fire at most
    once on each matching weekday, at the given clock minute.

Every job is wrapped in try/except so a single failure can't stop
the loop. The loop ticks at the top of each minute so daily jobs
land on the right minute (not at 08:00:43).
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Callable
from zoneinfo import ZoneInfo

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))


@dataclass
class _Job:
    name: str
    fn: Callable[[], None]
    interval_seconds: int | None = None   # for every()
    clock_time: dtime | None = None       # for daily()
    weekdays: set[int] | None = None      # 0=Mon..6=Sun; None = all
    last_run_at: datetime | None = field(default=None, init=False)


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- registration --------------------------------------------------- #

    def every(self, seconds: int, fn: Callable[[], None], *,
              name: str | None = None) -> None:
        """Run ``fn`` repeatedly, at least ``seconds`` apart."""
        with self._lock:
            self._jobs.append(_Job(
                name=name or fn.__name__,
                fn=fn,
                interval_seconds=max(1, int(seconds)),
            ))

    def daily(self, hhmm: str, fn: Callable[[], None], *,
              weekdays: set[int] | None = None,
              name: str | None = None) -> None:
        """Run ``fn`` once per matching day at the given ``HH:MM``."""
        hh, mm = hhmm.split(":", 1)
        with self._lock:
            self._jobs.append(_Job(
                name=name or fn.__name__,
                fn=fn,
                clock_time=dtime(int(hh), int(mm)),
                weekdays=weekdays,
            ))

    # --- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="jarvis-scheduler", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # --- loop ----------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(_LOCAL_TZ)
            with self._lock:
                jobs = list(self._jobs)
            for job in jobs:
                try:
                    if self._should_run(job, now):
                        job.last_run_at = now
                        job.fn()
                except Exception:  # noqa: BLE001 — isolate per job
                    print(f"[scheduler] {job.name} crashed:")
                    traceback.print_exc()
            # Sleep to the top of the next minute so daily jobs that
            # ask for 08:00 fire on the 08:00:00 tick rather than
            # 08:00:42. wait() also returns early if stop() was called.
            secs = max(1, 60 - now.second)
            if self._stop.wait(timeout=secs):
                break

    @staticmethod
    def _should_run(job: _Job, now: datetime) -> bool:
        if job.interval_seconds is not None:
            if job.last_run_at is None:
                return True
            return (now - job.last_run_at).total_seconds() >= job.interval_seconds
        if job.clock_time is not None:
            if job.weekdays is not None and now.weekday() not in job.weekdays:
                return False
            if (now.hour, now.minute) != (job.clock_time.hour, job.clock_time.minute):
                return False
            # Don't run twice in the same minute even if the loop ticks
            # twice within it (eg. after a long sleep that finally lets
            # multiple jobs fire back-to-back).
            if (job.last_run_at is not None
                    and job.last_run_at.replace(second=0, microsecond=0)
                        == now.replace(second=0, microsecond=0)):
                return False
            return True
        return False
