"""Apple Reminders access via AppleScript."""
from __future__ import annotations

import subprocess
from datetime import datetime


def _script(code: str, timeout: float = 20.0) -> tuple[str, bool]:
    try:
        p = subprocess.run(["osascript", "-e", code],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return p.stderr.strip() or "AppleScript-Fehler", True
        return p.stdout.strip(), False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung", True
    except Exception as exc:  # noqa: BLE001
        return str(exc), True


def list_reminders(list_name: str | None = None) -> tuple[str, bool]:
    """Return incomplete reminders, optionally filtered to one list."""
    if list_name:
        code = f'''
tell application "Reminders"
    set out to ""
    try
        set rl to list "{list_name}"
        set rems to (reminders of rl whose completed is false)
        repeat with r in rems
            set n to name of r
            set d to ""
            try
                set d to (due date of r) as text
            end try
            set out to out & n & tab & d & linefeed
        end repeat
    end try
    return out
end tell'''
    else:
        code = '''
tell application "Reminders"
    set out to ""
    repeat with rl in lists
        set rems to (reminders of rl whose completed is false)
        repeat with r in rems
            set n to name of r
            set ln to name of rl
            set d to ""
            try
                set d to (due date of r) as text
            end try
            set out to out & ln & tab & n & tab & d & linefeed
        end repeat
    end repeat
    return out
end tell'''
    raw, err = _script(code)
    if err:
        return raw, True
    if not raw:
        return "Keine offenen Erinnerungen.", False
    lines = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if list_name:
            name = parts[0] if parts else ""
            due = parts[1] if len(parts) > 1 else ""
            lines.append(f"• {name}" + (f" (fällig: {due})" if due else ""))
        else:
            lst = parts[0] if parts else ""
            name = parts[1] if len(parts) > 1 else ""
            due = parts[2] if len(parts) > 2 else ""
            lines.append(f"[{lst}] {name}" + (f" (fällig: {due})" if due else ""))
    return "\n".join(lines), False


def create_reminder(title: str, list_name: str | None = None,
                    due_date: str | None = None) -> tuple[str, bool]:
    """Create a new reminder. due_date as ISO string e.g. '2026-05-24T09:00'."""
    list_part = f'set rl to list "{list_name}"' if list_name else \
        'set rl to default list'

    date_part = ""
    if due_date:
        try:
            dt = datetime.fromisoformat(due_date)
            date_part = (
                f'set due date of nr to '
                f'(current date) - (time of (current date)) '
                f'+ {dt.hour * 3600 + dt.minute * 60} * seconds\n'
                f'                + {(dt - datetime.now()).days} * days'
            )
        except ValueError:
            pass

    code = f'''
tell application "Reminders"
    {list_part}
    set nr to make new reminder at end of rl with properties {{name:"{title}"}}
    {date_part}
    return name of nr
end tell'''
    out, err = _script(code)
    if err:
        return out, True
    return f"Erinnerung '{title}' erstellt.", False


def complete_reminder(title: str, list_name: str | None = None) -> tuple[str, bool]:
    """Mark the first matching reminder as complete."""
    list_part = (f'set rl to list "{list_name}"\n'
                 f'            set rems to reminders of rl whose name is "{title}"') \
        if list_name else \
        (f'set rems to {{}}\n'
         f'            repeat with rl in lists\n'
         f'                set rems to rems & (reminders of rl whose name is "{title}")\n'
         f'            end repeat')

    code = f'''
tell application "Reminders"
    {list_part}
    if length of rems > 0 then
        set completed of item 1 of rems to true
        return "done"
    else
        return "not_found"
    end if
end tell'''
    out, err = _script(code)
    if err:
        return out, True
    if out == "not_found":
        return f"Keine Erinnerung '{title}' gefunden.", True
    return f"Erinnerung '{title}' als erledigt markiert.", False


def list_reminder_lists() -> tuple[str, bool]:
    code = 'tell application "Reminders" to return name of every list'
    out, err = _script(code)
    if err:
        return out, True
    return out, False
