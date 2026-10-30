"""Vizio SmartCast (2016 and later). An HTTPS REST API on port 7345; the TV
shows a PIN once and returns an auth token we keep from then on."""

from __future__ import annotations

from typing import Any

from .base import (
    APPS,
    VOLUME,
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
    "select": "OK",
    "enter": "OK",
    "play": "PLAY",
    "pause": "PAUSE",
    "play_pause": "PLAY",
    "forward": "SEEK_FWD",
    "rewind": "SEEK_BACK",
    "volume_up": "VOL_UP",
    "volume_down": "VOL_DOWN",
    "mute": "MUTE_TOGGLE",
    "power_on": "POW_ON",
    "power_off": "POW_OFF",
    "channel_up": "CH_UP",
    "channel_down": "CH_DOWN",
    "info": "INFO",
    "menu": "MENU",
}


class VizioTV(TVClient):
    platform = "vizio"
    capabilities = frozenset({APPS, INPUTS, POWER_ON, VOLUME})
    default_port = 7345

    def _device_client(self, require_token: bool = True) -> Any:
        from pyvizio import VizioAsync

        token = self.credentials.get("auth_token")
        if require_token and not token:
            raise TVAuthRequired(
                f"{self.name} is not paired yet. Pair it first; the TV will show a "
                "PIN to read back."
            )
        port = self.device.port or self.default_port
        return VizioAsync(
            device_id="friday-house-agent",
            ip=f"{self.device.host}:{port}",
            name="Friday",
            auth_token=token or "",
            device_type="tv",
        )

    async def _call(self, coroutine: Any, what: str) -> Any:
        try:
            return await coroutine
        except Exception as exc:
            raise TVError(
                f"Could not {what} on {self.name} at {self.device.host}. Check it is "
                "awake and on the same LAN."
            ) from exc

    async def status(self) -> dict[str, Any]:
        client = self._device_client()
        powered = await self._call(client.get_power_state(), "read the power state")
        current = await self._call(client.get_current_app(), "read the current app")
        model = await self._call(client.get_model_name(), "read the model")
        source = await self._call(client.get_current_input(), "read the current input")
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": "on" if powered else "off",
            "model": model,
            "active_app": {"id": current, "name": current} if current else None,
            "input": source,
            "capabilities": sorted(self.capabilities),
        }

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        native = _KEYS.get(canonical)
        if native is None:
            raise TVUnsupported(
                f"The Vizio remote has no {canonical.replace('_', ' ')} key."
            )
        client = self._device_client()
        if not await self._call(client.remote(native), f"press {canonical}"):
            raise TVError(f"{self.name} did not accept the {canonical} key.")

    async def volume(self) -> dict[str, Any]:
        client = self._device_client()
        level = await self._call(client.get_current_volume(), "read the volume")
        muted = await self._call(client.is_muted(), "read the mute state")
        maximum = client.get_max_volume() or 100
        return {
            "level": round((level or 0) / maximum * 100),
            "muted": bool(muted),
            "raw_level": level,
            "raw_max": maximum,
        }

    async def set_volume(self, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
        client = self._device_client()
        if mute is not None:
            await self._call(
                client.mute_on() if mute else client.mute_off(), "change the mute state"
            )
        if level is not None:
            # SmartCast exposes volume as a numeric setting rather than a key.
            maximum = client.get_max_volume() or 100
            target = round(max(0, min(int(level), 100)) / 100 * maximum)
            await self._call(
                client.set_setting("audio", "volume", target), "set the volume"
            )
        return await self.volume()

    async def apps(self) -> list[TVApp]:
        from pyvizio import VizioAsync

        names = await self._call(VizioAsync.get_apps_list(), "list apps")
        return [TVApp(id=name, name=name) for name in names or []]

    async def launch_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        client = self._device_client()
        if not await self._call(client.launch_app(app.name), f"launch {app.name}"):
            raise TVError(f"{self.name} did not launch {app.name}.")
        return app

    async def switch_input(self, target: str) -> None:
        client = self._device_client()
        inputs = await self._call(client.get_inputs_list(), "list inputs") or []
        needle = target.strip().casefold().replace(" ", "")
        for item in inputs:
            labels = {
                str(getattr(item, "name", "")).casefold().replace(" ", ""),
                str(getattr(item, "meta_name", "")).casefold().replace(" ", ""),
            }
            if needle in labels:
                await self._call(client.set_input(item.name), f"switch to {target}")
                return
        names = ", ".join(str(getattr(i, "name", "")) for i in inputs) or "none reported"
        raise TVError(f"{self.name} has no input '{target}'. Available: {names}")

    async def begin_pairing(self) -> dict[str, Any]:
        client = self._device_client(require_token=False)
        challenge = await self._call(client.start_pair(), "start pairing")
        if challenge is None:
            raise TVError(f"{self.name} did not start pairing. Check it is awake.")
        return {
            "instruction": (
                f"{self.name} is showing a PIN on screen. Read the PIN back to "
                "finish pairing."
            ),
            "needs_code": True,
            # Carried into complete_pairing; the challenge is single-use.
            "challenge": {"ch_type": challenge.ch_type, "token": challenge.token},
        }

    async def complete_pairing(self, code: str | None = None) -> dict[str, Any]:
        challenge = self.credentials.get("challenge") or {}
        if not challenge:
            raise TVError("Pairing was not started; begin pairing first to get a PIN.")
        if not code:
            raise TVError(f"{self.name} needs the PIN shown on its screen.")
        client = self._device_client(require_token=False)
        result = await self._call(
            client.pair(challenge["ch_type"], challenge["token"], code.strip()),
            "finish pairing",
        )
        if result is None or not getattr(result, "auth_token", None):
            raise TVError(f"{self.name} rejected that PIN. Start pairing again for a new one.")
        return {"auth_token": result.auth_token}
