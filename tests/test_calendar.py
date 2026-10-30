from datetime import datetime, timedelta

import httpx
import numpy as np

from house_agent.calendar_feed import render_ics
from house_agent.config import AppConfig, StorageConfig, VoiceConfig, WeatherConfig
from house_agent.db import Database
from house_agent.scheduler import local_now
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry
from house_agent.voice import VoiceIdentity
from house_agent.weather import WeatherService


def registry(tmp_path) -> ToolRegistry:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "cal.db"),
        voice=VoiceConfig(),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return tools


async def test_plan_gets_a_quiet_weather_heads_up(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    start = (local_now() + timedelta(days=1)).replace(hour=16, minute=0)
    result = await tools.execute(
        "calendar_add",
        {"title": "Ana's party", "when": start.strftime("%Y-%m-%d %H:%M"), "location": "the park", "ends": "18:30"},
    )
    assert result["ok"] and result["when"].endswith("4:00 PM") and result["weather_heads_up_at"] == "3:00 PM"
    automations = tools.database.list_automations()
    assert len(automations) == 1
    check = automations[0]
    assert check["name"] == "weather heads-up: Ana's party"
    assert check["action"]["quiet"] is True and check["action"]["event_id"] == result["id"]
    assert "weather_at" in check["action"]["text"] and "Nothing to report" in check["action"]["text"]
    assert check["next_run_at"] == (start - timedelta(hours=1)).isoformat(timespec="minutes")
    assert check["profile"] == "Alex"

    events = await tools.execute("calendar_events", {"days": 2})
    assert events["events"][0]["when"] == "tomorrow at 4:00 PM"
    assert events["events"][0]["location"] == "the park"
    assert events["events"][0]["ends_at"] == start.replace(hour=18, minute=30).isoformat(timespec="minutes")
    single = await tools.execute("calendar_events", {"date": start.strftime("%Y-%m-%d")})
    assert [event["title"] for event in single["events"]] == ["Ana's party"]
    assert (await tools.execute("calendar_events", {"days": 1}))["events"] == []

    # Deleting the plan takes its heads-up with it.
    assert (await tools.execute("calendar_delete", {"id": result["id"]}))["deleted"] == "Ana's party"
    assert tools.database.list_automations() == []
    assert (await tools.execute("calendar_delete", {"id": result["id"]}))["ok"] is False


async def test_indoor_or_imminent_plans_skip_the_heads_up(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)
    soon = local_now() + timedelta(minutes=30)
    result = await tools.execute("calendar_add", {"title": "Picnic", "when": soon.strftime("%Y-%m-%d %H:%M")})
    assert result["ok"] and "weather_heads_up_at" not in result and "Too soon" in result["note"]
    call = await tools.execute(
        "calendar_add", {"title": "Call mom", "when": (local_now() + timedelta(days=1)).strftime("%Y-%m-%d 12:00")}
    )
    assert call["ok"] and "weather_heads_up_at" not in call and "No weather check" in call["note"]
    assert tools.database.list_automations() == []
    forced = await tools.execute(
        "calendar_add",
        {"title": "Call mom", "when": (local_now() + timedelta(days=1)).strftime("%Y-%m-%d 13:00"), "weather_heads_up": True},
    )
    assert forced["weather_heads_up_at"] == "12:00 PM"
    assert len(tools.database.list_automations()) == 1
    later = local_now() + timedelta(days=3)
    result = await tools.execute(
        "calendar_add",
        {"title": "Dentist", "when": later.strftime("%Y-%m-%d 14:00"), "weather_heads_up": False},
    )
    assert result["ok"] and "weather_heads_up_at" not in result
    assert len(tools.database.list_automations()) == 1  # only the forced one
    assert (await tools.execute("calendar_add", {"title": "", "when": "10:00"}))["ok"] is False
    assert "Not a date" in (await tools.execute("calendar_add", {"title": "x", "when": "2026-13-40 10:00"}))["error"]


def test_plans_ride_along_with_the_clock_line(tmp_path) -> None:
    from house_agent.llm import LocalAgent

    tools = registry(tmp_path)
    start = local_now().replace(hour=23, minute=30)
    tools.database.add_event("Concert", start.isoformat(timespec="minutes"), None, "the arena", None, "Alex")
    agent = LocalAgent(tools.config.llm, tools.database, tools)
    line = agent._clock_line()
    assert line.startswith("Now: ")
    assert "Plans: Concert at the arena today 11:30 PM (Alex)." in line


def test_ics_feed_is_well_formed() -> None:
    text = render_ics(
        [
            {"id": 3, "title": "Party; bring cake, candles", "starts_at": "2026-08-30T16:00-06:00",
             "ends_at": None, "location": "The park", "notes": "line one\nline two", "profile": None},
        ]
    )
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR" and lines[-1] == ""
    assert "UID:friday-3@friday.local" in lines
    assert "DTSTART:20260830T220000Z" in lines and "DTEND:20260830T230000Z" in lines
    assert "SUMMARY:Party\; bring cake\\, candles" in lines
    assert "DESCRIPTION:line one\\nline two" in lines
    assert lines.index("BEGIN:VALARM") < lines.index("END:VEVENT") and "TRIGGER:-PT30M" in lines
    assert all(len(line.encode()) <= 75 for line in lines)


def _hourly_payload(times: list[str], codes: list[int], chances: list[int], temps: list[int]) -> dict:
    return {
        "hourly_units": {"temperature_2m": "°F", "wind_speed_10m": "mph", "precipitation": "inch"},
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "apparent_temperature": temps,
            "precipitation_probability": chances,
            "precipitation": [0.0] * len(times),
            "weather_code": codes,
            "wind_speed_10m": [5] * len(times),
            "wind_gusts_10m": [12] * len(times),
        },
        "daily": {"time": []},
    }


