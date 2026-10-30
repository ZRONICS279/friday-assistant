import asyncio
import json
import time

import pytest

from house_agent.config import AppConfig, MatterConfig, StorageConfig, ZigbeeConfig
from house_agent.db import Database
from house_agent.smarthome import (
    Device,
    MatterBackend,
    SmartHomeError,
    SmartHomeHub,
    ZigbeeBackend,
    kelvin_to_mireds,
    mireds_to_kelvin,
)
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "smart.db")
    database.initialize()
    return database


class FakeBackend:
    name = "matter"

    def __init__(self, devices: dict[str, Device]) -> None:
        self.enabled = True
        self.connected = True
        self.devices = devices
        self.calls: list[dict] = []

    async def set_state(
        self, device, power=None, brightness=None, color_temp_kelvin=None, color=None
    ):
        call = {
            "key": device.key,
            "power": power,
            "brightness": brightness,
            "color_temp_kelvin": color_temp_kelvin,
        }
        if color is not None:
            call["color"] = color
        self.calls.append(call)

    async def commission(self, code: str) -> Device:
        device = Device(key="matter-9", backend="matter", name="New bulb", kind="light")
        self.devices[device.key] = device
        return device

    async def remove(self, device: Device) -> None:
        self.calls.append({"key": device.key, "removed": True})
        self.devices.pop(device.key, None)


def light(key: str, name: str, room: str | None = None) -> Device:
    return Device(
        key=key,
        backend="matter",
        name=name,
        kind="light",
        room=room,
        capabilities=("power", "brightness", "color_temp"),
        state={"power": False},
        extra={"node_id": 1, "endpoint": 1},
    )


MATTER_NODE = {
    "node_id": 5,
    "available": True,
    "attributes": {
        "0/40/1": "Linkind",
        "0/40/3": "Smart Light Bulb",
        "0/40/5": "",
        "1/6/0": True,
        "1/8/0": 127,
        "1/768/7": 250,
        "1/768/16395": 153,
        "1/768/16396": 370,
    },
}

Z2M_DEVICES = [
    {"type": "Coordinator", "friendly_name": "Coordinator", "ieee_address": "0x00"},
    {
        "type": "EndDevice",
        "friendly_name": "hall light",
        "ieee_address": "0xabc",
        "supported": True,
        "definition": {
            "exposes": [
                {
                    "type": "light",
                    "features": [
                        {"property": "state"},
                        {"property": "brightness"},
                        {"property": "color_temp", "value_min": 150, "value_max": 500},
                    ],
                }
            ]
        },
    },
    {
        "type": "EndDevice",
        "friendly_name": "door sensor",
        "ieee_address": "0xdef",
        "supported": True,
        "definition": {"exposes": [{"type": "binary", "property": "contact"}]},
    },
]


def test_kelvin_mireds_roundtrip() -> None:
    assert kelvin_to_mireds(2700) == 370
    assert kelvin_to_mireds(6500) == 154
    assert kelvin_to_mireds(100) == kelvin_to_mireds(1500)  # clamped
    assert mireds_to_kelvin(370) == 2703
    assert mireds_to_kelvin(None) is None


def test_matter_node_becomes_a_light() -> None:
    backend = MatterBackend(MatterConfig())
    backend._absorb_node(MATTER_NODE)
    assert list(backend.devices) == ["matter-5"]
    device = backend.devices["matter-5"]
    assert device.name == "Smart Light Bulb"
    assert device.kind == "light"
    assert device.capabilities == ("power", "brightness", "color_temp")
    assert device.state["power"] is True
    assert device.state["brightness"] == 50
    assert device.state["color_temp_kelvin"] == 4000
    assert device.extra["min_mireds"] == 153


def test_matter_attribute_update_changes_state() -> None:
    backend = MatterBackend(MatterConfig())
    backend._absorb_node(MATTER_NODE)
    backend._handle({"event": "attribute_updated", "data": [5, "1/6/0", False]})
    backend._handle({"event": "attribute_updated", "data": [5, "1/8/0", 254]})
    device = backend.devices["matter-5"]
    assert device.state["power"] is False
    assert device.state["brightness"] == 100


def test_zigbee_devices_and_state_parse() -> None:
    backend = ZigbeeBackend(ZigbeeConfig(enabled=True))
    backend._handle("zigbee2mqtt/bridge/devices", json.dumps(Z2M_DEVICES).encode())
    assert list(backend.devices) == ["zigbee-0xabc"]  # sensor filtered out
    device = backend.devices["zigbee-0xabc"]
    assert device.name == "hall light"
    assert device.kind == "light"
    backend._handle(
        "zigbee2mqtt/hall light",
        json.dumps({"state": "ON", "brightness": 254, "color_temp": 250}).encode(),
    )
    assert device.state == {
        "power": True,
        "brightness": 100,
        "color_temp_kelvin": 4000,
    }


