"""Roku (ECP over HTTP). Wraps the existing RokuClient so the behaviour the
household already relies on — input aliases, deep links, the 403 hint about
"Control by mobile apps" — carries over unchanged."""

from __future__ import annotations

from typing import Any

from ..roku import RokuClient, RokuError, resolve_input_key
from .base import (
    APPS,
    MEDIA_STATUS,
    DEEP_LINK,
    INPUTS,
    MEDIA_STATUS,
    POWER_ON,
    TEXT_INPUT,
    TVApp,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)

# Canonical key -> ECP key. Missing entries are genuinely absent from the
# Roku remote rather than an oversight.
_KEYS = {
    "home": "Home",
    "back": "Back",
    "exit": "Home",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "select": "Select",
    "enter": "Enter",
    "play": "Play",
    "pause": "Play",  # one key toggles on Roku
    "play_pause": "Play",
    "forward": "Fwd",
    "rewind": "Rev",
    "volume_up": "VolumeUp",
    "volume_down": "VolumeDown",
    "mute": "Mute",
    "power_on": "PowerOn",
    "power_off": "PowerOff",
    "channel_up": "ChannelUp",
    "channel_down": "ChannelDown",
    "info": "Info",
    "menu": "Info",  # the * button is Roku's options menu
    "search": "Search",
}


class RokuTV(TVClient):
    platform = "roku"
    capabilities = frozenset({APPS, DEEP_LINK, TEXT_INPUT, INPUTS, POWER_ON, MEDIA_STATUS})
    default_port = 8060

    def _client(self) -> RokuClient:
        # options carries an httpx client in tests; production builds its own.
        return RokuClient(self.device, client=self.options.get("http_client"))

    async def status(self) -> dict[str, Any]:
        try:
            state = await self._client().status()
        except RokuError as exc:
            raise TVError(str(exc)) from exc
        state["platform"] = self.platform
        state["capabilities"] = sorted(self.capabilities)
        return state

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        native = _KEYS.get(canonical)
        if native is None:
            raise TVUnsupported(f"The Roku remote has no {canonical.replace('_', ' ')} key.")
        try:
            await self._client().keypress(native)
        except RokuError as exc:
            raise TVError(str(exc)) from exc

    async def now_playing(self) -> dict[str, Any] | None:
        """Roku reports no title, but state, app and position are enough to
        answer "is it still playing" and "how far in are we"."""
        try:
            player = await self._client().media_player()
        except RokuError as exc:
            raise TVError(str(exc)) from exc
        state = str(player.get("state", "")).lower()
        if state in ("", "none", "close"):
            return None
        plugin = player.get("plugin")
        playing: dict[str, Any] = {"state": state}
        if isinstance(plugin, dict) and plugin.get("name"):
            playing["app"] = plugin["name"]
        for key, field in (("position", "position_seconds"), ("duration", "total_seconds")):
            raw = player.get(key)
            value = raw.get("text") if isinstance(raw, dict) else raw
            if isinstance(value, str) and value.endswith(" ms"):
                playing[field] = round(int(value[:-3]) / 1000)
        live = player.get("is_live")
        if isinstance(live, dict) and live.get("text") == "true":
            playing["live"] = True
        return playing

    async def apps(self) -> list[TVApp]:
        try:
            return [TVApp(id=a.id, name=a.name, version=a.version) for a in await self._client().apps()]
        except RokuError as exc:
            raise TVError(str(exc)) from exc

    async def launch_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        try:
            await self._client().launch(app.id)
        except RokuError as exc:
            raise TVError(str(exc)) from exc
        return app

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        target = await self.find_app(app)
        try:
            await self._client().deep_link(target.id, content_id, media_type)
        except RokuError as exc:
            raise TVError(str(exc)) from exc

    async def type_text(self, text: str) -> None:
        try:
            await self._client().type_text(text)
        except RokuError as exc:
            raise TVError(str(exc)) from exc

    async def switch_input(self, target: str) -> None:
        key = resolve_input_key(target, self.options.get("input_aliases"))
        try:
            await self._client().keypress(key)
        except RokuError as exc:
            raise TVError(str(exc)) from exc
