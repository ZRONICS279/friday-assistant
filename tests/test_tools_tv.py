import httpx
import pytest

from house_agent.config import AppConfig, StorageConfig, TVDeviceConfig
from house_agent.db import Database
from house_agent.tools import ToolError, ToolRegistry


def _registry(tmp_path, transport=None, platform="roku"):
    database = Database(tmp_path / "tools.db")
    database.initialize()
    config = AppConfig(
        storage=StorageConfig(tmp_path / "tools.db"),
        tv_devices=(
            TVDeviceConfig(id="den", name="Den TV", host="192.168.0.150", platform=platform),
            TVDeviceConfig(id="bedroom", name="Bedroom TV", host="192.168.0.160", platform="webos"),
        ),
    )
    registry = ToolRegistry(config, database)
    if transport is not None:
        client = httpx.AsyncClient(transport=transport)
        base = registry._tv_options
        registry._tv_options = lambda: {**base(), "http_client": client}
    return registry, database


def test_the_model_is_offered_one_brand_agnostic_tool_set(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    names = {schema["function"]["name"] for schema in registry.schemas}

    assert {"tv_press", "tv_launch_app", "tv_pair", "tv_discover"} <= names
    # The old brand-specific names must not be advertised any more.
    assert not any(name.startswith("roku_") for name in names)


def test_superseded_roku_names_still_dispatch(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    for suffix in ("press", "get_status", "launch_app", "run_routine"):
        assert registry._handlers[f"roku_{suffix}"] is registry._handlers[f"tv_{suffix}"]


@pytest.mark.asyncio
async def test_listing_devices_reports_platform_and_capabilities(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    result = await registry.execute("tv_list_devices", {})

    by_id = {device["id"]: device for device in result["devices"]}
    assert by_id["den"]["platform"] == "roku"
    assert "deep_link" in by_id["den"]["capabilities"]
    assert by_id["bedroom"]["platform"] == "webos"
    # LG cannot take typed text, and the model is told so up front.
    assert "text_input" not in by_id["bedroom"]["capabilities"]


@pytest.mark.asyncio
async def test_pressing_a_key_reaches_the_right_platform(tmp_path) -> None:
    pressed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pressed.append(request.url.path)
        return httpx.Response(200)

    registry, _ = _registry(tmp_path, httpx.MockTransport(handler))
    result = await registry.execute("tv_press", {"device_id": "den", "key": "ok", "count": 2})

    assert result == {"ok": True, "key": "ok", "count": 2}
    assert pressed == ["/keypress/Select", "/keypress/Select"]


@pytest.mark.asyncio
async def test_an_unknown_device_lists_the_configured_ones(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    with pytest.raises(ToolError, match="den, bedroom"):
        await registry.execute("tv_get_status", {"device_id": "kitchen"})


@pytest.mark.asyncio
async def test_pairing_a_roku_explains_it_needs_none(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    result = await registry.execute("tv_pair", {"device_id": "den", "step": "begin"})

    assert result["ok"] is False
    assert "does not need pairing" in result["error"]


@pytest.mark.asyncio
async def test_completing_a_pairing_that_never_started_is_refused(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    result = await registry.execute(
        "tv_pair", {"device_id": "bedroom", "step": "complete", "code": "1234"}
    )

    assert result["ok"] is False
    assert "not started" in result["error"]


@pytest.mark.asyncio
async def test_a_completed_pairing_is_persisted_for_next_time(tmp_path) -> None:
    registry, database = _registry(tmp_path)

    class FakeTV:
        platform = "webos"

        async def complete_pairing(self, code=None):
            return {"client_key": "stored-key"}

    registry._pairing["bedroom"] = FakeTV()
    result = await registry.execute("tv_pair", {"device_id": "bedroom", "step": "complete"})

    assert result["ok"] is True
    assert database.tv_credentials("bedroom") == {"client_key": "stored-key"}
    # The one-shot exchange is cleared, so a stale client cannot be reused.
    assert "bedroom" not in registry._pairing


@pytest.mark.asyncio
async def test_calibrated_routines_stay_roku_only(tmp_path) -> None:
    registry, database = _registry(tmp_path)
    database.save_routine("pick-profile", "bedroom", "choose a profile", [{"key": "Select"}])

    with pytest.raises(ToolError, match="only exist for Roku"):
        await registry.execute("tv_run_routine", {"name": "pick-profile"})


def _linked(tmp_path):
    """A Roku TV with an Apple TV box plugged into HDMI 2."""
    from house_agent.config import AppConfig, StorageConfig, TVDeviceConfig
    from house_agent.db import Database

    database = Database(tmp_path / "linked.db")
    database.initialize()
    config = AppConfig(
        storage=StorageConfig(tmp_path / "linked.db"),
        tv_devices=(
            TVDeviceConfig(id="living-room", name="Living Room TV", host="192.168.0.150"),
            TVDeviceConfig(
                id="apple-box",
                name="Apple TV",
                host="192.168.0.151",
                platform="appletv",
                connected_to="living-room",
                input="HDMI 2",
            ),
        ),
    )
    return ToolRegistry(config, database)


@pytest.mark.asyncio
async def test_a_box_reports_the_tv_and_input_it_lives_on(tmp_path) -> None:
    result = await _linked(tmp_path).execute("tv_list_devices", {})
    box = next(d for d in result["devices"] if d["id"] == "apple-box")

    assert box["connected_to"] == "living-room"
    assert box["input"] == "HDMI 2"
    assert "accounts" in box["capabilities"]


@pytest.mark.asyncio
async def test_launching_on_a_box_switches_the_tv_to_its_input_first(tmp_path) -> None:
    registry = _linked(tmp_path)
    order: list[str] = []

    async def fake_switch(target):
        order.append(f"switch:{target}")

    async def fake_launch(app):
        order.append(f"launch:{app}")
        from house_agent.tv.base import TVApp

        return TVApp(id="com.netflix", name="Netflix")

    real = registry.client_for

    def patched(device_id):
        client = real(device_id)
        if device_id == "living-room":
            client.switch_input = fake_switch
        else:
            client.launch_app = fake_launch
        return client

    registry.client_for = patched
    result = await registry.execute(
        "tv_launch_app", {"device_id": "apple-box", "app": "Netflix"}
    )

    assert order == ["switch:HDMI 2", "launch:Netflix"]
    assert "HDMI 2" in result["input"]


@pytest.mark.asyncio
async def test_a_bare_key_press_leaves_the_input_alone(tmp_path) -> None:
    """Turning the volume down must not change what the TV is showing."""
    registry = _linked(tmp_path)
    touched: list[str] = []

    real = registry.client_for

    def patched(device_id):
        client = real(device_id)
        touched.append(device_id)

        async def press(key):
            return None

        async def switch(target):
            raise AssertionError("a key press must not switch inputs")

        client.press = press
        client.switch_input = switch
        return client

    registry.client_for = patched
    await registry.execute("tv_press", {"device_id": "apple-box", "key": "up"})

    assert touched == ["apple-box"]


@pytest.mark.asyncio
async def test_tv_users_are_refused_on_platforms_without_them(tmp_path) -> None:
    registry = _linked(tmp_path)
    with pytest.raises(ToolError, match="cannot list its on-screen users"):
        await registry.execute("tv_list_users", {"device_id": "living-room"})


def _paused_roku(pressed):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/query/media-player":
            return httpx.Response(
                200,
                text='<player state="pause"><plugin id="12" name="Netflix"/><position>162000 ms</position></player>',
            )
        if request.url.path.startswith("/keypress/"):
            pressed.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, text="")

    return httpx.MockTransport(handler)


async def test_the_prompt_hears_what_the_tv_is_doing(tmp_path) -> None:
    registry, _ = _registry(tmp_path, _paused_roku([]))
    registry.devices.pop("bedroom")  # no webOS TV to reach in a test
    line = await registry.tv_live_line()
    assert "Den TV (id den): Netflix is PAUSED at 2:42." in line
    assert "tv_press play" in line


async def test_put_my_show_back_on_resumes_instead_of_searching(tmp_path) -> None:
    """The exact failure: 'put my show back on' over a paused Netflix became
    a catalog search for a title called 'my show'."""
    pressed: list[str] = []
    registry, _ = _registry(tmp_path, _paused_roku(pressed))

    async def no_catalog(**_):
        raise AssertionError("a vague title must not reach the catalog")

    registry.playback.play = no_catalog
    result = await registry.execute("content_play", {"device_id": "den", "title": "my show"})
    assert result["ok"] is True and result["resumed"] == "Netflix"
    assert pressed == ["Play"]


async def test_a_planned_title_says_it_is_not_playing_yet(tmp_path) -> None:
    registry, _ = _registry(tmp_path)
    registry.devices.pop("bedroom")

    async def plan(*_args):
        return {
            "status": "ready",
            "title": {"title": "The Love Hypothesis"},
            "link": {"provider": "prime_video", "media_type": "movie"},
        }

    registry.playback.plan = plan
    result = await registry.execute("content_plan_playback", {"title": "Love Hypothesis"})
    assert result["playing"] is False
    assert result["play_with"] == {
        "device_id": "den", "title": "The Love Hypothesis", "provider": "prime_video", "media_type": "movie",
    }
    assert "NOTHING IS PLAYING YET" in result["next_step"]


async def test_a_title_nobody_can_find_says_where_to_look_next(tmp_path) -> None:
    """A not_found plan used to come back bare — no next_step, no ok:false, no
    web fallback — so the turn ended on "I couldn't find that title"."""
    registry, _ = _registry(tmp_path)

    async def missing(*_args):
        return {"status": "not_found", "title": "Lokey", "catalog": {"results": []}}

    registry.playback.plan = missing
    result = await registry.execute("content_plan_playback", {"title": "Lokey"})
    assert "web_search with more of what the user said" in result["next_step"]

    async def guessed(*_args):
        return {
            "status": "not_found",
            "title": "Lokey",
            "did_you_mean": {"title": "Loki", "asked_for": "Lokey"},
        }

    registry.playback.plan = guessed
    result = await registry.execute("content_plan_playback", {"title": "Lokey"})
    assert "'Loki'" in result["next_step"] and "this same turn" in result["next_step"]


async def test_a_title_on_no_subscription_offers_something_else(tmp_path) -> None:
    registry, _ = _registry(tmp_path)

    async def elsewhere(*_args):
        return {"status": "not_on_subscription", "title": "Dune", "subscriptions": ["netflix"]}

    registry.playback.plan = elsewhere
    result = await registry.execute("content_plan_playback", {"title": "Dune"})
    assert "content_recommend with similar_to" in result["next_step"]