async def test_zigbee_bridge_offline_marks_lights_offline_and_refuses_commands() -> None:
    backend = ZigbeeBackend(ZigbeeConfig(enabled=True))
    backend.connected = True
    backend._handle("zigbee2mqtt/bridge/devices", json.dumps(Z2M_DEVICES).encode())
    device = backend.devices["zigbee-0xabc"]
    assert device.online and backend.problem is None and backend.bridge_down_for() is None

    backend._handle("zigbee2mqtt/bridge/state", b'{"state":"offline"}')
    assert device.online is False
    assert backend.problem == "Zigbee2MQTT is not running"
    assert backend.bridge_down_for() is not None
    with pytest.raises(SmartHomeError, match="offline"):
        await backend.set_state(device, power="on")
    # A device list re-read while the bridge is down keeps them offline.
    backend._handle("zigbee2mqtt/bridge/devices", json.dumps(Z2M_DEVICES).encode())
    assert backend.devices["zigbee-0xabc"].online is False

    backend._handle("zigbee2mqtt/bridge/state", b"online")  # the legacy plain payload
    assert backend.devices["zigbee-0xabc"].online is True
    assert backend.problem is None and backend.bridge_down_for() is None


def test_hub_find_matches_and_disambiguates(tmp_path) -> None:
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "Kitchen light", "kitchen"),
            "matter-2": light("matter-2", "Lamp", "bedroom"),
        }
    )
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    assert hub.find("kitchen light").key == "matter-1"
    assert hub.find("the bedroom lamp").key == "matter-2"
    assert hub.find("matter-2").key == "matter-2"
    with pytest.raises(SmartHomeError, match="ambiguous"):
        hub.find("light")
    with pytest.raises(SmartHomeError, match="No device matches"):
        hub.find("garage fan")


def test_hub_single_device_matches_generic_words(tmp_path) -> None:
    backend = FakeBackend({"matter-1": light("matter-1", "Smart Light Bulb")})
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    assert hub.find("the light").key == "matter-1"


async def test_hub_control_dispatches_to_backend(tmp_path) -> None:
    backend = FakeBackend({"matter-1": light("matter-1", "Kitchen light", "kitchen")})
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    await hub.control("kitchen", power="on", brightness=40, color_temp_kelvin=2700)
    assert backend.calls == [
        {"key": "matter-1", "power": "on", "brightness": 40, "color_temp_kelvin": 2700}
    ]
    with pytest.raises(SmartHomeError, match="Nothing to change"):
        await hub.control("kitchen")


def test_hub_rename_persists_alias(tmp_path) -> None:
    database = make_database(tmp_path)
    backend = FakeBackend({"matter-1": light("matter-1", "Smart Light Bulb")})
    hub = SmartHomeHub(AppConfig(), database, backends=[backend])
    hub.rename("smart light", name="Desk lamp", room="office")
    # A fresh hub over the same database sees the alias.
    fresh = SmartHomeHub(
        AppConfig(),
        database,
        backends=[FakeBackend({"matter-1": light("matter-1", "Smart Light Bulb")})],
    )
    found = fresh.find("desk lamp")
    assert found.name == "Desk lamp" and found.room == "office"


async def test_device_tools_flow(tmp_path) -> None:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    backend = FakeBackend({"matter-1": light("matter-1", "Kitchen light", "kitchen")})
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])

    listed = await tools.execute("device_list", {})
    assert listed["devices"][0]["name"] == "Kitchen light"
    assert listed["backends"]["matter"]["connected"] is True

    result = await tools.execute("device_set", {"device": "kitchen", "power": "on"})
    assert result["ok"] is True and backend.calls[0]["power"] == "on"

    result = await tools.execute("device_set", {"device": "garage", "power": "on"})
    assert result["ok"] is False and "No device matches" in result["error"]

    paired = await tools.execute("device_pair", {"network": "matter", "code": "1234"})
    assert paired["ok"] is True and paired["paired"]["id"] == "matter-9"

    renamed = await tools.execute(
        "device_rename", {"device": "matter-9", "name": "Hall light", "room": "hall"}
    )
    assert renamed["ok"] is True and renamed["device"]["room"] == "hall"


async def test_pair_needs_code_for_matter(tmp_path) -> None:
    hub = SmartHomeHub(
        AppConfig(), make_database(tmp_path), backends=[FakeBackend({})]
    )
    with pytest.raises(SmartHomeError, match="No pairing code was given"):
        await hub.pair("matter", None)


async def test_matter_commission_rejects_wrong_length_code() -> None:
    backend = MatterBackend(MatterConfig())
    backend.connected = True
    with pytest.raises(SmartHomeError, match="10 digits"):
        await backend.commission("3635881226")


async def test_matter_commission_same_code_twice_says_already_paired() -> None:
    import time as _time

    backend = MatterBackend(MatterConfig())
    backend.connected = True
    backend._last_commissioned = ("36358812226", _time.monotonic(), "Smart Bulb")
    with pytest.raises(SmartHomeError, match="ALREADY PAIRED"):
        await backend.commission("3635-881-2226")


