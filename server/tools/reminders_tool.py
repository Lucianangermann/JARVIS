"""Apple Reminders — fast hybrid reader.

Reading: SQLite direct (sub-second) + fast osascript for list names only.
Writing: osascript (create / complete — infrequent, latency acceptable).

Background: `whose completed is false` in the Reminders AppleScript
dictionary causes it to load every reminder object — on a 500+ entry
DB this reliably exceeds 30 s. Reading the underlying Core Data SQLite
store directly avoids all of that.
"""
from __future__ import annotations

import datetime
import glob
import os
import sqlite3
import subprocess

# Core Data timestamps are seconds since the reference epoch 2001-01-01.
_CD_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
_LOCAL_TZ = datetime.timezone(
    datetime.timedelta(seconds=-time.timezone) if False else datetime.timedelta()
)


def _db_path() -> str | None:
    """Return the Reminders SQLite file that contains the most reminders."""
    pattern = os.path.expanduser(
        "~/Library/Reminders/Container_v1/Stores/Data-*.sqlite"
    )
    best_path, best_count = None, -1
    for p in glob.glob(pattern):
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
            n = con.execute("SELECT COUNT(*) FROM ZREMCDREMINDER").fetchone()[0]
            con.close()
            if n > best_count:
                best_count, best_path = n, p
        except Exception:  # noqa: BLE001
            pass
    return best_path


def _list_name_map() -> dict[int, str]:
    """Fast osascript call: get list names in order, map to SQLite ZLIST ids."""
    p = subprocess.run(
        ["osascript", "-e",
         'tell application "Reminders" to return name of every list'],
        capture_output=True, text=True, timeout=10,
    )
    if p.returncode != 0 or not p.stdout.strip():
        return {}
    names = [n.strip() for n in p.stdout.strip().split(",")]
    db = _db_path()
    if not db:
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT ZLIST FROM ZREMCDREMINDER "
            "WHERE ZLIST IS NOT NULL ORDER BY ZLIST"
        ).fetchall()]
        con.close()
    except Exception:  # noqa: BLE001
        return {}
    return {lid: names[i] for i, lid in enumerate(ids) if i < len(names)}


def _fmt_due(ts: float | None) -> str:
    if ts is None:
        return ""
    dt = (_CD_EPOCH + datetime.timedelta(seconds=ts)).astimezone()
    return dt.strftime("%d.%m. %H:%M")


# ── Public API ────────────────────────────────────────────────────── #

def list_reminders(list_name: str | None = None) -> tuple[str, bool]:
    db = _db_path()
    if not db:
        return "Reminders-Datenbank nicht gefunden.", True
    id_map = _list_name_map()
    name_map = {v.lower(): k for k, v in id_map.items()}  # name→id
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        if list_name:
            lid = name_map.get(list_name.lower())
            if lid is None:
                con.close()
                return f"Liste '{list_name}' nicht gefunden.", True
            rows = con.execute(
                "SELECT ZTITLE, ZDUEDATE FROM ZREMCDREMINDER "
                "WHERE ZCOMPLETED=0 AND ZMARKEDFORDELETION=0 AND ZLIST=? "
                "ORDER BY ZDUEDATE NULLS LAST", (lid,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT ZTITLE, ZDUEDATE, ZLIST FROM ZREMCDREMINDER "
                "WHERE ZCOMPLETED=0 AND ZMARKEDFORDELETION=0 "
                "ORDER BY ZDUEDATE NULLS LAST"
            ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        return f"Datenbankfehler: {exc}", True

    if not rows:
        return "Keine offenen Erinnerungen.", False

    lines = []
    for row in rows:
        if list_name:
            title, due = row
            due_str = f" (fällig: {_fmt_due(due)})" if due else ""
            lines.append(f"• {title}{due_str}")
        else:
            title, due, lid = row
            lname = id_map.get(lid, "?")
            due_str = f" (fällig: {_fmt_due(due)})" if due else ""
            lines.append(f"[{lname}] {title}{due_str}")
    return "\n".join(lines), False


def list_reminder_lists() -> tuple[str, bool]:
    p = subprocess.run(
        ["osascript", "-e",
         "tell application \"Reminders\" to return name of every list"],
        capture_output=True, text=True, timeout=10,
    )
    if p.returncode != 0:
        return p.stderr.strip() or "Fehler beim Abrufen der Listen.", True
    return p.stdout.strip(), False


def create_reminder(title: str, list_name: str | None = None,
                    due_date: str | None = None) -> tuple[str, bool]:
    list_part = f'set rl to list "{list_name}"' if list_name else \
        "set rl to default list"
    date_part = ""
    if due_date:
        try:
            dt = datetime.datetime.fromisoformat(due_date)
            date_part = (
                f'set due date of nr to date "{dt.strftime("%d.%m.%Y %H:%M:%S")}"'
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
    try:
        p = subprocess.run(["osascript", "-e", code],
                           capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            return p.stderr.strip() or "Fehler beim Erstellen.", True
        return f"Erinnerung '{title}' erstellt.", False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung beim Erstellen der Erinnerung.", True


def complete_reminder(title: str, list_name: str | None = None) -> tuple[str, bool]:
    list_part = (
        f'set rems to reminders of list "{list_name}" whose name is "{title}"'
        if list_name else
        f'set rems to {{}}\nrepeat with rl in lists\n'
        f'set rems to rems & (reminders of rl whose name is "{title}")\nend repeat'
    )
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
    try:
        p = subprocess.run(["osascript", "-e", code],
                           capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            return p.stderr.strip() or "Fehler.", True
        if p.stdout.strip() == "not_found":
            return f"Keine Erinnerung '{title}' gefunden.", True
        return f"Erinnerung '{title}' als erledigt markiert.", False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung.", True
