"""Samsung Tizen (2016 and later). A WebSocket on 8002; the TV shows an
allow prompt the first time and returns a token we keep from then on."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from .base import (
    APPS,
    WAKE_ON_LAN,
    DEEP_LINK,
    MEDIA_STATUS,
    TEXT_INPUT,
    TVApp,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)

_KEYS = {
    "home": "KEY_HOME",
    "back": "KEY_RETURN",
    "exit": "KEY_EXIT",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "select": "KEY_ENTER",
    "enter": "KEY_ENTER",
    "play": "KEY_PLAY",
    "pause": "KEY_PAUSE",
    "play_pause": "KEY_PLAY_PAUSE",
    "stop": "KEY_STOP",
    "forward": "KEY_FF",
    "rewind": "KEY_REWIND",
    "volume_up": "KEY_VOLUP",
    "volume_down": "KEY_VOLDOWN",
    "mute": "KEY_MUTE",
    # The TV is unreachable over the network once it is fully off, so this
    # sleeps it; waking a Samsung needs Wake-on-LAN, not the WebSocket.
    "power_off": "KEY_POWER",
    "channel_up": "KEY_CHUP",
    "channel_down": "KEY_CHDOWN",
    "info": "KEY_INFO",
    "menu": "KEY_MENU",
    "guide": "KEY_GUIDE",
}


class SamsungTV(TVClient):
    platform = "samsung"
    capabilities = frozenset({APPS, DEEP_LINK, TEXT_INPUT, WAKE_ON_LAN})
    default_port = 8002

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        from samsungtvws.async_remote import SamsungTVWSAsyncRemote

        remote = SamsungTVWSAsyncRemote(
            host=self.device.host,
            port=self.device.port or self.default_port,
            token=self.credentials.get("token"),
            name="Friday",
            timeout=8,
        )
        try:
            await remote.start_listening()
        except Exception as exc:
            raise TVError(
                f"Could not reach {self.name} at {self.device.host}. Check it is "
                "awake and on the same LAN; the first connection needs the Allow "
                "prompt accepted on the TV."
            ) from exc
        try:
            yield remote
        except (TVError, ValueError):
            raise
        except Exception as exc:
            raise TVError(f"{self.name} rejected the command: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await remote.close()

    async def status(self) -> dict[str, Any]:
        """The remote socket says nothing about the TV itself, but Tizen's
        REST endpoint on 8001 reports the model, name and power state."""
        from samsungtvws.async_rest import SamsungTVAsyncRest

        rest = SamsungTVAsyncRest(host=self.device.host, port=8001, timeout=8)
        try:
            info = await rest.rest_device_info()
        except Exception as exc:
            raise TVError(
                f"Could not reach {self.name} at {self.device.host}. Check it is "
                "awake and on the same LAN."
            ) from exc
        device = (info or {}).get("device") or {}
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": str(device.get("PowerState", "on")).lower(),
            "model": device.get("modelName") or device.get("model"),
            "screen_name": device.get("name"),
            "active_app": None,
            "capabilities": sorted(self.capabilities),
            "note": "Samsung does not report the foreground app or volume level.",
        }

    async def press(self, key: str) -> None:
        from samsungtvws.remote import SendRemoteKey

        canonical = normalize_key(key)
        native = _KEYS.get(canonical)
        if native is None:
            raise TVUnsupported(
                f"The Samsung remote has no {canonical.replace('_', ' ')} key."
            )
        async with self._connect() as remote:
            await remote.send_command(SendRemoteKey.click(native))
            self._remember(remote)

    async def apps(self) -> list[TVApp]:
        async with self._connect() as remote:
            listing = await remote.app_list()
            self._remember(remote)
        if not listing:
            raise TVError(f"{self.name} did not return an app list; not all Tizen models do.")
        return [
            TVApp(id=str(item.get("appId", "")), name=str(item.get("name") or item.get("appId", "")))
            for item in listing
            if item.get("appId")
        ]

    async def launch_app(self, name_or_id: str) -> TVApp:
        from samsungtvws.remote import ChannelEmitCommand

        app = await self.find_app(name_or_id)
        async with self._connect() as remote:
            await remote.send_command(ChannelEmitCommand.launch_app(app.id, "NATIVE_LAUNCH"))
            self._remember(remote)
        return app

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        from samsungtvws.remote import ChannelEmitCommand

        target = await self.find_app(app)
        async with self._connect() as remote:
            await remote.send_command(
                ChannelEmitCommand.launch_app(target.id, "DEEP_LINK", content_id)
            )
            self._remember(remote)

    async def type_text(self, text: str) -> None:
        from samsungtvws.remote import SendInputString

        if not text or len(text) > 256:
            raise ValueError("Text must contain between 1 and 256 characters")
        async with self._connect() as remote:
            await remote.send_command(SendInputString.send(text))
            self._remember(remote)

    def _remember(self, remote: Any) -> None:
        """Tizen hands back a fresh token on the first accepted connection."""
        token = getattr(remote, "token", None)
        if token and token != self.credentials.get("token"):
            self.credentials["token"] = token

    async def begin_pairing(self) -> dict[str, Any]:
        return {
            "instruction": (
                f"Connecting to {self.name}. Accept the Allow prompt on the TV "
                "with its own remote, then say done."
            ),
            "needs_code": False,
        }

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        async with self._connect() as remote:
            self._remember(remote)
        token = self.credentials.get("token")
        if not token:
            raise TVError(
                f"{self.name} did not return a token. Accept the Allow prompt on "
                "the TV and try again."
            )
        return {"token": token}