async def test_matter_commission_tries_bluetooth_then_falls_back_to_network() -> None:
    """With an adapter, the first try searches the air and the network; if
    it fails (adapter off, BLE stack unhappy) the network alone is tried."""
    backend = MatterBackend(MatterConfig(bluetooth=True))
    backend.connected = True
    sent: list[dict] = []

    async def fake_command(command, args=None, timeout=None):
        sent.append(dict(args or {}))
        if len(sent) == 1:
            raise SmartHomeError("BLE connection failed")
        return MATTER_NODE

    backend._command = fake_command  # type: ignore[method-assign]
    device = await backend.commission("36358812226")
    assert device.key == "matter-5"
    assert [item["network_only"] for item in sent] == [False, True]

    # Both paths failing: one message that covers both, and no third try.
    sent.clear()
    backend._last_commissioned = None

    async def always_fails(command, args=None, timeout=None):
        sent.append(dict(args or {}))
        raise SmartHomeError("nothing found")

    backend._command = always_fails  # type: ignore[method-assign]
    with pytest.raises(SmartHomeError, match="over Bluetooth and then over the network"):
        await backend.commission("36358812226")
    assert len(sent) == 2


async def test_matter_commission_without_bluetooth_searches_the_network_once() -> None:
    backend = MatterBackend(MatterConfig(bluetooth=False))
    backend.connected = True
    sent: list[dict] = []

    async def fake_command(command, args=None, timeout=None):
        sent.append(dict(args or {}))
        raise SmartHomeError("nothing found")

    backend._command = fake_command  # type: ignore[method-assign]
    with pytest.raises(SmartHomeError, match="vendor app"):
        await backend.commission("36358812226")
    assert [item["network_only"] for item in sent] == [True]


async def test_device_pair_rejects_code_the_user_never_gave(tmp_path) -> None:
    from house_agent.tools import CURRENT_SPEAKER

    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "prov.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.smarthome = SmartHomeHub(config, database, backends=[FakeBackend({})])
    CURRENT_SPEAKER.set({"status": "unknown", "conversation_id": "c9"})
    database.add_message("c9", "user", "yes it has a QR code printed on it")

    # Invented code: rejected before any pairing attempt.
    result = await tools.execute("device_pair", {"network": "matter", "code": "36358812226"})
    assert result["ok"] is False and "NOT given this code" in result["error"]

    # The user reads the code (with pauses/dashes): accepted.
    database.add_message("c9", "user", "the code is 3635 881 2226")
    result = await tools.execute("device_pair", {"network": "matter", "code": "3635-881-2226"})
    assert result["ok"] is True and result["paired"]["id"] == "matter-9"

    # No conversation context (direct API call): trusted.
    CURRENT_SPEAKER.set(None)
    tools.smarthome = SmartHomeHub(config, database, backends=[FakeBackend({})])
    result = await tools.execute("device_pair", {"network": "matter", "code": "99999999999"})
    assert result["ok"] is True


def test_house_state_line_grounds_the_model(tmp_path) -> None:
    from house_agent.llm import LocalAgent

    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "state.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    database.seed_subscriptions([("netflix", "Netflix"), ("hulu", "Hulu")])
    tools = ToolRegistry(config, database)
    tools.smarthome = SmartHomeHub(
        config, database,
        backends=[FakeBackend({"matter-1": light("matter-1", "Bedroom lamp", "bedroom")})],
    )
    agent = LocalAgent(config.llm, database, tools)
    line = agent._house_state()
    assert "Bedroom lamp (bedroom, light)" in line
    assert "netflix" in line and "hulu" in line

    # Backend enabled but down, nothing paired: the model is told plainly.
    down = FakeBackend({})
    down.connected = False
    tools.smarthome = SmartHomeHub(config, database, backends=[down])
    assert "offline" in agent._house_state()


def test_hub_hands_the_bluetooth_adapter_to_the_controller(tmp_path) -> None:
    database = make_database(tmp_path)
    hub = SmartHomeHub(AppConfig(matter=MatterConfig(bluetooth=True, bluetooth_adapter=1)), database)
    assert hub.process is not None
    assert hub.process.env == {"MATTER_BLUETOOTH_ADAPTER": "1"}
    hub = SmartHomeHub(AppConfig(matter=MatterConfig(bluetooth=False)), database)
    assert hub.process is not None and hub.process.env == {}


async def test_removing_a_device_unpairs_it_and_forgets_its_name(tmp_path) -> None:
    """Unpairing on purpose is not the controller losing a device: the name
    goes with it, so nothing is reported missing and a re-pair starts fresh."""
    database = make_database(tmp_path)
    backend = FakeBackend({"matter-17": light("matter-17", "Smart Light Bulb")})
    hub = SmartHomeHub(AppConfig(), database, backends=[backend])
    hub.rename("smart light", name="Bedroom Lamp", room="bedroom")

    removed = await hub.remove("bedroom lamp")
    assert removed.key == "matter-17"
    assert backend.calls == [{"key": "matter-17", "removed": True}]
    assert hub.devices() == [] and hub.missing() == []
    assert database.smart_device_aliases() == {}
    with pytest.raises(SmartHomeError, match="No smart devices are set up"):
        hub.find("bedroom lamp")


