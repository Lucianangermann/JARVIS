"""RSS-feed news lookup. No API key required.

Pulls headlines from one or more RSS feeds and returns plain
dataclasses. Implementation is intentionally stdlib-only — we
parse just the few fields we actually consume (title, link,
optional pubDate) rather than pulling in feedparser. RSS in the
real world has all kinds of malformed variants; we ignore items
that don't have at least a title.

Defaults: Tagesschau + BBC top stories. Override via the
``NEWS_FEEDS`` env var (comma-separated URLs).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

_HTTP_TIMEOUT = 6.0

_DEFAULT_FEEDS: tuple[str, ...] = (
    "https://www.tagesschau.de/xml/rss2/",
    "http://feeds.bbci.co.uk/news/rss.xml",
)

# Inline tag-stripping rather than HTML parser — RSS descriptions
# routinely smuggle in <p>, <br>, &nbsp;, etc. that we don't want
# read aloud. Cheap regex; falls back to raw text if anything weird.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Headline:
    title: str
    source: str         # human-readable feed name, eg "Tagesschau"
    link: str
    published: datetime | None


def _feed_list() -> list[str]:
    raw = os.getenv("NEWS_FEEDS")
    if not raw:
        return list(_DEFAULT_FEEDS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    # Unescape a handful of common entities — full html unescape via
    # stdlib's html module would also work, but these three cover
    # ~95% of what RSS feeds emit in titles.
    return (text.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'"))


def _parse_pubdate(s: str | None) -> datetime | None:
    if not s:
        return None
    # RSS dates are RFC 822-ish. Try the most common formats; bail
    # silently if none match (the field is optional anyway).
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _fetch_feed(url: str) -> list[Headline]:
    """Parse one feed URL. Returns empty list on any error."""
    try:
        r = httpx.get(
            url,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "JARVIS/1.0 (news fetcher)"},
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"[news] fetch {url!r} failed: {exc}")
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        print(f"[news] parse {url!r} failed: {exc}")
        return []

    # Extract a friendly source name from <channel><title>; fall back
    # to the host name if missing.
    src = _clean((root.findtext(".//channel/title") or "")) or url
    items: list[Headline] = []
    for item in root.findall(".//item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub = _parse_pubdate(item.findtext("pubDate")
                              or item.findtext("{http://purl.org/dc/elements/1.1/}date"))
        items.append(Headline(title=title, source=src, link=link, published=pub))
    return items


def get_headlines_for_topics(topics: list[str], n: int = 5,
                              feeds: list[str] | None = None) -> list[Headline]:
    """Return up to ``n`` headlines that match any of the given topic keywords.

    Scoring: each topic keyword found in the title adds 1 point.
    Sorted by score desc, then by date. Falls back to unfiltered headlines
    when no matches are found."""
    pool = get_headlines(max(n * 4, 20), feeds)
    if not topics:
        return pool[:n]
    topics_lower = [t.lower() for t in topics]
    scored: list[tuple[int, Headline]] = []
    for h in pool:
        title_lower = h.title.lower()
        score = sum(1 for t in topics_lower if t in title_lower)
        if score > 0:
            scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)
    result = [h for _, h in scored[:n]]
    return result if result else pool[:n]


def get_headlines(n: int = 5,
                  feeds: list[str] | None = None) -> list[Headline]:
    """Top ``n`` headlines across all configured feeds.

    Sorted by published date (newest first) when timestamps are
    available; falls back to feed order otherwise. ``n`` is clamped
    to [1, 20] to keep a spoken briefing from running on forever.
    """
    n = max(1, min(int(n), 20))
    sources = feeds if feeds is not None else _feed_list()
    if not sources:
        return []
    all_items: list[Headline] = []
    for url in sources:
        all_items.extend(_fetch_feed(url))
    # Newest first when dates are known; items without dates keep
    # their original feed order (stable sort).
    all_items.sort(
        key=lambda h: h.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return all_items[:n]
