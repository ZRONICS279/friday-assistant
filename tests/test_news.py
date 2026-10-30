import httpx
import pytest

from house_agent.config import NewsConfig, NewsFeedConfig
from house_agent.news import NewsService


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example Wire</title>
  <item>
    <title>Bridge reopens after repairs</title>
    <description>&lt;p&gt;The span carried traffic again by &lt;b&gt;noon&lt;/b&gt;.&lt;/p&gt;</description>
    <link>https://example.test/bridge</link>
    <pubDate>Tue, 22 Sep 2026 18:30:00 GMT</pubDate>
  </item>
  <item>
    <title>Older story</title>
    <link>https://example.test/older</link>
    <pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Roads</title>
  <entry>
    <title>Crash blocks the left lane</title>
    <summary>Expect delays.</summary>
    <link rel="self" href="https://roads.test/feed"/>
    <link rel="alternate" href="https://roads.test/crash"/>
    <published>2026-09-23T07:15:00Z</published>
  </entry>
</feed>
"""


def _handler(**bodies: str):
    def handle(request: httpx.Request) -> httpx.Response:
        body = bodies.get(request.url.host)
        if body is None:
            return httpx.Response(500)
        return httpx.Response(200, text=body, headers={"Content-Type": "application/xml"})

    return handle


def _config(*feeds: NewsFeedConfig, **overrides) -> NewsConfig:
    return NewsConfig(feeds=feeds, **overrides)


@pytest.mark.asyncio
async def test_rss_items_lose_their_markup_and_keep_their_link() -> None:
    config = _config(NewsFeedConfig("https://wire.test/rss", "Example Wire"))
    transport = httpx.MockTransport(_handler(**{"wire.test": RSS}))
    async with httpx.AsyncClient(transport=transport) as client:
        body = await NewsService(config, client=client).headlines()

    first = body["stories"][0]
    assert first["title"] == "Bridge reopens after repairs"
    assert first["summary"] == "The span carried traffic again by noon."
    assert first["url"] == "https://example.test/bridge"
    assert first["source"] == "Example Wire"
    assert first["published_at"].startswith("2026-09-22T18:30")


@pytest.mark.asyncio
async def test_atom_entries_skip_the_self_link() -> None:
    config = _config(NewsFeedConfig("https://roads.test/feed", kind="traffic"))
    transport = httpx.MockTransport(_handler(**{"roads.test": ATOM}))
    async with httpx.AsyncClient(transport=transport) as client:
        body = await NewsService(config, client=client).headlines()

    story = body["stories"][0]
    assert story["url"] == "https://roads.test/crash"
    assert story["kind"] == "traffic"
    assert story["source"] == "Example Roads"  # no name set: the feed's own title


@pytest.mark.asyncio
async def test_feeds_merge_newest_first_and_a_dead_feed_is_skipped() -> None:
    config = _config(
        NewsFeedConfig("https://wire.test/rss"),
        NewsFeedConfig("https://roads.test/feed"),
        NewsFeedConfig("https://down.test/rss"),
    )
    transport = httpx.MockTransport(_handler(**{"wire.test": RSS, "roads.test": ATOM}))
    async with httpx.AsyncClient(transport=transport) as client:
        body = await NewsService(config, client=client).headlines()

    assert [story["title"] for story in body["stories"]] == [
        "Crash blocks the left lane",
        "Bridge reopens after repairs",
        "Older story",
    ]


@pytest.mark.asyncio
async def test_limit_caps_the_merged_list() -> None:
    config = _config(NewsFeedConfig("https://wire.test/rss"))
    transport = httpx.MockTransport(_handler(**{"wire.test": RSS}))
    async with httpx.AsyncClient(transport=transport) as client:
        body = await NewsService(config, client=client).headlines(limit=1)

    assert len(body["stories"]) == 1


@pytest.mark.asyncio
async def test_disabled_and_unconfigured_return_nothing_without_asking() -> None:
    transport = httpx.MockTransport(_handler())
    async with httpx.AsyncClient(transport=transport) as client:
        off = await NewsService(_config(enabled=False), client=client).headlines()
        empty = await NewsService(_config(), client=client).headlines()

    assert off == {"enabled": False, "stories": []}
    assert empty["stories"] == []
    assert "No news feeds" in empty["error"]
