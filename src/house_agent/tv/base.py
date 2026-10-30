"""One TV vocabulary for every brand.

The model never learns what brand a TV is. It says "press home" or "launch
Netflix" against a device id, and the adapter for that platform translates
into whatever that platform actually speaks — ECP for Roku, a WebSocket
button name for LG, a protobuf keycode for Android TV. Anything a platform
genuinely cannot do raises TVUnsupported, so the agent can say so plainly
instead of silently doing nothing."""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Mapping

# What a platform can do beyond pressing keys. Reported to the model with
# every status so it never offers a capability the TV in the room lacks.
APPS = "apps"  # list and launch installed apps by name
DEEP_LINK = "deep_link"  # jump straight to a title inside an app
TEXT_INPUT = "text_input"  # type into a focused search field
INPUTS = "inputs"  # switch to HDMI/tuner
POWER_ON = "power_on"  # wake from off, not just sleep
MEDIA_STATUS = "media_status"  # report what is playing
ACCOUNTS = "accounts"  # list and switch the TV's own on-screen users
VOLUME = "volume"  # read the level as a percent, and set it outright
WAKE_ON_LAN = "wake_on_lan"  # unreachable when off, so waking needs a magic packet

CAPABILITIES = frozenset(
    {
        APPS,
        DEEP_LINK,
        TEXT_INPUT,
        INPUTS,
        POWER_ON,
        MEDIA_STATUS,
        ACCOUNTS,
        VOLUME,
        WAKE_ON_LAN,
    }
)

# The only key words the model is ever asked to produce. Every adapter maps
# these onto its own remote; a platform missing one raises TVUnsupported.
CANONICAL_KEYS = (
    "home",
    "back",
    "exit",
    "up",
    "down",
    "left",
    "right",
    "select",
    "enter",
    "play",
    "pause",
    "play_pause",
    "stop",
    "forward",
    "rewind",
    "volume_up",
    "volume_down",
    "mute",
    "power_on",
    "power_off",
    "channel_up",
    "channel_down",
    "info",
    "menu",
    "guide",
    "search",
)

# Words people and models actually say, folded onto the canonical set.
KEY_ALIASES = {
    "ok": "select",
    "okay": "select",
    "confirm": "select",
    "return": "back",
    "fwd": "forward",
    "fast_forward": "forward",
    "ff": "forward",
    "rev": "rewind",
    "reverse": "rewind",
    "vol_up": "volume_up",
    "vol_down": "volume_down",
    "volup": "volume_up",
    "voldown": "volume_down",
    "unmute": "mute",
    "poweron": "power_on",
    "poweroff": "power_off",
    "on": "power_on",
    "off": "power_off",
    "toggle_play": "play_pause",
    "playpause": "play_pause",
    "settings": "menu",
    "options": "menu",
}


class TVError(RuntimeError):
    """Anything that stopped a TV command from working."""


class TVUnsupported(TVError):
    """The platform has no equivalent for what was asked."""


class TVAuthRequired(TVError):
    """The TV needs pairing before it will accept commands."""


@dataclass(frozen=True, slots=True)
class TVApp:
    id: str
    name: str
    version: str | None = None


def normalize_key(key: str) -> str:
    """Fold whatever the model produced onto the canonical vocabulary."""
    cleaned = " ".join(str(key).strip().lower().replace("-", " ").split())
    compact = cleaned.replace(" ", "_")
    canonical = KEY_ALIASES.get(compact, compact)
    if canonical not in CANONICAL_KEYS:
        raise ValueError(
            f"Unsupported key '{key}'. Known keys: {', '.join(CANONICAL_KEYS)}"
        )
    return canonical


