"""Reading a page, and refusing to read the ones we must not."""

import httpx
import pytest

from house_agent.websearch import WebSearchError, fetch_page, readable_text

PAGE = """
<html><head><title>Cozy mysteries</title>
<style>.a{color:red}</style><script>var x = "Not A Book";</script></head>
<body><nav><a href="/">Home</a><a href="/shop">Shop</a><a href="/join">Join</a></nav>
<h1>10 Cozy Mystery Audiobooks</h1>
<ul><li>The Thursday Murder Club by Richard Osman</li>
<li>Still Life by Louise Penny</li></ul>
<p>Happy listening &amp; enjoy!</p></body></html>
"""


@pytest.fixture
def public(monkeypatch):
    """Treat every host as public so the guard is not what is under test."""

    async def resolves(url):
        return "example.com"

    monkeypatch.setattr("house_agent.websearch._public_host", resolves)


def serve(monkeypatch, **response):
    handler = httpx.MockTransport(lambda request: httpx.Response(**response))
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = handler
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_readable_text_keeps_the_words_and_drops_the_machinery() -> None:
    text = readable_text(PAGE)
    # The titles a snippet would have cut off are exactly what we need.
    assert "The Thursday Murder Club by Richard Osman" in text
    assert "Still Life by Louise Penny" in text
    assert "Happy listening & enjoy!" in text
    # Script, style and navigation chrome are not page text.
    assert "Not A Book" not in text and "color:red" not in text
    assert "Shop" not in text and "Join" not in text
    # The page title and its heading survive; only the chrome between them goes.
    assert text.startswith("Cozy mysteries")
    assert text.index("10 Cozy Mystery Audiobooks") < text.index("Thursday Murder Club")
    assert "<" not in text


async def test_fetches_a_page_and_reports_truncation(monkeypatch, public) -> None:
    serve(monkeypatch, status_code=200, text=PAGE,
          headers={"content-type": "text/html; charset=utf-8"})
    page = await fetch_page("https://example.com/list")
    assert "Still Life by Louise Penny" in page["text"]
    assert page["truncated"] is False


async def test_a_long_page_is_capped(monkeypatch, public) -> None:
    long_page = "<html><body>" + "<p>Still Life by Louise Penny.</p>" * 200 + "</body></html>"
    serve(monkeypatch, status_code=200, text=long_page,
          headers={"content-type": "text/html"})
    page = await fetch_page("https://example.com/long", max_chars=500)
    assert page["truncated"] is True and len(page["text"]) == 500


async def test_a_private_address_is_never_fetched() -> None:
    # A search result is attacker-influenced input, and this box also runs
    # Ollama and the Matter server on loopback.
    for url in (
        "http://127.0.0.1:11434/v1/models",
        "http://localhost:8787/api/voice/profiles",
        "http://192.168.0.203:8060/query/device-info",
        "http://[::1]:5580/ws",
    ):
        with pytest.raises(WebSearchError, match="private or local"):
            await fetch_page(url)


async def test_only_http_urls_are_fetched() -> None:
    for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi"):
        with pytest.raises(WebSearchError, match="Only http"):
            await fetch_page(url)


async def test_a_non_page_is_refused(monkeypatch, public) -> None:
    serve(monkeypatch, status_code=200, content=b"\x00\x01",
          headers={"content-type": "application/octet-stream"})
    with pytest.raises(WebSearchError, match="Not a readable page"):
        await fetch_page("https://example.com/blob.bin")


async def test_an_error_page_is_refused(monkeypatch, public) -> None:
    serve(monkeypatch, status_code=404, text="gone",
          headers={"content-type": "text/html"})
    with pytest.raises(WebSearchError, match="404"):
        await fetch_page("https://example.com/missing")


async def test_a_throttled_search_is_an_error_not_an_empty_result(monkeypatch) -> None:
    # DuckDuckGo answers a suspected bot with 202 and a challenge page. Calling
    # that "no results" is what let the model invent books to fill the gap.
    from house_agent.websearch import web_search

    serve(monkeypatch, status_code=202, text="<html><body>challenge</body></html>",
          headers={"content-type": "text/html"})
    with pytest.raises(WebSearchError, match="unavailable"):
        await web_search("cozy mystery audiobooks")


async def test_an_unparseable_page_is_an_error_too(monkeypatch) -> None:
    from house_agent.websearch import web_search

    serve(monkeypatch, status_code=200, text="<html><body>nothing here</body></html>",
          headers={"content-type": "text/html"})
    with pytest.raises(WebSearchError, match="unavailable"):
        await web_search("cozy mystery audiobooks")
