"""Settings the house changes about itself on request: TV addresses and
names, and the Wi-Fi handed to new devices — written to the right file,
applied live, and never leaking the password."""

import asyncio

import pytest

from house_agent import tools as tools_module
from house_agent.config import AppConfig, MatterConfig, StorageConfig, TVDeviceConfig, load_config
from house_agent.db import Database
from house_agent.llm import _compact_trace
from house_agent.settings import HouseSettings, SettingsError, clean_host, clean_port
from house_agent.tools import ToolRegistry

CONFIG = '''[server]
host = "0.0.0.0"
port = 8787

[roku.inputs]
xbox = "InputHDMI2"

[[roku.devices]]
id = "living-room"
name = "Living Room TV"
host = "192.168.0.203"   # reserved on the old router
port = 8060

[[tv.devices]]
id = "bedroom"
name = "Bedroom TV"
host = "192.168.0.60"
platform = "lg"

[matter]
enabled = true
'''
ENV = '''# Use the Roku device IP from Settings > Network > About.
ROKU_IP_ADDRESS=192.168.0.203
ROKU_DEVICE_ID=living-room
ROKU_DEVICE_NAME=Living Room TV
ROKU_PORT=8060

LLM_MODEL=house-qwen
HOUSE_WIFI_SSID=OldNet
HOUSE_WIFI_PASSWORD=old secret
'''


def files(tmp_path, env=ENV):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    (tmp_path / ".env").write_text(env)
    return config


def test_hosts_are_cleaned_from_how_people_say_them() -> None:
    assert clean_host("192 dot 168 dot 0 dot 177") == "192.168.0.177"
    assert clean_host(" http://192.168.0.5:8060/ ") == "192.168.0.5"
    assert clean_host("Friday-TV.local.") == "Friday-TV.local"
    assert clean_host("192.168.0.177.") == "192.168.0.177"
    with pytest.raises(SettingsError):
        clean_host("hello world")
    with pytest.raises(SettingsError):
        clean_host("")
    assert clean_port(None) is None and clean_port("8060") == 8060
    with pytest.raises(SettingsError):
        clean_port(70000)


def test_env_lines_are_replaced_in_place_and_comments_survive(tmp_path) -> None:
    settings = HouseSettings(files(tmp_path))
    saved = settings.set_env("ROKU_IP_ADDRESS", "192.168.0.177")
    assert saved.previous == "192.168.0.203" and saved.value == "192.168.0.177" and saved.file == ".env"
    text = (tmp_path / ".env").read_text()
    assert text.startswith("# Use the Roku device IP")
    assert "ROKU_IP_ADDRESS=192.168.0.177\n" in text and "192.168.0.203" not in text
    assert "LLM_MODEL=house-qwen" in text
    added = settings.set_env("NEW_KEY", "with space # and hash")
    assert added.previous is None
    assert (tmp_path / ".env").read_text().endswith('NEW_KEY="with space # and hash"\n')
    assert settings.env_value("NEW_KEY") == "with space # and hash"


def test_toml_tv_fields_change_in_the_right_block_only(tmp_path) -> None:
    config = files(tmp_path)
    settings = HouseSettings(config)
    saved = settings.set_tv_field("living-room", "host", "192.168.0.177")
    assert saved.previous == "192.168.0.203" and saved.key == "living-room.host"
    text = config.read_text()
    assert 'host = "192.168.0.177"  # reserved on the old router' in text  # comment kept
    assert 'host = "192.168.0.60"' in text  # the bedroom TV untouched
    assert "[roku.inputs]" in text and 'xbox = "InputHDMI2"' in text
    # A missing key is added inside the block, not at the end of the file.
    settings.set_tv_field("bedroom", "mac", "aa:bb:cc:dd:ee:ff")
    text = config.read_text()
    bedroom = text.index('id = "bedroom"')
    matter = text.index("[matter]")
    assert bedroom < text.index('mac = "aa:bb:cc:dd:ee:ff"') < matter
    assert settings.set_tv_field("garage", "host", "1.2.3.4") is None
    assert load_config(config).tv_devices[0].host == "192.168.0.203"  # .env still overrides at load time