def validate_local_host(host: str) -> None:
    """A TV lives on the LAN. Refusing anything routable keeps a bad config
    or a confused model from firing commands at the open internet."""
    if not host or "/" in host or "://" in host or "@" in host:
        raise ValueError("TV host must be an IP address or a local hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not host.endswith(".local") and "." in host:
            raise ValueError("TV hostname must be local")
        return
    if not (address.is_private or address.is_link_local or address.is_loopback):
        raise ValueError("TV address must be on a private network")


class TVClient(ABC):
    """One TV, one connection's worth of work.

    Adapters are built per command rather than held open: the WebSocket
    platforms reconnect in well under a second, and a house assistant that
    talks to a TV every few minutes should not hold sockets against a
    device that sleeps."""

    platform: ClassVar[str] = "unknown"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    default_port: ClassVar[int] = 0

    def __init__(
        self,
        device: Any,
        credentials: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        validate_local_host(device.host)
        self.device = device
        self.credentials = dict(credentials or {})
        # Per-platform extras the factory passes through, such as the
        # household's own HDMI input names.
        self.options = dict(options or {})

    @property
    def name(self) -> str:
        return getattr(self.device, "name", None) or self.device.id

    def _require(self, capability: str, what: str) -> None:
        if capability not in self.capabilities:
            raise TVUnsupported(
                f"{self.name} is a {self.platform} TV, which cannot {what}."
            )

    def describe(self) -> dict[str, Any]:
        return {
            **asdict(self.device),
            "capabilities": sorted(self.capabilities),
        }

    # --- every platform supports these ---

    @abstractmethod
    async def status(self) -> dict[str, Any]:
        """Power state, model, and what app is in the foreground."""

    @abstractmethod
    async def press(self, key: str) -> None:
        """Press one canonical key."""

    # --- capability-gated; the defaults refuse cleanly ---

    async def apps(self) -> list[TVApp]:
        self._require(APPS, "list its installed apps")
        raise TVUnsupported(f"{self.name} cannot list apps.")

    async def launch_app(self, name_or_id: str) -> TVApp:
        self._require(APPS, "launch apps by name")
        raise TVUnsupported(f"{self.name} cannot launch apps.")

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        self._require(DEEP_LINK, "jump straight to a title")
        raise TVUnsupported(f"{self.name} cannot deep-link into apps.")

    async def type_text(self, text: str) -> None:
        self._require(TEXT_INPUT, "type text")
        raise TVUnsupported(f"{self.name} cannot accept typed text.")

    async def switch_input(self, target: str) -> None:
        self._require(INPUTS, "switch video inputs")
        raise TVUnsupported(f"{self.name} cannot switch inputs.")

    async def volume(self) -> dict[str, Any]:
        """Level as a percent plus mute state, so every brand answers "how
        loud is it" in the same units."""
        self._require(VOLUME, "report its volume level")
        raise TVUnsupported(f"{self.name} cannot report its volume.")

    async def set_volume(self, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
        self._require(VOLUME, "be set to a volume level")
        raise TVUnsupported(f"{self.name} cannot be set to a volume level.")

    async def now_playing(self) -> dict[str, Any] | None:
        """What is on screen, for platforms that will say. None means
        nothing is playing; TVUnsupported means the platform never tells."""
        self._require(MEDIA_STATUS, "report what is playing")
        raise TVUnsupported(f"{self.name} cannot report what is playing.")

    async def accounts(self) -> list[dict[str, str]]:
        self._require(ACCOUNTS, "list its on-screen users")
        raise TVUnsupported(f"{self.name} cannot list users.")

    async def switch_account(self, name_or_id: str) -> dict[str, str]:
        self._require(ACCOUNTS, "switch between on-screen users")
        raise TVUnsupported(f"{self.name} cannot switch users.")

    async def find_app(self, name_or_id: str) -> TVApp:
        """Shared name matching so every platform resolves 'netflix',
        'Netflix', and a raw id the same way."""
        installed = await self.apps()
        needle = name_or_id.casefold().strip()
        for app in installed:
            if app.id == name_or_id or app.name.casefold() == needle:
                return app
        partial = [app for app in installed if needle in app.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            names = ", ".join(app.name for app in partial[:5])
            raise TVError(f"App name is ambiguous; matches: {names}")
        # The installed list is in hand at this exact moment and used to be
        # thrown away with the error, leaving the model to guess or to ask.
        known = ", ".join(app.name for app in installed[:12]) or "none"
        raise TVError(
            f"{name_or_id!r} is not installed on {self.name}. These are: {known}. "
            "Open the closest one if the user clearly meant it, or say which apps are there."
        )

    # --- pairing; only platforms with PAIRING in their flow implement these ---

    async def begin_pairing(self) -> dict[str, Any]:
        """Ask the TV to show a code. Returns guidance for the user."""
        raise TVUnsupported(f"{self.name} does not need pairing.")

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        """Finish pairing; returns credentials for the caller to persist."""
        raise TVUnsupported(f"{self.name} does not need pairing.")
