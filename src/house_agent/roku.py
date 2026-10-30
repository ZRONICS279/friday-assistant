from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import httpx

from .config import RokuDeviceConfig


ROKU_KEYS = frozenset(
    {
        "Back",
        "Backspace",
        "ChannelDown",
        "ChannelUp",
        "Down",
        "Enter",
        "FindRemote",
        "Fwd",
        "Home",
        "Info",
        "InputAV1",
        "InputHDMI1",
        "InputHDMI2",
        "InputHDMI3",
        "InputHDMI4",
        "InputTuner",
        "Left",
        "Mute",
        "Play",
        "Power",
        "PowerOff",
        "PowerOn",
        "Replay",
        "Rev",
        "Right",
        "Search",
        "Select",
        "Up",
        "VolumeDown",
        "VolumeUp",
    }
)
KEY_ALIASES = {key.lower(): key for key in ROKU_KEYS} | {
    "forward": "Fwd",
    "reverse": "Rev",
    "ok": "Select",
    "volume_down": "VolumeDown",
    "volume_up": "VolumeUp",
    "power_on": "PowerOn",
    "power_off": "PowerOff",
}
DEFAULT_INPUT_ALIASES = {
    "antenna": "InputTuner",
    "home": "Home",
    "home screen": "Home",
    "normal tv": "Home",
    "roku": "Home",
    "roku home": "Home",
    "tuner": "InputTuner",
    "live tv": "InputTuner",
}


class RokuError(RuntimeError):
    pass


class RokuTimeout(RokuError):
    """The Roku took the request but did not answer in time. A launch often
    does this while the app cold-starts, and has usually worked anyway."""


# A launch is answered only once the app is up; Prime Video cold-starting
# took longer than the 5 seconds a key press gets, and the play that had
# actually started was reported as "could not reach the TV".
LAUNCH_TIMEOUT = httpx.Timeout(25.0, connect=5.0)


@dataclass(frozen=True, slots=True)
class RokuApp:
    id: str
    name: str
    version: str | None = None


