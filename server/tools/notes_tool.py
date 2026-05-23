"""Apple Notes access via AppleScript."""
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


def list_notes(folder: str | None = None, limit: int = 20) -> tuple[str, bool]:
    if folder:
        code = f'''
tell application "Notes"
    set out to ""
    try
        set f to folder "{folder}"
        set ns to notes of f
        set cnt to 0
        repeat with n in ns
            if cnt >= {limit} then exit repeat
            set out to out & name of n & linefeed
            set cnt to cnt + 1
        end repeat
    end try
    return out
end tell'''
    else:
        code = f'''
tell application "Notes"
    set out to ""
    set cnt to 0
    repeat with n in notes
        if cnt >= {limit} then exit repeat
        set out to out & name of n & linefeed
        set cnt to cnt + 1
    end repeat
    return out
end tell'''
    raw, err = _script(code)
    if err:
        return raw, True
    if not raw:
        return "Keine Notizen gefunden.", False
    return raw, False


def read_note(title: str) -> tuple[str, bool]:
    code = f'''
tell application "Notes"
    set matches to notes whose name is "{title}"
    if length of matches = 0 then
        return "not_found"
    end if
    set n to item 1 of matches
    return body of n
end tell'''
    out, err = _script(code)
    if err:
        return out, True
    if out == "not_found":
        return f"Keine Notiz mit dem Titel '{title}' gefunden.", True
    # Strip HTML tags (Notes stores body as HTML)
    import re
    text = re.sub(r"<[^>]+>", "", out).strip()
    return text or "(Leere Notiz)", False


def create_note(title: str, content: str,
                folder: str | None = None) -> tuple[str, bool]:
    folder_part = f'set f to folder "{folder}"\n        make new note at f' \
        if folder else 'make new note at default account'

    safe_title = title.replace('"', '\\"')
    safe_content = content.replace('"', '\\"').replace("\n", "\\n")
    code = f'''
tell application "Notes"
    set n to ({folder_part} with properties {{name:"{safe_title}", body:"{safe_content}"}})
    return name of n
end tell'''
    out, err = _script(code)
    if err:
        return out, True
    return f"Notiz '{title}' erstellt.", False


def search_notes(query: str, limit: int = 10) -> tuple[str, bool]:
    q = query.replace('"', '\\"')
    code = f'''
tell application "Notes"
    set out to ""
    set cnt to 0
    repeat with n in notes
        if cnt >= {limit} then exit repeat
        set t to name of n
        set b to body of n
        if t contains "{q}" or b contains "{q}" then
            set out to out & t & linefeed
            set cnt to cnt + 1
        end if
    end repeat
    return out
end tell'''
    raw, err = _script(code, timeout=10.0)
    if err:
        return raw, True
    if not raw:
        return f"Keine Notizen mit '{query}' gefunden.", False
    return raw, False


def append_to_note(title: str, text: str) -> tuple[str, bool]:
    safe_title = title.replace('"', '\\"')
    safe_text = text.replace('"', '\\"').replace("\n", "\\n")
    code = f'''
tell application "Notes"
    set matches to notes whose name is "{safe_title}"
    if length of matches = 0 then return "not_found"
    set n to item 1 of matches
    set body of n to (body of n) & "<br>{safe_text}"
    return "ok"
end tell'''
    out, err = _script(code)
    if err:
        return out, True
    if out == "not_found":
        return f"Notiz '{title}' nicht gefunden.", True
    return f"Text zu '{title}' hinzugefügt.", False