async def test_matter_remove_sends_remove_node_and_drops_the_device() -> None:
    backend = MatterBackend(MatterConfig())
    backend._absorb_node(MATTER_NODE)
    backend.connected = True
    sent: list[dict] = []

    async def fake_command(command, args=None, timeout=None):
        sent.append({"command": command, "args": args})
        return None

    backend._command = fake_command  # type: ignore[method-assign]
    await backend.remove(backend.devices["matter-5"])
    assert sent == [{"command": "remove_node", "args": {"node_id": 5}}]
    assert backend.devices == {} and 5 not in backend._nodes


async def test_device_remove_tool(tmp_path) -> None:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    backend = FakeBackend({"matter-1": light("matter-1", "Kitchen light", "kitchen")})
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])
    result = await tools.execute("device_remove", {"device": "kitchen light"})
    assert result["ok"] is True and result["removed"]["id"] == "matter-1"
    assert (await tools.execute("device_list", {}))["devices"] == []
    result = await tools.execute("device_remove", {"device": "kitchen light"})
    assert result["ok"] is False and "No smart devices are set up" in result["error"]


def test_a_device_the_controller_forgot_is_reported_missing(tmp_path) -> None:
    """A paired device does not just vanish: if the controller stops listing a
    device the house has named, say so by name instead of 'nothing is paired'."""
    database = make_database(tmp_path)
    backend = FakeBackend({"matter-17": light("matter-17", "Smart Light Bulb")})
    hub = SmartHomeHub(AppConfig(), database, backends=[backend])
    hub.rename("smart light", name="Bedroom Lamp", room="bedroom")
    assert hub.missing() == []

    # The controller lost its pairing list (what happened on 2026-08-30).
    backend.devices.clear()
    missing = hub.missing()
    assert [item["name"] for item in missing] == ["Bedroom Lamp"]
    assert missing[0]["id"] == "matter-17" and "pairing record" in missing[0]["problem"]
    with pytest.raises(SmartHomeError, match="lost the pairing record for Bedroom Lamp"):
        hub.find("bedroom lamp")

    # A controller that is simply down is a different problem, not a lost device.
    backend.connected = False
    assert hub.missing() == []


