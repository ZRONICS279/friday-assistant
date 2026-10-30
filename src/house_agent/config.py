from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


# scripts/matter-server.sh in this checkout: the launcher house-agent runs
# when it manages the Matter controller itself.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATTER_LAUNCHER = _REPO_ROOT / "scripts" / "matter-server.sh"
# The container stacks house-agent starts with itself: the Zigbee coordinator's
# broker and Zigbee2MQTT, and the speech-to-text/text-to-speech servers.
DEFAULT_COMPOSE_FILES = (
    _REPO_ROOT / "docker-compose.zigbee.yml",
    _REPO_ROOT / "docker-compose.tts.yml",
)
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LLM_MODEL = "house-qwen"
DEFAULT_LLM_API_KEY = "ollama"
DEFAULT_STT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_STT_MODEL = "whisper-1"
DEFAULT_STT_API_KEY = "local"
DEFAULT_TTS_BASE_URL = "http://127.0.0.1:8880/v1"
DEFAULT_TTS_MODEL = "tts-1"
DEFAULT_TTS_VOICE = "bf_isabella"
DEFAULT_TTS_SPANISH_VOICE = "ef_dora"
DEFAULT_TTS_API_KEY = "local"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    # The name phones use to reach this machine, for addresses Friday reads
    # out (the calendar feed). Matches the certificate's DNS name.
    public_host: str = "friday.local"
    # HTTPS for phones on the LAN: browsers only allow the microphone on a
    # secure origin. Point both at a certificate to serve https:// directly.
    ssl_certfile: Path | None = None
    # The pairing code every API call must carry (Authorization: Bearer …,
    # or ?token= for the event stream). Empty: one is generated at startup
    # and saved to .env as API_TOKEN, and printed in the log.
    api_token: str = ""
    ssl_keyfile: Path | None = None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str = DEFAULT_LLM_MODEL
    api_key: str = DEFAULT_LLM_API_KEY
    timeout_seconds: float = 120.0
    max_tool_rounds: int = 6
    # Spend reasoning tokens on the first round of complex requests
    # (multi-constraint recommendations). Costs 10-30s on those turns.
    think_complex: bool = True
    # Keep the local model loaded in the GPU between conversations. Ollama
    # unloads a model after five idle minutes, and reloading a 9 GB model
    # adds several seconds to the next reply. Off unless config turns it on
    # (load_config does, by default), so tests never load a real model.
    keep_warm: bool = False
    # The model's context window, in tokens. Every turn is trimmed to fit it
    # (Ollama would otherwise keep the tail of an over-long prompt and drop
    # the system prompt). Checked against the server at startup.
    context_tokens: int = 16384
    # Send each turn only the tool groups it needs, within this many tokens
    # of tool schemas, plus the guidance for those groups. Off sends all.
    route_tools: bool = True
    tool_budget_tokens: int = 6000


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    base_url: str = DEFAULT_STT_BASE_URL
    model: str = DEFAULT_STT_MODEL
    api_key: str = DEFAULT_STT_API_KEY
    language: str = "en"
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class TextToSpeechConfig:
    base_url: str = DEFAULT_TTS_BASE_URL
    model: str = DEFAULT_TTS_MODEL
    voice: str = DEFAULT_TTS_VOICE
    # Used automatically whenever the text being spoken is Spanish.
    spanish_voice: str = DEFAULT_TTS_SPANISH_VOICE
    api_key: str = DEFAULT_TTS_API_KEY
    response_format: str = "mp3"
    speed: float = 1.25
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    region: str = "US"
    tmdb_token: str | None = None
    watchmode_api_key: str | None = None
    exact_episode_playback_enabled: bool = False
    timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class TVDeviceConfig:
    """One television, whatever brand. `platform` picks the adapter that
    knows how to talk to it; the rest is the same for every brand."""

    id: str
    name: str
    host: str
    port: int = 8060
    platform: str = "roku"
    # A streaming box shares a screen with the TV it is plugged into. Naming
    # that TV and the input it occupies lets the agent get the picture onto
    # the right source before it starts anything playing.
    connected_to: str | None = None
    input: str | None = None
    # Needed only to wake an LG or Samsung that is fully off; those answer
    # nothing on the network until a magic packet reaches them.
    mac: str | None = None