def _validate_local_host(host: str) -> None:
    if "/" in host or "://" in host or "@" in host:
        raise ValueError("Roku host must be an IP address or local hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not host.endswith(".local") and "." in host:
            raise ValueError("Roku hostname must be local") from None
        return
    if not (address.is_private or address.is_link_local or address.is_loopback):
        raise ValueError("Roku address must be on a private network")


def normalize_key(key: str) -> str:
    normalized = KEY_ALIASES.get(key.strip().lower())
    if normalized is None:
        raise ValueError(f"Unsupported Roku key: {key}")
    return normalized


def resolve_input_key(target: str, aliases: Mapping[str, str] | None = None) -> str:
    normalized = " ".join(target.strip().lower().replace("_", " ").split())
    available = dict(DEFAULT_INPUT_ALIASES)
    available.update(
        {
            " ".join(alias.strip().lower().replace("_", " ").split()): key
            for alias, key in (aliases or {}).items()
        }
    )
    if "switch" in available:
        available["nintendo switch"] = available["switch"]
    if normalized in available:
        return normalize_key(available[normalized])
    compact = normalized.replace(" ", "")
    if compact.startswith("inputhdmi"):
        return normalize_key(compact)
    if compact.startswith("hdmi"):
        return normalize_key(f"Input{compact}")
    choices = ", ".join(sorted(available))
    raise ValueError(f"Unknown Roku input '{target}'. Configured names: {choices}")


class RokuClient:
    def __init__(
        self,
        device: RokuDeviceConfig,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        _validate_local_host(device.host)
        if not 1 <= device.port <= 65535:
            raise ValueError("Roku port is invalid")
        self.device = device
        self._client = client
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        host = f"[{self.device.host}]" if ":" in self.device.host else self.device.host
        return f"http://{host}:{self.device.port}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            if response.status_code == 403:
                raise RokuError(
                    f"{self.device.name} rejected local control. On the Roku, open Settings > "
                    "System > Advanced system settings > Control by mobile apps and set Network "
                    "access to Enabled."
                )
            response.raise_for_status()
            return response
        except RokuError:
            raise
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise RokuTimeout(
                f"{self.device.name} took too long to answer (it may be busy starting an app)."
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RokuError(
                f"Could not reach {self.device.name} at {self.base_url}. Check its IP address, "
                "make sure both devices are on the same LAN, and confirm the TV is awake."
            ) from exc
        except httpx.HTTPError as exc:
            raise RokuError(f"{self.device.name} did not accept the Roku command: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def device_info(self) -> dict[str, str]:
        response = await self._request("GET", "/query/device-info")
        root = ET.fromstring(response.content)
        return {child.tag: child.text or "" for child in root}

    async def apps(self) -> list[RokuApp]:
        response = await self._request("GET", "/query/apps")
        root = ET.fromstring(response.content)
        return [
            RokuApp(id=node.attrib["id"], name=node.text or "", version=node.attrib.get("version"))
            for node in root.findall("app")
        ]

    async def app_icon(self, app_id: str) -> tuple[bytes, str]:
        """The channel's artwork as the Roku serves it (usually a PNG)."""
        response = await self._request("GET", f"/query/icon/{quote(app_id, safe='')}")
        return response.content, response.headers.get("content-type", "image/png")

    async def active_app(self) -> RokuApp | None:
        response = await self._request("GET", "/query/active-app")
        root = ET.fromstring(response.content)
        node = root.find("app")
        if node is None or node.attrib.get("id") is None:
            return None
        return RokuApp(id=node.attrib["id"], name=node.text or "", version=node.attrib.get("version"))

    async def media_player(self) -> dict[str, Any]:
        response = await self._request("GET", "/query/media-player")
        root = ET.fromstring(response.content)
        result: dict[str, Any] = dict(root.attrib)
        for child in root:
            value: dict[str, str] | str = dict(child.attrib)
            if child.text and child.text.strip():
                if value:
                    value["text"] = child.text.strip()
                else:
                    value = child.text.strip()
            result[child.tag] = value
        return result

    async def keypress(self, key: str) -> None:
        await self._request("POST", f"/keypress/{normalize_key(key)}", content=b"")

    async def type_text(self, text: str) -> None:
        if not text or len(text) > 256:
            raise ValueError("Roku text must contain between 1 and 256 characters")
        await self._request("POST", f"/keypress/Lit_{quote(text, safe='')}", content=b"")

    async def launch(self, app_id: str) -> None:
        if not app_id.isdigit():
            raise ValueError("Roku app ID must be numeric")
        await self._request("POST", f"/launch/{app_id}", content=b"", timeout=LAUNCH_TIMEOUT)

    async def deep_link(self, app_id: str, content_id: str, media_type: str) -> None:
        allowed_types = {"movie", "episode", "season", "series", "shortFormVideo", "special", "live"}
        if media_type not in allowed_types:
            raise ValueError(f"Unsupported deep-link media type: {media_type}")
        if not content_id or len(content_id) > 512:
            raise ValueError("Content ID is missing or too long")
        if not app_id.isdigit():
            raise ValueError("Roku app ID must be numeric")
        await self._request(
            "POST",
            f"/launch/{app_id}",
            params={
                "contentId": content_id,
                "mediaType": media_type,
                "source": "hs-search",
            },
            content=b"",
            timeout=LAUNCH_TIMEOUT,
        )

    async def find_app(self, name_or_id: str) -> RokuApp:
        applications = await self.apps()
        exact = [
            app
            for app in applications
            if app.id == name_or_id or app.name.casefold() == name_or_id.casefold()
        ]
        if exact:
            return exact[0]
        partial = [app for app in applications if name_or_id.casefold() in app.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            names = ", ".join(app.name for app in partial[:5])
            raise RokuError(f"App name is ambiguous; matches: {names}")
        # The installed list is in hand right here; throwing it away with the
        # error left the model with nothing to offer instead.
        known = ", ".join(app.name for app in applications[:12]) or "none"
        raise RokuError(
            f"{name_or_id!r} is not installed on {self.device.name}. These are: {known}. "
            "Open the closest one if the user clearly meant it, or say which apps are there."
        )

    async def status(self) -> dict[str, Any]:
        info, active = await asyncio.gather(self.device_info(), self.active_app())
        return {
            "device": asdict(self.device),
            "power_mode": info.get("power-mode", "unknown"),
            "model": info.get("model-name") or info.get("friendly-device-name"),
            "active_app": asdict(active) if active else None,
        }


def _parse_ssdp_response(payload: bytes) -> dict[str, str]:
    lines = payload.decode("utf-8", errors="replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def discover_roku_devices(timeout: float = 1.5) -> list[dict[str, Any]]:
    timeout = max(0.2, min(timeout, 5.0))
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: roku:ecp\r\n\r\n"
    ).encode()
    found: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.settimeout(timeout)
            sock.sendto(message, ("239.255.255.250", 1900))
            while time.monotonic() < deadline:
                try:
                    payload, address = sock.recvfrom(65535)
                except TimeoutError:
                    break
                headers = _parse_ssdp_response(payload)
                location = headers.get("location", "")
                parsed = urlparse(location)
                host = parsed.hostname or address[0]
                if not host:
                    continue
                try:
                    _validate_local_host(host)
                except ValueError:
                    continue
                found[host] = {
                    "host": host,
                    "port": parsed.port or 8060,
                    "location": location,
                    "server": headers.get("server"),
                    "usn": headers.get("usn"),
                }
    except OSError as exc:
        raise RokuError(
            "Roku discovery could not access SSDP multicast; add the TV's LAN IP to config.toml"
        ) from exc
    return list(found.values())
