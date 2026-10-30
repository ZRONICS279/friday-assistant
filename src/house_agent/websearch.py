from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx


class WebSearchError(RuntimeError):
    pass


_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r".*?"
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment)).strip()


def _real_url(href: str) -> str:
    # DuckDuckGo wraps results in a redirect: //duckduckgo.com/l/?uddg=<url>.
    parsed = urlparse(href, scheme="https")
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


_YAHOO_URL = "https://search.yahoo.com/search"
_YAHOO_BLOCK = re.compile(r'<div class="[^"]*\balgo-sr\b[^"]*"')
_YAHOO_HREF = re.compile(r'<a[^>]+href="([^"]+)"')
_YAHOO_TITLE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
_YAHOO_SNIPPET = re.compile(r'<(?:p|span) class="[^"]*fc-falcon[^"]*"[^>]*>(.*?)</(?:p|span)>', re.DOTALL)
# Yahoo renames its classes every few months (fc-falcon became fc-dustygray
# inside a compText div in 2026), and a missed rename means every result
# comes back with an empty snippet -- which reads as "the web knows nothing"
# and sends the model to fetch pages it did not need to fetch. So the class
# is a hint, not the rule: any paragraph of real prose in the block will do.
_YAHOO_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL)
SNIPPET_MIN_CHARS = 30


def _yahoo_snippet(block: str) -> str:
    match = _YAHOO_SNIPPET.search(block)
    if match:
        return _clean(match.group(1))[:300]
    for paragraph in _YAHOO_PARAGRAPH.finditer(block):
        text = _clean(paragraph.group(1))
        if len(text) >= SNIPPET_MIN_CHARS:
            return text[:300]
    return ""


_YAHOO_TARGET = re.compile(r"/RU=([^/]+)/")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Accept-Language": "en-US,en;q=0.8",
}
# DuckDuckGo throttles a house that searches too often, answering with a
# challenge page for a while. Once seen, it is left alone for a few minutes
# instead of being asked (and failing) on every search in between.
DDG_BACKOFF_SECONDS = 600
_ddg_blocked_until = 0.0
UNAVAILABLE = (
    "Web search is unavailable right now (every search engine this house "
    "can reach refused or rate limited the request). Do not answer from "
    "memory instead."
)


async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
    response = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
    response.raise_for_status()
    # A throttled or suspected-bot request gets 202 and a challenge page that
    # parses to nothing. Reporting that as an empty result set invites the
    # model to fill the silence from memory, so it is an error instead.
    if response.status_code == 202 or "result__a" not in response.text:
        raise WebSearchError("DuckDuckGo is rate limiting this house")
    results: list[dict[str, str]] = []
    for match in _RESULT.finditer(response.text):
        url = _real_url(match.group("href"))
        if not url.startswith("http"):
            continue
        if "duckduckgo.com/y.js" in url:
            continue  # advertisement, not a result
        results.append(
            {
                "title": _clean(match.group("title")),
                "url": url,
                "snippet": _clean(match.group("snippet"))[:300],
            }
        )
        if len(results) >= limit:
            break
    return results


