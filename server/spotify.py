"""Tiny Spotify Web API wrapper — search only, no user auth.

We use the **Client Credentials** OAuth flow, which gives us a bearer
token good for searching the public catalogue (tracks, albums, public
playlists, artists). This flow does not require any user login — JARVIS
just needs a Spotify *developer app* with a client id + secret.

Setup
-----
1. Go to https://developer.spotify.com/dashboard
2. *Create app* → any name, any description.
3. *Settings* → copy *Client ID* and *Client Secret*.
4. Add both to ``.env``::

       SPOTIFY_CLIENT_ID=...
       SPOTIFY_CLIENT_SECRET=...

Playback itself is handled by the local Spotify desktop app via
AppleScript (see ``command_guard.py``); this module only resolves a
natural-language query to a ``spotify:track:…`` / ``spotify:playlist:…``
URI.
"""
from __future__ import annotations

import base64
import threading
import time

import requests

from .config import settings

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

_token: str | None = None
_token_expiry: float = 0.0
_token_lock = threading.Lock()


class SpotifyConfigError(RuntimeError):
    """Raised when SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET aren't set."""


def _get_token() -> str:
    """Return a cached access token, refreshing when within 30 s of expiry."""
    global _token, _token_expiry
    with _token_lock:
        if _token and time.monotonic() < _token_expiry - 30:
            return _token

        if not settings.SPOTIFY_CLIENT_ID or not settings.SPOTIFY_CLIENT_SECRET:
            raise SpotifyConfigError(
                "Spotify-Suche braucht SPOTIFY_CLIENT_ID und "
                "SPOTIFY_CLIENT_SECRET in .env. Anlegen unter "
                "https://developer.spotify.com/dashboard."
            )

        creds = f"{settings.SPOTIFY_CLIENT_ID}:{settings.SPOTIFY_CLIENT_SECRET}"
        auth = base64.b64encode(creds.encode("utf-8")).decode("ascii")

        resp = requests.post(
            _TOKEN_URL,
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _token = data["access_token"]
        _token_expiry = time.monotonic() + float(data.get("expires_in", 3600))
        return _token


def search(query: str, kind: str = "track", limit: int = 1) -> dict | None:
    """Search Spotify; return the top result dict, or None on no match.

    ``kind`` is one of ``track``, ``playlist``, ``album``, ``artist``.
    Returned dict has at least ``uri`` and ``name`` keys.
    """
    if kind not in {"track", "playlist", "album", "artist"}:
        raise ValueError(f"unsupported search kind: {kind!r}")
    if not query or not query.strip():
        raise ValueError("query must be non-empty")

    token = _get_token()
    params = {"q": query.strip(), "type": kind, "limit": str(limit)}
    if settings.SPOTIFY_MARKET:
        params["market"] = settings.SPOTIFY_MARKET

    resp = requests.get(
        _SEARCH_URL,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code == 401:
        # Token expired between cache check and request — retry once.
        global _token
        _token = None
        token = _get_token()
        resp = requests.get(
            _SEARCH_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    if resp.status_code != 200:
        return None

    data = resp.json()
    bucket = data.get(f"{kind}s", {}) or {}
    items = bucket.get("items") or []
    # Some search results include None entries (e.g. removed playlists).
    items = [it for it in items if it]
    if not items:
        return None
    item = items[0]
    return {
        "uri": item.get("uri"),
        "name": item.get("name"),
        "owner": (item.get("owner") or {}).get("display_name"),  # playlists
        "artists": ", ".join(a.get("name", "") for a in item.get("artists", []))
                   if item.get("artists") else "",
    }
