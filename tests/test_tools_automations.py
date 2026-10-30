from datetime import timedelta

import numpy as np

from house_agent import tools as tools_module
from house_agent.config import AppConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.scheduler import local_now
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry
from house_agent.voice import VoiceIdentity

from test_scheduler import FakeHub


def registry(tmp_path) -> ToolRegistry:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        voice=VoiceConfig(),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return tools


def test_new_tools_are_advertised(tmp_path) -> None:
    names = {schema["function"]["name"] for schema in registry(tmp_path).schemas}
    assert {
        "reminder_add", "light_schedule_add", "light_fade", "automation_list",
        "automation_delete", "list_add", "list_show", "list_remove", "list_clear",
        "recipe_search", "recipe_get", "recipe_save", "recipe_list", "recipe_forget",
    } <= names


async def test_reminder_for_the_speaker(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    tomorrow = (local_now() + timedelta(days=1)).strftime("%Y-%m-%d 08:00")
    result = await tools.execute("reminder_add", {"text": "Call your grandma", "when": tomorrow})
    assert result["ok"] is True
    assert result["action"] == "say: Call your grandma"
    assert "8:00 AM" in result["next_run"]
    listed = await tools.execute("automation_list", {})
    assert listed["automations"][0]["for"] == "Alex"
    assert listed["automations"][0]["name"] == "reminder: Call your grandma"

    past = (local_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    result = await tools.execute("reminder_add", {"text": "Too late", "when": past})
    assert result["ok"] is False and "already passed" in result["error"]

    result = await tools.execute(
        "reminder_add",
        {"text": "Is rain expected between 2 and 3pm today?", "when": "13:00", "repeat": "weekdays", "kind": "ask"},
    )
    assert result["ok"] and result["action"].startswith("check and report")
    assert result["schedule"] == "weekdays at 1:00 PM"

    deleted = await tools.execute("automation_delete", {"id": listed["automations"][0]["id"]})
    assert deleted == {"ok": True, "deleted": "reminder: Call your grandma"}
    assert (await tools.execute("automation_delete", {"id": 999}))["ok"] is False


async def test_light_schedule_resolves_the_device_up_front(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)
    hub = FakeHub()
    tools.smarthome = hub
    tools.fades = tools_module.FadeRunner(hub)
    result = await tools.execute(
        "light_schedule_add",
        {
            "name": "bedroom sunrise", "device": "bedroom", "when": "07:30",
            "brightness_from": 0, "brightness_to": 100, "minutes": 20,
            "color_temp_from": 2200, "color_temp_to": 4000,
        },
    )
    assert result["ok"] is True and result["device"] == "Bedroom lamp"
    assert result["schedule"] == "daily at 7:30 AM"
    assert result["action"] == "fade Bedroom lamp from 0% to 100% over 20 minutes"
    stored = tools.database.list_automations()[0]
    assert stored["action"]["device"] == "matter-1" and stored["action"]["color_temp_to"] == 4000

    missing = await tools.execute(
        "light_schedule_add", {"name": "x", "device": "garage", "when": "07:30", "brightness_to": 0}
    )
    assert missing["ok"] is False and "No device" in missing["error"]

    fade = await tools.execute("light_fade", {"device": "bedroom", "brightness_to": 0, "minutes": 0})
    assert fade["ok"] is True and fade["brightness_from"] == 40
    await tools.fades.stop()


async def test_light_schedule_takes_the_time_as_spoken(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)
    hub = FakeHub()
    tools.smarthome = hub
    tools.fades = tools_module.FadeRunner(hub)
    result = await tools.execute(
        "light_schedule_add",
        {"name": "bedroom night dim", "device": "bedroom", "when": "6:20pm", "brightness_to": 0, "minutes": 30},
    )
    assert result["ok"] is True and result["schedule"] == "daily at 6:20 PM"
    result = await tools.execute(
        "light_schedule_add", {"name": "lunch", "device": "bedroom", "when": "noon", "brightness_to": 100}
    )
    assert result["ok"] is True and result["schedule"] == "daily at 12:00 PM"


async def test_a_time_the_user_never_said_is_refused(tmp_path) -> None:
    """The exact failure: 'starting at 6:20pm' became when='22:20'."""
    tools = registry(tmp_path)
    hub = FakeHub()
    tools.smarthome = hub
    tools.fades = tools_module.FadeRunner(hub)
    CURRENT_SPEAKER.set({"status": "unknown", "conversation_id": "c7"})
    tools.database.add_message("c7", "user", "dim the light till it's off starting at 6:20pm every day")
    args = {"name": "bedroom night dim", "device": "bedroom", "when": "22:20", "brightness_to": 0, "minutes": 30}
    result = await tools.execute("light_schedule_add", args)
    assert result["ok"] is False
    assert "'6:20pm' is 18:20" in result["error"] and tools.database.list_automations() == []

    # The time as said, or its 24-hour form, is accepted.
    result = await tools.execute("light_schedule_add", {**args, "when": "18:20"})
    assert result["ok"] is True and result["schedule"] == "daily at 6:20 PM"

    # Reminders get the same check; 'tomorrow morning' with no clock time is not policed.
    result = await tools.execute("reminder_add", {"text": "call grandma", "when": "2030-01-01T08:00"})
    assert result["ok"] is False and "did not say that time" in result["error"]
    tools.database.add_message("c7", "user", "remind me at 8am to call grandma")
    result = await tools.execute("reminder_add", {"text": "call grandma", "when": "2030-01-01T08:00"})
    assert result["ok"] is True
    CURRENT_SPEAKER.set(None)


async def test_bedtime_gets_a_sensible_time_despite_other_times_in_the_chat(tmp_path) -> None:
    tools = registry(tmp_path)
    hub = FakeHub()
    tools.smarthome = hub
    tools.fades = tools_module.FadeRunner(hub)
    CURRENT_SPEAKER.set({"status": "unknown", "conversation_id": "c8"})
    tools.database.add_message("c8", "user", "my meeting is at 2pm tomorrow")
    tools.database.add_message("c8", "assistant", "Noted.")
    tools.database.add_message("c8", "user", "dim the light every day at bedtime")
    result = await tools.execute(
        "light_schedule_add", {"name": "night dim", "device": "bedroom", "when": "bedtime", "brightness_to": 0, "minutes": 30}
    )
    assert result["ok"] is True and result["schedule"] == "daily at 10:20 PM"
    assert result["assumed"].startswith("bedtime taken as 10:20 PM")
    # The model choosing its own bedtime is fine too: the request named no clock time.
    result = await tools.execute(
        "light_schedule_add", {"name": "night dim", "device": "bedroom", "when": "22:00", "brightness_to": 0}
    )
    assert result["ok"] is True and "assumed" not in result
    CURRENT_SPEAKER.set(None)


async def test_setting_a_schedule_by_the_same_name_replaces_it(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)
    hub = FakeHub()
    tools.smarthome = hub
    tools.fades = tools_module.FadeRunner(hub)
    first = await tools.execute(
        "light_schedule_add", {"name": "Bedroom night dim", "device": "bedroom", "when": "22:20", "brightness_to": 0}
    )
    second = await tools.execute(
        "light_schedule_add", {"name": "bedroom night dim", "device": "bedroom", "when": "6:20pm", "brightness_to": 0}
    )
    assert second["ok"] is True and second["replaced"] == [first["id"]]
    stored = tools.database.list_automations()
    assert len(stored) == 1 and stored[0]["schedule"]["time"] == "18:20"


def test_spoken_times_are_read_from_plain_speech() -> None:
    times = ToolRegistry._spoken_times
    assert times("starting at 6:20pm every day") == {"18:20": "6:20pm"}
    assert times("at 7 am and again at 12 PM, then 12am") == {"07:00": "7 am", "12:00": "12 PM", "00:00": "12am"}
    assert times("meeting 2 p.m. or 14:30") == {"14:00": "2 p.m.", "14:30": "14:30"}
    assert times("noon works, or midnight") == {"12:00": "noon", "00:00": "midnight"}
    assert times("dim it over 30 minutes tomorrow morning") == {}


async def test_lists_add_show_remove_clear(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    result = await tools.execute("list_add", {"items": ["eggs", "Milk", " eggs "]})
    assert result["list"] == "shopping" and result["added"] == ["eggs", "Milk"]
    result = await tools.execute("list_add", {"items": ["eggs"], "list": "Shopping list"})
    assert result["added"] == [] and result["already_there"] == ["eggs"]
    await tools.execute("list_add", {"items": ["call the plumber"], "list": "todo"})
    everything = await tools.execute("list_show", {})
    assert everything == {"lists": {"shopping": ["eggs", "Milk"], "todo": ["call the plumber"]}}
    shown = await tools.execute("list_show", {"list": "shopping"})
    assert shown["items"] == ["eggs", "Milk"]
    removed = await tools.execute("list_remove", {"items": ["milk", "bread"]})
    assert removed["removed"] == ["Milk"] and removed["not_found"] == ["bread"]
    assert removed["remaining"] == ["eggs"]
    assert tools.database.list_items("shopping")[0]["added_by"] == "Alex"
    cleared = await tools.execute("list_clear", {"list": "shopping"})
    assert cleared["cleared"] == 1
    assert (await tools.execute("list_show", {"list": "shopping"}))["items"] == []


async def test_recipe_save_and_list(tmp_path, monkeypatch) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)

    async def fake_get(url):
        return {"title": "Fluffy Pancakes", "url": url, "ingredients": ["flour", "eggs"], "steps": ["mix", "cook"]}

    monkeypatch.setattr(tools_module, "get_recipe", fake_get)
    saved = await tools.execute("recipe_save", {"url": "https://a.example/p"})
    assert saved["ok"] and saved["title"] == "Fluffy Pancakes"
    again = await tools.execute("recipe_save", {"url": "https://a.example/p"})
    assert again["id"] == saved["id"]
    listed = await tools.execute("recipe_list", {"query": "eggs"})
    assert listed["recipes"][0]["ingredient_count"] == 2 and "steps" not in listed["recipes"][0]
    assert (await tools.execute("recipe_list", {"query": "beef"}))["recipes"] == []
    fetched = await tools.execute("recipe_get", {"url": "https://a.example/p"})
    assert fetched["saved"] is True and fetched["steps"] == ["mix", "cook"]
    assert (await tools.execute("recipe_forget", {"id": saved["id"]}))["ok"] is True
    assert (await tools.execute("recipe_forget", {"id": saved["id"]}))["ok"] is False