async def test_hub_flash_blinks_and_restores(tmp_path, monkeypatch) -> None:
    """flash() identifies a light by toggling power (some firmwares, e.g.
    Third Reality, time out on the standard identify effect) and puts the
    light back how it found it."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("house_agent.smarthome.asyncio.sleep", no_sleep)
    backend = FakeBackend({"matter-1": light("matter-1", "Kitchen light", "kitchen")})
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    device = await hub.flash("kitchen", times=2)
    assert device.key == "matter-1"
    powers = [call["power"] for call in backend.calls]
    # light() starts powered off, so the blink ends with a restore to off.
    assert powers == ["off", "on", "off", "on", "off"]


async def test_hub_control_room_sets_every_light(tmp_path) -> None:
    """"Turn on the bedroom lights" must act on all of them, not ask which."""
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "bedroom lamp", "bedroom"),
            "matter-2": light("matter-2", "bedroom light", "bedroom"),
        }
    )
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    devices, failures = await hub.control("bedroom", power="on")
    assert failures == []
    assert sorted(d.key for d in devices) == ["matter-1", "matter-2"]
    assert sorted(call["key"] for call in backend.calls) == ["matter-1", "matter-2"]


async def test_hub_control_exact_name_beats_room_tie(tmp_path) -> None:
    """"bedroom lamp" names one device exactly, even though the fuzzy corpus
    ties it with "bedroom light" (kind padding makes lamp/light synonyms)."""
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "bedroom lamp", "bedroom"),
            "matter-2": light("matter-2", "bedroom light", "bedroom"),
        }
    )
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    devices, failures = await hub.control("bedroom lamp", power="on")
    assert failures == []
    assert [d.key for d in devices] == ["matter-1"]
    assert [call["key"] for call in backend.calls] == ["matter-1"]


async def test_hub_control_cross_room_tie_stays_ambiguous(tmp_path) -> None:
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "Kitchen light", "kitchen"),
            "matter-2": light("matter-2", "Bedroom light", "bedroom"),
        }
    )
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    with pytest.raises(SmartHomeError, match="ambiguous"):
        await hub.control("light", power="on")
    assert backend.calls == []


async def test_hub_control_survives_one_dead_device(tmp_path) -> None:
    """An unreachable lamp must not stop the rest of the room turning off."""

    class FlakyBackend(FakeBackend):
        async def set_state(self, device, **kwargs):
            if device.key == "matter-1":
                raise SmartHomeError("Node 19 is not (yet) available.")
            await super().set_state(device, **kwargs)

    backend = FlakyBackend(
        {
            "matter-1": light("matter-1", "bedroom lamp", "bedroom"),
            "matter-2": light("matter-2", "bedroom light", "bedroom"),
        }
    )
    hub = SmartHomeHub(AppConfig(), make_database(tmp_path), backends=[backend])
    devices, failures = await hub.control("bedroom", power="off")
    assert [d.key for d in devices] == ["matter-2"]
    assert len(failures) == 1 and "bedroom lamp" in failures[0]
    # All devices dead -> the whole call fails loudly.
    backend.devices.pop("matter-2")
    with pytest.raises(SmartHomeError, match="not .yet. available"):
        await hub.control("bedroom lamp", power="off")


def test_hex_to_hue_sat_corners() -> None:
    from house_agent.smarthome import _color_to_hex, _hex_to_hue_sat

    assert _hex_to_hue_sat("#ff0000") == (0, 254)  # red: hue 0, full sat
    assert _hex_to_hue_sat("#ffffff")[1] == 0  # white: no saturation
    assert _hex_to_hue_sat(_color_to_hex("blue")) == (round(2 / 3 * 254), 254)
    with pytest.raises(SmartHomeError, match="Unknown color"):
        _color_to_hex("chartreuse-ish")


async def test_rename_conflict_asks_before_stealing_a_name(tmp_path) -> None:
    database = make_database(tmp_path)
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "Smart Bulb"),
            "matter-2": light("matter-2", "Smart Bulb 2"),
        }
    )
    hub = SmartHomeHub(AppConfig(), database, backends=[backend])
    hub.rename("matter-1", "bedroom lamp", "bedroom")
    # Same name for a different device: refused with guidance, nothing saved.
    with pytest.raises(SmartHomeError, match="already a device called 'bedroom lamp'"):
        hub.rename("matter-2", "Bedroom Lamp", "bedroom")
    assert database.smart_device_aliases().get("matter-2") is None
    # With replace: the newcomer takes the name, the old holder loses it.
    hub.rename("matter-2", "Bedroom Lamp", "bedroom", replace=True)
    aliases = database.smart_device_aliases()
    assert aliases["matter-2"]["name"] == "Bedroom Lamp"
    assert aliases.get("matter-1") is None
    # Renaming a device to a name nobody holds never asks.
    hub.rename("matter-1", "desk light", None)


async def test_zigbee_setup_pending_walks_one_at_a_time(tmp_path) -> None:
    """Unnamed zigbee lights are surfaced with step-by-step guidance:
    device_list counts them, device_flash says ask-did-you-see-it, and
    device_rename hands the model the next light's id."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    a, b = light("zigbee-0xaaa", "0xaaa"), light("zigbee-0xbbb", "0xbbb")
    a.backend = b.backend = "zigbee"
    backend = FakeBackend({a.key: a, b.key: b})
    backend.name = "zigbee"
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])

    listed = await tools.execute("device_list", {})
    assert len(listed["setup_pending"]) == 2
    assert "2 zigbee light(s)" in listed["setup_note"]
    assert "'zigbee-0xaaa'" in listed["setup_note"]  # a literal id, not a hint

    flashed = await tools.execute("device_flash", {"device": "zigbee-0xaaa"})
    assert "where it is" in flashed["note"]
    assert "'zigbee-0xbbb'" in flashed["note"]  # the fallback id is literal too
    assert "1 more light" in flashed["note"]

    renamed = await tools.execute(
        "device_rename", {"device": "zigbee-0xaaa", "name": "kitchen one", "room": "kitchen"}
    )
    assert "zigbee-0xbbb" in renamed["note"]  # the model is told what's next

    renamed = await tools.execute(
        "device_rename", {"device": "zigbee-0xbbb", "name": "kitchen two", "room": "kitchen"}
    )
    assert "note" not in renamed  # loop complete, no further nudge
    listed = await tools.execute("device_list", {})
    assert "setup_pending" not in listed


async def test_flash_placeholder_resolves_to_first_unnamed(tmp_path) -> None:
    """'setup_pending' or 'next' as a device means the first unnamed zigbee
    light — small models pass placeholders, the tool absorbs them."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    a = light("zigbee-0xaaa", "0xaaa")
    a.backend = "zigbee"
    backend = FakeBackend({a.key: a})
    backend.name = "zigbee"
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])

    flashed = await tools.execute("device_flash", {"device": "setup_pending"})
    assert flashed["ok"] is True and flashed["device"]["id"] == "zigbee-0xaaa"
    flashed = await tools.execute("device_flash", {"device": "next"})
    assert flashed["ok"] is True

    await tools.execute(
        "device_rename", {"device": "zigbee-0xaaa", "name": "porch", "room": "porch"}
    )
    flashed = await tools.execute("device_flash", {"device": "next"})
    assert flashed["ok"] is False and "No unnamed zigbee lights" in flashed["error"]


async def test_zigbee_pair_does_not_reopen_in_same_conversation(tmp_path) -> None:
    """A second device_pair call in one conversation gets redirected to
    device_list instead of fresh put-it-in-pairing-mode instructions — the
    exact loop small models fall into."""
    from house_agent.tools import CURRENT_SPEAKER

    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)

    class JoinBackend(FakeBackend):
        name = "zigbee"

        async def permit_join(self, seconds: int = 180) -> None:
            self.calls.append({"permit_join": seconds})

    backend = JoinBackend({})
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])

    token = CURRENT_SPEAKER.set({"conversation_id": "conv-1"})
    try:
        first = await tools.execute("device_pair", {"network": "zigbee"})
        assert first["ok"] is True and "3 minutes" in first["note"]
        assert "watched" in first["note"]  # the background watcher is on
        assert backend.calls == [{"permit_join": 180}]

        second = await tools.execute("device_pair", {"network": "zigbee"})
        assert second["ok"] is True
        # Ground truth, not instructions: nothing has joined, so the model
        # is told exactly that and cannot invent a found light.
        assert "ALREADY open" in second["note"]
        assert "NO new light has joined" in second["note"]
        assert backend.calls == [{"permit_join": 180}]  # no second window
    finally:
        CURRENT_SPEAKER.reset(token)

    # A different conversation opens its own window normally.
    token = CURRENT_SPEAKER.set({"conversation_id": "conv-2"})
    try:
        again = await tools.execute("device_pair", {"network": "zigbee"})
        assert again["ok"] is True and "3 minutes" in again["note"]
    finally:
        CURRENT_SPEAKER.reset(token)
        await tools.close()  # stop the join watchers this test started


def zigbee_tools(tmp_path):
    """A ToolRegistry over a fake zigbee backend, ready for setup-flow tests."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)

    class JoinBackend(FakeBackend):
        name = "zigbee"

        async def permit_join(self, seconds: int = 180) -> None:
            self.calls.append({"permit_join": seconds})

    backend = JoinBackend({})
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])
    return tools, backend, database


