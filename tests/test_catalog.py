import httpx
import pytest

from house_agent.catalog import (
    CatalogConfig,
    CatalogError,
    TMDBCatalog,
    WatchmodeCatalog,
    canonical_provider,
)


def test_canonical_provider_aliases() -> None:
    assert canonical_provider("Disney Plus") == "disney_plus"
    assert canonical_provider("HBO Max") == "max"
    assert canonical_provider("Amazon Prime Video") == "prime_video"
    assert canonical_provider("AppleTV+") == "apple_tv_plus"
    assert canonical_provider("Apple TV+") == "apple_tv_plus"
    assert canonical_provider("Paramount Plus") == "paramount_plus"


@pytest.mark.asyncio
async def test_watchmode_search_includes_subscription_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-key"
        if request.url.path == "/v1/search/":
            return httpx.Response(
                200,
                json={
                    "title_results": [
                        {
                            "id": 101,
                            "name": "Loki",
                            "type": "tv_series",
                            "year": 2021,
                            "imdb_id": "tt9140554",
                            "tmdb_id": 84958,
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/title/101/sources/"
        return httpx.Response(
            200,
            json=[
                {
                    "source_id": 372,
                    "name": "Disney+",
                    "type": "sub",
                    "region": "US",
                    "web_url": "https://www.disneyplus.com/video/example-id",
                },
                {
                    "source_id": 1,
                    "name": "Amazon",
                    "type": "rent",
                    "region": "US",
                    "web_url": "https://amazon.com/example",
                },
            ],
        )

    config = CatalogConfig(region="US", watchmode_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        titles = await WatchmodeCatalog(config, client=client).search("Loki")

    assert titles[0].title == "Loki"
    assert titles[0].offerings[0].provider == "disney_plus"
    assert len(titles[0].offerings) == 1


@pytest.mark.asyncio
async def test_watchmode_playback_resolution_fetches_only_exact_title_sources() -> None:
    source_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search/":
            return httpx.Response(
                200,
                json={
                    "title_results": [
                        {"id": 1, "name": "Loki 7", "type": "movie", "year": 2016},
                        {"id": 2, "name": "Loki", "type": "tv_series", "year": 2021},
                    ]
                },
            )
        source_paths.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {
                    "name": "Disney+",
                    "type": "sub",
                    "region": "US",
                    "web_url": "https://www.disneyplus.com/video/loki-id",
                }
            ],
        )

    config = CatalogConfig(region="US", watchmode_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        title = await WatchmodeCatalog(config, client=client).resolve("Loki")

    assert title is not None
    assert title.catalog_id == "2"
    assert source_paths == ["/v1/title/2/sources/"]


@pytest.mark.asyncio
async def test_watchmode_resolves_exact_episode_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/title/3158249/episodes/"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 203,
                    "name": "1893",
                    "season_number": 2,
                    "episode_number": 3,
                    "tmdb_id": 4388221,
                    "imdb_id": "tt1234567",
                    "sources": [
                        {
                            "source_id": 372,
                            "name": "Disney+",
                            "type": "sub",
                            "region": "US",
                            "web_url": "https://www.disneyplus.com/video/episode-id",
                        }
                    ],
                }
            ],
        )

    config = CatalogConfig(region="US", watchmode_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WatchmodeCatalog(config, client=client).episode("3158249", 2, 3)

    assert result is not None
    assert result["title"] == "1893"
    assert result["offerings"][0]["provider"] == "disney_plus"


@pytest.mark.asyncio
async def test_watchmode_discovers_high_quality_subscription_titles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources/":
            return httpx.Response(
                200,
                json=[
                    {"id": 203, "name": "Netflix"},
                    {"id": 999, "name": "Unsupported Service"},
                ],
            )
        if request.url.path == "/v1/genres/":
            return httpx.Response(200, json=[{"id": 4, "name": "Comedy"}])
        if request.url.path == "/v1/list-titles/":
            assert request.url.params["source_ids"] == "203"
            assert request.url.params["genres"] == "4"
            assert request.url.params["release_date_start"] == "20240101"
            return httpx.Response(
                200,
                json={
                    "titles": [
                        {
                            "id": 500,
                            "title": "A Good Comedy",
                            "type": "movie",
                            "year": 2025,
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/title/500/details/"
        return httpx.Response(
            200,
            json={
                "id": 500,
                "title": "A Good Comedy",
                "type": "movie",
                "year": 2025,
                "plot_overview": "Funny things happen.",
                "genre_names": ["Comedy"],
                "user_rating": 8.1,
                "critic_score": 84,
                "sources": [
                    {
                        "name": "Netflix",
                        "type": "sub",
                        "region": "US",
                        "web_url": "https://www.netflix.com/title/123",
                    }
                ],
            },
        )

    config = CatalogConfig(region="US", watchmode_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await WatchmodeCatalog(config, client=client).discover(
            {"netflix"},
            media_type="movie",
            genres=["Comedy"],
            year_min=2024,
            limit=1,
        )

    assert results[0]["title"] == "A Good Comedy"
    assert results[0]["user_rating"] == 8.1
    assert results[0]["offerings"][0]["provider"] == "netflix"


@pytest.mark.asyncio
async def test_tmdb_discovers_subscription_movies_by_genre_and_year() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/watch/providers/movie":
            return httpx.Response(
                200,
                json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]},
            )
        if request.url.path == "/3/genre/movie/list":
            return httpx.Response(
                200,
                json={"genres": [{"id": 27, "name": "Horror"}]},
            )
        if request.url.path == "/3/discover/movie":
            assert request.url.params["with_watch_providers"] == "8"
            assert request.url.params["with_genres"] == "27"
            assert request.url.params["primary_release_date.gte"] == "1990-01-01"
            assert request.url.params["primary_release_date.lte"] == "1999-12-31"
            assert request.url.params["vote_average.gte"] == "7.0"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 500,
                            "title": "A Good Horror Movie",
                            "release_date": "1996-10-01",
                            "overview": "Something scary happens.",
                            "genre_ids": [27],
                            "vote_average": 7.8,
                            "vote_count": 2000,
                            "popularity": 50,
                        }
                    ]
                },
            )
        assert request.url.path == "/3/movie/500/watch/providers"
        return httpx.Response(
            200,
            json={
                "results": {
                    "US": {
                        "flatrate": [
                            {"provider_id": 8, "provider_name": "Netflix"}
                        ]
                    }
                }
            },
        )

    config = CatalogConfig(region="US", tmdb_token="read-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await TMDBCatalog(config, client=client).discover(
            {"netflix"},
            media_type="movie",
            genres=["Horror"],
            year_min=1990,
            year_max=1999,
            limit=1,
        )

    assert results[0]["title"] == "A Good Horror Movie"
    assert results[0]["year"] == 1996
    assert results[0]["availability_source"] == "JustWatch via TMDB"


def test_taste_rank_is_gentle_with_little_evidence_and_firm_with_much() -> None:
    from house_agent.recommendations import _taste_rank

    candidates = [
        {"title": "Popular Drama", "genres": ["Drama"]},
        {"title": "Niche Comedy", "genres": ["Comedy"]},
    ]
    loves_comedy = {"scores": {"comedy": 1.0, "drama": -1.0}}

    # Two feedback signals: catalog order still wins.
    weak = _taste_rank(candidates, {**loves_comedy, "signals": 2}, 2)
    assert weak[0]["title"] == "Popular Drama"

    # Twenty-five signals: learned taste outranks popularity.
    strong = _taste_rank(candidates, {**loves_comedy, "signals": 25}, 2)
    assert strong[0]["title"] == "Niche Comedy"

    # No evidence at all: pure catalog order.
    none = _taste_rank(candidates, {"scores": {}, "signals": 0}, 1)
    assert none == [candidates[0]]


async def test_tmdb_similar_to_boosts_neighbours_and_hides_the_example() -> None:
    providers = {
        "results": {"US": {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]}}
    }

    def movie(identifier: int, title: str, genre_ids: list[int], popularity: float) -> dict:
        return {
            "id": identifier,
            "title": title,
            "release_date": "2019-11-27",
            "overview": "",
            "genre_ids": genre_ids,
            "vote_average": 7.5,
            "vote_count": 3000,
            "popularity": popularity,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/watch/providers/movie":
            return httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        if path == "/3/genre/movie/list":
            return httpx.Response(
                200,
                json={"genres": [{"id": 9648, "name": "Mystery"}, {"id": 35, "name": "Comedy"}]},
            )
        if path == "/3/discover/movie":
            if request.url.params.get("page") != "1":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        movie(1, "Knives Out", [9648, 35], 900),
                        movie(2, "Popular Comedy Mystery", [9648, 35], 500),
                    ]
                },
            )
        if path == "/3/search/multi":
            assert request.url.params["query"] == "Knives Out"
            return httpx.Response(
                200, json={"results": [{"id": 1, "media_type": "movie", "title": "Knives Out"}]}
            )
        if path == "/3/movie/1/recommendations":
            return httpx.Response(
                200,
                json={
                    "results": [
                        movie(3, "Glass Onion", [9648, 35], 10),
                        movie(4, "Death on the Nile", [9648], 20),
                        movie(5, "Unavailable Neighbour", [9648, 35], 30),
                    ]
                },
            )
        assert path.endswith("/watch/providers"), path
        if path == "/3/movie/5/watch/providers":
            return httpx.Response(200, json={"results": {}})
        return httpx.Response(200, json=providers)

    TMDBCatalog._CACHE.clear()  # genre/provider caches are process-wide
    config = CatalogConfig(region="US", tmdb_token="read-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await TMDBCatalog(config, client=client).discover(
            {"netflix"},
            media_type="movie",
            genres=["mystery", "comedy"],
            similar_to=["Knives Out"],
            limit=5,
        )

    titles = [item["title"] for item in results]
    # The example itself is gone; its full-genre neighbour leads even though it
    # is far less popular; the partial-genre neighbour still trails full matches.
    assert titles == ["Glass Onion", "Popular Comedy Mystery", "Death on the Nile"]
    assert results[0]["similar_to"] == "Knives Out"
    assert results[2]["genre_match"] == "partial"


