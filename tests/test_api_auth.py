"""Nobody on the Wi-Fi controls the house without the pairing code."""

import httpx

from house_agent.api import create_app
from house_agent.config import AppConfig, ServerConfig, StorageConfig


def app_for(tmp_path, token="abc123"):
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "api.db"),
        server=ServerConfig(api_token=token),
        config_path=tmp_path / "config.toml",  # absent: nothing is written
    )
    return create_app(config)


async def test_api_calls_need_the_code_but_the_shell_and_health_do_not(tmp_path) -> None:
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/api/health")).json()["paired"] is False
            refused = await client.get("/api/lists")
            assert refused.status_code == 401 and "Pairing code" in refused.text
            assert (await client.get("/api/lists", headers={"Authorization": "Bearer abc123"})).status_code == 200
            assert (await client.get("/api/lists?token=abc123")).status_code == 200
            assert (await client.get("/api/lists", cookies={"house_token": "abc123"})).status_code == 200
            assert (await client.get("/api/lists", headers={"Authorization": "Bearer nope"})).status_code == 401
            health = await client.get("/api/health", headers={"Authorization": "Bearer abc123"})
            assert health.json()["paired"] is True
            assert (await client.get("/")).status_code == 200


async def test_a_missing_code_is_minted_and_kept_in_env(tmp_path, caplog) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[server]\n")
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "api.db"),
        server=ServerConfig(api_token=""),
        config_path=config_path,
    )
    app = create_app(config)
    token = app.state.api_token
    assert len(token) >= 12
    assert token in (tmp_path / ".env").read_text()
    assert any("Pairing code for the web app" in r.message for r in caplog.records)


def test_device_set_takes_warmth_under_either_name() -> None:
    """The warmth slider sent color_temp_kelvin, which the model dropped,
    leaving 'Nothing to change'."""
    from house_agent.api import DeviceSetRequest

    for field in ("color_temp", "color_temp_kelvin"):
        body = DeviceSetRequest.model_validate({"device": "lamp", field: 2700})
        assert body.model_dump(exclude_none=True) == {"device": "lamp", "color_temp": 2700}
