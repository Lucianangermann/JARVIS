"""Safari control via AppleScript — open URLs, read current page, search."""
from __future__ import annotations

import subprocess
import urllib.parse


def _script(code: str, timeout: float = 8.0) -> tuple[str, bool]:
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


def open_url(url: str) -> tuple[str, bool]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    safe = url.replace('"', '%22')
    code = f'''
tell application "Safari"
    activate
    if (count of windows) = 0 then make new document
    set URL of front document to "{safe}"
end tell'''
    _, err_out = _script(code)
    if err_out:
        return err_out, True
    return f"Safari öffnet: {url}", False


def search_in_safari(query: str) -> tuple[str, bool]:
    """Open DuckDuckGo search in Safari."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/?q={encoded}"
    return open_url(url)


def current_url() -> tuple[str, bool]:
    code = '''
tell application "Safari"
    if (count of windows) = 0 then return "Kein Fenster offen."
    return URL of front document
end tell'''
    return _script(code)


def current_title() -> tuple[str, bool]:
    code = '''
tell application "Safari"
    if (count of windows) = 0 then return "Kein Fenster offen."
    return name of front document
end tell'''
    return _script(code)


def current_page_text() -> tuple[str, bool]:
    """Extract visible text from the current Safari page (via JavaScript)."""
    code = '''
tell application "Safari"
    if (count of windows) = 0 then return "Kein Fenster offen."
    set txt to do JavaScript "document.body.innerText" in front document
    return txt
end tell'''
    out, err = _script(code, timeout=10.0)
    if err:
        return out, True
    return out[:4000], False  # Cap to avoid overwhelming Claude context


def navigate_back() -> tuple[str, bool]:
    code = 'tell application "Safari" to do JavaScript "history.back()" in front document'
    _, err = _script(code)
    if err:
        return err, True
    return "Safari: zurück navigiert.", False


def navigate_forward() -> tuple[str, bool]:
    code = 'tell application "Safari" to do JavaScript "history.forward()" in front document'
    _, err = _script(code)
    if err:
        return err, True
    return "Safari: vorwärts navigiert.", False


def open_new_tab(url: str | None = None) -> tuple[str, bool]:
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    url_part = f'set URL of new_tab to "{url}"' if url else ""
    code = f'''
tell application "Safari"
    activate
    if (count of windows) = 0 then make new document
    tell front window
        set new_tab to make new tab
        set current tab to new_tab
        {url_part}
    end tell
end tell'''
    _, err = _script(code)
    if err:
        return err, True
    return "Neuer Tab geöffnet.", False