# The project spoke only Roku first. Keep the old name importable so
# existing call sites and configs keep working unchanged.
RokuDeviceConfig = TVDeviceConfig


DEFAULT_MATTER_WS_URL = "ws://127.0.0.1:5580/ws"


@dataclass(frozen=True, slots=True)
class MatterConfig:
    """Connection to the matter-server controller (its own service)."""

    enabled: bool = True
    url: str = DEFAULT_MATTER_WS_URL
    # Needed once, for commissioning WiFi devices over Bluetooth: the
    # controller sends these credentials to the new device.
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    # This machine has a Bluetooth adapter the controller can use for
    # brand-new devices. Without one, devices must already be on the LAN
    # (vendor app first), and pairing searches the network only.
    bluetooth: bool = False
    # Which adapter: the hci index (hci0 -> 0), handed to matter-server as
    # --bluetooth-adapter by the launcher.
    bluetooth_adapter: int = 0
    timeout_seconds: float = 15.0
    # Start and supervise matter-server as part of house-agent (it is tiny:
    # tens of MB, seconds of CPU a day) instead of running it as a separate
    # service. Only applies when url points at this machine.
    manage: bool = True
    launcher: Path = DEFAULT_MATTER_LAUNCHER


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """Container stacks house-agent starts with itself and stops on exit.

    Off by default so a checkout without Docker behaves as it always has;
    config.toml turns it on. house-agent stops only the containers it
    actually started -- one it found already running is left alone.
    """

    manage: bool = False
    compose_files: tuple[Path, ...] = DEFAULT_COMPOSE_FILES
    # Podman users: ("podman", "compose"). Older installs: ("docker-compose",).
    command: tuple[str, ...] = ("docker", "compose")
    # Generous, because a first run may be pulling images.
    start_timeout_seconds: float = 180.0
    stop_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ZigbeeConfig:
    """Zigbee2MQTT over an MQTT broker. Off until a coordinator dongle
    and broker exist; flipping enabled is all it takes then."""

    enabled: bool = False
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    base_topic: str = "zigbee2mqtt"


