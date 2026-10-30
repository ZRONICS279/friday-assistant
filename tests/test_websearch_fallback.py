"""When DuckDuckGo throttles, Yahoo answers; when both fail, it is an error."""

import httpx

from house_agent import websearch
from house_agent.websearch import WebSearchError, web_search

DDG_PAGE = """<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fp">Pancakes A</a>
<a class="result__snippet">Fluffy ones</a>"""
YAHOO_PAGE = """
<div class="dd algo algo-sr relsrch richAlgo"><h3 class="title"><a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fwww.allrecipes.com%2frecipe%2f162760%2ffluffy-pancakes%2f/RK=2/RS=y">Fluffy Pancakes Recipe</a></h3>
<p class="fc-falcon">This <b>fluffy</b> pancake recipe is easy.</p></div>
<div class="dd algo algo-sr relsrch"><h3 class="title"><a href="https://r.search.yahoo.com/RU=https%3a%2f%2fb.example%2fq/RK=2">Other</a></h3></div>
<div class="dd algo algo-sr relsrch"><h3 class="title"><a href="https://search.yahoo.com/more">More from Yahoo</a></h3></div>
"""


def route(monkeypatch, ddg: httpx.Response, yahoo: httpx.Response) -> list[str]:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return ddg if "duckduckgo" in request.url.host else yahoo

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(websearch, "_ddg_blocked_until", 0.0)
    return calls


async def test_duckduckgo_is_used_when_it_answers(monkeypatch) -> None:
    calls = route(monkeypatch, httpx.Response(200, text=DDG_PAGE), httpx.Response(200, text=YAHOO_PAGE))
    results = await web_search("fluffy pancakes")
    assert results == [{"title": "Pancakes A", "url": "https://a.example/p", "snippet": "Fluffy ones"}]
    assert calls == ["html.duckduckgo.com"]


async def test_yahoo_answers_while_duckduckgo_throttles(monkeypatch) -> None:
    calls = route(monkeypatch, httpx.Response(202, text="<html>challenge</html>"), httpx.Response(200, text=YAHOO_PAGE))
    results = await web_search("fluffy pancakes", 5)
    assert [item["url"] for item in results] == [
        "https://www.allrecipes.com/recipe/162760/fluffy-pancakes/",
        "https://b.example/q",
    ]
    assert results[0]["title"] == "Fluffy Pancakes Recipe"
    assert results[0]["snippet"] == "This fluffy pancake recipe is easy."
    assert calls == ["html.duckduckgo.com", "search.yahoo.com"]
    # The throttle is remembered: the next search goes straight to Yahoo.
    await web_search("again")
    assert calls[2:] == ["search.yahoo.com"]
    assert websearch._ddg_blocked_until > 0


async def test_both_engines_failing_is_an_error_not_silence(monkeypatch) -> None:
    route(monkeypatch, httpx.Response(202, text="challenge"), httpx.Response(429, text="slow down"))
    try:
        await web_search("anything")
    except WebSearchError as exc:
        assert "Do not answer from memory" in str(exc)
    else:
        raise AssertionError("expected WebSearchError")


# Yahoo's markup as it stands in September 2026: no fc-falcon anywhere, the
# snippet is a <p> inside a compText div. Parsed by class alone, every result
# came back with an empty snippet -- which reads as a web that knows nothing.
YAHOO_2026 = """
<div class="dd algo algo-sr relsrch Sr"><div class="compTitle"><a href="https://r.search.yahoo.com/RU=https%3a%2f%2few.com%2fthe-love-hypothesis%2f/RK=2"><h3 class="title"><span>The Love Hypothesis ending explained</span></h3></a></div>
<div class="compText aAbs"><p class="fc-dustygray fz-14"><span class="fc-smoke">6 hours ago &middot; </span> The Love Hypothesis follows two scientists in a fake romance.</p></div></div>
<div class="dd algo algo-sr relsrch"><h3 class="title"><a href="https://r.search.yahoo.com/RU=https%3a%2f%2fb.example%2fq/RK=2">No snippet here</a></h3><p>short</p></div>
"""


async def test_a_renamed_snippet_class_does_not_empty_every_result(monkeypatch) -> None:
    route(monkeypatch, httpx.Response(202, text="challenge"), httpx.Response(200, text=YAHOO_2026))
    results = await web_search("the love hypothesis plot", 5)
    assert results[0]["url"] == "https://ew.com/the-love-hypothesis/"
    assert results[0]["snippet"] == (
        "6 hours ago ·  The Love Hypothesis follows two scientists in a fake romance."
    )
    # A block with nothing but boilerplate still reports no snippet.
    assert results[1]["snippet"] == ""


async def test_a_page_that_refuses_the_browser_agent_is_asked_again_politely(monkeypatch) -> None:
    """Wikimedia answers a spoofed Firefox with 403 and an honest agent with
    the page. Wikipedia is the most useful page this house fetches, so a 403
    is retried once before it becomes "I couldn't fetch that"."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        if request.headers["user-agent"] == websearch.BROWSER_AGENT:
            return httpx.Response(403, text="denied")
        return httpx.Response(
            200,
            text="<html><body><p>A 2021 romance novel.</p></body></html>",
            headers={"content-type": "text/html"},
        )

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    async def public(url: str) -> str:
        return "en.wikipedia.org"

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(websearch, "_public_host", public)
    page = await websearch.fetch_page("https://en.wikipedia.org/wiki/The_Love_Hypothesis", 500)

    assert "2021 romance novel" in page["text"]
    assert seen == [websearch.BROWSER_AGENT, websearch.POLITE_AGENT]
