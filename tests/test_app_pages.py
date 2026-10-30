"""The app pages: past chats listed and forgotten, rules and preferences
written down by hand, televisions added and removed from the settings page,
and light colours remembered so the cards can show them."""

import asyncio

import pytest

from house_agent import smarthome
from house_agent.config import AppConfig, MatterConfig, StorageConfig, TVDeviceConfig, ZigbeeConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.settings import HouseSettings, SettingsError
from house_agent.smarthome import Device
from house_agent.tools import ToolError, ToolRegistry

CONFIG = '''[server]
port = 8787

[[roku.devices]]
id = "living-room"
name = "Living Room TV"
host = "192.168.0.203"   # reserved on the old router
port = 8060

[matter]
enabled = false
'''


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "app.db")
    database.initialize()
    return database


# ---------------------------------------------------------------- past chats


def test_past_chats_are_listed_newest_first_with_their_first_question(tmp_path) -> None:
    database = _database(tmp_path)
    database.add_message("older", "user", "turn the lamp on")
    database.add_message("older", "assistant", "Done.", metadata={"trace": [{"tool": "device_set"}]})
    database.add_message("newer", "user", "  what's   on tonight?  ")
    database.add_message("newer", "assistant", "Nothing yet.")

    listed = database.list_conversations()

    assert [item["id"] for item in listed] == ["newer", "older"]
    assert listed[0]["title"] == "what's on tonight?"
    assert listed[0]["messages"] == 2
    messages = database.conversation_messages("older")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["trace"] == [{"tool": "device_set"}]


def test_forgetting_a_chat_takes_its_summary_speaker_and_board_with_it(tmp_path) -> None:
    database = _database(tmp_path)
    database.add_message("gone", "user", "hello")
    database.save_conversation_summary("gone", "said hello", 1)
    database.save_conversation_speaker("gone", "identified", "Alex", 0.9, None, 3.0)
    database.add_inbox_message("Sign in", "steps", "https://example.test", "link", conversation_id="gone")
    database.add_message("kept", "user", "still here")

    assert database.delete_conversation("gone") is True
    assert database.delete_conversation("gone") is False

    assert [item["id"] for item in database.list_conversations()] == ["kept"]
    assert database.conversation_messages("gone") == []
    assert database.get_history("gone")["summary"] is None
    assert database.get_conversation_speaker("gone") is None
    assert database.list_inbox("gone") == []


# ------------------------------------------------------ rules and preferences


def test_notes_of_every_kind_are_listed_counted_and_editable(tmp_path) -> None:
    database = _database(tmp_path)
    rule = database.add_profile_note("Alex", "never order without asking", kind="rule")
    database.add_profile_note("Alex", "keep it short", kind="style")
    database.add_profile_note("Alex", "loves noir", kind="taste")

    kinds = [note["kind"] for note in database.list_notes("Alex")]
    assert kinds == ["rule", "style", "taste"]
    assert database.note_counts()["Alex"] == {"rule": 1, "style": 1, "taste": 1}

    edited = database.update_profile_note(rule["id"], "  always   ask first ")
    assert edited["note"] == "always ask first"
    assert database.update_profile_note(rule["id"], "   ") is None
    assert database.update_profile_note(9999, "nobody") is None
    # Taste notes still stay out of the recommendation-shaping style list...
    assert [n["note"] for n in database.list_profile_notes("Alex")] == ["loves noir"]
    # ...while rules ride along with style preferences into every turn.
    standing = database.list_style_preferences(["Alex"])
    assert [(n["kind"], n["note"]) for n in standing] == [("rule", "always ask first"), ("style", "keep it short")]