DEFAULT_VOICE_MODEL_PATH = Path("data/models/wespeaker_en_voxceleb_resnet34_LM.onnx")


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Speaker identification (who is talking), run locally on the CPU."""

    enabled: bool = True
    model_path: Path = DEFAULT_VOICE_MODEL_PATH
    threshold: float = 0.62  # centred cosine similarity needed to accept a match
    margin: float = 0.08  # best match must beat the runner-up by this much
    min_speech_seconds: float = 2.5  # evidence needed before deciding
    max_wait_seconds: float = 12.0  # decide anyway after this much audio
    adapt: bool = True  # bank confident matches so the voice is learned over time
    max_banked_seconds: float = 30.0  # how much of each person's speech to keep
    threads: int = 2


@dataclass(frozen=True, slots=True)
class WeatherConfig:
    """Open-Meteo forecasts plus National Weather Service alerts. Neither
    needs an API key, so a latitude and longitude is the whole setup."""

    latitude: float | None = None
    longitude: float | None = None
    place: str = "home"
    units: str = "imperial"  # imperial or metric
    alerts_enabled: bool = True  # NWS severe weather warnings, US-only
    timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class NewsFeedConfig:
    """One RSS or Atom feed. `kind` is only a label: traffic incidents are
    published as RSS too, and the app groups them under their own heading."""

    url: str
    name: str = ""  # falls back to the feed's own title
    kind: str = "news"  # news or traffic


# Two wire services with open feeds, so the home screen has headlines before
# anyone edits config.toml. Replace them under [[news.feeds]].
DEFAULT_NEWS_FEEDS = (
    NewsFeedConfig("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    NewsFeedConfig("https://feeds.npr.org/1001/rss.xml", "NPR"),
)


@dataclass(frozen=True, slots=True)
class NewsConfig:
    """Headlines on the home screen. Feeds are read straight from the
    publisher, so there is no key and no account — only URLs."""

    enabled: bool = True
    feeds: tuple[NewsFeedConfig, ...] = DEFAULT_NEWS_FEEDS
    max_items: int = 12  # how many stories the home screen gets back
    per_feed: int = 8  # how deep to read each feed before merging
    timeout_seconds: float = 12.0


@dataclass(frozen=True, slots=True)
class DirectionsConfig:
    """Travel time from OpenStreetMap's own services, which need no key.
    Shares the house's coordinates with the weather so there is one place
    to set where home is. An OpenRouteService key is optional and only buys
    an availability guarantee the community servers do not offer."""

    units: str = "imperial"
    # Optional upgrades, both keyed. TomTom is the only one that adds live
    # traffic; OpenRouteService only adds an availability guarantee.
    tomtom_key: str | None = None
    openrouteservice_key: str | None = None
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """Calendars Friday reads so it knows about plans made elsewhere.

    Each entry is an iCalendar (ICS) URL: Google's per-calendar "secret
    address", an iCloud public share link, a Nextcloud or Outlook publish
    link. Read-only, no account, no OAuth; the URL itself is the secret,
    so it belongs in .env rather than config.toml."""

    subscriptions: tuple[str, ...] = ()
    refresh_minutes: float = 15.0
    # How far ahead to expand recurring events from subscribed calendars.
    horizon_days: int = 60
    # Weather heads-ups an hour before subscribed events, like spoken plans.
    weather_heads_up: bool = True
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class TravelConfig:
    """Flights and hotels from LetsFG (letsfg.co), a search engine built for
    agents. Hotel search needs nothing. Flight search needs a free Bearer
    token: run `letsfg auth` once on the house computer — a zero-amount card
    setup, nothing is charged — and the token is read from
    ~/.letsfg/config.json. Set it here or in LETSFG_BEARER_TOKEN to keep it
    somewhere else."""

    letsfg_token: str | None = None
    currency: str = "USD"
    units: str = "imperial"  # how far a hotel is from the area asked for
    # A flight search fans out to hundreds of airlines server-side and can
    # take a minute or two; this bounds how long a reply waits for it.
    timeout_seconds: float = 150.0


@dataclass(frozen=True, slots=True)
class DoorDashConfig:
    """Food, grocery, and retail delivery through DoorDash's own CLI, dd-cli
    (github.com/doordash-oss/doordash-cli, beta, by invitation). Install the
    binary and run `dd-cli login` once on the house computer; it keeps the
    sign-in in the OS keychain. A machine with no keychain takes the token
    from `dd-cli export-token` instead, in DD_CLI_ACCESS_TOKEN. Nothing
    DoorDash returns is stored by this house."""

    enabled: bool = True
    binary: Path | None = None  # found on PATH or in ~/.local/bin when unset
    access_token: str | None = None
    timeout_seconds: float = 90.0
    # After an order is placed, watch its status in the background and
    # announce when the store confirms it, the Dasher is close, and it lands.
    announce_updates: bool = True


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database_path: Path = Path("data/house-agent.db")


@dataclass(frozen=True, slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    text_to_speech: TextToSpeechConfig = field(default_factory=TextToSpeechConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    directions: DirectionsConfig = field(default_factory=DirectionsConfig)
    travel: TravelConfig = field(default_factory=TravelConfig)
    doordash: DoorDashConfig = field(default_factory=DoorDashConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    matter: MatterConfig = field(default_factory=MatterConfig)
    zigbee: ZigbeeConfig = field(default_factory=ZigbeeConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    tv_devices: tuple[TVDeviceConfig, ...] = ()
    roku_input_aliases: tuple[tuple[str, str], ...] = ()
    household_profile: str = "default"
    # Where this came from, so settings changed by voice land in the same
    # file (and its .env) and survive a restart.
    config_path: Path = Path("config.toml")


def _compose_files(from_toml: Any, config_path: Path) -> tuple[Path, ...]:
    """[docker] compose_files, resolved against the config file's folder so a
    relative name means what it looks like it means. Unset keeps the two
    stacks that ship with this checkout."""
    if from_toml is None:
        return DEFAULT_COMPOSE_FILES
    if isinstance(from_toml, str):
        from_toml = [from_toml]
    base = config_path.resolve().parent
    return tuple((base / str(name)).resolve() for name in from_toml)


def _calendar_urls(from_env: Any, from_toml: Any) -> tuple[str, ...]:
    """ICS URLs from CALENDAR_ICS_URLS (comma-separated) and [calendar]
    subscriptions (a list of strings, or tables with a url key), deduplicated."""
    urls: list[str] = []
    if from_env:
        urls += [part.strip() for part in str(from_env).split(",")]
    if isinstance(from_toml, list):
        for item in from_toml:
            if isinstance(item, dict):
                item = item.get("url")
            if item:
                urls.append(str(item).strip())
    seen: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.append(url)
    return tuple(seen)


def _news_feeds(from_env: Any, from_toml: Any) -> tuple[NewsFeedConfig, ...]:
    """Feeds from NEWS_FEEDS (comma-separated URLs) and [[news.feeds]]
    tables, deduplicated by URL. Either source on its own replaces the
    built-in feeds; setting both merges them, env first."""
    feeds: list[NewsFeedConfig] = []
    if from_env:
        feeds += [
            NewsFeedConfig(url=part.strip())
            for part in str(from_env).split(",")
            if part.strip()
        ]
    if isinstance(from_toml, list):
        for item in from_toml:
            if isinstance(item, str):
                item = {"url": item}
            if not isinstance(item, dict) or not item.get("url"):
                continue
            kind = str(item.get("kind", "news")).strip().lower()
            feeds.append(
                NewsFeedConfig(
                    url=str(item["url"]).strip(),
                    name=str(item.get("name", "")).strip(),
                    kind="traffic" if kind == "traffic" else "news",
                )
            )
    if not feeds:
        return DEFAULT_NEWS_FEEDS
    seen: dict[str, NewsFeedConfig] = {}
    for feed in feeds:
        seen.setdefault(feed.url, feed)
    return tuple(seen.values())


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def _tv_platform(value: Any) -> str:
    """Imported lazily: the tv package imports config, so a module-level
    import here would be circular."""
    from .tv.registry import normalize_platform

    return normalize_platform(value if value is None else str(value))


def _default_tv_port(value: Any) -> int:
    from .tv.registry import default_port

    return default_port(_tv_platform(value))


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("HOUSE_AGENT_CONFIG", "config.toml"))
    dotenv = dotenv_values(config_path.with_name(".env"))

    def env(name: str, default: Any = None) -> Any:
        return os.getenv(name, dotenv.get(name, default))

    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)

    server = _section(data, "server")
    llm = _section(data, "llm")
    speech = _section(data, "speech")
    text_to_speech = _section(data, "text_to_speech")
    catalog = _section(data, "catalog")
    weather = _section(data, "weather")
    news = _section(data, "news")
    directions = _section(data, "directions")
    travel = _section(data, "travel")
    doordash = _section(data, "doordash")
    storage = _section(data, "storage")
    calendar = _section(data, "calendar")
    voice = _section(data, "voice")
    matter = _section(data, "matter")
    zigbee = _section(data, "zigbee")
    docker = _section(data, "docker")
    roku = _section(data, "roku")
    tv = _section(data, "tv")
    raw_input_aliases = roku.get("inputs", {})
    if not isinstance(raw_input_aliases, dict):
        raw_input_aliases = {}
    input_aliases = {
        str(alias): str(key)
        for alias, key in raw_input_aliases.items()
    }
    for alias, variable in {
        "xbox": "ROKU_INPUT_XBOX",
        "switch": "ROKU_INPUT_SWITCH",
        "tv": "ROKU_INPUT_TV",
        "live tv": "ROKU_INPUT_LIVE_TV",
    }.items():
        value = env(variable, input_aliases.get(alias))
        if value:
            input_aliases[alias] = str(value)
    # [[roku.devices]] predates other brands, so entries there are Roku
    # unless they say otherwise; [[tv.devices]] is the general form.
    raw_devices = [
        (item, "roku") for item in roku.get("devices", []) if isinstance(item, dict)
    ] + [
        (item, None) for item in tv.get("devices", []) if isinstance(item, dict)
    ]
    configured_devices = tuple(
        TVDeviceConfig(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            host=str(item["host"]),
            port=int(item.get("port", 0)) or _default_tv_port(item.get("platform", fallback)),
            platform=_tv_platform(item.get("platform", fallback)),
            connected_to=(
                str(item["connected_to"]) if item.get("connected_to") else None
            ),
            input=str(item["input"]) if item.get("input") else None,
            mac=str(item["mac"]) if item.get("mac") else None,
        )
        for item, fallback in raw_devices
        if "id" in item and "host" in item
    )
    roku_ip = env("ROKU_IP_ADDRESS")
    devices = (
        (
            TVDeviceConfig(
                id=str(env("ROKU_DEVICE_ID", "living-room")),
                name=str(env("ROKU_DEVICE_NAME", "Living Room TV")),
                host=str(roku_ip),
                port=int(env("ROKU_PORT", 8060)),
                platform="roku",
            ),
        )
        if roku_ip
        else configured_devices
    )

    return AppConfig(
        server=ServerConfig(
            public_host=str(env("PUBLIC_HOST", server.get("public_host", "friday.local"))),
            api_token=str(env("API_TOKEN", server.get("api_token", "")) or "").strip(),
            host=str(env("HOUSE_AGENT_HOST", server.get("host", "127.0.0.1"))),
            port=int(env("HOUSE_AGENT_PORT", server.get("port", 8787))),
            ssl_certfile=(
                Path(value)
                if (value := env("HOUSE_AGENT_SSL_CERTFILE", server.get("ssl_certfile")))
                else None
            ),
            ssl_keyfile=(
                Path(value)
                if (value := env("HOUSE_AGENT_SSL_KEYFILE", server.get("ssl_keyfile")))
                else None
            ),
        ),
        llm=LLMConfig(
            base_url=str(env("LLM_BASE_URL", llm.get("base_url", DEFAULT_LLM_BASE_URL))),
            model=str(env("LLM_MODEL", llm.get("model", DEFAULT_LLM_MODEL))),
            api_key=str(env("LLM_API_KEY", llm.get("api_key", DEFAULT_LLM_API_KEY))),
            timeout_seconds=float(llm.get("timeout_seconds", 120)),
            max_tool_rounds=max(1, min(int(llm.get("max_tool_rounds", 6)), 12)),
            think_complex=_boolean(llm.get("think_complex", True)),
            keep_warm=_boolean(llm.get("keep_warm", True)),
            context_tokens=max(2048, int(llm.get("context_tokens", 16384))),
            route_tools=_boolean(llm.get("route_tools", True)),
            tool_budget_tokens=max(1000, int(llm.get("tool_budget_tokens", 6000))),
        ),
        speech=SpeechConfig(
            base_url=str(env("STT_BASE_URL", speech.get("base_url", DEFAULT_STT_BASE_URL))),
            model=str(env("STT_MODEL", speech.get("model", DEFAULT_STT_MODEL))),
            api_key=str(env("STT_API_KEY", speech.get("api_key", DEFAULT_STT_API_KEY))),
            language=str(speech.get("language", "en")),
            timeout_seconds=float(speech.get("timeout_seconds", 120)),
        ),
        text_to_speech=TextToSpeechConfig(
            base_url=str(
                env(
                    "TTS_BASE_URL",
                    text_to_speech.get("base_url", DEFAULT_TTS_BASE_URL),
                )
            ),
            model=str(env("TTS_MODEL", text_to_speech.get("model", DEFAULT_TTS_MODEL))),
            voice=str(env("TTS_VOICE", text_to_speech.get("voice", DEFAULT_TTS_VOICE))),
            spanish_voice=str(
                env(
                    "TTS_SPANISH_VOICE",
                    text_to_speech.get("spanish_voice", DEFAULT_TTS_SPANISH_VOICE),
                )
            ),
            api_key=str(
                env("TTS_API_KEY", text_to_speech.get("api_key", DEFAULT_TTS_API_KEY))
            ),
            response_format=str(
                env("TTS_RESPONSE_FORMAT", text_to_speech.get("response_format", "mp3"))
            ),
            speed=max(
                0.25,
                min(float(env("TTS_SPEED", text_to_speech.get("speed", 1.25))), 4.0),
            ),
            timeout_seconds=float(text_to_speech.get("timeout_seconds", 120)),
        ),
        catalog=CatalogConfig(
            region=str(env("CATALOG_REGION", catalog.get("region", "US"))).upper(),
            tmdb_token=env(
                "TMDB_READ_ACCESS_TOKEN",
                env("TMDB_API_TOKEN", catalog.get("tmdb_token")),
            ),
            watchmode_api_key=env(
                "WATCHMODE_API_KEY", catalog.get("watchmode_api_key")
            ),
            exact_episode_playback_enabled=_boolean(
                env(
                    "EXACT_EPISODE_PLAYBACK_ENABLED",
                    catalog.get("exact_episode_playback_enabled", False),
                )
            ),
            timeout_seconds=float(catalog.get("timeout_seconds", 15)),
        ),
        weather=WeatherConfig(
            latitude=_optional_float(env("HOUSE_LATITUDE", weather.get("latitude"))),
            longitude=_optional_float(env("HOUSE_LONGITUDE", weather.get("longitude"))),
            place=str(env("WEATHER_PLACE", weather.get("place", "home"))),
            units=(
                "metric"
                if str(env("WEATHER_UNITS", weather.get("units", "imperial"))).strip().lower()
                == "metric"
                else "imperial"
            ),
            alerts_enabled=_boolean(
                env("WEATHER_ALERTS_ENABLED", weather.get("alerts_enabled", True))
            ),
            timeout_seconds=float(weather.get("timeout_seconds", 15)),
        ),
        news=NewsConfig(
            enabled=_boolean(env("NEWS_ENABLED", news.get("enabled", True))),
            feeds=_news_feeds(env("NEWS_FEEDS"), news.get("feeds")),
            max_items=max(1, min(int(news.get("max_items", 12)), 50)),
            per_feed=max(1, min(int(news.get("per_feed", 8)), 50)),
            timeout_seconds=float(news.get("timeout_seconds", 12)),
        ),
        directions=DirectionsConfig(
            units=(
                "metric"
                if str(env("WEATHER_UNITS", directions.get("units", "imperial"))).strip().lower()
                == "metric"
                else "imperial"
            ),
            tomtom_key=env("TOMTOM_API_KEY", directions.get("tomtom_key")) or None,
            openrouteservice_key=env(
                "OPENROUTESERVICE_API_KEY", directions.get("openrouteservice_key")
            )
            or None,
            timeout_seconds=float(directions.get("timeout_seconds", 20)),
        ),
        travel=TravelConfig(
            letsfg_token=env("LETSFG_BEARER_TOKEN", travel.get("letsfg_token")) or None,
            currency=str(env("TRAVEL_CURRENCY", travel.get("currency", "USD"))).strip().upper()
            or "USD",
            units=(
                "metric"
                if str(env("WEATHER_UNITS", travel.get("units", "imperial"))).strip().lower()
                == "metric"
                else "imperial"
            ),
            timeout_seconds=float(travel.get("timeout_seconds", 150)),
        ),
        doordash=DoorDashConfig(
            enabled=_boolean(env("DOORDASH_ENABLED", doordash.get("enabled", True))),
            binary=(
                Path(str(env("DD_CLI_BINARY", doordash.get("binary")))).expanduser()
                if env("DD_CLI_BINARY", doordash.get("binary"))
                else None
            ),
            access_token=env("DD_CLI_ACCESS_TOKEN", doordash.get("access_token")) or None,
            timeout_seconds=float(doordash.get("timeout_seconds", 90)),
            announce_updates=_boolean(doordash.get("announce_updates", True)),
        ),
        storage=StorageConfig(
            database_path=Path(storage.get("database_path", "data/house-agent.db"))
        ),
        calendar=CalendarConfig(
            subscriptions=_calendar_urls(env("CALENDAR_ICS_URLS"), calendar.get("subscriptions")),
            refresh_minutes=max(1.0, float(calendar.get("refresh_minutes", 15))),
            horizon_days=max(1, min(int(calendar.get("horizon_days", 60)), 366)),
            weather_heads_up=_boolean(calendar.get("weather_heads_up", True)),
            timeout_seconds=float(calendar.get("timeout_seconds", 20)),
        ),
        voice=VoiceConfig(
            enabled=_boolean(env("VOICE_ID_ENABLED", voice.get("enabled", True))),
            model_path=Path(
                env(
                    "VOICE_ID_MODEL_PATH",
                    voice.get("model_path", DEFAULT_VOICE_MODEL_PATH),
                )
            ),
            threshold=max(
                0.2, min(float(env("VOICE_ID_THRESHOLD", voice.get("threshold", 0.62))), 0.95)
            ),
            margin=max(0.0, min(float(voice.get("margin", 0.08)), 0.5)),
            min_speech_seconds=float(voice.get("min_speech_seconds", 2.5)),
            max_wait_seconds=float(voice.get("max_wait_seconds", 12.0)),
            adapt=_boolean(voice.get("adapt", False)),
            threads=max(1, int(voice.get("threads", 2))),
        ),
        matter=MatterConfig(
            enabled=_boolean(env("MATTER_ENABLED", matter.get("enabled", True))),
            url=str(env("MATTER_WS_URL", matter.get("url", DEFAULT_MATTER_WS_URL))),
            wifi_ssid=env("HOUSE_WIFI_SSID", matter.get("wifi_ssid")),
            wifi_password=env("HOUSE_WIFI_PASSWORD", matter.get("wifi_password")),
            bluetooth=_boolean(env("MATTER_BLUETOOTH", matter.get("bluetooth", False))),
            bluetooth_adapter=int(
                env("MATTER_BLUETOOTH_ADAPTER", matter.get("bluetooth_adapter", 0))
            ),
            timeout_seconds=float(matter.get("timeout_seconds", 15)),
            manage=_boolean(env("MATTER_MANAGE", matter.get("manage", True))),
            launcher=Path(matter.get("launcher", DEFAULT_MATTER_LAUNCHER)),
        ),
        zigbee=ZigbeeConfig(
            enabled=_boolean(env("ZIGBEE_ENABLED", zigbee.get("enabled", False))),
            mqtt_host=str(env("MQTT_HOST", zigbee.get("mqtt_host", "127.0.0.1"))),
            mqtt_port=int(env("MQTT_PORT", zigbee.get("mqtt_port", 1883))),
            mqtt_username=env("MQTT_USERNAME", zigbee.get("mqtt_username")),
            mqtt_password=env("MQTT_PASSWORD", zigbee.get("mqtt_password")),
            base_topic=str(zigbee.get("base_topic", "zigbee2mqtt")),
        ),
        docker=DockerConfig(
            manage=_boolean(env("DOCKER_MANAGE", docker.get("manage", False))),
            compose_files=_compose_files(docker.get("compose_files"), config_path),
            command=tuple(docker.get("command", ("docker", "compose"))) or ("docker", "compose"),
            start_timeout_seconds=float(docker.get("start_timeout_seconds", 180)),
            stop_timeout_seconds=float(docker.get("stop_timeout_seconds", 30)),
        ),
        tv_devices=devices,
        roku_input_aliases=tuple(sorted(input_aliases.items())),
        household_profile=str(env("HOUSEHOLD_PROFILE", "default")),
        config_path=config_path,
    )