@pytest.mark.asyncio
async def test_watchmode_reuses_responses_within_the_cache_window() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/search/":
            return httpx.Response(
                200,
                json={"title_results": [{"id": 2, "name": "Loki", "type": "tv_series", "year": 2021}]},
            )
        return httpx.Response(
            200,
            json=[{"name": "Disney+", "type": "sub", "region": "US", "web_url": "https://www.disneyplus.com/video/loki-id"}],
        )

    config = CatalogConfig(region="US", watchmode_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = WatchmodeCatalog(config, client=client)
        first = await catalog.resolve("Loki")
        second = await catalog.resolve("Loki")  # a play re-resolves its title

    assert first is not None and second is not None
    assert calls == ["/v1/search/", "/v1/title/2/sources/"]  # no repeat requests


def test_a_placeholder_url_is_not_treated_as_a_link() -> None:
    """Watchmode fills the url fields of a plan without episode deep links
    with a sentence. Handed on as-is, that sentence reaches a TV as an
    address, so it is dropped the same as a missing link."""
    from house_agent.catalog import _playable_url

    assert _playable_url("Episode links available for paid plans only.") is None
    assert _playable_url("Deeplinks available for paid plans only.") is None
    assert _playable_url("") is None and _playable_url(None) is None
    assert _playable_url("https://www.hulu.com/series/abc") == "https://www.hulu.com/series/abc"


async def test_similar_to_is_not_padded_with_an_off_genre_hit() -> None:
    """The real failure (2026-09-25): "a good movie like Gravity" came back
    with *The Love Hypothesis*, a romantic comedy, in fifth place — TMDB's
    popular-this-week list filling the slots the similarity graph could not.
    Padding now has to at least share a genre with the title they named."""
    providers = {
        "results": {"US": {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]}}
    }

    def movie(identifier: int, title: str, genre_ids: list[int], popularity: float) -> dict:
        return {
            "id": identifier,
            "title": title,
            "release_date": "2013-10-04",
            "overview": "",
            "genre_ids": genre_ids,
            "vote_average": 7.5,
            "vote_count": 3000,
            "popularity": popularity,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/watch/providers/movie":
            return httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        if path == "/3/genre/movie/list":
            return httpx.Response(
                200,
                json={
                    "genres": [
                        {"id": 878, "name": "Science Fiction"},
                        {"id": 10749, "name": "Romance"},
                    ]
                },
            )
        if path == "/3/discover/movie":
            if request.url.params.get("page") != "1":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        movie(20, "The Love Hypothesis", [10749], 900),
                        movie(21, "Project Hail Mary", [878], 800),
                    ]
                },
            )
        if path == "/3/search/multi":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "media_type": "movie",
                            "title": "Gravity",
                            "genre_ids": [878],
                        }
                    ]
                },
            )
        if path == "/3/movie/1/recommendations":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, json={"results": [movie(3, "Interstellar", [878], 10)]})
            # Pages two and three are read too: most neighbours are on some
            # service nobody in the house pays for, so one page is not enough.
            if page == "3":
                return httpx.Response(200, json={"results": [movie(4, "I Am Mother", [878], 5)]})
            return httpx.Response(200, json={"results": []})
        assert path.endswith("/watch/providers"), path
        return httpx.Response(200, json=providers)

    TMDBCatalog._CACHE.clear()
    config = CatalogConfig(region="US", tmdb_token="read-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await TMDBCatalog(config, client=client).discover(
            {"netflix"}, media_type="movie", similar_to=["Gravity"], limit=5
        )

    titles = [item["title"] for item in results]
    assert titles == ["Interstellar", "I Am Mother", "Project Hail Mary"]
    assert "The Love Hypothesis" not in titles
    assert [item["similar_to"] for item in results] == ["Gravity", "Gravity", None]