async def test_rename_resolves_placeholders_to_the_flashed_light(tmp_path) -> None:
    """After a flash, the user answers with a room and a name. Whatever the
    model passes as the device — 'setup_pending', 'next', 'it', or the very
    name it is about to give — means the light that was just flashed."""
    from house_agent.tools import CURRENT_SPEAKER

    tools, backend, database = zigbee_tools(tmp_path)
    a, b = light("zigbee-0xaaa", "0xaaa"), light("zigbee-0xbbb", "0xbbb")
    a.backend = b.backend = "zigbee"
    backend.devices = {a.key: a, b.key: b}

    token = CURRENT_SPEAKER.set({"conversation_id": "conv-r"})
    try:
        await tools.execute("device_flash", {"device": "zigbee-0xbbb"})
        # The 2026-09-03 sessions verbatim: rename with a placeholder, and
        # rename with the room name the user just said.
        renamed = await tools.execute(
            "device_rename",
            {"device": "setup_pending", "name": "bathroom one", "room": "bathroom"},
        )
        assert renamed["ok"] is True and renamed["device"]["id"] == "zigbee-0xbbb"

        await tools.execute("device_flash", {"device": "zigbee-0xaaa"})
        renamed = await tools.execute(
            "device_rename",
            {"device": "kitchen", "name": "kitchen light one", "room": "kitchen"},
        )
        assert renamed["ok"] is True and renamed["device"]["id"] == "zigbee-0xaaa"
    finally:
        CURRENT_SPEAKER.reset(token)


async def test_rename_placeholder_without_flash_takes_first_pending(tmp_path) -> None:
    tools, backend, database = zigbee_tools(tmp_path)
    a = light("zigbee-0xaaa", "0xaaa")
    a.backend = "zigbee"
    backend.devices = {a.key: a}
    renamed = await tools.execute(
        "device_rename", {"device": "next", "name": "porch", "room": "porch"}
    )
    assert renamed["ok"] is True and renamed["device"]["id"] == "zigbee-0xaaa"


async def test_join_watcher_announces_flashes_and_reopens(tmp_path, monkeypatch) -> None:
    """Opening a zigbee window starts a background watcher: when a light
    joins it is announced and flashed with no user turn in between, the
    announcement lands in conversation history, and the spent window can be
    reopened afterwards."""
    from house_agent.tools import CURRENT_SPEAKER

    # Scale the flash's blink pauses down rather than nulling them: asyncio
    # is one shared module, so a sleep that never yields would starve every
    # other task, the watcher included.
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(seconds / 200)

    monkeypatch.setattr("house_agent.smarthome.asyncio.sleep", fast_sleep)
    tools, backend, database = zigbee_tools(tmp_path)
    tools.join_watch_poll = 0.005
    tools.join_watch_seconds = 1.0

    published: list[str] = []

    class FakeAnnouncer:
        def publish(self, text: str, **extra) -> None:
            published.append(text)

    tools.announcer = FakeAnnouncer()

    token = CURRENT_SPEAKER.set({"conversation_id": "conv-w"})
    try:
        result = await tools.execute("device_pair", {"network": "zigbee"})
        assert "watched" in result["note"]

        await asyncio.sleep(0.05)  # window open, nothing joined yet
        assert published == []

        joined = light("zigbee-0xnew", "0xnew")
        joined.backend = "zigbee"
        backend.devices[joined.key] = joined

        for _ in range(200):
            await asyncio.sleep(0.01)
            if published:
                break
        assert published and "just joined" in published[0]
        # The join was flashed without any user turn.
        assert any(call.get("power") == "off" for call in backend.calls)
        # The announcement is in history, so the model's next turn knows.
        messages = database.get_messages("conv-w")
        assert any("just joined" in m["content"] for m in messages)
        # The flashed light is what a follow-up rename resolves to.
        renamed = await tools.execute(
            "device_rename", {"device": "the flashed light", "name": "den", "room": "den"}
        )
        assert renamed["ok"] is True and renamed["device"]["id"] == "zigbee-0xnew"

        # Once the watcher ends, the window is spent and can truly reopen.
        watcher = tools._join_watchers.get("conv-w")  # noqa: SLF001
        if watcher is not None:
            await watcher
        assert "conv-w" not in tools._zigbee_windows
        again = await tools.execute("device_pair", {"network": "zigbee"})
        assert "3 minutes" in again["note"]
    finally:
        CURRENT_SPEAKER.reset(token)
        await tools.close()


