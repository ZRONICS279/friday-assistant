from pathlib import Path

import pytest

from house_agent.config import AppConfig, CatalogConfig, StorageConfig
from house_agent.content import ContentError, PlaybackService, content_id_from_url
from house_agent.db import Database


@pytest.mark.parametrize(
    ("provider", "url", "expected"),
    [
        ("Netflix", "https://www.netflix.com/title/80100172", "80100172"),
        ("Disney+", "https://www.disneyplus.com/video/abc-123", "abc-123"),
        (
            "Disney+",
            "https://www.disneyplus.com/browse/entity-8f8c5cbb-e5ba-4285-9e2c-86abcac9fd50",
            "8f8c5cbb-e5ba-4285-9e2c-86abcac9fd50",
        ),
        ("Hulu", "https://www.hulu.com/series/show-name-uuid", "show-name-uuid"),
        ("Max", "https://play.max.com/show/01c72e76", "01c72e76"),
        ("Prime Video", "https://www.amazon.com/gp/video/detail/B012345", "B012345"),
        (
            "Prime Video",
            "https://watch.amazon.com/detail?gti=amzn1.dv.gti.9ab600aa-6625-b547-cfc0-190e3ad8a27d",
            "amzn1.dv.gti.9ab600aa-6625-b547-cfc0-190e3ad8a27d",
        ),
        (
            "AppleTV+",
            "https://tv.apple.com/us/episode/sweet-vitriol/umc.cmc.episode-id?showId=umc.cmc.series-id",
            "umc.cmc.series-id",
        ),
        (
            "Paramount+",
            "https://paramountplus.qflm.net/5bbABo?u=https%3A%2F%2Fwww.paramountplus.com%2Fshows%2Fstar-trek%2Fvideo%2Fvideo-id%2Fepisode-name",
            "video-id",
        ),
    ],
)
def test_extracts_provider_content_ids(provider: str, url: str, expected: str) -> None:
    assert content_id_from_url(provider, url) == expected


class FakeCatalog:
    async def search(self, query: str, subscriptions: set[str]):
        assert subscriptions == {"disney_plus"}
        return {
            "query": query,
            "region": "US",
            "results": [
                {
                    "catalog": "fake",
                    "catalog_id": "tv-1",
                    "title": "Loki",
                    "media_type": "series",
                    "year": 2021,
                    "imdb_id": None,
                    "tmdb_id": 1,
                    "offerings": [
                        {
                            "provider": "disney_plus",
                            "display_name": "Disney+",
                            "region": "US",
                            "kind": "sub",
                            "web_url": "https://www.disneyplus.com/video/loki-id",
                        }
                    ],
                    "available_on_subscription": True,
                }
            ],
        }

    async def episode(
        self,
        catalog: str,
        catalog_id: str,
        season: int,
        episode: int,
        subscriptions: set[str],
    ):
        assert (catalog, catalog_id) == ("fake", "tv-1")
        assert (season, episode) == (2, 3)
        assert subscriptions == {"disney_plus"}
        return {
            "catalog": "fake",
            "catalog_id": "episode-3",
            "title": "1893",
            "media_type": "episode",
            "season": 2,
            "episode": 3,
            "offerings": [
                {
                    "provider": "disney_plus",
                    "display_name": "Disney+",
                    "region": "US",
                    "kind": "sub",
                    "web_url": "https://www.disneyplus.com/video/loki-s02e03",
                }
            ],
            "available_on_subscription": True,
        }


