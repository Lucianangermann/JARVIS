"""Fetch upcoming birthdays from macOS Contacts via AppleScript."""
from __future__ import annotations

import subprocess


def get_upcoming_birthdays(days_ahead: int = 7) -> tuple[str, bool]:
    """Return spoken string of upcoming birthdays within days_ahead days."""
    script = f"""
tell application "Contacts"
    set output to ""
    set today to current date
    set checkEnd to today + ({days_ahead} * days)
    repeat with p in every person
        try
            set bd to birth date of p
            if bd is not missing value then
                set thisYear to year of today
                set bdThisYear to bd
                set year of bdThisYear to thisYear
                if bdThisYear >= today and bdThisYear <= checkEnd then
                    set fn to ""
                    set ln to ""
                    try
                        set fn to first name of p
                    end try
                    try
                        set ln to last name of p
                    end try
                    set output to output & fn & " " & ln & ": " & ((month of bd) as integer) & "/" & (day of bd) & "\\n"
                end if
            end if
        end try
    end repeat
    return output
end tell
"""
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        raw = p.stdout.strip()
        if not raw:
            return f"Keine Geburtstage in den nächsten {days_ahead} Tagen.", False

        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "Name: month/day"
            if ":" in line:
                name, date_part = line.rsplit(":", 1)
                name = name.strip()
                date_part = date_part.strip()
                try:
                    month, day = date_part.split("/")
                    entries.append(f"{name} am {int(day)}.{int(month)}.")
                except ValueError:
                    entries.append(name)

        if not entries:
            return f"Keine Geburtstage in den nächsten {days_ahead} Tagen.", False
        return "Kommende Geburtstage: " + ", ".join(entries), False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung beim Lesen der Kontakte.", True
    except Exception as exc:  # noqa: BLE001
        return f"Geburtstags-Fehler: {exc}", True


def check_todays_birthdays() -> str | None:
    """Return name(s) if someone has a birthday today, else None."""
    script = """
tell application "Contacts"
    set output to ""
    set today to current date
    set todayMonth to (month of today) as integer
    set todayDay to day of today
    repeat with p in every person
        try
            set bd to birth date of p
            if bd is not missing value then
                if ((month of bd) as integer) = todayMonth and (day of bd) = todayDay then
                    set fn to ""
                    set ln to ""
                    try
                        set fn to first name of p
                    end try
                    try
                        set ln to last name of p
                    end try
                    set output to output & fn & " " & ln & "\\n"
                end if
            end if
        end try
    end repeat
    return output
end tell
"""
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        raw = p.stdout.strip()
        if not raw:
            return None
        names = [line.strip() for line in raw.splitlines() if line.strip()]
        return ", ".join(names) if names else None
    except Exception:  # noqa: BLE001
        return None