def test_rules_reach_the_prompt_as_standing_instructions(tmp_path) -> None:
    database = _database(tmp_path)
    config = AppConfig(storage=StorageConfig(tmp_path / "app.db"), household_profile="default")
    tools = ToolRegistry(config, database)
    agent = LocalAgent(config.llm, database, tools)
    database.add_profile_note("default", "Always confirm before ordering food.", kind="rule")
    database.add_profile_note("Alex", "skip the years", kind="style")

    line = agent._style_line({"status": "identified", "profile": "Alex"})

    assert "Standing rules" in line
    assert "(everyone) Always confirm before ordering food." in line
    assert "(Alex) skip the years" in line
    # A person with only rules and no style notes still gets the rules.
    only_rules = agent._style_line({"status": "anonymous"})
    assert "Standing rules" in only_rules and "How replies should be written" not in only_rules


# ---------------------------------------------------------------- televisions


def test_a_television_can_be_added_and_removed_from_the_config_file(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(CONFIG)
    settings = HouseSettings(tmp_path / "config.toml")

    settings.add_tv({"id": "bedroom", "name": "Bedroom TV", "host": "192.168.0.60", "port": 3000, "platform": "webos"})
    text = (tmp_path / "config.toml").read_text()
    assert '[[tv.devices]]\nid = "bedroom"\nname = "Bedroom TV"\nhost = "192.168.0.60"\nport = 3000\nplatform = "webos"\n' in text
    assert "# reserved on the old router" in text  # the rest of the file is untouched
    with pytest.raises(SettingsError):
        settings.add_tv({"id": "bedroom", "name": "Again", "host": "192.168.0.61"})

    settings.remove_tv("bedroom")
    text = (tmp_path / "config.toml").read_text()
    assert "bedroom" not in text
    assert "[[roku.devices]]" in text
    with pytest.raises(SettingsError):
        settings.remove_tv("bedroom")


def test_adding_a_second_tv_retires_the_env_override_without_losing_the_first(tmp_path) -> None:
    """ROKU_IP_ADDRESS in .env replaces every TV in config.toml at load
    time, so a second TV would never appear while it is set. Adding one
    moves the override's TV into config.toml and comments the override out."""
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text("ROKU_IP_ADDRESS=192.168.0.99\nROKU_DEVICE_ID=living-room\nROKU_DEVICE_NAME=Big TV\nOTHER=1\n")
    settings = HouseSettings(tmp_path / "config.toml")

    settings.add_tv({"id": "den", "name": "Den TV", "host": "192.168.0.70", "port": 8060, "platform": "roku"})

    env = (tmp_path / ".env").read_text()
    assert "# ROKU_IP_ADDRESS=192.168.0.99" in env and "\nOTHER=1\n" in env
    assert settings.roku_env_override() is None
    from house_agent.config import load_config

    loaded = load_config(tmp_path / "config.toml")
    by_id = {device.id: device for device in loaded.tv_devices}
    assert set(by_id) == {"living-room", "den"}
    # The file's stale address was replaced by what .env had been saying.
    assert by_id["living-room"].host == "192.168.0.99"
    assert by_id["living-room"].name == "Big TV"


@pytest.mark.asyncio
async def test_the_registry_applies_a_new_tv_live_and_forgets_a_removed_one(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(CONFIG)
    database = _database(tmp_path)
    config = AppConfig(
        storage=StorageConfig(tmp_path / "app.db"),
        tv_devices=(TVDeviceConfig(id="living-room", name="Living Room TV", host="192.168.0.203"),),
        config_path=tmp_path / "config.toml",
        matter=MatterConfig(enabled=False),
        zigbee=ZigbeeConfig(enabled=False),
    )
    tools = ToolRegistry(config, database)

    async def unreachable(_: str) -> bool:
        return False

    tools._tv_reachable = unreachable
    result = await tools.add_tv("Den TV!", "Den TV", "http://192.168.0.70:8060/", None, "roku")

    assert result["device"]["id"] == "den-tv"
    assert result["device"]["host"] == "192.168.0.70"
    assert "den-tv" in tools.devices
    assert tools.config.tv_devices[-1].id == "den-tv"
    overview = await tools.tv_overview()
    assert [tv["id"] for tv in overview] == ["living-room", "den-tv"]
    assert overview[1]["reachable"] is False

    with pytest.raises(ToolError):
        await tools.add_tv("den-tv", "Twice", "192.168.0.71")
    with pytest.raises(ToolError):
        await tools.add_tv("other", "Same address", "192.168.0.70")

    removed = await tools.remove_tv("den-tv")
    assert removed["removed"] == "den-tv"
    assert "den-tv" not in tools.devices
    assert "den-tv" not in (tmp_path / "config.toml").read_text()
    with pytest.raises(ToolError):
        await tools.remove_tv("den-tv")


# --------------------------------------------------------------- light colour


class FakeBackend:
    name = "zigbee"

    def __init__(self, devices):
        self.enabled = True
        self.connected = True
        self.devices = devices

    async def set_state(self, device, power=None, brightness=None, color_temp_kelvin=None, color=None):
        pass


def test_colour_conversions_give_the_shade_not_the_dimming_level() -> None:
    assert smarthome._hue_sat_to_hex(0, 254) == "#ff0000"
    assert smarthome._hue_sat_to_hex(0, 0) == "#ffffff"
    assert smarthome._hue_sat_to_hex(None, 10) is None
    assert smarthome._xy_to_hex(0.7, 0.3).startswith("#ff")
    assert smarthome._xy_to_hex(0.3127, 0.329)[1:3] >= "f0"  # near white
    assert smarthome._xy_to_hex(45000, 20000) == smarthome._xy_to_hex(45000 / 65535, 20000 / 65535)
    assert smarthome._xy_to_hex(0.1, 0) is None


def test_zigbee_state_reports_carry_the_colour_into_the_device(tmp_path) -> None:
    backend = smarthome.ZigbeeBackend(ZigbeeConfig(enabled=True))
    device = Device(key="zigbee-0x1", backend="zigbee", name="Bedside", kind="light", capabilities=("power", "brightness", "color_temp", "color"))

    backend._absorb_state(device, {"state": "ON", "brightness": 127, "color_mode": "hs", "color": {"hue": 120, "saturation": 100}})
    assert device.state["color"] == "#00ff00"
    backend._absorb_state(device, {"color_mode": "color_temp", "color_temp": 250})
    assert device.state["color"] is None
    backend._absorb_state(device, {"color_mode": "xy", "color": {"x": 0.7, "y": 0.3}})
    assert device.state["color"].startswith("#ff")


@pytest.mark.asyncio
async def test_setting_a_colour_shows_up_in_the_device_list(tmp_path) -> None:
    database = _database(tmp_path)
    backend = FakeBackend({})
    config = AppConfig(storage=StorageConfig(tmp_path / "app.db"), matter=MatterConfig(enabled=False))
    tools = ToolRegistry(config, database)
    tools.smarthome.backends = [backend]
    tools.smarthome.process = None
    device = Device(key="zigbee-0x1", backend="zigbee", name="Bedside", kind="light", capabilities=("power", "brightness", "color_temp", "color"), state={"power": True})
    backend.devices[device.key] = device

    async def set_state(dev, power=None, brightness=None, color_temp_kelvin=None, color=None):
        if color:
            dev.state["color"] = smarthome._color_to_hex(color)
        if color_temp_kelvin:
            dev.state["color"] = None

    backend.set_state = set_state
    result = await tools.execute("device_set", {"device": "bedside", "color": "teal"})
    assert result["device"]["state"]["color"] == "#008080"
    listed = await tools.execute("device_list", {})
    assert listed["devices"][0]["state"]["color"] == "#008080"
    await tools.execute("device_set", {"device": "bedside", "color_temp": 3000})
    listed = await tools.execute("device_list", {})
    assert listed["devices"][0]["state"]["color"] is None
    await asyncio.sleep(0)
