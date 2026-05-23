"""Apple Mail access via AppleScript."""
from __future__ import annotations

import subprocess


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


def list_unread(mailbox: str = "INBOX", limit: int = 10) -> tuple[str, bool]:
    code = f'''
tell application "Mail"
    set out to ""
    set cnt to 0
    repeat with a in accounts
        try
            set mb to mailbox "{mailbox}" of a
            set msgs to (messages of mb whose read status is false)
            repeat with m in msgs
                if cnt >= {limit} then exit repeat
                set s to subject of m
                set sndr to sender of m
                set dt to date received of m as text
                set out to out & sndr & tab & s & tab & dt & linefeed
                set cnt to cnt + 1
            end repeat
        end try
    end repeat
    return out
end tell'''
    raw, err = _script(code)
    if err:
        return raw, True
    if not raw:
        return "Keine ungelesenen Mails.", False
    lines = []
    for line in raw.splitlines():
        parts = line.split("\t")
        sender = parts[0] if parts else ""
        subject = parts[1] if len(parts) > 1 else ""
        date = parts[2] if len(parts) > 2 else ""
        lines.append(f"Von: {sender}\nBetreff: {subject}\nErhalten: {date}\n")
    return "\n".join(lines), False


def read_message(subject_fragment: str) -> tuple[str, bool]:
    safe = subject_fragment.replace('"', '\\"')
    code = f'''
tell application "Mail"
    repeat with a in accounts
        try
            repeat with mb in mailboxes of a
                set matches to (messages of mb whose subject contains "{safe}")
                if length of matches > 0 then
                    set m to item 1 of matches
                    return "Von: " & sender of m & linefeed & ¬
                           "Betreff: " & subject of m & linefeed & ¬
                           "Datum: " & (date received of m as text) & linefeed & ¬
                           "Inhalt:" & linefeed & content of m
                end if
            end repeat
        end try
    end repeat
    return "not_found"
end tell'''
    out, err = _script(code, timeout=12.0)
    if err:
        return out, True
    if out == "not_found":
        return f"Keine Mail mit '{subject_fragment}' im Betreff gefunden.", True
    return out[:3000], False  # Cap at 3000 chars


def send_message(to: str, subject: str, body: str) -> tuple[str, bool]:
    safe_to = to.replace('"', '\\"')
    safe_sub = subject.replace('"', '\\"')
    safe_body = body.replace('"', '\\"').replace("\n", "\\n")
    code = f'''
tell application "Mail"
    set msg to make new outgoing message with properties ¬
        {{subject:"{safe_sub}", content:"{safe_body}", visible:true}}
    tell msg
        make new to recipient with properties {{address:"{safe_to}"}}
    end tell
    send msg
    return "sent"
end tell'''
    out, err = _script(code, timeout=15.0)
    if err:
        return out, True
    return f"E-Mail an {to} gesendet.", False


def get_unread_count() -> tuple[str, bool]:
    code = '''
tell application "Mail"
    set cnt to 0
    repeat with a in accounts
        try
            set mb to mailbox "INBOX" of a
            set cnt to cnt + (unread count of mb)
        end try
    end repeat
    return cnt as text
end tell'''
    return _script(code)
