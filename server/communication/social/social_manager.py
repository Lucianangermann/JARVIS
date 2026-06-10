"""Social media — Reddit (RSS), birthdays, Claude drafts.

Scope is deliberately narrow (user-chosen): Reddit via its free public
RSS, birthday reminders reusing the entertainment layer's Contacts
reader, and Claude-generated post drafts that are NEVER auto-posted — they
return text for the user to review/copy. Twitter and LinkedIn have no
usable free read API, so those methods are honest stubs that say so unless
a key is configured.
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from ...config import settings

try:
    import httpx  # type: ignore[import-not-found]
    _HTTPX_OK = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]
    _HTTPX_OK = False

_ATOM = "{http://www.w3.org/2005/Atom}"
_HTTP_TIMEOUT = 8.0

# Per-platform character limits for drafts.
_CHAR_LIMITS = {"twitter": 280, "x": 280, "linkedin": 3000, "reddit": 4000}


class SocialManager:
    def __init__(self, db: Any = None, client: Any = None,
                 default_subreddits: list[str] | None = None) -> None:
        self._db = db
        self._client = client
        self._subreddits = default_subreddits or ["technology", "programming"]

    # ── Reddit (free RSS) ──────────────────────────────────────────────── #

    async def get_reddit_feed(self, subreddits: list[str] | None = None,
                              per_sub: int = 3) -> str:
        if not _HTTPX_OK:
            return "Reddit-Feed nicht verfügbar (httpx fehlt)."
        subs = subreddits or self._subreddits
        lines: list[str] = []
        for sub in subs:
            posts = self._fetch_subreddit(sub, per_sub)
            if posts:
                lines.append(f"r/{sub}: " + "; ".join(posts))
        if not lines:
            return "Keine Reddit-Beiträge gefunden."
        return " ".join(lines)

    def _fetch_subreddit(self, sub: str, n: int) -> list[str]:
        try:
            r = httpx.get(
                f"https://www.reddit.com/r/{sub}/.rss",
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": "JARVIS/1.0 (reddit reader)"},
                follow_redirects=True,
            )
            r.raise_for_status()
            root = ET.fromstring(r.content)
            titles = [e.findtext(f"{_ATOM}title") for e in
                      root.findall(f"{_ATOM}entry")[:n]]
            return [t for t in titles if t]
        except Exception as exc:  # noqa: BLE001
            print(f"[Social] reddit r/{sub} fetch failed: {exc}")
            return []

    # ── birthdays (reuse entertainment) ────────────────────────────────── #

    async def get_birthday_reminders(self, days_ahead: int = 7) -> str:
        try:
            from ...entertainment.birthdays import get_upcoming_birthdays
            out, err = get_upcoming_birthdays(days_ahead=days_ahead)
            return out if not err else "Geburtstage momentan nicht lesbar."
        except Exception as exc:  # noqa: BLE001
            print(f"[Social] birthdays failed: {exc}")
            return "Geburtstage momentan nicht verfügbar."

    # ── post drafts (never auto-post) ──────────────────────────────────── #

    async def draft_post(self, platform: str, topic: str,
                         style: str = "professional") -> dict[str, Any]:
        limit = _CHAR_LIMITS.get(platform.lower(), 280)
        if self._client is None:
            return {"ok": False, "draft": "", "note": "Claude nicht verfügbar."}
        prompt = (f"Write a {style} {platform} post about {topic}. "
                  f"Maximum {limit} characters. Return ONLY the post text.")
        try:
            resp = self._client.messages.create(
                model=settings.MODEL, max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            draft = ""
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    draft = (b.text or "").strip()
                    break
            return {"ok": bool(draft), "draft": draft[:limit],
                    "chars": len(draft[:limit]), "limit": limit,
                    "note": "Entwurf — wird NICHT automatisch gepostet."}
        except Exception as exc:  # noqa: BLE001
            print(f"[Social] draft failed: {exc}")
            return {"ok": False, "draft": "", "note": str(exc)}

    # ── Twitter / LinkedIn (honest stubs) ──────────────────────────────── #

    async def get_twitter_mentions(self) -> str:
        token = getattr(settings, "TWITTER_BEARER_TOKEN", "")
        if not token:
            return ("Twitter ist nicht konfiguriert. Ein TWITTER_BEARER_TOKEN "
                    "(kostenpflichtige API v2) wäre nötig.")
        # With a token, a real fetch would go here. Kept honest: not wired
        # until the user opts into the paid API.
        return ("Twitter-Token gesetzt, aber Live-Abruf ist noch nicht "
                "aktiviert.")

    async def get_linkedin_messages(self) -> str:
        if not getattr(settings, "LINKEDIN_ENABLED", False):
            return ("LinkedIn ist nicht konfiguriert. Es gibt keine offizielle "
                    "Lese-API; dieser Kanal bleibt deaktiviert.")
        return "LinkedIn-Abruf ist noch nicht aktiviert."
