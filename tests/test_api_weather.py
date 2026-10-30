"""The glance card's weather: unknown until the house has a location."""

import httpx

from house_agent.api import create_app
from house_agent.config import AppConfig, ServerConfig, StorageConfig


async def test_weather_reports_an_unknown_home_without_caching_it(tmp_path) -> None:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "api.db"),
        server=ServerConfig(api_token="abc123"),
        config_path=tmp_path / "config.toml",
    )
    app = create_app(config)
    calls = []

    async def now():
        calls.append(1)
        if len(calls) == 1:
            from house_agent.weather import HomeLocationUnknown

            raise HomeLocationUnknown("no home yet")
        return {"place": "Home", "temperature": 61, "code": 3}

    async with app.router.lifespan_context(app):
        app.state.tools.weather.now = now
        headers = {"Authorization": "Bearer abc123"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/api/weather", headers=headers)).json() == {"known": False}
            second = (await client.get("/api/weather", headers=headers)).json()
            third = (await client.get("/api/weather", headers=headers)).json()
    assert second == {"known": True, "place": "Home", "temperature": 61, "code": 3}
    assert third == second
    assert len(calls) == 2  # the good answer is cached
