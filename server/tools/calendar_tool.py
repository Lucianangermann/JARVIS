"""Read events from macOS Calendar.app via AppleScript.

The first call triggers a TCC "Calendars access" prompt; until the
user grants it, osascript returns an error and we degrade to an
empty list (with a single warning logged the first time). All
public functions return native ``CalendarEvent`` objects so the
intelligence layer doesn't have to know about AppleScript.

Why AppleScript: the macOS Calendar app stores events in
``~/Library/Calendars/`` in an Apple-private binary format. The
sanctioned read paths from outside Swift are EventKit (PyObjC, but
the framework hates being driven from a non-app process and the
permission prompts behave oddly) and AppleScript via osascript,
which is what we use here. It's slow for large databases but a
24-hour window is fast enough for the morning briefing.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_LOCAL_TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Europe/Berlin"))

# ── Cache + timeout settings ────────────────────────────────────────
# AppleScript Calendar.app queries are slow at the best of times
# (200-800 ms for a small DB) and catastrophic when something pins
# Calendar.app or its sync agents — we've seen consistent 15 s
# timeouts. Every chat turn calls get_context_for_brain() which hits
# get_next_event() + get_today_events(), so an unhealthy Calendar
# adds 30 s of latency to every reply.
#
# Two defences:
#   1. A short TTL cache so back-to-back chat turns share one
#      AppleScript invocation.
#   2. A shorter osascript timeout — 3 s instead of 15 s. If
#      Calendar can't answer in 3 s it's broken; we'd rather have a
#      stale-but-fresh-ish empty list than block the user for 15.
_OSASCRIPT_TIMEOUT_S = float(os.getenv("CALENDAR_OSASCRIPT_TIMEOUT_S", "3.0"))
_CACHE_TTL_S = float(os.getenv("CALENDAR_CACHE_TTL_S", "30"))
_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: datetime
    end: datetime
    location: str
    calendar_name: str

    @property
    def is_all_day(self) -> bool:
        # All-day events in Calendar.app are midnight-to-midnight in
        # the user's local zone; some span exactly 24h, some end one
        # second before midnight. The 23h floor catches both.
        local_start = self.start.astimezone(_LOCAL_TZ)
        return (local_start.hour == 0 and local_start.minute == 0
                and (self.end - self.start) >= timedelta(hours=23))


# Build the AppleScript with the offsets already baked in via Python
# string formatting; passing args through `osascript -e` argv is
# fiddly and using AS's `date "..."` parser pulls in locale-specific
# date string formats. Numeric offsets from `current date` work
# everywhere regardless of system language.
_SCRIPT_TEMPLATE = r"""
on iso(d)
    set y to year of d as integer
    set mo to (month of d as integer)
    set da to day of d
    set h to hours of d
    set mi to minutes of d
    set s to seconds of d
    return (y as text) & "-" & ¬
        text -2 thru -1 of ("0" & mo) & "-" & ¬
        text -2 thru -1 of ("0" & da) & "T" & ¬
        text -2 thru -1 of ("0" & h) & ":" & ¬
        text -2 thru -1 of ("0" & mi) & ":" & ¬
        text -2 thru -1 of ("0" & s)
end iso

set startOffset to {start_offset}
set endOffset to {end_offset}
set rangeStart to (current date) + startOffset
set rangeEnd to (current date) + endOffset
set out to ""
tell application "Calendar"
    repeat with c in calendars
        try
            set theEvents to (every event of c whose ¬
                (start date >= rangeStart) and (start date < rangeEnd))
        on error
            set theEvents to {{}}
        end try
        repeat with ev in theEvents
            set evTitle to summary of ev
            if evTitle is missing value then set evTitle to ""
            set evLoc to location of ev
            if evLoc is missing value then set evLoc to ""
            set out to out & evTitle & tab & ¬
                my iso(start date of ev) & tab & ¬
                my iso(end date of ev) & tab & ¬
                (title of c) & tab & evLoc & linefeed
        end repeat
    end repeat
