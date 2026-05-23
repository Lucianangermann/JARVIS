"""Apple Music control via AppleScript."""
from __future__ import annotations

import subprocess


def _script(code: str, timeout: float = 5.0) -> tuple[str, bool]:
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


def play() -> tuple[str, bool]:
    return _script('tell application "Music" to play')


def pause() -> tuple[str, bool]:
    return _script('tell application "Music" to pause')


def next_track() -> tuple[str, bool]:
    out, err = _script('tell application "Music" to next track')
    if err:
        return out, True
    return current_track()


def previous_track() -> tuple[str, bool]:
    out, err = _script('tell application "Music" to previous track')
    if err:
        return out, True
    return current_track()


def current_track() -> tuple[str, bool]:
    code = '''
tell application "Music"
    if player state is stopped then return "Musik ist gestoppt."
    set t to name of current track
    set a to artist of current track
    set al to album of current track
    return t & " – " & a & " (" & al & ")"
end tell'''
    return _script(code)


def set_volume(level: int) -> tuple[str, bool]:
    level = max(0, min(100, level))
    out, err = _script(f'tell application "Music" to set sound volume to {level}')
    if err:
        return out, True
    return f"Lautstärke auf {level}% gesetzt.", False


def get_volume() -> tuple[str, bool]:
    return _script('tell application "Music" to return sound volume')


def play_by_name(query: str) -> tuple[str, bool]:
    """Search for a track/artist/album and play the first match."""
    code = f'''
tell application "Music"
    set results to search playlist "Mediathek" for "{query}"
    if results is {{}} then
        return "not_found"
    end if
    play item 1 of results
    set t to name of current track
    set a to artist of current track
    return "playing:" & t & " – " & a
end tell'''
    out, err = _script(code, timeout=8.0)
    if err:
        return out, True
    if out == "not_found":
        return f"Kein Treffer für '{query}' in der Mediathek.", True
    label = out.removeprefix("playing:")
    return f"Spiele jetzt: {label}", False


def toggle_shuffle(on: bool) -> tuple[str, bool]:
    val = "true" if on else "false"
    out, err = _script(f'tell application "Music" to set shuffle enabled to {val}')
    if err:
        return out, True
    state = "an" if on else "aus"
    return f"Zufallswiedergabe {state}geschaltet.", False


def player_state() -> tuple[str, bool]:
    code = '''
tell application "Music"
    set s to player state
    if s is playing then
        set t to name of current track
        set a to artist of current track
        return "playing:" & t & " – " & a
    else if s is paused then
        return "paused"
    else
        return "stopped"
    end if
end tell'''
    return _script(code)