def test_taste_never_promotes_a_stranger_over_a_neighbour() -> None:
    from house_agent.recommendations import _taste_rank

    candidates = [
        {"title": "Interstellar", "genres": ["Science Fiction"], "similar_to": "Gravity"},
        {"title": "The Love Hypothesis", "genres": ["Romance"], "similar_to": None},
    ]
    loves_romance = {"scores": {"romance": 1.0, "science fiction": -1.0}, "signals": 40}
    assert _taste_rank(candidates, loves_romance, 2)[0]["title"] == "Interstellar"


async def test_a_timed_out_tmdb_call_is_tried_once_more() -> None:
    """A read timeout stringifies to nothing, so the failure that broke a
    2026-09-25 turn read "TMDB request failed: " — and it was one blip."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.path))
        if len(calls) == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, json={"genres": [{"id": 878, "name": "Science Fiction"}]})

    TMDBCatalog._CACHE.clear()
    config = CatalogConfig(region="US", tmdb_token="read-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = await TMDBCatalog(config, client=client)._get("/genre/movie/list", {})

    assert body["genres"][0]["name"] == "Science Fiction"
    assert len(calls) == 2


async def test_a_tmdb_outage_says_what_went_wrong() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    TMDBCatalog._CACHE.clear()
    config = CatalogConfig(region="US", tmdb_token="read-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CatalogError, match="ReadTimeout"):
            await TMDBCatalog(config, client=client)._get("/genre/movie/list", {})
