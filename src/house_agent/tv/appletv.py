"""Apple TV. Pairs over the Companion protocol with a PIN shown on screen;
the credentials it returns are what every later connection uses."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator

from .base import (
    ACCOUNTS,
    APPS,
    VOLUME,
    MEDIA_STATUS,
    POWER_ON,
    TEXT_INPUT,
    TVApp,
    TVAuthRequired,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)

# Canonical key -> RemoteControl coroutine name.
_KEYS = {
    "home": "home",
    "back": "menu",
    "exit": "top_menu",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "select": "select",
    "enter": "select",
    "play": "play",
    "pause": "pause",
    "play_pause": "play_pause",
    "stop": "stop",
    "forward": "skip_forward",
    "rewind": "skip_backward",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "channel_up": "channel_up",
    "channel_down": "channel_down",
    "menu": "menu",
    "guide": "guide",
}


class AppleTV(TVClient):
    platform = "appletv"
    capabilities = frozenset(
        {APPS, TEXT_INPUT, POWER_ON, MEDIA_STATUS, ACCOUNTS, VOLUME}
    )
    default_port = 49152  # Companion; the real port comes from discovery

    async def _config(self) -> Any:
        import pyatv

        loop = asyncio.get_running_loop()
        found = await pyatv.scan(loop, hosts=[self.device.host], timeout=5)
        if not found:
            raise TVError(
                f"Could not find {self.name} at {self.device.host}. Check it is "
                "awake and on the same LAN."
            )
        return found[0]

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        import pyatv
        from pyatv.const import Protocol

        credentials = self.credentials.get("companion")
        if not credentials:
            raise TVAuthRequired(
                f"{self.name} is not paired yet. Pair it first; the Apple TV will "
                "show a four-digit PIN to read back."
            )
        config = await self._config()
        config.set_credentials(Protocol.Companion, credentials)
        try:
            atv = await pyatv.connect(config, asyncio.get_running_loop())
        except Exception as exc:
            raise TVError(f"Could not connect to {self.name}: {exc}") from exc
        try:
            yield atv
        except (TVError, ValueError):
            raise
        except Exception as exc:
            raise TVError(f"{self.name} rejected the command: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                atv.close()

    async def status(self) -> dict[str, Any]:
        """Unlike every other platform here, an Apple TV will say what is
        actually playing — title, series, episode and position — so the agent
        can answer "what are we watching" without asking anyone."""
        async with self._connect() as atv:
            power = atv.power.power_state
            app = atv.metadata.app
            info = atv.device_info
            try:
                playing = await atv.metadata.playing()
            except Exception:  # nothing playing, or the app reports nothing
                playing = None
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": str(getattr(power, "name", power)).lower(),
            "model": str(getattr(info, "model", "")) or None,
            "active_app": (
                {"id": getattr(app, "identifier", ""), "name": getattr(app, "name", "")}
                if app
                else None
            ),
            "now_playing": _now_playing(playing),
            "capabilities": sorted(self.capabilities),
        }

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        if canonical in {"power_on", "power_off"}:
            async with self._connect() as atv:
                if canonical == "power_on":
                    await atv.power.turn_on()
                else:
                    await atv.power.turn_off()
            return
        method = _KEYS.get(canonical)
        if method is None:
            raise TVUnsupported(
                f"The Apple TV remote has no {canonical.replace('_', ' ')} key."
            )
        async with self._connect() as atv:
            await getattr(atv.remote_control, method)()

    async def apps(self) -> list[TVApp]:
        async with self._connect() as atv:
            listing = await atv.apps.app_list()
        return [TVApp(id=app.identifier, name=app.name) for app in listing]

    async def launch_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        async with self._connect() as atv:
            await atv.apps.launch_app(app.id)
        return app

    async def type_text(self, text: str) -> None:
        """Sets the contents of the focused field outright, so unlike the
        key-by-key platforms a retyped search replaces what was there."""
        if not text or len(text) > 256:
            raise ValueError("Text must contain between 1 and 256 characters")
        async with self._connect() as atv:
            await atv.keyboard.text_set(text)

    async def volume(self) -> dict[str, Any]:
        async with self._connect() as atv:
            level = atv.audio.volume
        return {"level": round(level), "muted": round(level) == 0}

    async def set_volume(self, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
        async with self._connect() as atv:
            if mute is not None:
                # tvOS has no mute flag over Companion; zero is the equivalent.
                await atv.audio.set_volume(0.0 if mute else 30.0)
            if level is not None:
                await atv.audio.set_volume(float(max(0, min(int(level), 100))))
            return {"level": round(atv.audio.volume)}

    async def now_playing(self) -> dict[str, Any] | None:
        async with self._connect() as atv:
            try:
                return _now_playing(await atv.metadata.playing())
            except Exception:
                return None

    async def accounts(self) -> list[dict[str, str]]:
        async with self._connect() as atv:
            listing = await atv.user_accounts.account_list()
        return [
            {"id": str(account.identifier), "name": str(account.name)}
            for account in listing
        ]

    async def switch_account(self, name_or_id: str) -> dict[str, str]:
        available = await self.accounts()
        needle = name_or_id.casefold().strip()
        for account in available:
            if account["id"] == name_or_id or account["name"].casefold() == needle:
                async with self._connect() as atv:
                    await atv.user_accounts.switch_account(account["id"])
                return account
        names = ", ".join(account["name"] for account in available) or "none"
        raise TVError(f"{self.name} has no user called '{name_or_id}'. Users: {names}")

    async def begin_pairing(self) -> dict[str, Any]:
        import pyatv
        from pyatv.const import Protocol

        config = await self._config()
        handler = await pyatv.pair(config, Protocol.Companion, asyncio.get_running_loop())
        await handler.begin()
        # The handler owns the open session, so the caller keeps it for the
        # second half of the exchange.
        self._pairing = handler
        return {
            "instruction": (
                f"{self.name} is showing a four-digit PIN. Read the PIN back to "
                "finish pairing."
            ),
            "needs_code": True,
        }

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        handler = getattr(self, "_pairing", None)
        if handler is None:
            raise TVError("Pairing was not started; begin pairing first to get a PIN.")
        if not code:
            raise TVError(f"{self.name} needs the PIN shown on its screen.")
        try:
            handler.pin(code.strip())
            await handler.finish()
            if not handler.has_paired:
                raise TVError(
                    f"{self.name} rejected that PIN. Start pairing again for a new one."
                )
            credentials = handler.service.credentials
        finally:
            with contextlib.suppress(Exception):
                await handler.close()
            self._pairing = None
        return {"companion": credentials}


def _now_playing(playing: Any) -> dict[str, Any] | None:
    """Only the fields worth speaking; pyatv returns many that are usually
    empty, and an empty key in a tool result invites the model to guess."""
    if playing is None:
        return None
    fields = {
        "title": getattr(playing, "title", None),
        "series": getattr(playing, "series_name", None),
        "season": getattr(playing, "season_number", None),
        "episode": getattr(playing, "episode_number", None),
        "artist": getattr(playing, "artist", None),
        "album": getattr(playing, "album", None),
        "state": str(getattr(getattr(playing, "device_state", None), "name", "")).lower() or None,
        "position_seconds": getattr(playing, "position", None),
        "total_seconds": getattr(playing, "total_time", None),
    }
    trimmed = {key: value for key, value in fields.items() if value not in (None, "")}
    return trimmed or None
