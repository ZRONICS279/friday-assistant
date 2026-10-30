import httpx
import pytest

from house_agent.config import TVDeviceConfig
from house_agent.tv import (
    TVUnsupported,
    client_for,
    normalize_key,
    normalize_platform,
)
from house_agent.tv.registry import PLATFORMS
from house_agent.tv.androidtv import AndroidTV
from house_agent.tv.roku import RokuTV
from house_agent.tv.samsung import SamsungTV
from house_agent.tv.vizio import VizioTV
from house_agent.tv.webos import WebOSTV


def _device(platform: str = "roku", host: str = "192.168.0.150") -> TVDeviceConfig:
    return TVDeviceConfig(id="den", name="Den TV", host=host, platform=platform)


def test_brand_words_resolve_to_platforms() -> None:
    assert normalize_platform("LG") == "webos"
    assert normalize_platform("Fire TV") == "firetv"
    assert normalize_platform("Apple TV") == "appletv"
    assert normalize_platform("Google TV") == "androidtv"
    assert normalize_platform("Sony") == "androidtv"
    assert normalize_platform("SmartCast") == "vizio"
    assert normalize_platform("Tizen") == "samsung"
    # Configs written before other brands existed stay Roku.
    assert normalize_platform(None) == "roku"


def test_unknown_platform_names_the_supported_ones() -> None:
    with pytest.raises(ValueError, match="androidtv"):
        normalize_platform("betamax")


def test_spoken_key_words_fold_onto_one_vocabulary() -> None:
    assert normalize_key("OK") == "select"
    assert normalize_key("Fast-Forward") == "forward"
    assert normalize_key("vol up") == "volume_up"
    assert normalize_key("  Power Off ") == "power_off"


def test_unknown_key_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="Unsupported key"):
        normalize_key("eject")


def test_every_platform_is_reachable_through_the_factory() -> None:
    for platform, expected in PLATFORMS.items():
        assert isinstance(client_for(_device(platform)), expected)


def test_a_tv_off_the_local_network_is_refused() -> None:
    with pytest.raises(ValueError, match="private network"):
        client_for(_device("roku", host="8.8.8.8"))


@pytest.mark.asyncio
async def test_roku_translates_canonical_keys_to_ecp() -> None:
    pressed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pressed.append(request.url.path)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tv = RokuTV(_device("roku"), options={"http_client": client})
        await tv.press("select")
        await tv.press("ok")
        await tv.press("fast forward")
        await tv.press("volume up")

    assert pressed == [
        "/keypress/Select",
        "/keypress/Select",
        "/keypress/Fwd",
        "/keypress/VolumeUp",
    ]


@pytest.mark.asyncio
async def test_a_key_the_remote_lacks_says_so_by_name() -> None:
    tv = RokuTV(_device("roku"))
    with pytest.raises(TVUnsupported, match="guide"):
        await tv.press("guide")


@pytest.mark.asyncio
async def test_capabilities_gate_what_a_platform_is_asked_to_do() -> None:
    # Vizio has no text entry over its REST API, Samsung no input switching.
    with pytest.raises(TVUnsupported, match="type text"):
        await VizioTV(_device("vizio")).type_text("hello")
    with pytest.raises(TVUnsupported, match="switch video inputs"):
        await SamsungTV(_device("samsung")).switch_input("hdmi 1")


def test_capabilities_are_declared_for_every_platform() -> None:
    for platform, cls in PLATFORMS.items():
        assert cls.capabilities, f"{platform} declares no capabilities"
        assert cls.default_port > 0, f"{platform} has no default port"


@pytest.mark.asyncio
async def test_unpaired_platforms_refuse_before_touching_the_network() -> None:
    """An unpaired TV must fail with pairing guidance, not a timeout."""
    from house_agent.tv.base import TVAuthRequired

    with pytest.raises(TVAuthRequired, match="not paired"):
        async with WebOSTV(_device("webos"))._connect():
            pass

    with pytest.raises(TVAuthRequired, match="not paired"):
        VizioTV(_device("vizio"))._device_client()

    with pytest.raises(TVAuthRequired, match="not paired"):
        async with AndroidTV(_device("androidtv"))._connect():
            pass


@pytest.mark.asyncio
async def test_a_stored_credential_gets_past_the_pairing_gate() -> None:
    """With a client key present webOS proceeds to connect, so the failure
    that surfaces is about reaching the TV, not about pairing."""
    from house_agent.tv.base import TVError

    tv = WebOSTV(_device("webos", host="127.0.0.1"), credentials={"client_key": "k"})
    with pytest.raises(TVError, match="Could not reach"):
        async with tv._connect():
            pass


@pytest.mark.asyncio
async def test_roku_reports_position_and_app_from_the_media_player() -> None:
    player = (
        b'<?xml version="1.0"?><player state="play" error="false">'
        b'<plugin id="2285" name="Hulu"/><position>1718516 ms</position>'
        b"<duration>4624135 ms</duration></player>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=player)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        playing = await RokuTV(_device("roku"), options={"http_client": client}).now_playing()

    assert playing == {
        "state": "play",
        "app": "Hulu",
        "position_seconds": 1719,
        "total_seconds": 4624,
    }


@pytest.mark.asyncio
async def test_a_closed_roku_player_reports_nothing_playing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'<player state="close" error="false"/>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tv = RokuTV(_device("roku"), options={"http_client": client})
        assert await tv.now_playing() is None


@pytest.mark.asyncio
async def test_android_tv_reads_volume_but_refuses_to_set_a_level() -> None:
    """The remote protocol carries key codes only, so a level is impossible
    and saying so beats silently doing nothing."""
    tv = AndroidTV(_device("androidtv"), credentials={"paired": True})
    with pytest.raises(TVUnsupported, match="volume up or volume down"):
        await tv.set_volume(40)


def test_volume_support_is_declared_honestly() -> None:
    from house_agent.tv.base import VOLUME

    supports = {name for name, cls in PLATFORMS.items() if VOLUME in cls.capabilities}
    # Roku's ECP has no volume query at all, and Samsung's socket none either.
    assert supports == {"webos", "vizio", "androidtv", "appletv"}


def test_wake_on_lan_is_declared_only_where_the_socket_dies() -> None:
    from house_agent.tv.base import WAKE_ON_LAN

    needs = {name for name, cls in PLATFORMS.items() if WAKE_ON_LAN in cls.capabilities}
    assert needs == {"webos", "samsung"}