async def test_join_watcher_reports_an_empty_window(tmp_path) -> None:
    """If the window closes with nothing joined, the user hears the factory
    reset instructions unprompted instead of silence."""
    from house_agent.tools import CURRENT_SPEAKER

    tools, backend, database = zigbee_tools(tmp_path)
    tools.join_watch_poll = 0.01
    tools.join_watch_seconds = 0.05

    token = CURRENT_SPEAKER.set({"conversation_id": "conv-e"})
    try:
        await tools.execute("device_pair", {"network": "zigbee"})
        watcher = tools._join_watchers.get("conv-e")  # noqa: SLF001
        assert watcher is not None
        await watcher
        messages = database.get_messages("conv-e")
        assert any("no light joined" in m["content"] for m in messages)
    finally:
        CURRENT_SPEAKER.reset(token)
        await tools.close()


def test_zigbee_join_events_are_remembered() -> None:
    """A factory-reset light rejoins under its old address: the device list
    shows nothing new, so the bridge's join event is the only tell."""
    backend = ZigbeeBackend(ZigbeeConfig(enabled=True))
    backend._handle(
        "zigbee2mqtt/bridge/event",
        json.dumps(
            {"type": "device_joined", "data": {"friendly_name": "0xabc", "ieee_address": "0xabc"}}
        ).encode(),
    )
    assert "zigbee-0xabc" in backend.recent_joins
    backend._handle("zigbee2mqtt/bridge/event", b"not json")  # ignored, no crash


async def test_join_watcher_catches_a_named_light_rejoining(tmp_path, monkeypatch) -> None:
    """Session 1c7065d1 (z2m log 18:52): the user factory-reset a light this
    house already knew. It rejoined under its old address and old name, the
    device list showed nothing new, and Friday announced that nothing had
    joined. The network's join event is the tell: say so, flash it, and hand
    the model the id so the user's next words rename it."""
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(seconds / 200)

    monkeypatch.setattr("house_agent.smarthome.asyncio.sleep", fast_sleep)
    tools, backend, database = zigbee_tools(tmp_path)
    tools.join_watch_poll = 0.005
    tools.join_watch_seconds = 1.0
    known = light("zigbee-0xold", "0xold")
    known.backend = "zigbee"
    backend.devices = {known.key: known}
    backend.recent_joins = {}
    database.save_smart_device_alias(known.key, "zigbee", "bathroom light two", "bathroom")

    published: list[str] = []

    class FakeAnnouncer:
        def publish(self, text: str, **extra) -> None:
            published.append(text)

    tools.announcer = FakeAnnouncer()
    token = CURRENT_SPEAKER.set({"conversation_id": "conv-j"})
    try:
        await tools.execute("device_pair", {"network": "zigbee"})
        await asyncio.sleep(0.05)
        assert published == []  # a known light sitting there is not news

        backend.recent_joins[known.key] = time.monotonic()  # ...but rejoining is
        for _ in range(400):
            await asyncio.sleep(0.01)
            if published:
                break
        assert published and "already paired here as 'bathroom light two'" in published[0]
        assert any(call.get("power") == "off" for call in backend.calls)  # flashed
        messages = database.get_messages("conv-j")
        assert any(
            m["role"] == "system" and "zigbee-0xold" in m["content"] for m in messages
        ), "the model must be told which device id just reconnected"
        # The user's next words name that light, however the model refers to it.
        renamed = await tools.execute(
            "device_rename", {"device": "it", "name": "hall light", "room": "hall"}
        )
        assert renamed["ok"] is True and renamed["device"]["id"] == "zigbee-0xold"
    finally:
        CURRENT_SPEAKER.reset(token)
        await tools.close()


async def test_flash_placeholder_during_open_window_says_wait(tmp_path) -> None:
    """Session 1c7065d1 #787: told 'they are in pairing mode', the model
    called device_flash before anything joined. The error must say the
    window is watched and to wait — not send it off to device_list to
    hallucinate from."""
    tools, backend, database = zigbee_tools(tmp_path)
    tools.join_watch_poll = 0.01
    tools.join_watch_seconds = 0.5
    token = CURRENT_SPEAKER.set({"conversation_id": "conv-f"})
    try:
        await tools.execute("device_pair", {"network": "zigbee"})
        flashed = await tools.execute("device_flash", {"device": "last flashed"})
        assert flashed["ok"] is False
        assert "still open" in flashed["error"] and "do not call device_flash again" in flashed["error"]
    finally:
        CURRENT_SPEAKER.reset(token)
        await tools.close()