async def _yahoo(client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
    """Yahoo's web results (Bing's index) as a second opinion. Result links
    are wrapped in a redirect whose RU= segment carries the real URL."""
    response = await client.get(_YAHOO_URL, params={"p": query})
    response.raise_for_status()
    starts = [match.start() for match in _YAHOO_BLOCK.finditer(response.text)]
    if not starts:
        raise WebSearchError("Yahoo returned no results page")
    results: list[dict[str, str]] = []
    for index, begin in enumerate(starts):
        block = response.text[begin : starts[index + 1] if index + 1 < len(starts) else begin + 6000]
        href = _YAHOO_HREF.search(block)
        title = _YAHOO_TITLE.search(block)
        if not href or not title:
            continue
        target = html.unescape(href.group(1))
        wrapped = _YAHOO_TARGET.search(target)
        url = unquote(wrapped.group(1)) if wrapped else target
        if not url.startswith("http") or "yahoo.com" in urlparse(url).netloc:
            continue
        results.append(
            {
                "title": _clean(title.group(1)),
                "url": url,
                "snippet": _yahoo_snippet(block),
            }
        )
        if len(results) >= limit:
            break
    return results


async def web_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """A deliberately narrow web search: top result titles, URLs, and
    snippets. DuckDuckGo first, Yahoo when DuckDuckGo is throttling; no page
    fetching, no keys."""
    global _ddg_blocked_until
    query = query.strip()
    if not query:
        raise ValueError("Search query cannot be empty")
    if len(query) > 200:
        raise ValueError("Search query exceeds 200 characters")
    limit = max(1, min(limit, 5))
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
        if time.monotonic() >= _ddg_blocked_until:
            try:
                results = await _duckduckgo(client, query, limit)
                if results:
                    return results
                failures.append("DuckDuckGo found nothing")
            except (httpx.HTTPError, WebSearchError) as exc:
                failures.append(str(exc))
                _ddg_blocked_until = time.monotonic() + DDG_BACKOFF_SECONDS
        try:
            results = await _yahoo(client, query, limit)
            if results:
                return results
            failures.append("Yahoo found nothing")
        except (httpx.HTTPError, WebSearchError) as exc:
            failures.append(str(exc))
    if any("found nothing" in item for item in failures) and len(failures) == 2:
        return []
    raise WebSearchError(UNAVAILABLE + " (" + "; ".join(failures) + ")")


_SCRIPT = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# Menus, headers, footers and sidebars. On a typical blog these run to
# thousands of characters of link text before the article starts, which is
# budget spent on navigation instead of on the answer.
_CHROME = re.compile(r"<(nav|header|footer|aside|form)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BREAK = re.compile(r"</(?:p|div|li|h[1-6]|tr|section|article)\s*>|<br\s*/?>", re.IGNORECASE)
_BLANK = re.compile(r"[ \t]*\n[ \t]*(?:\n[ \t]*)+")
READABLE_TYPES = ("text/html", "text/plain", "application/xhtml")
MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000


async def _public_host(url: str) -> str:
    """The host of a URL we are willing to fetch, or an error explaining why not.

    This assistant sits on the home network beside Ollama, the Matter server
    and its own API. A URL arriving from a search result is attacker-influenced
    input, so anything resolving to a private or loopback address is refused
    rather than turned into a request from inside the network.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebSearchError("Only http and https pages can be fetched")
    host = parsed.hostname
    if not host:
        raise WebSearchError(f"Not a fetchable URL: {url}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebSearchError(f"Could not resolve {host}: {exc}") from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise WebSearchError(f"Could not read the address of {host}") from None
        if not address.is_global:
            raise WebSearchError(f"Refusing to fetch a private or local address: {host}")
    return host


def readable_text(markup: str) -> str:
    """Page markup reduced to the words a person would read."""
    text = _SCRIPT.sub(" ", markup)
    text = _CHROME.sub(" ", text)
    text = _BREAK.sub("\n", text)
    text = html.unescape(_TAGS.sub("", text))
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK.sub("\n\n", text).strip()


# Wikipedia (and a growing number of other sites) block the browser user
# agent outright -- Wikimedia's policy asks automated clients to identify
# themselves instead, and answers a spoofed Firefox with 403. Wikipedia is
# the single most useful page this house fetches, so a refusal is retried
# once with an honest agent before it is reported as a dead end.
BROWSER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0"
POLITE_AGENT = "friday-house-agent/1.0 (private home assistant; +https://github.com/knightlie-code/friday)"
RETRY_AGENT_STATUSES = (401, 403, 406, 429)


async def _one_fetch(url: str, agent: str, timeout: float) -> tuple[httpx.Response, str]:
    """One page, following redirects by hand so each hop is address-checked."""
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": agent, "Accept-Language": "en-US,en;q=0.8"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _public_host(url)
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise WebSearchError(f"Could not fetch that page: {exc}") from exc
            if not response.is_redirect:
                return response, url
            location = response.headers.get("location")
            if not location:
                raise WebSearchError("That page redirected without a destination")
            url = str(response.url.join(location))
        raise WebSearchError("That page redirected too many times")


async def fetch_html(url: str, timeout: float = 15) -> tuple[str, str]:
    """The raw markup of one page and the URL it finally came from, after
    the same private-address checks as any other fetch."""
    response, url = await _one_fetch(url, BROWSER_AGENT, timeout)
    if response.status_code in RETRY_AGENT_STATUSES:
        response, url = await _one_fetch(url, POLITE_AGENT, timeout)
    if response.status_code >= 400:
        raise WebSearchError(f"That page returned {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if not any(kind in content_type for kind in READABLE_TYPES):
        raise WebSearchError(f"Not a readable page ({content_type or 'unknown type'})")
    return str(response.url), response.text[:MAX_BYTES]


async def fetch_page(url: str, max_chars: int = 8000) -> dict[str, str]:
    """The readable text of one page.

    Search snippets stop at 300 characters -- enough to see that a page lists
    twenty audiobooks, never enough to learn one of their names. Without this
    the model is asked for titles from data containing no titles, and it fills
    the gap by inventing them.
    """
    max_chars = max(500, min(int(max_chars), 20_000))
    final_url, markup = await fetch_html(url)
    text = readable_text(markup)
    if not text:
        raise WebSearchError("That page had no readable text")
    return {
        "url": final_url,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }
