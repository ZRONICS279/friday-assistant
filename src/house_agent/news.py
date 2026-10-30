"""Headlines for the house: plain RSS and Atom feeds, fetched directly.

No API key and no aggregator in the middle — a feed URL is the whole
setup, which is also why traffic sits here. Most transport departments
publish incidents as RSS, so a traffic feed is a news feed with a
different label on it."""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator
from xml.etree import ElementTree

import httpx

from .config import NewsConfig, NewsFeedConfig


class NewsError(RuntimeError):
    pass


_USER_AGENT = "house-agent/1.0 (Friday home assistant)"
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# Tags become a space so words never run together, which leaves a gap in
# front of the punctuation that followed an inline tag: "by noon ."
_ORPHAN = re.compile(r"\s+([,.;:!?])")

# Atom puts everything in its own namespace; RSS puts almost nothing in one.
_ATOM = "{http://www.w3.org/2005/Atom}"


def _find(entry: ElementTree.Element, *paths: str) -> ElementTree.Element | None:
    """The first tag that exists. An ElementTree element with no children is
    falsy, so the usual `a.find(x) or a.find(y)` drops real tags."""
    for path in paths:
        found = entry.find(path)
        if found is not None:
            return found
    return None


def _text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    flat = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", "".join(node.itertext()))))
    return _ORPHAN.sub(r"\1", flat).strip()


def _when(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return parsedate_to_datetime(raw)  # RSS: RFC 822
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom: ISO 8601
    return None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _link(entry: ElementTree.Element) -> str:
    """RSS keeps the URL as text; Atom keeps it in an href, often beside
    other links (comments, the feed itself) that are not the article."""
    direct = entry.findtext("link") or entry.findtext(f"{_ATOM}link") or ""
    if direct.strip():
        return direct.strip()
    links = entry.findall("link") + entry.findall(f"{_ATOM}link")
    alternates = [el for el in links if (el.get("rel") or "alternate") == "alternate"]
    for element in alternates or links:
        href = (element.get("href") or "").strip()
        if href:
            return href
    return ""


def _entries(root: ElementTree.Element) -> list[ElementTree.Element]:
    items = root.findall(".//item")
    return items if items else root.findall(f".//{_ATOM}entry")


def _feed_title(root: ElementTree.Element) -> str:
    return _text(_find(root, "./channel/title", f"./{_ATOM}title"))


class NewsService:
    def __init__(self, config: NewsConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._injected = client

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """An injected client belongs to the caller, so never close it."""
        if self._injected is not None:
            yield self._injected
            return
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
        ) as client:
            yield client

    async def _pull(self, client: httpx.AsyncClient, feed: NewsFeedConfig) -> list[dict[str, Any]]:
        """One feed's stories. A feed that is down or malformed contributes
        nothing rather than failing the whole panel."""
        try:
            response = await client.get(feed.url)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except (httpx.HTTPError, ElementTree.ParseError):
            return []
        source = feed.name or _feed_title(root) or "News"
        stories = []
        for entry in _entries(root)[: self.config.per_feed]:
            title = _text(_find(entry, "title", f"{_ATOM}title"))
            if not title:
                continue
            summary = _text(
                _find(entry, "description", f"{_ATOM}summary", f"{_ATOM}content")
            )
            published = _aware(
                _when(
                    entry.findtext("pubDate")
                    or entry.findtext(f"{_ATOM}published")
                    or entry.findtext(f"{_ATOM}updated")
                    or ""
                )
            )
            stories.append(
                {
                    "title": title[:300],
                    "summary": summary[:400],
                    "url": _link(entry),
                    "source": source,
                    "kind": feed.kind,
                    "published_at": published.isoformat() if published else None,
                    "_sort": published or datetime.min.replace(tzinfo=timezone.utc),
                }
            )
        return stories

    async def headlines(self, limit: int | None = None) -> dict[str, Any]:
        """The newest stories across every configured feed, newest first."""
        if not self.config.enabled:
            return {"enabled": False, "stories": []}
        if not self.config.feeds:
            return {"enabled": True, "stories": [], "error": "No news feeds are configured."}
        try:
            async with self._client() as client:
                pulled = await asyncio.gather(
                    *(self._pull(client, feed) for feed in self.config.feeds)
                )
        except httpx.HTTPError as exc:  # pragma: no cover - per-feed errors are swallowed above
            raise NewsError(f"News lookup failed: {exc}") from exc

        stories = [story for feed in pulled for story in feed]
        stories.sort(key=lambda story: story["_sort"], reverse=True)
        seen: set[str] = set()
        newest: list[dict[str, Any]] = []
        cap = max(1, min(limit or self.config.max_items, 50))
        for story in stories:
            key = story["url"] or story["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            story.pop("_sort")
            newest.append(story)
            if len(newest) >= cap:
                break
        body: dict[str, Any] = {"enabled": True, "stories": newest}
        if not newest:
            body["error"] = "No feed answered."
        return body