@pytest.mark.asyncio
async def test_plan_caches_candidate_but_requires_verification(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    database.seed_subscriptions([("disney_plus", "Disney+")])
    config = AppConfig(
        catalog=CatalogConfig(region="US"),
        storage=StorageConfig(tmp_path / "test.db"),
    )
    service = PlaybackService(config, database, catalog=FakeCatalog())

    plan = await service.plan("Loki")

    assert plan["status"] == "needs_verification"
    assert plan["link"]["content_id"] == "loki-id"
    assert plan["link"]["verified"] is False

    database.set_provider_resolver_verified("disney_plus", True, "Known test passed")
    trusted_plan = await service.plan("Loki")
    assert trusted_plan["status"] == "ready"
    assert trusted_plan["resolver_verified"] is True


@pytest.mark.asyncio
async def test_plan_resolves_exact_episode_without_changing_resume_behavior(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    database.seed_subscriptions([("disney_plus", "Disney+")])
    config = AppConfig(
        catalog=CatalogConfig(region="US", exact_episode_playback_enabled=True),
        storage=StorageConfig(tmp_path / "test.db"),
    )
    service = PlaybackService(config, database, catalog=FakeCatalog())

    episode_plan = await service.plan("Loki", "Disney+", season=2, episode=3)
    series_plan = await service.plan("Loki", "Disney+")

    assert episode_plan["link"]["title"] == "Loki S02E03"
    assert episode_plan["link"]["media_type"] == "episode"
    assert episode_plan["link"]["content_id"] == "loki-s02e03"
    assert episode_plan["episode_title"] == "1893"
    assert series_plan["link"]["media_type"] == "series"


@pytest.mark.asyncio
async def test_plan_requires_season_and_episode_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    config = AppConfig(storage=StorageConfig(tmp_path / "test.db"))
    service = PlaybackService(config, database, catalog=FakeCatalog())

    with pytest.raises(ContentError, match="provided together"):
        await service.plan("Loki", season=2)


@pytest.mark.asyncio
async def test_plan_reports_exact_episode_feature_disabled(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    config = AppConfig(storage=StorageConfig(tmp_path / "test.db"))
    service = PlaybackService(config, database, catalog=FakeCatalog())

    plan = await service.plan("Loki", season=2, episode=3)

    assert plan["status"] == "feature_disabled"
    assert plan["enable_with"] == "EXACT_EPISODE_PLAYBACK_ENABLED=true"


class FakeRoku:
    """Stands in for RokuClient: records the deep link and reports the
    player state it is told to."""

    launched: list[tuple[str, str, str]] = []
    player_state = "play"

    def __init__(self, device) -> None:
        self.device = device

    async def find_app(self, name_or_id: str):
        from house_agent.roku import RokuApp

        return RokuApp(id="123", name=name_or_id, version="1")

    async def deep_link(self, app_id: str, content_id: str, media_type: str) -> None:
        FakeRoku.launched.append((app_id, content_id, media_type))

    async def media_player(self):
        return {"state": FakeRoku.player_state}

    async def keypress(self, key: str) -> None:
        pass


class MisspellingCatalog(FakeCatalog):
    """Knows Loki, and nothing about 'Lokey'."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, subscriptions: set[str]):
        self.queries.append(query)
        if query != "Loki":
            return {"query": query, "region": "US", "results": []}
        return await super().search(query, subscriptions)


async def test_plan_recovers_a_misspelled_title_from_the_web(tmp_path: Path) -> None:
    """'Put on Lokey' must not end in 'not in the catalog'."""
    service, _ = _service(tmp_path)
    catalog = MisspellingCatalog()
    service.catalog = catalog
    asked: list[str] = []

    async def guess(query: str):
        asked.append(query)
        return {"title": "Loki", "year": 2021, "similarity": 0.8}

    service.guess_title = guess
    result = await service.plan("Lokey")
    assert result["status"] in {"ready", "needs_verification"}
    assert result["did_you_mean"] == {
        "title": "Loki", "year": 2021, "similarity": 0.8, "asked_for": "Lokey"
    }
    assert asked == ["Lokey"] and catalog.queries == ["Lokey", "Loki"]

    # Search behaves the same way, keeping the user's words as the query.
    found = await service.search("Lokey")
    assert found["query"] == "Lokey" and found["results"][0]["title"] == "Loki"
    assert found["did_you_mean"]["title"] == "Loki"


async def test_plan_stays_not_found_when_the_web_has_no_idea(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.catalog = MisspellingCatalog()

    async def no_guess(query: str):
        return None

    service.guess_title = no_guess
    result = await service.plan("Lokey")
    assert result["status"] == "not_found" and "did_you_mean" not in result

    async def wrong_guess(query: str):
        return {"title": "Thor", "similarity": 0.3}

    service.guess_title = wrong_guess
    result = await service.plan("Lokey")
    assert result["status"] == "not_found" and result["did_you_mean"]["title"] == "Thor"


def _service(tmp_path: Path) -> tuple[PlaybackService, Database]:
    from house_agent.config import RokuDeviceConfig

    database = Database(tmp_path / "play.db")
    database.initialize()
    database.seed_subscriptions([("disney_plus", "Disney+")])
    config = AppConfig(
        catalog=CatalogConfig(region="US"),
        storage=StorageConfig(tmp_path / "play.db"),
        tv_devices=(RokuDeviceConfig(id="living-room", name="TV", host="127.0.0.1"),),
    )
    return PlaybackService(config, database, catalog=FakeCatalog()), database


@pytest.mark.asyncio
async def test_first_play_on_a_provider_proceeds_and_self_verifies(tmp_path, monkeypatch):
    import house_agent.content as content_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(content_module, "RokuClient", FakeRoku)
    monkeypatch.setattr(content_module.asyncio, "sleep", no_sleep)
    FakeRoku.launched.clear()
    FakeRoku.player_state = "play"
    service, database = _service(tmp_path)

    result = await service.play(device_id="living-room", title="Loki")

    # No refusal: the unverified provider's first play went ahead...
    assert result["ok"] is True and result["status"] == "launched"
    assert FakeRoku.launched == [("123", "loki-id", "series")]
    assert result["first_play_on_provider"] is True
    # ...and playback confirmation verified the provider for the future.
    assert result["provider_newly_verified"] is True
    assert database.get_provider_resolver("disney_plus")["verified"] is True
    assert "next_step" not in result


@pytest.mark.asyncio
async def test_first_play_unconfirmed_asks_the_user_to_check(tmp_path, monkeypatch):
    import house_agent.content as content_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(content_module, "RokuClient", FakeRoku)
    monkeypatch.setattr(content_module.asyncio, "sleep", no_sleep)
    FakeRoku.launched.clear()
    FakeRoku.player_state = "stop"
    service, database = _service(tmp_path)

    result = await service.play(device_id="living-room", title="Loki")

    assert result["ok"] is True and result["first_play_on_provider"] is True
    assert result["provider_newly_verified"] is False
    assert "confirm the right title started" in result["next_step"]
    assert database.get_provider_resolver("disney_plus")["verified"] is False


@pytest.mark.asyncio
async def test_a_slow_launch_still_clears_the_profile_picker(tmp_path, monkeypatch):
    """The exact failure: Prime Video took longer than the request timeout
    to answer the launch, so play reported 'could not reach the TV' and
    never pressed Select on the who's-watching screen. Here the launch
    times out, the first status check is dropped, the second shows nothing
    playing (the picker), Select is pressed, and then it plays."""
    import house_agent.content as content_module
    from house_agent.roku import RokuError, RokuTimeout

    async def no_sleep(_seconds):
        return None

    presses: list[str] = []
    states = iter([RokuError("busy"), "stop", "play"])

    class SlowRoku(FakeRoku):
        async def deep_link(self, app_id, content_id, media_type):
            FakeRoku.launched.append((app_id, content_id, media_type))
            raise RokuTimeout("TV took too long to answer")

        async def media_player(self):
            state = next(states)
            if isinstance(state, Exception):
                raise state
            return {"state": state}

        async def keypress(self, key):
            presses.append(key)

    monkeypatch.setattr(content_module, "RokuClient", SlowRoku)
    monkeypatch.setattr(content_module.asyncio, "sleep", no_sleep)
    FakeRoku.launched.clear()
    service, _ = _service(tmp_path)

    result = await service.play(device_id="living-room", title="Loki")
    assert result["ok"] is True
    assert result["profile_confirmation"] == "select_confirmed"
    assert presses == ["Select"]


@pytest.mark.asyncio
async def test_the_roku_says_slow_rather_than_unreachable() -> None:
    import httpx

    from house_agent.config import RokuDeviceConfig
    from house_agent.roku import RokuClient, RokuError, RokuTimeout

    device = RokuDeviceConfig(id="tv", name="TV", host="192.168.0.150")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(RokuTimeout, match="too long"):
            await RokuClient(device, client=client).launch("12")
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(RokuError, match="Could not reach") as caught:
            await RokuClient(device, client=client).launch("12")
        assert not isinstance(caught.value, RokuTimeout)


class EpisodeCatalog(FakeCatalog):
    """Loki, with a season that has already aired and one still going."""

    LISTED = [
        {"season": 1, "episode": 1, "name": "Glorious Purpose", "released": "2021-06-09", "overview": None, "runtime_minutes": 52},
        {"season": 2, "episode": 1, "name": "Ouroboros", "released": "2026-09-14", "overview": None, "runtime_minutes": 48},
        {"season": 2, "episode": 2, "name": "Breaking Brad", "released": "2026-09-21", "overview": None, "runtime_minutes": 45},
        {"season": 2, "episode": 3, "name": "1893", "released": "2026-10-05", "overview": None, "runtime_minutes": 47},
    ]

    async def search_for_playback(self, query: str, subscriptions: set[str], media_type=None):
        if "loki" not in query.lower():
            return {"query": query, "region": "US", "results": []}
        return await self.search("Loki", subscriptions)

    async def episodes(self, catalog: str, catalog_id: str):
        assert (catalog, catalog_id) == ("fake", "tv-1")
        return [dict(item) for item in self.LISTED]


def episode_service(tmp_path: Path) -> PlaybackService:
    database = Database(tmp_path / "episodes.db")
    database.initialize()
    database.seed_subscriptions([("disney_plus", "Disney+")])
    config = AppConfig(
        catalog=CatalogConfig(region="US"),
        storage=StorageConfig(database_path=tmp_path / "episodes.db"),
    )
    return PlaybackService(config, database, catalog=EpisodeCatalog())


@pytest.mark.asyncio
async def test_newest_episode_is_the_last_one_that_has_aired(tmp_path: Path, monkeypatch) -> None:
    """"The newest episode of Always Sunny" had no answer at all: the series
    played from wherever it left off and nobody was told which one was new."""
    import house_agent.content as content

    monkeypatch.setattr(content, "date", _FrozenDate(2026, 9, 24))
    result = await episode_service(tmp_path).episodes("Loki")

    assert result["status"] == "ok" and result["title"] == "Loki"
    assert result["seasons"] == 2 and result["episode_count"] == 4
    # Not 1893, which has not aired yet.
    assert (result["latest"]["season"], result["latest"]["episode"]) == (2, 2)
    assert result["latest"]["name"] == "Breaking Brad"
    assert result["next_episode"]["name"] == "1893"
    assert result["available_on"] == ["disney_plus"]


@pytest.mark.asyncio
async def test_a_season_can_be_listed_and_a_missing_one_says_so(tmp_path: Path, monkeypatch) -> None:
    import house_agent.content as content

    monkeypatch.setattr(content, "date", _FrozenDate(2026, 9, 24))
    service = episode_service(tmp_path)

    season = await service.episodes("Loki", season=1)
    assert [item["name"] for item in season["episodes"]] == ["Glorious Purpose"]

    missing = await service.episodes("Loki", season=9)
    assert missing["status"] == "no_such_season" and "no season 9" in missing["error"]


@pytest.mark.asyncio
async def test_a_short_name_is_looked_up_before_giving_up(tmp_path: Path, monkeypatch) -> None:
    import house_agent.content as content

    monkeypatch.setattr(content, "date", _FrozenDate(2026, 9, 24))
    service = episode_service(tmp_path)
    service.guess_title = lambda query: _resolved({"title": "Loki", "why": "shortened"})

    result = await service.episodes("that loki show")
    assert result["status"] == "ok" and result["title"] == "Loki"

    service.guess_title = lambda query: _resolved(None)
    assert (await service.episodes("nothing at all"))["status"] == "not_found"


class _FrozenDate:
    """Only `date.today()` is used, so only that needs to hold still."""

    def __init__(self, *parts: int) -> None:
        from datetime import date as real_date

        self._today = real_date(*parts)

    def today(self):
        return self._today


async def _resolved(value):
    return value