end tell
return out
"""

# One-shot flag so we only emit the "calendar access denied" warning
# once per process — otherwise the morning briefing would print it
# every single time the user hasn't granted permission yet.
_warned_about_permission = False


def _run_applescript(script: str) -> str | None:
    global _warned_about_permission
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"[calendar] osascript timed out ({_OSASCRIPT_TIMEOUT_S:.0f}s)")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[calendar] osascript failed to spawn: {exc}")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # 1743 is the canonical "Not authorised to send Apple events
        # to Calendar" code; -1743 is its TCC variant. Tag those as
        # permission errors so the user sees an actionable message.
        if "1743" in err or "Calendar" in err and "not allowed" in err.lower():
            if not _warned_about_permission:
                print("[calendar] permission denied — grant 'Kalender' "
                      "access to Terminal in System Settings → Privacy")
                _warned_about_permission = True
            return None
        print(f"[calendar] osascript exited {proc.returncode}: {err[:200]}")
        return None
    return proc.stdout


def _build_script(range_start: datetime, range_end: datetime) -> str:
    now = datetime.now(_LOCAL_TZ)
    start_offset = int((range_start - now).total_seconds())
    end_offset = int((range_end - now).total_seconds())
    return _SCRIPT_TEMPLATE.format(
        start_offset=start_offset, end_offset=end_offset,
    )


def get_events(range_start: datetime, range_end: datetime) -> list[CalendarEvent]:
    """All events whose start falls in ``[range_start, range_end)``,
    sorted ascending. Cached for ``_CACHE_TTL_S`` to avoid hammering
    AppleScript from every chat turn — Calendar's contents move on
    the order of minutes, not seconds, so a 30 s stale window is
    invisible to the user while saving repeated 200-800 ms
    osascript invocations (and 3 s timeouts when it's broken)."""
    if range_start.tzinfo is None:
        range_start = range_start.replace(tzinfo=_LOCAL_TZ)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=_LOCAL_TZ)
    # Bucket the range bounds to minute resolution for the cache key
    # — back-to-back calls with timestamps milliseconds apart hit the
    # same cache entry instead of missing on a different microsecond.
    cache_key = (
        range_start.replace(second=0, microsecond=0).isoformat(),
        range_end.replace(second=0, microsecond=0).isoformat(),
    )
    now_mono = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and (now_mono - hit[0]) < _CACHE_TTL_S:
            return list(hit[1])
    out = _run_applescript(_build_script(range_start, range_end))
    if not out:
        # Cache the empty result too — if AppleScript timed out or
        # was denied, we don't want every following turn to pay the
        # same timeout. We use a SHORTER TTL for negative results so
        # transient outages clear within a minute.
        with _cache_lock:
            _cache[cache_key] = (now_mono - _CACHE_TTL_S / 2, [])
        return []
    events: list[CalendarEvent] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        title, start_iso, end_iso, cal, loc = (p.strip() for p in parts[:5])
        try:
            start = datetime.fromisoformat(start_iso).replace(tzinfo=_LOCAL_TZ)
            end   = datetime.fromisoformat(end_iso).replace(tzinfo=_LOCAL_TZ)
        except ValueError:
            continue
        events.append(CalendarEvent(
            title=title, start=start, end=end,
            location=loc, calendar_name=cal,
        ))
    events.sort(key=lambda e: e.start)
    # Cache positive AND negative results — a timeout/permission
    # denial that returns [] is just as worth caching, otherwise we
    # keep paying the full timeout on every chat turn.
    with _cache_lock:
        _cache[cache_key] = (now_mono, list(events))
    return events


def get_today_events() -> list[CalendarEvent]:
    """Events that start between local-midnight today and local-midnight
    tomorrow."""
    now = datetime.now(_LOCAL_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    return get_events(start, end)


def get_next_event() -> CalendarEvent | None:
    """The next event starting after ``now`` within the next 48 hours."""
    now = datetime.now(_LOCAL_TZ)
    for ev in get_events(now, now + timedelta(days=2)):
        if ev.start > now:
            return ev
    return None