async def test_flash_next_moves_past_the_light_just_flashed(tmp_path, monkeypatch) -> None:
    """'Flash the next one' means a different bulb than the one just
    flashed; 'it' means the same one again."""
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(seconds / 200)

    monkeypatch.setattr("house_agent.smarthome.asyncio.sleep", fast_sleep)
    tools, backend, database = zigbee_tools(tmp_path)
    a, b = light("zigbee-0xaaa", "0xaaa"), light("zigbee-0xbbb", "0xbbb")
    a.backend = b.backend = "zigbee"
    backend.devices = {a.key: a, b.key: b}
    token = CURRENT_SPEAKER.set({"conversation_id": "conv-n"})
    try:
        first = await tools.execute("device_flash", {"device": "next"})
        assert first["device"]["id"] == "zigbee-0xaaa"
        again = await tools.execute("device_flash", {"device": "next"})
        assert again["device"]["id"] == "zigbee-0xbbb"
        same = await tools.execute("device_flash", {"device": "it"})
        assert same["device"]["id"] == "zigbee-0xbbb"
    finally:
        CURRENT_SPEAKER.reset(token)


async def test_rename_reports_a_missing_room(tmp_path) -> None:
    """Session f32b495b #780: 'that one is in the kitchen, call it kitchen
    light one' was saved with the name and no room. The result now says so,
    so the model fixes it in the same turn."""
    tools, backend, database = zigbee_tools(tmp_path)
    a = light("zigbee-0xaaa", "0xaaa")
    a.backend = "zigbee"
    backend.devices = {a.key: a}
    renamed = await tools.execute(
        "device_rename", {"device": "zigbee-0xaaa", "name": "kitchen light one"}
    )
    assert renamed["ok"] is True and "NO room" in renamed["room_note"]
    renamed = await tools.execute(
        "device_rename", {"device": "zigbee-0xaaa", "room": "kitchen"}
    )
    assert "room_note" not in renamed


def _house(tmp_path):
    """Two bedroom lights and a kitchen light, as in the real house."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    backend = FakeBackend(
        {
            "matter-1": light("matter-1", "bedroom lamp", "bedroom"),
            "matter-2": light("matter-2", "bedroom light", "bedroom"),
            "matter-3": light("matter-3", "kitchen light one", "kitchen"),
        }
    )
    tools.smarthome = SmartHomeHub(config, database, backends=[backend])
    return tools, backend


async def test_all_the_lights_means_all_of_them_whatever_the_model_passes(tmp_path) -> None:
    """The exact failure: 'Turn off all the lights' ran device_set with
    device='bedroom', left the kitchen on, and the reply claimed the other
    bedroom light was still on."""
    from house_agent.tools import CURRENT_REQUEST

    tools, backend = _house(tmp_path)
    CURRENT_REQUEST.set("Turn off all the lights")
    result = await tools.execute("device_set", {"device": "bedroom", "power": "off"})
    assert {call["key"] for call in backend.calls} == {"matter-1", "matter-2", "matter-3"}
    assert result["summary"].startswith("Turned off: bedroom lamp, bedroom light and kitchen light one.")
    assert "all 3" in result["summary"]

    # A room named in the request keeps its scope.
    backend.calls.clear()
    CURRENT_REQUEST.set("turn off all the lights in the kitchen")
    await tools.execute("device_set", {"device": "kitchen", "power": "off"})
    assert [call["key"] for call in backend.calls] == ["matter-3"]
    CURRENT_REQUEST.set(None)


async def test_a_room_schedule_fades_every_light_in_it(tmp_path) -> None:
    """'bedroom' used to come back as ambiguous (lamp or light?) and the
    automation was never saved."""
    from house_agent import tools as tools_module

    tools, backend = _house(tmp_path)
    CURRENT_SPEAKER.set(None)
    tools.fades = tools_module.FadeRunner(tools.smarthome)
    result = await tools.execute(
        "light_schedule_add",
        {"name": "bedroom night dim", "device": "bedroom", "when": "9:05pm", "brightness_to": 0, "minutes": 5},
    )
    assert result["ok"] is True
    assert result["device"] == "bedroom lamp and bedroom light"
    assert result["action"] == "fade bedroom lamp and bedroom light from current level to 0% over 5 minutes"
    stored = tools.database.list_automations()[0]
    assert stored["action"]["devices"] == ["matter-1", "matter-2"]


async def test_zigbee_reports_the_state_it_was_just_sent() -> None:
    backend = ZigbeeBackend(ZigbeeConfig(enabled=True))
    backend._handle("zigbee2mqtt/bridge/devices", json.dumps(Z2M_DEVICES).encode())
    device = backend.devices["zigbee-0xabc"]
    device.state = {"power": True, "brightness": 80}
    published = []

    async def publish(topic, payload):
        published.append((topic, payload))

    backend._publish = publish
    await backend.set_state(device, power="off")
    assert published == [("hall light/set", {"state": "OFF"})]
    assert device.state["power"] is False
