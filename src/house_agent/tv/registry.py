"""Platform lookup and network discovery for every supported TV brand."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from .androidtv import AndroidTV
from .appletv import AppleTV
from .base import TVClient, TVError
from .firetv import FireTV
from .roku import RokuTV
from .samsung import SamsungTV
from .vizio import VizioTV
from .webos import WebOSTV

PLATFORMS: dict[str, type[TVClient]] = {
    cls.platform: cls
    for cls in (RokuTV, WebOSTV, SamsungTV, VizioTV, AndroidTV, FireTV, AppleTV)
}

# What people call each platform, so config and setup accept the obvious word.
PLATFORM_ALIASES = {
    "lg": "webos",
    "web os": "webos",
    "tizen": "samsung",
    "smartcast": "vizio",
    "android": "androidtv",
    "android tv": "androidtv",
    "google": "androidtv",
    "google tv": "androidtv",
    "sony": "androidtv",
    "nvidia shield": "androidtv",
    "shield": "androidtv",
    "chromecast": "androidtv",
    "fire": "firetv",
    "fire tv": "firetv",
    "firestick": "firetv",
    "amazon": "firetv",
    "apple": "appletv",
    "apple tv": "appletv",
}

# mDNS service types each platform advertises, used for discovery.
_MDNS = {
    "_androidtvremote2._tcp.local.": "androidtv",
    "_samsungmsf._tcp.local.": "samsung",
    "_viziocast._tcp.local.": "vizio",
    "_lg-smart-device._tcp.local.": "webos",
    "_companion-link._tcp.local.": "appletv",
}


def normalize_platform(value: str | None) -> str:
    """Fold a brand word onto a platform id, defaulting to Roku so existing
    configs written before other brands were supported keep working."""
    if not value:
        return "roku"
    cleaned = " ".join(str(value).strip().lower().replace("-", " ").split())
    resolved = PLATFORM_ALIASES.get(cleaned, cleaned.replace(" ", ""))
    if resolved not in PLATFORMS:
        known = ", ".join(sorted(PLATFORMS))
        raise ValueError(f"Unknown TV platform '{value}'. Supported: {known}")
    return resolved


def client_for(
    device: Any,
    credentials: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
) -> TVClient:
    platform = normalize_platform(getattr(device, "platform", None))
    return PLATFORMS[platform](device, credentials, options)


def default_port(platform: str) -> int:
    return PLATFORMS[normalize_platform(platform)].default_port


async def discover(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Every TV we can see on the LAN, from SSDP and mDNS together."""
    roku, others = await asyncio.gather(
        _discover_roku(min(timeout, 3.0)),
        _discover_mdns(timeout),
        return_exceptions=True,
    )
    found: list[dict[str, Any]] = []
    for group in (roku, others):
        if isinstance(group, list):
            found.extend(group)
    # One entry per host; SSDP and mDNS can both answer for the same TV.
    unique: dict[str, dict[str, Any]] = {}
    for entry in found:
        unique.setdefault(entry["host"], entry)
    return list(unique.values())


async def _discover_roku(timeout: float) -> list[dict[str, Any]]:
    from ..roku import RokuError, discover_roku_devices

    try:
        devices = await asyncio.to_thread(discover_roku_devices, timeout)
    except RokuError:
        return []
    # SSDP only reports a firmware banner, so ask each Roku its own name.
    async def named(device: dict[str, Any]) -> dict[str, Any]:
        from ..config import TVDeviceConfig
        from ..roku import RokuClient

        entry = {
            "platform": "roku",
            "host": device["host"],
            "port": device.get("port", 8060),
            "name": "Roku",
        }
        try:
            info = await RokuClient(
                TVDeviceConfig(id="discovered", name="Roku", host=entry["host"], port=entry["port"])
            ).device_info()
        except (RokuError, ValueError):
            return entry
        entry["name"] = (
            info.get("friendly-device-name") or info.get("model-name") or "Roku"
        )
        return entry

    return list(await asyncio.gather(*(named(device) for device in devices)))


async def _discover_mdns(timeout: float) -> list[dict[str, Any]]:
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return []

    found: list[dict[str, Any]] = []
    pending: list[asyncio.Task[None]] = []
    aiozc = AsyncZeroconf()

    async def resolve(zeroconf: Any, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000):
            return
        for address in info.parsed_addresses():
            if ":" in address:
                continue  # IPv6; every one of these TVs answers on IPv4 too
            found.append(
                {
                    "platform": _MDNS[service_type],
                    "host": address,
                    "port": info.port or 0,
                    "name": name.split(".")[0].replace("\\032", " ").strip(),
                }
            )
            return

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: Any, **_: Any) -> None:
        if state_change is ServiceStateChange.Added:
            pending.append(asyncio.create_task(resolve(zeroconf, service_type, name)))

    browser = AsyncServiceBrowser(aiozc.zeroconf, list(_MDNS), handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await browser.async_cancel()
        await aiozc.async_close()
    return found
