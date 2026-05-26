"""Mood-to-music mapping for Spotify and Apple Music."""
from __future__ import annotations

import os
import subprocess

MOOD_MAP: dict[str, dict[str, str]] = {
    "entspannt": {"spotify_query": "chill lofi playlist", "apple_genre": "Electronic"},
    "konzentriert": {"spotify_query": "deep focus study playlist", "apple_genre": "Electronic"},
    "glücklich": {"spotify_query": "happy good vibes playlist", "apple_genre": "Pop"},
    "traurig": {"spotify_query": "sad emotional playlist", "apple_genre": "Singer/Songwriter"},
    "sport": {"spotify_query": "workout motivation playlist", "apple_genre": "Electronic"},
    "party": {"spotify_query": "party hits playlist", "apple_genre": "Dance"},
    "schlafen": {"spotify_query": "sleep calm ambient playlist", "apple_genre": "Ambient"},
    "romantisch": {"spotify_query": "romantic dinner playlist", "apple_genre": "Jazz"},
    "morgen": {"spotify_query": "good morning acoustic playlist", "apple_genre": "Acoustic"},
    "gaming": {"spotify_query": "gaming epic playlist", "apple_genre": "Electronic"},
}


def _find_mood(mood_str: str) -> tuple[str, dict[str, str]] | None:
    """Find matching mood entry by exact or substring match."""
    normalized = mood_str.lower().strip()
    # Exact match first
    if normalized in MOOD_MAP:
        return normalized, MOOD_MAP[normalized]
    # Fuzzy: check if any MOOD_MAP key is contained in the mood string
    for key, val in MOOD_MAP.items():
        if key in normalized:
            return key, val
    return None


def _play_spotify_uri(uri: str) -> tuple[str, bool]:
    """Play a Spotify URI via AppleScript."""
    script = f'tell application "Spotify" to play track "{uri}"'
    try:
        p = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode != 0:
            return p.stderr.strip() or "Spotify AppleScript-Fehler", True
        return "", False
    except subprocess.TimeoutExpired:
        return "Zeitüberschreitung beim Starten von Spotify", True
    except Exception as exc:  # noqa: BLE001
        return str(exc), True


def play_mood(mood: str, prefer_spotify: bool = True) -> tuple[str, bool]:
    """Play music matching the given mood.

    Returns (spoken_message, is_error).
    """
    match = _find_mood(mood)
    if match is None:
        # Default to entspannt if nothing matched
        match = ("entspannt", MOOD_MAP["entspannt"])

    mood_key, mapping = match

    # Try Spotify if credentials are available and preferred
    if prefer_spotify and os.environ.get("SPOTIFY_CLIENT_ID"):
        try:
            from .. import spotify as _spotify
            result = _spotify.search(mapping["spotify_query"], "playlist")
            if result and result.get("uri"):
                uri = result["uri"]
                playlist_name = result.get("name", mapping["spotify_query"])
                err_msg, is_err = _play_spotify_uri(uri)
                if not is_err:
                    return (
                        f"Spiele {mood_key}-Musik: {playlist_name}",
                        False,
                    )
                # Spotify failed, fall through to Apple Music
        except Exception:  # noqa: BLE001
            pass  # Fall through to Apple Music

    # Apple Music fallback
    try:
        from ..tools.music_tool import play_by_name as _play_by_name
        genre = mapping["apple_genre"]
        msg, is_err = _play_by_name(genre)
        if is_err:
            return f"Spiele {mood_key}-Musik.", False
        return f"Spiele {mood_key}-Musik: {genre}", False
    except Exception as exc:  # noqa: BLE001
        return f"Musik-Fehler: {exc}", True