def test_a_tv_address_lands_wherever_the_running_value_came_from(tmp_path) -> None:
    config = files(tmp_path)
    settings = HouseSettings(config)
    assert settings.roku_env_override()["id"] == "living-room"
    saved = settings.save_tv_address("living-room", "192.168.0.177", 8061)
    assert {s.file for s in saved} == {".env", "config.toml"}
    loaded = load_config(config)
    assert loaded.tv_devices[0].host == "192.168.0.177" and loaded.tv_devices[0].port == 8061
    # Without the .env override, only config.toml carries it.
    config2 = files(tmp_path / "two", env="LLM_MODEL=x\n") if (tmp_path / "two").mkdir() is None else None
    settings2 = HouseSettings(config2)
    saved2 = settings2.save_tv_address("bedroom", "192.168.0.61")
    assert [s.file for s in saved2] == ["config.toml"]
    assert {d.id: d.host for d in load_config(config2).tv_devices}["bedroom"] == "192.168.0.61"
    with pytest.raises(SettingsError, match="no saved entry"):
        settings2.save_tv_address("garage", "192.168.0.9")


def test_wifi_is_saved_without_ever_showing_the_password(tmp_path) -> None:
    settings = HouseSettings(files(tmp_path))
    saved = settings.save_wifi("NewNet 5G", "hunter2 rocks")
    assert saved[0].value == "NewNet 5G" and saved[1].value == "•••" and saved[1].previous == "•••"
    loaded = load_config(tmp_path / "config.toml")
    assert loaded.matter.wifi_ssid == "NewNet 5G" and loaded.matter.wifi_password == "hunter2 rocks"
    with pytest.raises(SettingsError):
        settings.save_wifi("", "x")


class FakeHub:
    def __init__(self) -> None:
        self.wifi = None
        self.backends = []

    def devices(self):
        return []

    def status(self):
        return {}

    async def set_wifi(self, ssid, password):
        self.wifi = (ssid, password)
        return True

    def start(self):
        pass

    async def stop(self):
        pass


def registry(tmp_path) -> ToolRegistry:
    config_path = files(tmp_path)
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        tv_devices=(
            TVDeviceConfig(id="living-room", name="Living Room TV", host="192.168.0.203", platform="roku"),
        ),
        matter=MatterConfig(enabled=False, wifi_ssid="OldNet", wifi_password="old secret"),
        config_path=config_path,
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.smarthome = FakeHub()

    async def unreachable(device_id):
        return False

    tools._tv_reachable = unreachable
    return tools


def test_the_tools_are_offered_and_the_prompt_forbids_refusing(tmp_path) -> None:
    from house_agent.config import LLMConfig
    from house_agent.llm import LocalAgent

    tools = registry(tmp_path)
    names = {s["function"]["name"] for s in tools.schemas}
    assert {"house_config_show", "house_config_set"} <= names
    prompt = LocalAgent(LLMConfig(), tools.database, tools).system_prompt
    assert "Never say these are beyond your reach" in prompt
    assert "Never read a Wi-Fi password back" in prompt


async def test_changing_the_tv_address_by_voice_is_saved_and_live(tmp_path) -> None:
    tools = registry(tmp_path)
    result = await tools.execute(
        "house_config_set", {"setting": "tv_address", "device": "living room", "host": "192 dot 168 dot 0 dot 177"}
    )
    assert result["ok"] is True and result["host"] == "192.168.0.177" and result["previous"] == "192.168.0.203"
    assert result["saved_in"] == [".env", "config.toml"] and result["reachable"] is False
    assert "may be off" in result["note"]
    # Live: the next client goes to the new address, playback too, and the
    # loaded configuration reflects it.
    assert tools.devices["living-room"].host == "192.168.0.177"
    assert tools.playback.devices["living-room"].host == "192.168.0.177"
    assert tools.config.tv_devices[0].host == "192.168.0.177"
    assert tools.client_for("living-room").device.host == "192.168.0.177"
    # Durable: a fresh load of the files agrees.
    assert load_config(tmp_path / "config.toml").tv_devices[0].host == "192.168.0.177"
    again = await tools.execute("house_config_set", {"setting": "tv_address", "host": "192.168.0.177"})
    assert again["unchanged"] is True


