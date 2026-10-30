"""LG webOS. A WebSocket on port 3000/3001; the TV shows an accept prompt
the first time and hands back a client key we keep forever after."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from .base import (
    APPS,
    VOLUME,
    WAKE_ON_LAN,
    DEEP_LINK,
    INPUTS,
    MEDIA_STATUS,
    POWER_ON,
    TVApp,
    TVAuthRequired,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)

_KEYS = {
    "home": "HOME",
    "back": "BACK",
    "exit": "EXIT",
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
    "select": "ENTER",
    "enter": "ENTER",
    "play": "PLAY",
    "pause": "PAUSE",
    "play_pause": "PLAY",
    "stop": "STOP",
    "forward": "FASTFORWARD",
    "rewind": "REWIND",
    "volume_up": "VOLUMEUP",
    "volume_down": "VOLUMEDOWN",
    "mute": "MUTE",
    "channel_up": "CHANNELUP",
    "channel_down": "CHANNELDOWN",
    "info": "INFO",
    "menu": "MENU",
    "guide": "GUIDE",
}


class WebOSTV(TVClient):
    platform = "webos"
    capabilities = frozenset(
        {APPS, DEEP_LINK, INPUTS, POWER_ON, MEDIA_STATUS, VOLUME, WAKE_ON_LAN}
    )
    default_port = 3000

    @contextlib.asynccontextmanager
    async def _connect(self, allow_pairing: bool = False) -> AsyncIterator[Any]:
        from aiowebostv import WebOsClient

        key = self.credentials.get("client_key")
        if not key and not allow_pairing:
            raise TVAuthRequired(
                f"{self.name} is not paired yet. Pair it first; the TV will show "
                "an accept prompt to confirm with its own remote."
            )
        client = WebOsClient(self.device.host, key)
        try:
            try:
                await client.connect()
            except Exception as exc:  # aiowebostv raises a family of its own errors
                raise TVError(
                    f"Could not reach {self.name} at {self.device.host}. Check it is "
                    "awake, on the same LAN, and has LG Connect Apps enabled."
                ) from exc
            yield client
        except (TVError, ValueError):
            raise
        except Exception as exc:
            raise TVError(f"{self.name} rejected the command: {exc}") from exc
        finally:
            # Covers a failed connect too: aiowebostv leaves a reconnect task
            # running otherwise.
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def status(self) -> dict[str, Any]:
        async with self._connect() as client:
            power = await client.get_power_state()
            current = await client.get_current_app()
            info = client.tv_info or {}
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": str((power or {}).get("state", "unknown")).lower(),
            "model": (info.get("system") or {}).get("modelName") if isinstance(info, dict) else None,
            "active_app": {"id": current, "name": current} if current else None,
            "capabilities": sorted(self.capabilities),
        }

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        async with self._connect() as client:
            if canonical == "power_off":
                await client.power_off()
                return
            if canonical == "power_on":
                # Only reachable if the TV is merely asleep; a fully off LG
                # needs the magic packet the tool layer sends first.
                await client.power_on()
                return
            native = _KEYS.get(canonical)
            if native is None:
                raise TVUnsupported(
                    f"The LG remote has no {canonical.replace('_', ' ')} key."
                )
            await client.button(native)

    async def volume(self) -> dict[str, Any]:
        async with self._connect() as client:
            level = await client.get_volume()
            muted = await client.get_muted()
        return {"level": level, "muted": bool(muted)}

    async def set_volume(self, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
        async with self._connect() as client:
            if mute is not None:
                await client.set_mute(bool(mute))
            if level is not None:
                await client.set_volume(max(0, min(int(level), 100)))
            return {"level": await client.get_volume(), "muted": bool(await client.get_muted())}

    async def now_playing(self) -> dict[str, Any] | None:
        """webOS names the app holding the media session but not the title."""
        async with self._connect() as client:
            foreground = await client.get_media_foreground_app() or []
        if not foreground:
            return None
        entry = foreground[0] or {}
        playing = {"app": entry.get("appId")} if entry.get("appId") else {}
        state = entry.get("playState") or entry.get("playbackStatus")
        if state:
            playing["state"] = str(state).lower()
        return playing or None

    async def close_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        async with self._connect() as client:
            await client.close_app(app.id)
        return app

    async def apps(self) -> list[TVApp]:
        async with self._connect() as client:
            launch_points = await client.get_apps() or []
        return [
            TVApp(id=str(point.get("id", "")), name=str(point.get("title") or point.get("id", "")))
            for point in launch_points
            if point.get("id")
        ]

    async def launch_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        async with self._connect() as client:
            await client.launch_app(app.id)
        return app

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        target = await self.find_app(app)
        async with self._connect() as client:
            await client.launch_app_with_content_id(target.id, content_id)

    async def switch_input(self, target: str) -> None:
        async with self._connect() as client:
            inputs = await client.get_inputs() or {}
            needle = target.strip().casefold().replace(" ", "")
            for input_id, details in inputs.items():
                label = str((details or {}).get("label") or input_id)
                if needle in (input_id.casefold(), label.casefold().replace(" ", "")):
                    await client.set_input(input_id)
                    return
            names = ", ".join(sorted(inputs)) or "none reported"
            raise TVError(f"{self.name} has no input '{target}'. Available: {names}")

    async def begin_pairing(self) -> dict[str, Any]:
        return {
            "instruction": (
                f"Connecting to {self.name}. Accept the pairing prompt on the TV "
                "with its own remote, then say done."
            ),
            "needs_code": False,
        }

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        async with self._connect(allow_pairing=True) as client:
            key = client.client_key
        if not key:
            raise TVError(f"{self.name} did not return a client key; accept the prompt and retry.")
        return {"client_key": key}
