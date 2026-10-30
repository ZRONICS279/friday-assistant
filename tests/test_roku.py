import httpx
import pytest

from house_agent.config import RokuDeviceConfig
from house_agent.roku import (
    RokuClient,
    RokuError,
    discover_roku_devices,
    normalize_key,
    resolve_input_key,
)


@pytest.fixture
def device() -> RokuDeviceConfig:
    return RokuDeviceConfig(id="living-room", name="Living Room TV", host="192.168.0.150")


@pytest.mark.asyncio
async def test_reads_status_and_active_app(device: RokuDeviceConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/query/device-info":
            return httpx.Response(
                200,
                text="<device-info><power-mode>PowerOn</power-mode><model-name>Roku TV</model-name></device-info>",
            )
        return httpx.Response(200, text='<active-app><app id="291097">Disney Plus</app></active-app>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status = await RokuClient(device, client=client).status()

    assert status["power_mode"] == "PowerOn"
    assert status["active_app"]["name"] == "Disney Plus"


@pytest.mark.asyncio
async def test_reads_media_player_state(device: RokuDeviceConfig) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '<player state="play" error="false">'
                '<plugin id="61322" name="Max" bandwidth="0">Max</plugin>'
                "</player>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        player = await RokuClient(device, client=client).media_player()

    assert player["state"] == "play"
    assert player["plugin"]["name"] == "Max"


@pytest.mark.asyncio
async def test_resolves_and_launches_installed_app(device: RokuDeviceConfig) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/query/apps":
            return httpx.Response(
                200,
                text='<apps><app id="12">Netflix</app><app id="291097">Disney Plus</app></apps>',
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        roku = RokuClient(device, client=client)
        app = await roku.find_app("disney")
        await roku.launch(app.id)

    assert app.id == "291097"
    assert requests[-1].url.path == "/launch/291097"


@pytest.mark.asyncio
async def test_deep_link_identifies_roku_search_source(device: RokuDeviceConfig) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RokuClient(device, client=client).deep_link("12", "80057281", "series")

    assert requests[0].url.params["source"] == "hs-search"


@pytest.mark.asyncio
async def test_encodes_literal_text(device: RokuDeviceConfig) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RokuClient(device, client=client).type_text("Loki season 2")

    assert requests[0].url.path == "/keypress/Lit_Loki season 2"
    assert "%20" in str(requests[0].url)


@pytest.mark.asyncio
async def test_explains_roku_local_control_403(device: RokuDeviceConfig) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="ECP command not allowed in Limited mode")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception, match="Control by mobile apps"):
            await RokuClient(device, client=client).keypress("Home")


def test_normalizes_human_key_aliases() -> None:
    assert normalize_key("volume_down") == "VolumeDown"
    assert normalize_key("ok") == "Select"
    with pytest.raises(ValueError):
        normalize_key("FactoryReset")


def test_resolves_configured_and_standard_tv_inputs() -> None:
    aliases = {"xbox": "InputHDMI2", "switch": "InputHDMI4", "tv": "Home"}

    assert resolve_input_key("Xbox", aliases) == "InputHDMI2"
    assert resolve_input_key("Nintendo Switch", aliases) == "InputHDMI4"
    assert resolve_input_key("TV", aliases) == "Home"
    assert resolve_input_key("live TV", aliases) == "InputTuner"
    assert resolve_input_key("HDMI 3", aliases) == "InputHDMI3"


def test_rejects_public_device_address() -> None:
    with pytest.raises(ValueError):
        RokuClient(RokuDeviceConfig(id="bad", name="Bad", host="8.8.8.8"))


def test_discovery_reports_unavailable_multicast(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendto(self, *_):
            raise OSError("no multicast route")

    monkeypatch.setattr("house_agent.roku.socket.socket", lambda *_: UnavailableSocket())

    with pytest.raises(RokuError, match="config.toml"):
        discover_roku_devices()


@pytest.mark.asyncio
async def test_fetches_app_icon(device: RokuDeviceConfig) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        content, media_type = await RokuClient(device, client=client).app_icon("12")

    assert seen == ["/query/icon/12"]
    assert content == b"\x89PNG"
    assert media_type == "image/png"


@pytest.mark.asyncio
async def test_an_app_that_is_not_there_names_the_ones_that_are(device: RokuDeviceConfig) -> None:
    """The installed list is in hand at the moment of the failure and used to
    be thrown away with it, leaving "Hulu is not installed" and nothing to
    offer instead."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/query/apps":
            return httpx.Response(
                200,
                text='<apps><app id="12">Netflix</app><app id="291097">Disney Plus</app></apps>',
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RokuError) as raised:
            await RokuClient(device, client=client).find_app("hulu")

    message = str(raised.value)
    assert "Netflix" in message and "Disney Plus" in message
    assert "not installed" in message
