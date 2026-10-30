"""Android TV and Google TV — Sony, Hisense, Philips, TCL's Google TV sets,
Nvidia Shield, Chromecast with Google TV.

Uses the same Remote protocol v2 the Google TV phone app speaks, so it needs
no ADB and no developer mode: only the Remote Service that ships on the
device. Pairing shows a six-digit code on the TV and leaves us a client
certificate on disk."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, AsyncIterator

from .base import (
    APPS,
    VOLUME,
    DEEP_LINK,
    MEDIA_STATUS,
    TEXT_INPUT,
    POWER_ON,
    TVApp,
    TVAuthRequired,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)

_KEYS = {
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "exit": "KEYCODE_HOME",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
    "enter": "KEYCODE_ENTER",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "stop": "KEYCODE_MEDIA_STOP",
    "forward": "KEYCODE_MEDIA_FAST_FORWARD",
    "rewind": "KEYCODE_MEDIA_REWIND",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
    "power_on": "KEYCODE_WAKEUP",
    "power_off": "KEYCODE_SLEEP",
    "channel_up": "KEYCODE_CHANNEL_UP",
    "channel_down": "KEYCODE_CHANNEL_DOWN",
    "info": "KEYCODE_INFO",
    "menu": "KEYCODE_MENU",
    "guide": "KEYCODE_GUIDE",
    "search": "KEYCODE_SEARCH",
}

# The remote protocol cannot enumerate installed apps, so launching works off
# the app's package id. These cover what people actually ask for by name; a
# raw package id or an https deep link is always accepted too.
_APP_IDS = {
    "netflix": "com.netflix.ninja",
    "youtube": "com.google.android.youtube.tv",
    "youtube tv": "com.google.android.youtube.tvunplugged",
    "disney+": "com.disney.disneyplus",
    "disney plus": "com.disney.disneyplus",
    "prime video": "com.amazon.amazonvideo.livingroom",
    "amazon prime video": "com.amazon.amazonvideo.livingroom",
    "hulu": "com.hulu.livingroomplus",
    "max": "com.wbd.stream",
    "hbo max": "com.wbd.stream",
    "peacock": "com.peacocktv.peacockandroid",
    "paramount+": "com.cbs.ott",
    "paramount plus": "com.cbs.ott",
    "apple tv": "com.apple.atve.androidtv.appletv",
    "spotify": "com.spotify.tv.android",
    "plex": "com.plexapp.android",
    "twitch": "tv.twitch.android.app",
}


class AndroidTV(TVClient):
    platform = "androidtv"
    capabilities = frozenset(
        {APPS, DEEP_LINK, TEXT_INPUT, POWER_ON, MEDIA_STATUS, VOLUME}
    )
    default_port = 6466

    def _cert_paths(self) -> tuple[str, str]:
        """One certificate per TV, kept beside the database."""
        directory = Path(self.options.get("cert_dir") or "data/tv-certs")
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in self.device.id if c.isalnum() or c in "-_") or "tv"
        return str(directory / f"{safe}.crt"), str(directory / f"{safe}.key")

    def _remote(self) -> Any:
        from androidtvremote2 import AndroidTVRemote

        certfile, keyfile = self._cert_paths()
        return AndroidTVRemote(
            client_name="Friday",
            certfile=certfile,
            keyfile=keyfile,
            host=self.device.host,
            api_port=self.device.port or self.default_port,
        )

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        from androidtvremote2 import CannotConnect, InvalidAuth

        if not self.credentials.get("paired"):
            raise TVAuthRequired(
                f"{self.name} is not paired yet. Pair it first; the TV will show a "
                "six-digit code to read back."
            )
        remote = self._remote()
        await remote.async_generate_cert_if_missing()
        try:
            await remote.async_connect()
        except InvalidAuth as exc:
            raise TVAuthRequired(
                f"{self.name} no longer trusts this pairing. Pair it again."
            ) from exc
        except CannotConnect as exc:
            raise TVError(
                f"Could not reach {self.name} at {self.device.host}. Check it is "
                "awake and on the same LAN."
            ) from exc
        try:
            yield remote
        except (TVError, ValueError):
            raise
        except Exception as exc:
            raise TVError(f"{self.name} rejected the command: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                remote.disconnect()

    async def status(self) -> dict[str, Any]:
        async with self._connect() as remote:
            info = remote.device_info or {}
            current = remote.current_app
            is_on = remote.is_on
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": "on" if is_on else "standby",
            "model": info.get("model"),
            "active_app": {"id": current, "name": current} if current else None,
            "capabilities": sorted(self.capabilities),
        }

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        native = _KEYS.get(canonical)
        if native is None:
            raise TVUnsupported(
                f"The Android TV remote has no {canonical.replace('_', ' ')} key."
            )
        async with self._connect() as remote:
            remote.send_key_command(native)

    async def volume(self) -> dict[str, Any]:
        async with self._connect() as remote:
            info = remote.volume_info or {}
        maximum = info.get("max") or 100
        return {
            "level": round((info.get("level") or 0) / maximum * 100),
            "muted": bool(info.get("muted")),
            "raw_level": info.get("level"),
            "raw_max": maximum,
        }

    async def set_volume(self, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
        # The remote protocol carries key codes only, with no absolute setter.
        raise TVUnsupported(
            f"{self.name} reports its volume but cannot be set to a level; use "
            "volume up or volume down instead."
        )

    async def now_playing(self) -> dict[str, Any] | None:
        async with self._connect() as remote:
            current = remote.current_app
        return {"app": current} if current else None

    async def mac_address(self) -> str | None:
        """Needed to wake the device once it is fully powered off."""
        remote = self._remote()
        await remote.async_generate_cert_if_missing()
        try:
            _, mac = await remote.async_get_name_and_mac()
        except Exception:
            return None
        return mac

    async def apps(self) -> list[TVApp]:
        # The protocol offers no listing; report the names we can launch so the
        # model has something honest to work from.
        return [TVApp(id=package, name=name) for name, package in sorted(_APP_IDS.items())]

    async def launch_app(self, name_or_id: str) -> TVApp:
        needle = name_or_id.strip().casefold()
        package = _APP_IDS.get(needle)
        if package is None:
            # A raw package id or an https deep link passes straight through.
            if "." in name_or_id or name_or_id.startswith("http"):
                package = name_or_id
            else:
                known = ", ".join(sorted(_APP_IDS))
                raise TVError(f"Unknown app '{name_or_id}' for Android TV. Known: {known}")
        async with self._connect() as remote:
            remote.send_launch_app_command(package)
        return TVApp(id=package, name=name_or_id)

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        # Android TV launches by link, so the content id must already be a URL.
        if not content_id.startswith("http"):
            raise TVUnsupported(
                "Android TV deep links need a full https URL for the title, not a "
                "provider-specific content id."
            )
        async with self._connect() as remote:
            remote.send_launch_app_command(content_id)

    async def type_text(self, text: str) -> None:
        if not text or len(text) > 256:
            raise ValueError("Text must contain between 1 and 256 characters")
        async with self._connect() as remote:
            remote.send_text(text)

    async def begin_pairing(self) -> dict[str, Any]:
        from androidtvremote2 import CannotConnect

        remote = self._remote()
        await remote.async_generate_cert_if_missing()
        try:
            await remote.async_start_pairing()
        except CannotConnect as exc:
            raise TVError(
                f"Could not reach {self.name} at {self.device.host} to start pairing."
            ) from exc
        # The pairing socket must stay open until the code comes back, so the
        # caller holds this client for the second half of the exchange.
        self._pairing = remote
        return {
            "instruction": (
                f"{self.name} is showing a six-digit code. Read the code back to "
                "finish pairing."
            ),
            "needs_code": True,
        }

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        from androidtvremote2 import InvalidAuth

        remote = getattr(self, "_pairing", None)
        if remote is None:
            raise TVError("Pairing was not started; begin pairing first to get a code.")
        if not code:
            raise TVError(f"{self.name} needs the six-digit code shown on its screen.")
        try:
            await remote.async_finish_pairing(code.strip())
        except InvalidAuth as exc:
            raise TVError(
                f"{self.name} rejected that code. Start pairing again for a new one."
            ) from exc
        finally:
            self._pairing = None
        return {"paired": True}
