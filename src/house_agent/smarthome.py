"""Smart-device control: lights, plugs, and whatever else joins the house.

One hub, two wire protocols, so users are never locked into one radio:

* Matter — spoken through the open-source ``matter-server`` controller
  (the same one Home Assistant uses), reached over its WebSocket API. It
  owns the Matter fabric, commissions devices over Bluetooth, and pushes
  attribute updates back to us. It runs as its own small service because
  its native CHIP wheels trail this project's Python version.
* Zigbee — spoken through Zigbee2MQTT over an MQTT broker. Dormant until
  a coordinator dongle and broker are configured; the code path is ready
  so enabling it later is a config change, not a build.

Backends discover devices and keep live state; the hub merges them into
one list, applies user-given names and rooms from the database, and
resolves fuzzy references like "the kitchen light".
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import replace, dataclass, field
from typing import Any

from .config import AppConfig, MatterConfig, ZigbeeConfig
from .db import Database
from .matter_process import MatterProcess, is_local

log = logging.getLogger(__name__)

# Matter cluster and attribute ids used below. Attribute paths are the
# matter-server serialization: "endpoint/cluster/attribute".
ON_OFF_CLUSTER = 6
LEVEL_CLUSTER = 8
COLOR_CLUSTER = 768
BASIC_INFO_CLUSTER = 40
BRIDGED_INFO_CLUSTER = 57

KELVIN_MIN, KELVIN_MAX = 1500, 6500


class SmartHomeError(RuntimeError):
    pass


def kelvin_to_mireds(kelvin: int) -> int:
    kelvin = max(KELVIN_MIN, min(int(kelvin), KELVIN_MAX))
    return round(1_000_000 / kelvin)


def mireds_to_kelvin(mireds: int | float | None) -> int | None:
    if not mireds:
        return None
    return round(1_000_000 / float(mireds))


_COLOR_HEX = {
    "red": "#ff0000", "orange": "#ff7f00", "yellow": "#ffff00",
    "green": "#00ff00", "teal": "#008080", "cyan": "#00ffff",
    "blue": "#0000ff", "purple": "#8000ff", "violet": "#8000ff",
    "magenta": "#ff00ff", "pink": "#ff69b4", "white": "#ffffff",
}


def _color_to_hex(color: str) -> str:
    wanted = color.strip().casefold()
    if wanted.startswith("#") and len(wanted) == 7:
        return wanted
    if wanted in _COLOR_HEX:
        return _COLOR_HEX[wanted]
    raise SmartHomeError(
        f"Unknown color '{color}'. Use a hex value like #ff2200 or one of: "
        + ", ".join(sorted(_COLOR_HEX))
    )


def _hex_to_hue_sat(hex_color: str) -> tuple[int, int]:
    """#rrggbb -> Matter/ZCL 8-bit hue (0-254 spanning the wheel) and
    saturation (0-254)."""
    import colorsys

    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, _light, sat = colorsys.rgb_to_hls(r, g, b)
    return round(hue * 254), round(sat * 254)


def _hue_sat_to_hex(hue: Any, saturation: Any) -> str | None:
    """ZCL 8-bit hue and saturation (0-254) -> #rrggbb, at full lightness
    of the hue: brightness is reported separately."""
    import colorsys

    if hue is None or saturation is None:
        return None
    try:
        h = max(0.0, min(float(hue), 254.0)) / 254
        sat = max(0.0, min(float(saturation), 254.0)) / 254
    except (TypeError, ValueError):
        return None
    r, g, b = colorsys.hls_to_rgb(h, 0.5 + (1 - sat) * 0.5, sat)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _xy_to_hex(x: Any, y: Any) -> str | None:
    """CIE xy (ZCL 16-bit or 0-1 floats) -> #rrggbb, normalised so the
    brightest channel is full: the shade, not the dimming level."""
    if x is None or y is None:
        return None
    try:
        cx, cy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if cx > 1 or cy > 1:
        cx, cy = cx / 65535, cy / 65535
    if cy <= 0:
        return None
    big_y = 1.0
    big_x = big_y / cy * cx
    big_z = big_y / cy * (1 - cx - cy)
    r = big_x * 1.656 - big_y * 0.355 - big_z * 0.255
    g = -big_x * 0.707 + big_y * 1.655 + big_z * 0.036
    b = big_x * 0.052 - big_y * 0.121 + big_z * 1.012
    channels = [max(0.0, c) for c in (r, g, b)]
    peak = max(channels) or 1.0
    channels = [c / peak for c in channels]
    gamma = [
        12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in channels
    ]
    return "#%02x%02x%02x" % tuple(round(max(0.0, min(1.0, c)) * 255) for c in gamma)


def _tokens(text: str) -> list[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.casefold())
    return [token for token in cleaned.split() if token]


@dataclass(slots=True)
class Device:
    key: str  # stable id, e.g. "matter-12" or "zigbee-0x00158d0001"
    backend: str  # "matter" | "zigbee"
    name: str
    kind: str  # "light" | "switch" | "unknown"
    online: bool = True
    room: str | None = None
    capabilities: tuple[str, ...] = ()  # of: power, brightness, color_temp
    state: dict[str, Any] = field(default_factory=dict)
    # backend-private addressing/limits (matter node+endpoint, mired range)
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "name": self.name,
            "room": self.room,
            "type": self.kind,
            "backend": self.backend,
            "online": self.online,
            "capabilities": list(self.capabilities),
            "state": self.state,
        }


class MatterBackend:
    """Client for matter-server's WebSocket API (JSON request/response
    plus pushed events). Reconnects forever with capped backoff."""

    name = "matter"

    def __init__(self, config: MatterConfig) -> None:
        self.config = config
        self.enabled = config.enabled
        self.connected = False
        self.devices: dict[str, Device] = {}
        # The Wi-Fi changed since the controller last got it.
        self._wifi_dirty = False
        self._ws: Any = None
        self._message_ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._nodes: dict[int, dict[str, Any]] = {}
        self._reported_down = False
        # (code, monotonic time, device name) of the last successful pair,
        # to catch a model re-pairing when it meant to rename.
        self._last_commissioned: tuple[str, float, str] | None = None

    async def run(self) -> None:
        import websockets

        backoff = 5.0
        while True:
            setup_task: asyncio.Task[None] | None = None
            try:
                async with websockets.connect(
                    self.config.url, max_size=32 * 1024 * 1024
                ) as ws:
                    self._ws = ws
                    info = json.loads(await ws.recv())  # server hello
                    setup_task = asyncio.create_task(self._setup(info))
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the reconnect loop alive
                level = logging.DEBUG if self._reported_down else logging.INFO
                log.log(
                    level,
                    "matter: controller at %s unavailable (%s); retrying in %.0fs",
                    self.config.url,
                    exc,
                    backoff,
                )
                self._reported_down = True
            finally:
                if setup_task:
                    setup_task.cancel()
                self._disconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _disconnect(self) -> None:
        self.connected = False
        self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(SmartHomeError("Matter controller disconnected"))
        self._pending.clear()

    async def set_wifi(self, ssid: str, password: str) -> bool:
        """The house moved to a new network: hand it to the controller now
        if it is up, or at the next connection. True when it took it now."""
        self.config = replace(self.config, wifi_ssid=ssid, wifi_password=password)
        self._wifi_dirty = True
        if not self.connected:
            return False
        await self._command("set_wifi_credentials", {"ssid": ssid, "credentials": password})
        self._wifi_dirty = False
        log.info("matter: new WiFi credentials handed to the controller")
        return True

    async def _setup(self, info: dict[str, Any]) -> None:
        if (
            self.config.wifi_ssid
            and self.config.wifi_password
            and (self._wifi_dirty or not info.get("wifi_credentials_set"))
        ):
            await self._command(
                "set_wifi_credentials",
                {"ssid": self.config.wifi_ssid, "credentials": self.config.wifi_password},
            )
            self._wifi_dirty = False
            log.info("matter: WiFi credentials handed to the controller")
        nodes = await self._command("start_listening")
        self._nodes.clear()
        self.devices.clear()
        for node in nodes or []:
            self._absorb_node(node)
        self.connected = True
        self._reported_down = False
        log.info(
            "matter: connected to %s (%d devices)", self.config.url, len(self.devices)
        )

    async def _command(
        self, command: str, args: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        ws = self._ws
        if ws is None:
            raise SmartHomeError("Matter controller is not connected")
        message_id = str(next(self._message_ids))
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await ws.send(
                json.dumps(
                    {"message_id": message_id, "command": command, "args": args or {}}
                )
            )
            return await asyncio.wait_for(
                future, timeout or self.config.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise SmartHomeError(f"Matter command {command} timed out") from exc
        finally:
            self._pending.pop(message_id, None)

    def _handle(self, message: dict[str, Any]) -> None:
        message_id = message.get("message_id")
        if message_id is not None:
            future = self._pending.get(str(message_id))
            if future and not future.done():
                if message.get("error_code") is not None:
                    future.set_exception(
                        SmartHomeError(str(message.get("details") or "Matter command failed"))
                    )
                else:
                    future.set_result(message.get("result"))
            return
        event = message.get("event")
        data = message.get("data")
        if event in {"node_added", "node_updated"} and isinstance(data, dict):
            self._absorb_node(data)
        elif event == "node_removed":
            self._nodes.pop(int(data), None) if data is not None else None
            self.devices = {
                key: device
                for key, device in self.devices.items()
                if device.extra.get("node_id") != data
            }
        elif event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
            self._absorb_attribute(int(data[0]), str(data[1]), data[2])

    def _absorb_node(self, node: dict[str, Any]) -> None:
        node_id = int(node["node_id"])
        self._nodes[node_id] = node
        attributes: dict[str, Any] = node.get("attributes") or {}
        endpoints: dict[int, dict[int, dict[int, Any]]] = {}
        for path, value in attributes.items():
            try:
                endpoint, cluster, attribute = (int(part) for part in path.split("/"))
            except ValueError:
                continue
            endpoints.setdefault(endpoint, {}).setdefault(cluster, {})[attribute] = value

        base_info = endpoints.get(0, {}).get(BASIC_INFO_CLUSTER, {})
        controllable = [
            endpoint
            for endpoint, clusters in sorted(endpoints.items())
            if ON_OFF_CLUSTER in clusters
        ]
        for endpoint in controllable:
            clusters = endpoints[endpoint]
            bridged = clusters.get(BRIDGED_INFO_CLUSTER, {})
            name = str(
                bridged.get(5)
                or base_info.get(5)  # NodeLabel
                or base_info.get(3)  # ProductName
                or f"Matter device {node_id}"
            ).strip() or f"Matter device {node_id}"
            key = (
                f"matter-{node_id}"
                if len(controllable) == 1
                else f"matter-{node_id}-{endpoint}"
            )
            capabilities = ["power"]
            kind = "switch"
            if LEVEL_CLUSTER in clusters:
                capabilities.append("brightness")
                kind = "light"
            color = clusters.get(COLOR_CLUSTER, {})
            if COLOR_CLUSTER in clusters:
                capabilities.append("color_temp")
                # ColorCapabilities bitmask: bit 0 hue/sat, bit 3 xy, bit 4 ct.
                if int(color.get(16394) or 0) & 0b1001:
                    capabilities.append("color")
                kind = "light"
            level = clusters.get(LEVEL_CLUSTER, {}).get(0)
            device = Device(
                key=key,
                backend=self.name,
                name=name,
                kind=kind,
                online=bool(node.get("available", True)),
                capabilities=tuple(capabilities),
                state={
                    "power": bool(clusters[ON_OFF_CLUSTER].get(0))
                    if clusters[ON_OFF_CLUSTER].get(0) is not None
                    else None,
                    "brightness": round(level / 254 * 100) if level else None,
                    "color_temp_kelvin": mireds_to_kelvin(color.get(7)),
                    # ColorMode 0 is hue/saturation: the light is showing a
                    # color rather than a white; 1 is xy, 2 is a temperature.
                    "color": (
                        _hue_sat_to_hex(color.get(0), color.get(1))
                        if color.get(8) == 0
                        else _xy_to_hex(color.get(3), color.get(4))
                        if color.get(8) == 1
                        else None
                    ),
                },
                extra={
                    "node_id": node_id,
                    "endpoint": endpoint,
                    "min_mireds": color.get(0x400B),
                    "max_mireds": color.get(0x400C),
                    "vendor": base_info.get(1),
                    "product": base_info.get(3),
                },
            )
            existing = self.devices.get(key)
            if existing:
                device.state = {**existing.state, **{
                    k: v for k, v in device.state.items() if v is not None
                }}
            self.devices[key] = device

    def _absorb_attribute(self, node_id: int, path: str, value: Any) -> None:
        try:
            endpoint, cluster, attribute = (int(part) for part in path.split("/"))
        except ValueError:
            return
        node = self._nodes.get(node_id)
        if node is not None:
            (node.setdefault("attributes", {}))[path] = value
        for device in self.devices.values():
            if (
                device.extra.get("node_id") != node_id
                or device.extra.get("endpoint") != endpoint
            ):
                continue
            if cluster == ON_OFF_CLUSTER and attribute == 0:
                device.state["power"] = bool(value)
            elif cluster == LEVEL_CLUSTER and attribute == 0 and value is not None:
                device.state["brightness"] = round(value / 254 * 100)
            elif cluster == COLOR_CLUSTER and attribute == 7:
                device.state["color_temp_kelvin"] = mireds_to_kelvin(value)
            elif cluster == COLOR_CLUSTER and attribute in (0, 1, 3, 4, 8):
                attributes = (node or {}).get("attributes", {})
                base = f"{endpoint}/{COLOR_CLUSTER}/"
                mode = attributes.get(base + "8")
                if mode == 0:
                    device.state["color"] = _hue_sat_to_hex(
                        attributes.get(base + "0"), attributes.get(base + "1")
                    )
                elif mode == 1:
                    device.state["color"] = _xy_to_hex(
                        attributes.get(base + "3"), attributes.get(base + "4")
                    )
                elif mode == 2:
                    device.state["color"] = None

    async def set_state(
        self,
        device: Device,
        power: str | None = None,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
        color: str | None = None,
    ) -> None:
        if color is not None and "color" not in device.capabilities:
            raise SmartHomeError(f"{device.name} does not support colors")
        node_id = device.extra["node_id"]
        endpoint = device.extra["endpoint"]

        async def command(cluster: int, name: str, payload: dict[str, Any]) -> None:
            await self._command(
                "device_command",
                {
                    "node_id": node_id,
                    "endpoint_id": endpoint,
                    "cluster_id": cluster,
                    "command_name": name,
                    "payload": payload,
                },
            )

        if brightness == 0 and power is None:
            power, brightness = "off", None
        if power:
            await command(
                ON_OFF_CLUSTER, {"on": "On", "off": "Off", "toggle": "Toggle"}[power], {}
            )
            if power != "toggle":
                device.state["power"] = power == "on"
        if brightness is not None:
            await command(
                LEVEL_CLUSTER,
                "MoveToLevelWithOnOff",
                {
                    "level": max(1, round(brightness / 100 * 254)),
                    "transitionTime": 0,
                    "optionsMask": 0,
                    "optionsOverride": 0,
                },
            )
            device.state["brightness"] = brightness
            device.state["power"] = True
        if color_temp_kelvin is not None:
            mireds = kelvin_to_mireds(color_temp_kelvin)
            low, high = device.extra.get("min_mireds"), device.extra.get("max_mireds")
            if low and high:
                mireds = max(int(low), min(mireds, int(high)))
            await command(
                COLOR_CLUSTER,
                "MoveToColorTemperature",
                {
                    "colorTemperatureMireds": mireds,
                    "transitionTime": 0,
                    "optionsMask": 0,
                    "optionsOverride": 0,
                },
            )
            device.state["color_temp_kelvin"] = mireds_to_kelvin(mireds)
            device.state["color"] = None
        if color is not None:
            hex_color = _color_to_hex(color)
            hue, saturation = _hex_to_hue_sat(hex_color)
            await command(
                COLOR_CLUSTER,
                "MoveToHueAndSaturation",
                {
                    "hue": hue,
                    "saturation": saturation,
                    "transitionTime": 0,
                    "optionsMask": 0,
                    "optionsOverride": 0,
                },
            )
            device.state["color"] = hex_color

    async def commission(self, code: str) -> Device:
        """Add a new Matter device using the pairing code from its box or
        QR label. Slow: BLE discovery plus network join can take a minute."""
        if not self.connected:
            raise SmartHomeError(
                "The Matter controller is not running. Start it with "
                "scripts/matter-server.sh, then try pairing again."
            )
        code = code.strip()
        if not code.upper().startswith("MT:"):
            code = "".join(ch for ch in code if ch.isdigit())
            # Voice transcription loves to drop a doubled digit; catch that
            # here instead of sending a code that can never parse.
            if len(code) not in (11, 21):
                raise SmartHomeError(
                    f"That code has {len(code)} digits, but Matter pairing "
                    "codes are always 11 or 21 digits — it was probably "
                    "misheard (repeated digits often get dropped). Nothing "
                    "was attempted. Ask the user to read the code again "
                    "slowly, digit by digit, or type it."
                )
        if not code:
            raise SmartHomeError("A Matter pairing code is required")
        last = self._last_commissioned
        if last and last[0] == code and time.monotonic() - last[1] < 600:
            raise SmartHomeError(
                f"This device is ALREADY PAIRED (it is '{last[2]}' in "
                "device_list) — pairing succeeded moments ago and must not be "
                "repeated. To name it or set its room, call device_rename; to "
                "control it, call device_set. Do not apologize for a failure; "
                "nothing failed."
            )
        before = set(self.devices)
        # With a Bluetooth adapter the controller searches the air and the
        # network at once (a brand-new device is only on the air). If that
        # fails -- the adapter is off, the BLE stack is unhappy -- fall back
        # to the network alone, which is all a controller without Bluetooth
        # ever does: the device may already be on the LAN through its
        # vendor app. Without Bluetooth there is only the network.
        attempts = [False, True] if self.config.bluetooth else [True]
        node: Any = None
        for network_only in attempts:
            try:
                node = await self._command(
                    "commission_with_code",
                    {"code": code, "network_only": network_only},
                    timeout=180.0 if not network_only else 120.0,
                )
                break
            except SmartHomeError as exc:
                if not network_only:
                    log.warning(
                        "matter: pairing over Bluetooth+network failed (%s); "
                        "trying the network only",
                        exc,
                    )
                    continue
                self._raise_pairing_failure(exc)
        if isinstance(node, dict):
            self._absorb_node(node)
        added = [self.devices[key] for key in self.devices.keys() - before]
        if added:
            self._last_commissioned = (code, time.monotonic(), added[0].name)
            return added[0]
        raise SmartHomeError(
            "Commissioning finished but no controllable device appeared"
        )

    def _raise_pairing_failure(self, exc: SmartHomeError) -> None:
        """Every path failed: explain what to try, given which paths exist."""
        paired = ", ".join(device.name for device in self.devices.values())
        hint = (
            f" Note: these devices are already paired and working: {paired}. "
            "If the user means one of them (naming it, moving it to a room, "
            "controlling it), use device_rename or device_set — not pairing."
            if paired
            else ""
        )
        if self.config.bluetooth:
            raise SmartHomeError(
                f"Pairing failed over Bluetooth and then over the network ({exc}). "
                "Check the code, and that the device is powered on and in "
                "pairing mode: cut its power completely at the switch or plug "
                "— not from an app — wait 10 seconds, turn it back on, and try "
                "the code again right away. If it still fails, set the device "
                "up in its vendor app (Linkind: AiDot) so it joins the home "
                "WiFi, then try the code once more." + hint
            ) from exc
        raise SmartHomeError(
            "No Matter device in pairing mode was found on the network. "
            "The device must first be connected to the home WiFi through "
            "its vendor app (for Linkind bulbs: the AiDot app). Its pairing "
            "window also only opens for about 15 minutes after power-on: "
            "have the user cut its power completely at the switch or plug — "
            "not from an app — wait 10 seconds, turn it back on, and then "
            "try the code again right away." + hint
        ) from exc

    async def remove(self, device: Device) -> None:
        """Unpair a device: drop it from the controller, which also tells the
        device to forget this fabric. With no fabric left the device goes
        back into pairing mode on its own (some need a power cycle or a
        factory reset first), ready to be commissioned again."""
        if not self.connected:
            raise SmartHomeError("The Matter controller is not running.")
        node_id = device.extra.get("node_id")
        if node_id is None:
            raise SmartHomeError(f"{device.name} is not a Matter node")
        # The controller removes the record first and only then reaches out
        # to the device, which may be slow or unreachable; either way the
        # node is gone from this fabric.
        await self._command("remove_node", {"node_id": int(node_id)}, timeout=60.0)
        self._nodes.pop(int(node_id), None)
        self.devices = {
            key: item
            for key, item in self.devices.items()
            if item.extra.get("node_id") != node_id
        }
        self._last_commissioned = None


class ZigbeeBackend:
    """Zigbee2MQTT over MQTT. Everything arrives on retained topics, so a
    fresh connection immediately rebuilds the device list and states."""

    name = "zigbee"

    def __init__(self, config: ZigbeeConfig) -> None:
        self.config = config
        self.enabled = config.enabled
        self.connected = False
        self.devices: dict[str, Device] = {}
        self._client: Any = None
        self._names: dict[str, str] = {}  # friendly_name -> device key
        self._reported_down = False
        # device key -> monotonic time of its last join/announce on the
        # network. A factory-reset light REJOINS under its old address and
        # old name, which the device list cannot show as news — this is the
        # only signal that the light in the user's hand just connected.
        self.recent_joins: dict[str, float] = {}
        # Zigbee2MQTT's own heartbeat (bridge/state, retained, with a last
        # will of "offline"). The broker being reachable says nothing about
        # the bridge: in September 2026 Zigbee2MQTT crash-looped for hours
        # while mosquitto's retained device list made every light look fine.
        self.bridge_online: bool | None = None
        self._bridge_down_at: float | None = None

    @property
    def problem(self) -> str | None:
        if self.connected and self.bridge_online is False:
            return "Zigbee2MQTT is not running"
        return None

    def bridge_down_for(self) -> float | None:
        """Seconds the bridge has been offline while the broker is up."""
        if not self.connected or self.bridge_online is not False or self._bridge_down_at is None:
            return None
        return time.monotonic() - self._bridge_down_at

    def _set_bridge(self, online: bool) -> None:
        if online != self.bridge_online:
            log.log(logging.INFO if online else logging.WARNING, "zigbee: Zigbee2MQTT is %s", "online" if online else "OFFLINE")
        self.bridge_online = online
        self._bridge_down_at = None if online else (self._bridge_down_at or time.monotonic())
        for device in self.devices.values():
            device.online = online and not device.extra.get("disabled", False)

    async def run(self) -> None:
        import aiomqtt

        backoff = 5.0
        while True:
            try:
                async with aiomqtt.Client(
                    self.config.mqtt_host,
                    port=self.config.mqtt_port,
                    username=self.config.mqtt_username,
                    password=self.config.mqtt_password,
                ) as client:
                    self._client = client
                    await client.subscribe(f"{self.config.base_topic}/#")
                    self.connected = True
                    self._reported_down = False
                    log.info("zigbee: connected to MQTT at %s", self.config.mqtt_host)
                    backoff = 5.0
                    async for message in client.messages:
                        self._handle(str(message.topic), bytes(message.payload or b""))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the reconnect loop alive
                level = logging.DEBUG if self._reported_down else logging.INFO
                log.log(
                    level,
                    "zigbee: MQTT at %s:%d unavailable (%s); retrying in %.0fs",
                    self.config.mqtt_host,
                    self.config.mqtt_port,
                    exc,
                    backoff,
                )
                self._reported_down = True
            finally:
                self.connected = False
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _handle(self, topic: str, payload: bytes) -> None:
        base = self.config.base_topic
        if not topic.startswith(f"{base}/"):
            return
        suffix = topic[len(base) + 1 :]
        if suffix == "bridge/state":
            text = payload.decode("utf-8", "replace").strip()
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = text
            state = parsed.get("state") if isinstance(parsed, dict) else parsed
            if state in ("online", "offline"):
                self._set_bridge(state == "online")
        elif suffix == "bridge/devices":
            try:
                self._absorb_devices(json.loads(payload))
            except (ValueError, TypeError):
                log.warning("zigbee: unreadable bridge/devices payload")
        elif suffix == "bridge/event":
            try:
                event = json.loads(payload)
            except (ValueError, TypeError):
                return
            if isinstance(event, dict) and event.get("type") in (
                "device_joined",
                "device_announce",
            ):
                address = str((event.get("data") or {}).get("ieee_address") or "")
                if address:
                    self.recent_joins[f"zigbee-{address}"] = time.monotonic()
                    log.info("zigbee: device %s joined/announced", address)
        elif "/" not in suffix and not suffix.startswith("bridge"):
            key = self._names.get(suffix)
            if key and key in self.devices:
                try:
                    self._absorb_state(self.devices[key], json.loads(payload))
                except (ValueError, TypeError):
                    pass

    def _absorb_devices(self, listed: Any) -> None:
        if not isinstance(listed, list):
            return
        self._names.clear()
        kept: dict[str, Device] = {}
        for item in listed:
            if not isinstance(item, dict) or item.get("type") == "Coordinator":
                continue
            friendly = str(item.get("friendly_name") or "")
            address = str(item.get("ieee_address") or friendly)
            if not friendly or not item.get("supported", True):
                continue
            features = _zigbee_features(item)
            if "power" not in features:
                continue  # sensors and remotes are out of scope for now
            key = f"zigbee-{address}"
            capabilities = tuple(
                name
                for name in ("power", "brightness", "color_temp", "color")
                if name in features
            )
            device = Device(
                key=key,
                backend=self.name,
                name=friendly,
                kind="light" if len(capabilities) > 1 else "switch",
                online=not item.get("disabled", False) and self.bridge_online is not False,
                capabilities=capabilities,
                state=self.devices.get(key).state if key in self.devices else {},
                extra={
                    "friendly_name": friendly,
                    "disabled": bool(item.get("disabled", False)),
                    "min_mireds": features.get("color_temp", {}).get("value_min"),
                    "max_mireds": features.get("color_temp", {}).get("value_max"),
                },
            )
            kept[key] = device
            self._names[friendly] = key
        self.devices = kept

    def _absorb_state(self, device: Device, state: Any) -> None:
        if not isinstance(state, dict):
            return
        if "state" in state:
            device.state["power"] = str(state["state"]).upper() == "ON"
        if isinstance(state.get("brightness"), (int, float)):
            device.state["brightness"] = round(state["brightness"] / 254 * 100)
        if isinstance(state.get("color_temp"), (int, float)):
            device.state["color_temp_kelvin"] = mireds_to_kelvin(state["color_temp"])
        mode = state.get("color_mode")
        color = state.get("color") if isinstance(state.get("color"), dict) else {}
        if mode == "color_temp":
            device.state["color"] = None
        elif mode == "hs" or (mode is None and "hue" in color):
            if "hue" in color and "saturation" in color:
                device.state["color"] = _hue_sat_to_hex(
                    float(color["hue"]) / 360 * 254, float(color["saturation"]) / 100 * 254
                )
        elif mode == "xy" or (mode is None and "x" in color):
            if "x" in color and "y" in color:
                device.state["color"] = _xy_to_hex(color["x"], color["y"])

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        client = self._client
        if client is None or not self.connected:
            raise SmartHomeError(
                "Zigbee is not connected. It needs an MQTT broker and "
                "Zigbee2MQTT running (see README), plus a coordinator dongle."
            )
        await client.publish(
            f"{self.config.base_topic}/{topic}", json.dumps(payload)
        )

    async def set_state(
        self,
        device: Device,
        power: str | None = None,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
        color: str | None = None,
    ) -> None:
        if self.bridge_online is False:
            raise SmartHomeError(
                "The Zigbee bridge (Zigbee2MQTT) is offline, so Zigbee lights cannot "
                "hear commands right now. house-agent restarts it by itself; if it "
                "stays down, `docker logs house-agent-zigbee2mqtt` says why."
            )
        payload: dict[str, Any] = {}
        if brightness == 0 and power is None:
            power, brightness = "off", None
        if color is not None:
            if "color" not in device.capabilities:
                raise SmartHomeError(f"{device.name} does not support colors")
            payload["color"] = {"hex": _color_to_hex(color)}
            device.state["color"] = payload["color"]["hex"]
        if power:
            payload["state"] = power.upper()
        if brightness is not None:
            payload["brightness"] = max(1, round(brightness / 100 * 254))
        if color_temp_kelvin is not None:
            mireds = kelvin_to_mireds(color_temp_kelvin)
            low, high = device.extra.get("min_mireds"), device.extra.get("max_mireds")
            if low and high:
                mireds = max(int(low), min(mireds, int(high)))
            payload["color_temp"] = mireds
            device.state["color"] = None
        if payload:
            await self._publish(f"{device.extra['friendly_name']}/set", payload)
            # Zigbee2MQTT echoes the bulb's new state a moment later; until it
            # does, report what was just asked for, not what came before.
            if power in ("on", "off"):
                device.state["power"] = power == "on"
            elif power == "toggle":
                device.state["power"] = not device.state.get("power", False)
            if brightness is not None:
                device.state["brightness"] = max(1, min(100, int(brightness)))
            if color_temp_kelvin is not None:
                device.state["color_temp_kelvin"] = int(color_temp_kelvin)

    async def permit_join(self, seconds: int = 180) -> None:
        await self._publish("bridge/request/permit_join", {"time": seconds})


class SmartHomeHub:
    """One face over every backend: a merged device list with the user's
    names and rooms applied, fuzzy device lookup, and pairing."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        backends: list[Any] | None = None,
    ) -> None:
        self.database = database
        self.process: MatterProcess | None = None
        if backends is not None:
            self.backends = backends
        else:
            self.backends = []
            if config.matter.enabled:
                self.backends.append(MatterBackend(config.matter))
                if config.matter.manage and is_local(config.matter.url):
                    # The launcher turns this into --bluetooth-adapter.
                    env = (
                        {"MATTER_BLUETOOTH_ADAPTER": str(config.matter.bluetooth_adapter)}
                        if config.matter.bluetooth
                        else {}
                    )
                    self.process = MatterProcess(config.matter.launcher, env=env)
            if config.zigbee.enabled:
                self.backends.append(ZigbeeBackend(config.zigbee))
        self._tasks: list[asyncio.Task[None]] = []

    async def set_wifi(self, ssid: str, password: str) -> bool:
        """Every backend that hands Wi-Fi to new devices learns the new one;
        True when a live controller took it immediately."""
        taken = False
        for backend in self.backends:
            if hasattr(backend, "set_wifi"):
                try:
                    taken = await backend.set_wifi(ssid, password) or taken
                except Exception:  # noqa: BLE001 - it is saved; the next connect retries
                    log.exception("could not hand the new WiFi to %s", getattr(backend, "name", backend))
        return taken

    def start(self) -> None:
        if self.process is not None:
            # The controller first, so the backend's connect loop finds it.
            self._tasks.append(asyncio.create_task(self.process.run(), name="matter-server"))
        for backend in self.backends:
            self._tasks.append(
                asyncio.create_task(backend.run(), name=f"smarthome-{backend.name}")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        if self.process is not None:
            await self.process.stop()

    def status(self) -> dict[str, Any]:
        status = {
            backend.name: {
                "enabled": backend.enabled,
                "connected": backend.connected and not getattr(backend, "problem", None),
                "devices": len(backend.devices),
                **({"problem": backend.problem} if getattr(backend, "problem", None) else {}),
            }
            for backend in self.backends
        }
        if self.process is not None and "matter" in status:
            status["matter"]["managed_process"] = (
                "running" if self.process.running else (self.process.gave_up or "stopped")
            )
        return status

    def devices(self) -> list[Device]:
        aliases = self.database.smart_device_aliases()
        merged: list[Device] = []
        for backend in self.backends:
            for device in backend.devices.values():
                alias = aliases.get(device.key)
                if alias:
                    device.name = alias.get("name") or device.name
                    device.room = alias.get("room") or device.room
                merged.append(device)
        return merged

    def missing(self) -> list[dict[str, Any]]:
        """Devices this house named that their backend, though connected, no
        longer lists. A paired device does not just vanish: this is the
        controller having lost its pairing record (its storage was reset or
        wiped), and the fix is restoring that record or pairing again -- not
        telling the user they have no devices."""
        present = {device.key for device in self.devices()}
        connected = {backend.name for backend in self.backends if backend.connected}
        return [
            {
                "id": item["key"],
                "name": item["name"] or item["key"],
                "room": item["room"],
                "backend": item["backend"],
                "problem": (
                    f"paired before as {item['key']} but the {item['backend']} "
                    "controller no longer lists it: its pairing record was lost. "
                    "Restore the controller's storage from a snapshot, or pair the "
                    "device again."
                ),
            }
            for item in self.database.smart_devices_known()
            if item["key"] not in present and item["backend"] in connected
        ]

    def _matches(self, query: str) -> list[Device]:
        """All devices tied for the best fuzzy-match score (an exact key
        match wins outright)."""
        devices = self.devices()
        if not devices:
            missing = self.missing()
            if missing:
                names = ", ".join(item["name"] for item in missing)
                raise SmartHomeError(
                    f"The controller has lost the pairing record for {names}. "
                    "Nothing is paired right now; restore the controller's storage "
                    "or pair again."
                )
            raise SmartHomeError(
                "No smart devices are set up yet. Pair one first with device_pair."
            )
        wanted = query.strip().casefold()
        for device in devices:
            if device.key.casefold() == wanted:
                return [device]
        named = [device for device in devices if device.name.strip().casefold() == wanted]
        if len(named) == 1:
            # "bedroom lamp" must mean the device literally named that, even
            # though the fuzzy corpus would tie it with "bedroom light".
            return named
        query_tokens = _tokens(query)
        scored: list[tuple[int, Device]] = []
        for device in devices:
            corpus = _tokens(
                f"{device.name} {device.room or ''} {device.kind} {device.backend} "
                + ("light lamp bulb" if device.kind == "light" else "")
            )
            hits = sum(
                1
                for token in query_tokens
                if any(word.startswith(token) or token.startswith(word) for word in corpus)
            )
            scored.append((hits, device))
        best = max(hits for hits, _ in scored)
        top = [device for hits, device in scored if hits == best and best > 0]
        if not top:
            names = ", ".join(
                f"{device.name}" + (f" ({device.room})" if device.room else "")
                for device in devices
            )
            raise SmartHomeError(f"No device matches '{query}'. Devices: {names}")
        return top

    def find(self, query: str) -> Device:
        top = self._matches(query)
        if len(top) == 1:
            return top[0]
        names = ", ".join(
            f"{device.name}" + (f" ({device.room})" if device.room else "")
            for device in top
        )
        raise SmartHomeError(f"'{query}' is ambiguous. Which one: {names}?")

    def find_targets(self, query: str) -> list[Device]:
        """Devices a control request should act on. A request that matches
        several devices in the SAME room means all of them ("turn on the
        bedroom lights"); matches spread across rooms stay ambiguous and
        need the user to pick."""
        top = self._matches(query)
        if len(top) == 1:
            return top
        rooms = {device.room for device in top}
        query_tokens = set(_tokens(query))
        if len(rooms) == 1 and rooms != {None}:
            return top
        if query_tokens & {"all", "every", "everything", "everywhere", "house"}:
            return top
        names = ", ".join(
            f"{device.name}" + (f" ({device.room})" if device.room else "")
            for device in top
        )
        raise SmartHomeError(f"'{query}' is ambiguous. Which one: {names}?")

    def _backend(self, name: str) -> Any:
        for backend in self.backends:
            if backend.name == name:
                return backend
        raise SmartHomeError(
            f"The {name} backend is not enabled. Enable it in config.toml first."
        )

    def zigbee_bridge_down_for(self) -> float | None:
        for backend in self.backends:
            if backend.name == "zigbee":
                return backend.bridge_down_for()
        return None

    def zigbee_joins_since(self, since: float) -> list[str]:
        """Device keys that joined or re-announced on the zigbee network
        after the given monotonic time — including already-paired devices
        rejoining after a factory reset, which the device list cannot show."""
        for backend in self.backends:
            if backend.name == "zigbee":
                joins = getattr(backend, "recent_joins", {}) or {}
                return [key for key, stamp in joins.items() if stamp >= since]
        return []

    async def control(
        self,
        query: str,
        power: str | None = None,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
        color: str | None = None,
    ) -> tuple[list[Device], list[str]]:
        """Apply the change to every matched device. One unreachable device
        must not stop the rest of the room: failures are collected and
        reported per device; only a total failure raises."""
        if power is None and brightness is None and color_temp_kelvin is None and color is None:
            raise SmartHomeError(
                "Nothing to change: give power, brightness, color_temp, or color"
            )
        devices = self.find_targets(query)
        changed: list[Device] = []
        failures: list[str] = []
        for device in devices:
            backend = self._backend(device.backend)
            kwargs: dict[str, Any] = {
                "power": power,
                "brightness": brightness,
                "color_temp_kelvin": color_temp_kelvin,
            }
            if color is not None:
                kwargs["color"] = color
            try:
                await backend.set_state(device, **kwargs)
            except SmartHomeError as exc:
                failures.append(f"{device.name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - a dead node must not stop the room
                failures.append(f"{device.name}: {exc}")
            else:
                changed.append(device)
        if not changed:
            raise SmartHomeError("; ".join(failures))
        return changed, failures

    async def flash(self, query: str, times: int = 3) -> Device:
        """Visibly blink a light so a person can tell which physical device
        it is. Plain on/off toggling on purpose: it works on every light,
        while the standard identify effect times out on some firmwares
        (e.g. Third Reality color bulbs)."""
        device = self.find(query)
        if "power" not in device.capabilities:
            raise SmartHomeError(f"{device.name} has no power control to flash with")
        backend = self._backend(device.backend)
        was_on = device.state.get("power", True)
        times = max(1, min(int(times), 10))
        for _ in range(times):
            await backend.set_state(device, power="off")
            await asyncio.sleep(1.2)
            await backend.set_state(device, power="on")
            await asyncio.sleep(1.2)
        if not was_on:
            await backend.set_state(device, power="off")
        return device

    async def pair(self, network: str, code: str | None = None) -> dict[str, Any]:
        if network == "matter":
            if not code:
                raise SmartHomeError(
                    "No pairing code was given, so nothing was attempted — do not "
                    "tell the user pairing failed. Ask them to read out the 11-digit "
                    "code (or MT: QR string) printed on the device, its box, or "
                    "shown in its vendor app, then call this again with it."
                )
            device = await self._backend("matter").commission(code)
            return {
                "ok": True,
                "paired": device.summary(),
                "note": "Paired — never call device_pair again for this device. "
                "Ask the user what to name it and which room it is in, then "
                f"save their answer with device_rename, passing '{device.key}' "
                "as the device — never the new name.",
            }
        if network == "zigbee":
            await self._backend("zigbee").permit_join()
            return {
                "ok": True,
                "note": "Zigbee join window is open for 3 minutes. Put the device "
                "in pairing mode now; it will appear in device_list once joined. "
                "NEVER call device_pair again this conversation — the window is "
                "already open. When the user says the device is in pairing mode "
                "or asks about progress, call device_list to see whether a new "
                "device appeared. When one does, flash it with device_flash so "
                "the user can see which physical light paired, then ask its "
                "name and room. If nothing appears after a minute, say: "
                '"Switch the light off and on six times, about a second '
                'apart, until it blinks — that resets it into pairing mode."'
                " Then check device_list again.",
            }
        raise SmartHomeError("Network must be matter or zigbee")

    async def remove(self, query: str) -> Device:
        """Unpair a device and forget its name and room, so that when it is
        paired again it starts fresh instead of being reported as lost."""
        device = self.find(query)
        backend = self._backend(device.backend)
        if not hasattr(backend, "remove"):
            raise SmartHomeError(
                f"{device.backend} devices cannot be removed from here yet"
            )
        await backend.remove(device)
        self.database.delete_smart_device_alias(device.key)
        return device

    def rename(
        self,
        query: str,
        name: str | None,
        room: str | None,
        replace: bool = False,
    ) -> Device:
        if not name and not room:
            raise SmartHomeError("Give a new name, a room, or both")
        device = self.find(query)
        if name:
            wanted = name.strip().casefold()
            taken = [
                other
                for other in self.devices()
                if other.key != device.key and other.name.strip().casefold() == wanted
            ]
            if taken and not replace:
                holder = taken[0]
                where = f" in the {holder.room}" if holder.room else ""
                raise SmartHomeError(
                    f"There is already a device called '{holder.name}'{where}. "
                    "Ask the user whether this new device should take over that "
                    "name; only if they agree, call device_rename again with "
                    "replace set to true (the old device keeps working but goes "
                    "back to its factory name until renamed)."
                )
            if taken and replace:
                for holder in taken:
                    self.database.delete_smart_device_alias(holder.key)
                    alias = self.database.smart_device_aliases().get(holder.key)
                    if alias is None:
                        holder.name = holder.key
        self.database.save_smart_device_alias(device.key, device.backend, name, room)
        if name:
            device.name = name
        if room:
            device.room = room
        return device


def _zigbee_features(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a Zigbee2MQTT device definition to our capability names."""
    found: dict[str, dict[str, Any]] = {}
    definition = item.get("definition") or {}
    for expose in definition.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        features = expose.get("features") or []
        if expose.get("type") in {"light", "switch"}:
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                prop = feature.get("property")
                if prop == "state":
                    found["power"] = feature
                elif prop == "brightness":
                    found["brightness"] = feature
                elif prop == "color_temp":
                    found["color_temp"] = feature
                elif prop == "color":
                    found["color"] = feature
    return found
