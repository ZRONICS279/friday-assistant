"""A feature with no API key: Friday falls back to a web search and says so,
unless the household switched that off in Settings."""

import httpx

from house_agent.api import create_app
from house_agent.config import AppConfig, ServerConfig, StorageConfig
from house_agent.db import Database
from house_agent.keys import WEB_FALLBACK_SETTING
from house_agent.tools import ToolRegistry
from house_agent.travel import TravelNotConfigured


def _tools(tmp_path) -> ToolRegistry:
    config = AppConfig(storage=StorageConfig(database_path=tmp_path / "agent.db"))
    database = Database(config.storage.database_path)
    database.initialize()
    return ToolRegistry(config, database)


async def test_no_catalog_key_offers_a_web_search(tmp_path) -> None:
    tools = _tools(tmp_path)
    result = await tools.execute("content_recommend", {"genres": ["Horror"]})
    assert result["ok"] is False and result["no_key"] is True
    assert result["fallback"] == "web_search"
    assert "web_search" in result["next_step"] and "less accurate" in result["next_step"]


async def test_playing_without_a_key_opens_the_app_instead(tmp_path) -> None:
    tools = _tools(tmp_path)
    result = await tools.execute("content_plan_playback", {"title": "The Bear"})
    assert result["no_key"] is True
    assert "tv_launch_app" in result["next_step"]


async def test_flights_without_a_token_fall_back_too(tmp_path) -> None:
    tools = _tools(tmp_path)

    async def not_set_up(*_, **__):
        raise TravelNotConfigured("Flight search is not set up on this house yet.")

    tools.travel.search_flights = not_set_up
    result = await tools.execute("flight_search", {"origin": "SEA", "destination": "LAX", "depart": "2026-10-01"})
    assert result["no_key"] is True and result["feature"] == "flight search"
    assert result["fallback"] == "web_search"


async def test_switched_off_it_names_the_key_instead(tmp_path) -> None:
    tools = _tools(tmp_path)
    tools.database.set_setting(WEB_FALLBACK_SETTING, "off")
    result = await tools.execute("content_recommend", {})
    assert result["no_key"] is True and "fallback" not in result
    assert "TMDB_READ_ACCESS_TOKEN" in result["next_step"]
    assert "Don't search the web" in result["next_step"]


async def test_the_switch_is_per_feature(tmp_path) -> None:
    tools = _tools(tmp_path)
    tools.database.set_setting("web_fallback.flights", "off")

    async def not_set_up(*_, **__):
        raise TravelNotConfigured("Flight search is not set up on this house yet.")

    tools.travel.search_flights = not_set_up
    flights = await tools.execute("flight_search", {"origin": "SEA", "destination": "LAX", "depart": "2026-10-01"})
    movies = await tools.execute("content_recommend", {})
    assert "fallback" not in flights and "letsfg auth" in flights["next_step"]
    assert movies["fallback"] == "web_search"


async def test_settings_switches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("house_agent.travel._token_file", lambda: tmp_path / "no-token.json")
    monkeypatch.delenv("LETSFG_BEARER_TOKEN", raising=False)
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "api.db"),
        server=ServerConfig(api_token="abc123"),
        config_path=tmp_path / "config.toml",
    )
    app = create_app(config)
    headers = {"Authorization": "Bearer abc123"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            house = (await client.get("/api/house", headers=headers)).json()["web_fallback"]
            features = {f["id"]: f for f in house["features"]}
            assert house["default"] is True
            assert features["movies"]["has_key"] is False and features["movies"]["web_search"] is True
            off = await client.put(
                "/api/house/web-fallback", json={"feature": "movies", "enabled": False}, headers=headers
            )
            after = {f["id"]: f["web_search"] for f in off.json()["features"]}
            assert after == {"movies": False, "flights": True}
            bad = await client.put("/api/house/web-fallback", json={"feature": "nope", "enabled": False}, headers=headers)
            assert bad.status_code == 404