async def test_weather_at_picks_the_hour_and_names_concerns() -> None:
    target = (local_now() + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
    day = target.strftime("%Y-%m-%d")
    times = [f"{day}T{hour:02d}:00" for hour in range(24)]
    codes = [0] * 24
    codes[16], codes[17] = 95, 61
    chances = [0] * 24
    chances[17] = 70
    temps = [75] * 24
    requested: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.open-meteo.com":
            requested.append(dict(request.url.params))
            return httpx.Response(200, json=_hourly_payload(times, codes, chances, temps))
        return httpx.Response(200, json={"features": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = WeatherService(WeatherConfig(latitude=1.0, longitude=2.0, place="home"), client=client)
        result = await service.at(f"{day} 16:00")
    assert requested[0]["forecast_days"] == "2" and "precipitation_probability" in requested[0]["hourly"]
    assert result["for"].endswith("16:00")
    assert [hour["time"] for hour in result["hours"]] == times[15:20]
    assert result["hours"][1]["condition"] == "thunderstorms"
    assert result["concerns"] == ["thunderstorms expected"] and result["looks_bad"] is True

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = WeatherService(WeatherConfig(latitude=1.0, longitude=2.0), client=client)
        calm = await service.at(f"{day} 03:00")
    assert calm["concerns"] == [] and calm["looks_bad"] is False


async def test_weather_at_rejects_the_past_and_far_future() -> None:
    service = WeatherService(WeatherConfig(latitude=1.0, longitude=2.0))
    for when in ["2020-01-01 10:00", (local_now() + timedelta(days=9)).strftime("%Y-%m-%d 10:00")]:
        try:
            await service.at(when)
        except Exception as exc:  # noqa: BLE001
            assert "passed" in str(exc) or "seven days" in str(exc)
        else:
            raise AssertionError("expected an error")


async def test_setup_help_asks_which_app_first_then_gives_steps(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set(None)
    first = await tools.execute("calendar_setup_help", {})
    assert "Apple Calendar or Google Calendar" in first["ask_first"] and "steps" not in first
    apple = await tools.execute("calendar_setup_help", {"app": "apple"})
    assert apple["address"] == "http://friday.local:8787/calendar.ics"
    assert apple["address_spoken"].startswith("h t t p colon slash slash friday dot local colon 8787")
    assert "Add Subscribed Calendar" in apple["steps"][1] and apple["address"] in apple["steps"][2]
    assert any("Remove Alerts" in step for step in apple["steps"])
    google = await tools.execute("calendar_setup_help", {"app": "google"})
    assert any("ICSx5" in step for step in google["steps"]) and google["address"] in " ".join(google["steps"])
    read = await tools.execute("calendar_setup_help", {"app": "google", "goal": "let_friday_read_my_calendar"})
    assert any("Secret address in iCal format" in step for step in read["steps"])
    assert any("CALENDAR_ICS_URLS" in step for step in read["steps"])
    events = await tools.execute("calendar_events", {})
    assert "calendar_setup_help" in events["note"]


def test_feed_url_follows_server_config(tmp_path) -> None:
    from house_agent.config import ServerConfig

    tools = registry(tmp_path)
    tools.config = AppConfig(
        server=ServerConfig(public_host="house.lan", port=9000, ssl_certfile=tmp_path / "c", ssl_keyfile=tmp_path / "k"),
        storage=tools.config.storage,
    )
    assert tools.feed_url() == "https://house.lan:9000/calendar.ics"


async def test_a_day_named_in_words_lands_on_that_day(tmp_path) -> None:
    """A 14B model asked to turn "next Monday" into a date produced tomorrow.
    The words go to the parser now, so the date does not depend on it."""
    tools = registry(tmp_path)
    result = await tools.execute(
        "calendar_add", {"title": "Buy ticket to Mexico", "when": "next Monday 9am"}
    )
    assert result["ok"]
    start = datetime.fromisoformat(tools.database.get_event(result["id"])["starts_at"])
    today = local_now().date()
    assert start.weekday() == 0
    assert today < start.date() <= today + timedelta(days=7)
    assert start.hour == 9 and start.minute == 0


async def test_a_plan_can_be_moved_instead_of_duplicated(tmp_path) -> None:
    tools = registry(tmp_path)
    added = await tools.execute("calendar_add", {"title": "Buy ticket to Mexico", "when": "tomorrow 9am"})
    moved = await tools.execute("calendar_update", {"title": "mexico ticket", "when": "next Monday 9am"})

    assert moved["ok"] and moved["id"] == added["id"]
    assert moved["when"].startswith("Monday") and moved["was"] == "tomorrow at 9:00 AM"
    listed = await tools.execute("calendar_events", {"days": 30})
    assert [event["title"] for event in listed["events"]] == ["Buy ticket to Mexico"]

    renamed = await tools.execute("calendar_update", {"id": added["id"], "new_title": "Book the Mexico flight"})
    assert renamed["ok"] and renamed["title"] == "Book the Mexico flight"


async def test_a_moved_plan_loses_the_heads_up_set_for_its_old_time(tmp_path) -> None:
    tools = registry(tmp_path)
    start = (local_now() + timedelta(days=1)).replace(hour=16, minute=0)
    added = await tools.execute(
        "calendar_add",
        {"title": "Ana's party", "when": start.strftime("%Y-%m-%d %H:%M"), "location": "the park"},
    )
    assert added.get("weather_heads_up_at")
    await tools.execute("calendar_update", {"id": added["id"], "when": "next Saturday 2pm"})
    assert tools.database.list_automations() == []


async def test_a_plan_is_removed_by_name(tmp_path) -> None:
    tools = registry(tmp_path)
    await tools.execute("calendar_add", {"title": "Buy ticket to Mexico", "when": "next Monday 9am"})
    gone = await tools.execute("calendar_delete", {"title": "mexico ticket"})
    assert gone["ok"] and gone["deleted"] == "Buy ticket to Mexico"
    assert (await tools.execute("calendar_events", {"days": 30}))["events"] == []


async def test_an_ambiguous_name_asks_rather_than_guessing(tmp_path) -> None:
    tools = registry(tmp_path)
    await tools.execute("calendar_add", {"title": "Buy ticket to Mexico", "when": "next Monday 9am"})
    await tools.execute("calendar_add", {"title": "Mexico dinner", "when": "next Monday 7pm"})

    answer = await tools.execute("calendar_delete", {"title": "mexico"})
    assert answer["ok"] is False
    assert [event["title"] for event in answer["several"]] == ["Buy ticket to Mexico", "Mexico dinner"]
    assert (await tools.execute("calendar_events", {"days": 30}))["events"] != []

    missing = await tools.execute("calendar_delete", {"title": "dentist"})
    assert missing["ok"] is False and "No plan matching" in missing["error"]


async def test_an_empty_window_says_it_was_the_window(tmp_path) -> None:
    """"You have no plans" said of a three-day window is a wrong answer when
    the party is next month. The result now says which days were looked at."""
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    result = await tools.execute("calendar_events", {"days": 3})

    assert result["events"] == []
    assert "no other" in result["next_step"]
    assert "larger days" in result["next_step"]


async def test_an_empty_list_offers_the_lists_that_are_not(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    await tools.execute("list_add", {"list": "hardware", "items": ["bulbs"]})

    empty = await tools.execute("list_show", {"list": "shopping"})
    assert empty["items"] == []
    assert empty["other_lists"] == ["hardware"]
    assert "these have items: hardware" in empty["note"]

    stocked = await tools.execute("list_show", {"list": "hardware"})
    assert stocked["items"] == ["bulbs"] and "note" not in stocked


async def test_a_house_with_nothing_paired_is_offered_pairing(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    result = await tools.execute("device_list", {})
    assert result["devices"] == []
    assert "device_pair" in result["note"] and "QR code" in result["note"]