async def test_with_no_address_the_network_is_searched(tmp_path, monkeypatch) -> None:
    tools = registry(tmp_path)

    async def discover():
        return [
            {"platform": "roku", "host": "192.168.0.177", "port": 8060, "name": "Z tv 55 inch TCL Roku TV"},
            {"platform": "lg", "host": "192.168.0.190", "port": 3000, "name": "LG webOS TV"},
        ]

    monkeypatch.setattr(tools_module, "tv_discover", discover)
    result = await tools.execute("house_config_set", {"setting": "tv_address", "device": "the tv"})
    assert result["ok"] is True and result["discovered"] is True and result["host"] == "192.168.0.177"
    assert tools.devices["living-room"].host == "192.168.0.177"


async def test_ambiguous_or_empty_discovery_asks_instead_of_guessing(tmp_path, monkeypatch) -> None:
    tools = registry(tmp_path)

    async def two_rokus():
        return [
            {"platform": "roku", "host": "192.168.0.177", "port": 8060, "name": "Roku Ultra"},
            {"platform": "roku", "host": "192.168.0.178", "port": 8060, "name": "Roku Express"},
        ]

    monkeypatch.setattr(tools_module, "tv_discover", two_rokus)
    result = await tools.execute("house_config_set", {"setting": "tv_address"})
    assert result["ok"] is False and "Several TVs" in result["error"] and len(result["found"]) == 2
    assert tools.devices["living-room"].host == "192.168.0.203"

    async def nothing():
        return []

    monkeypatch.setattr(tools_module, "tv_discover", nothing)
    result = await tools.execute("house_config_set", {"setting": "tv_address"})
    assert result["ok"] is False and "No roku TV answered" in result["error"]


async def test_renaming_a_tv(tmp_path) -> None:
    tools = registry(tmp_path)
    result = await tools.execute("house_config_set", {"setting": "tv_name", "device": "living-room", "name": "Den TV"})
    assert result["ok"] and result["previous"] == "Living Room TV"
    assert tools.devices["living-room"].name == "Den TV"
    assert load_config(tmp_path / "config.toml").tv_devices[0].name == "Den TV"


async def test_new_wifi_is_saved_handed_to_the_controller_and_kept_out_of_the_trace(tmp_path) -> None:
    tools = registry(tmp_path)
    result = await tools.execute("house_config_set", {"setting": "wifi", "ssid": "NewNet", "password": "s3cret pass"})
    assert result["ok"] is True and result["ssid"] == "NewNet" and result["controller_updated"] is True
    assert "s3cret" not in str(result)
    assert tools.smarthome.wifi == ("NewNet", "s3cret pass")
    assert tools.config.matter.wifi_ssid == "NewNet" and tools.config.matter.wifi_password == "s3cret pass"
    loaded = load_config(tmp_path / "config.toml")
    assert loaded.matter.wifi_ssid == "NewNet" and loaded.matter.wifi_password == "s3cret pass"
    shown = await tools.execute("house_config_show", {})
    assert shown["wifi"] == {"ssid": "NewNet", "password_saved": True}
    assert shown["televisions"][0]["host"] == "192.168.0.203" and "s3cret" not in str(shown)
    trace = _compact_trace([{"tool": "house_config_set", "arguments": {"setting": "wifi", "ssid": "NewNet", "password": "s3cret pass"}, "result": result}])
    assert trace[0]["arguments"] == {"setting": "wifi", "ssid": "NewNet", "password": "•••"}
    assert "s3cret" not in trace[0]["result"]
    half = await tools.execute("house_config_set", {"setting": "wifi", "ssid": "NewNet"})
    assert half["ok"] is False


async def test_the_matter_backend_takes_new_wifi_now_or_at_the_next_connection() -> None:
    from house_agent.smarthome import MatterBackend

    backend = MatterBackend(MatterConfig(enabled=True, wifi_ssid="OldNet", wifi_password="old"))
    sent = []

    async def command(name, payload=None):
        sent.append((name, payload))
        return [] if name == "start_listening" else None

    backend._command = command
    assert await backend.set_wifi("NewNet", "new") is False  # not connected: remembered for later
    assert backend.config.wifi_ssid == "NewNet" and sent == []
    await backend._setup({"wifi_credentials_set": True})  # the controller already had the old one
    assert sent[0] == ("set_wifi_credentials", {"ssid": "NewNet", "credentials": "new"})
    backend.connected = True
    assert await backend.set_wifi("NewerNet", "newer") is True
    assert sent[-1] == ("set_wifi_credentials", {"ssid": "NewerNet", "credentials": "newer"})
