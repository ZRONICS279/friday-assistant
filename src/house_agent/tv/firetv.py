"""Amazon Fire TV. Fire OS ships no Remote Service, so this drives the device
over ADB on port 5555.

That means one-time setup on the device itself: Settings > My Fire TV >
Developer Options > ADB debugging, on. The first connection then shows an
"Allow USB debugging" prompt to accept. No account and no key are involved."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any, AsyncIterator

from .base import (
    APPS,
    DEEP_LINK,
    MEDIA_STATUS,
    POWER_ON,
    TEXT_INPUT,
    TVApp,
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
    "menu": "KEYCODE_MENU",
    "search": "KEYCODE_SEARCH",
    "info": "KEYCODE_INFO",
}

_FOCUS = re.compile(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.]+)")


class FireTV(TVClient):
    platform = "firetv"
    capabilities = frozenset({APPS, DEEP_LINK, TEXT_INPUT, POWER_ON, MEDIA_STATUS})
    default_port = 5555

    def _signer(self) -> Any:
        """One ADB keypair for the house, generated on first use. The Fire TV
        remembers it after the on-screen prompt is accepted once."""
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner

        directory = Path(self.options.get("cert_dir") or "data/tv-certs")
        directory.mkdir(parents=True, exist_ok=True)
        private = directory / "adbkey"
        if not private.exists():
            keygen(str(private))
        return PythonRSASigner(
            private.with_suffix(".pub").read_text(), private.read_text()
        )

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        from adb_shell.adb_device_async import AdbDeviceTcpAsync

        device = AdbDeviceTcpAsync(
            self.device.host,
            self.device.port or self.default_port,
            default_transport_timeout_s=9.0,
        )
        try:
            await device.connect(rsa_keys=[self._signer()], auth_timeout_s=15.0)
        except Exception as exc:
            raise TVError(
                f"Could not reach {self.name} over ADB at {self.device.host}. Turn on "
                "Settings > My Fire TV > Developer Options > ADB debugging, then "
                "accept the prompt the first time."
            ) from exc
        try:
            yield device
        except (TVError, ValueError):
            raise
        except Exception as exc:
            raise TVError(f"{self.name} rejected the command: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await device.close()

    async def status(self) -> dict[str, Any]:
        async with self._connect() as device:
            focus = await device.shell("dumpsys window windows | grep -E 'mCurrentFocus'")
            awake = await device.shell("dumpsys power | grep -E 'mWakefulness='")
        match = _FOCUS.search(focus or "")
        return {
            "device": {"id": self.device.id, "name": self.name, "host": self.device.host},
            "platform": self.platform,
            "power_mode": "on" if "Awake" in (awake or "") else "standby",
            "model": None,
            "active_app": {"id": match.group(1), "name": match.group(1)} if match else None,
            "capabilities": sorted(self.capabilities),
        }

    async def press(self, key: str) -> None:
        canonical = normalize_key(key)
        native = _KEYS.get(canonical)
        if native is None:
            raise TVUnsupported(
                f"The Fire TV remote has no {canonical.replace('_', ' ')} key."
            )
        async with self._connect() as device:
            await device.shell(f"input keyevent {native}")

    async def apps(self) -> list[TVApp]:
        async with self._connect() as device:
            listing = await device.shell("pm list packages -3")
        packages = [
            line.removeprefix("package:").strip()
            for line in (listing or "").splitlines()
            if line.startswith("package:")
        ]
        # Fire OS exposes no human-readable label over ADB, so the package's
        # last segment is the closest thing to a name people would say.
        return [TVApp(id=package, name=package.rsplit(".", 1)[-1]) for package in sorted(packages)]

    async def launch_app(self, name_or_id: str) -> TVApp:
        app = await self.find_app(name_or_id)
        async with self._connect() as device:
            await device.shell(f"monkey -p {app.id} -c android.intent.category.LAUNCHER 1")
        return app

    async def now_playing(self) -> dict[str, Any] | None:
        """Fire OS keeps a media session per app; the active one names the
        package and whether it is playing or paused."""
        async with self._connect() as device:
            dump = await device.shell("dumpsys media_session | grep -E 'package=|state=PlaybackState'")
        package = re.search(r"package=([A-Za-z0-9_.]+)", dump or "")
        state = re.search(r"state=PlaybackState \{state=(\d)", dump or "")
        if not package:
            return None
        # Android's PlaybackState constants: 2 paused, 3 playing, 6 buffering.
        states = {"2": "paused", "3": "playing", "6": "buffering"}
        playing: dict[str, Any] = {"app": package.group(1)}
        if state and state.group(1) in states:
            playing["state"] = states[state.group(1)]
        return playing

    async def deep_link(self, app: str, content_id: str, media_type: str) -> None:
        if not content_id.startswith("http"):
            raise TVUnsupported(
                "Fire TV deep links need a full https URL for the title, not a "
                "provider-specific content id."
            )
        if not re.fullmatch(r"[A-Za-z0-9:/?&=._~%#+-]+", content_id):
            raise ValueError("That link contains characters the TV shell cannot accept")
        async with self._connect() as device:
            await device.shell(
                f"am start -a android.intent.action.VIEW -d '{content_id}'"
            )

    async def type_text(self, text: str) -> None:
        if not text or len(text) > 256:
            raise ValueError("Text must contain between 1 and 256 characters")
        # input text takes %s for spaces and chokes on shell metacharacters.
        if not re.fullmatch(r"[A-Za-z0-9 ._@-]+", text):
            raise ValueError("Fire TV text may only contain letters, digits, spaces, . _ @ -")
        async with self._connect() as device:
            await device.shell(f"input text {text.replace(' ', '%s')}")
