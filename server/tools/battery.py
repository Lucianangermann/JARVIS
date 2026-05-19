"""Read MacBook battery state via ``pmset -g batt``.

Public surface is a single function ``get_status()`` returning a
``BatteryStatus`` snapshot or ``None`` if the machine has no battery
(Mac mini, Mac Studio) or pmset is unavailable. The proactive engine
calls this once a minute; we deliberately avoid PyObjC / IOKit to keep
the dependency footprint flat.

Sample pmset output (MacBook Pro, AC plugged in):

    Now drawing from 'AC Power'
     -InternalBattery-0 (id=…)	49%; charging; 2:02 remaining present: true

Sample on battery:

    Now drawing from 'Battery Power'
     -InternalBattery-0 (id=…)	37%; discharging; 3:45 remaining present: true
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_PMSET_TIMEOUT_S = 2.0  # pmset is fast; anything slower is a hung subsystem.

# Captures "49%" and the state word ("charging" / "discharging" /
# "AC attached" / "finishing charge"). One regex over the whole blob
# is simpler than parsing the multi-line output line by line.
_PCT_RE = re.compile(r"(\d{1,3})%\s*;\s*([A-Za-z ]+?)\s*[;\n]")


@dataclass(frozen=True)
class BatteryStatus:
    percent: int
    charging: bool
    on_ac: bool  # plugged into wall power, even if "not charging"


def get_status() -> BatteryStatus | None:
    """Snapshot the current battery state. Returns ``None`` on any
    parsing or subprocess failure — callers treat that as 'unknown'
    and skip the battery-driven trigger this tick."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=_PMSET_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout or ""
    if not text:
        return None

    # First line tells us the power source — robust against the
    # state-word in line 2 varying ("charging" vs "AC attached" vs
    # "finishing charge"). If that word is missing entirely (some
    # desktops) we fall back to the source line.
    on_ac = "AC Power" in text.splitlines()[0] if text else False

    m = _PCT_RE.search(text)
    if m is None:
        return None
    try:
        pct = int(m.group(1))
    except ValueError:
        return None
    pct = max(0, min(100, pct))
    state = m.group(2).strip().lower()
    charging = ("charging" in state) and ("not" not in state) \
               and ("finishing" not in state or True)
    # "finishing charge" still counts as charging for our purposes —
    # we don't want to fire a low-battery warning while the cable is
    # in and the battery is at 99%.

    return BatteryStatus(percent=pct, charging=charging, on_ac=on_ac)
