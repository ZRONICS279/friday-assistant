import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from house_agent import scheduler as sched
from house_agent.db import Database
from house_agent.scheduler import (
    Announcer,
    FadeRunner,
    ScheduleError,
    Scheduler,
    describe_schedule,
    next_run,
    normalize_action,
    normalize_schedule,
    parse_when,
    weather_relevant,
)
from house_agent.smarthome import Device, SmartHomeError

TZ = timezone(timedelta(hours=-6))
# A Saturday morning.
NOW = datetime(2026, 8, 29, 8, 0, tzinfo=TZ)


def test_parse_when_bare_time_means_next_occurrence() -> None:
    assert parse_when("07:30", NOW) == NOW.replace(day=30, hour=7, minute=30)
    assert parse_when("9pm", NOW) == NOW.replace(hour=21, minute=0)
    assert parse_when("2026-08-30 08:00", NOW) == NOW.replace(day=30)
    assert parse_when("2026-08-30T20:15", NOW) == NOW.replace(day=30, hour=20, minute=15)
    with pytest.raises(ScheduleError):
        parse_when("half past", NOW)


def test_spoken_times_parse() -> None:
    from house_agent.scheduler import _parse_time
    from datetime import time as dtime
    assert _parse_time("6:20pm") == dtime(18, 20)
    assert _parse_time("6:20 P.M.") == dtime(18, 20)
    assert _parse_time("12am") == dtime(0, 0)
    assert _parse_time("noon") == dtime(12, 0)
    assert _parse_time("midnight") == dtime(0, 0)


def test_vague_times_have_house_defaults() -> None:
    from house_agent.scheduler import _parse_time, assumed_time
    from datetime import time as dtime
    assert _parse_time("bedtime") == dtime(22, 20)
    assert _parse_time("at bedtime") == dtime(22, 20)
    assert _parse_time("in the morning") == dtime(8, 0)
    assert assumed_time("bedtime") == "bedtime taken as 10:20 PM"
    assert assumed_time("6:20pm") is None
    assert normalize_schedule("daily", "bedtime", now=NOW)["time"] == "22:20"


def test_once_must_be_in_the_future() -> None:
    with pytest.raises(ScheduleError):
        normalize_schedule("once", "2026-08-29 07:00", now=NOW)
    schedule = normalize_schedule("once", "2026-08-30 08:00", now=NOW)
    assert schedule == {"repeat": "once", "at": "2026-08-30T08:00-06:00"}
    assert next_run(schedule, NOW) == NOW.replace(day=30)
    assert next_run(schedule, NOW.replace(day=30, hour=9)) is None
    assert "Sunday August 30" in describe_schedule(schedule)


def test_repeating_schedules_pick_the_next_matching_day() -> None:
    daily = normalize_schedule("daily", "07:30", now=NOW)
    assert next_run(daily, NOW) == NOW.replace(day=30, hour=7, minute=30)
    assert next_run(daily, NOW.replace(hour=7)) == NOW.replace(hour=7, minute=30)
    weekdays = normalize_schedule("weekdays", "22:00", now=NOW)
    assert next_run(weekdays, NOW).weekday() == 0  # Saturday -> Monday
    weekends = normalize_schedule("weekends", "10:00", now=NOW)
    assert next_run(weekends, NOW) == NOW.replace(hour=10)
    weekly = normalize_schedule("weekly", "6:15pm", ["Wednesday", "mon"], now=NOW)
    assert weekly["days"] == ["mon", "wed"] and weekly["time"] == "18:15"
    assert next_run(weekly, NOW).weekday() == 0
    assert describe_schedule(weekly) == "Mon, Wed at 6:15 PM"
    with pytest.raises(ScheduleError):
        normalize_schedule("weekly", "07:00", [], now=NOW)
    with pytest.raises(ScheduleError):
        normalize_schedule("hourly", "07:00", now=NOW)


def test_actions_are_validated_and_clamped() -> None:
    assert normalize_action({"type": "say", "text": "  Call   grandma "}) == {
        "type": "say",
        "text": "Call grandma",
    }
    fade = normalize_action(
        {"type": "fade", "device": "bedroom", "brightness_to": 140, "minutes": 30, "color_temp_to": 9000}
    )
    assert fade["brightness_to"] == 100 and fade["color_temp_to"] == 6500
    with pytest.raises(ScheduleError):
        normalize_action({"type": "fade", "device": "bedroom", "minutes": 5})
    with pytest.raises(ScheduleError):
        normalize_action({"type": "device", "device": "bedroom"})
    with pytest.raises(ScheduleError):
        normalize_action({"type": "explode", "text": "no"})


class FakeHub:
    def __init__(self, brightness: int = 40, power: bool = True) -> None:
        self.device = Device(
            key="matter-1",
            backend="matter",
            name="Bedroom lamp",
            kind="light",
            room="bedroom",
            capabilities=("power", "brightness", "color_temp"),
            state={"power": power, "brightness": brightness, "color_temp_kelvin": 2700},
        )
        self.calls: list[dict] = []
        self.fail_times = 0

    def find(self, query: str) -> Device:
        if query in ("matter-1", "bedroom", "bedroom lamp"):
            return self.device
        raise SmartHomeError(f"No device matches '{query}'")

    def find_targets(self, query: str) -> list[Device]:
        return [self.find(query)]

    def devices(self) -> list[Device]:
        return [self.device]

    async def control(self, query, power=None, brightness=None, color_temp_kelvin=None):
        if self.fail_times:
            self.fail_times -= 1
            raise SmartHomeError("offline")
        self.calls.append(
            {"power": power, "brightness": brightness, "color_temp": color_temp_kelvin}
        )
        return self.device


async def test_instant_fade_is_a_single_set() -> None:
    hub = FakeHub()
    fades = FadeRunner(hub)
    plan = await fades.fade("bedroom", 100, 0, wait=True)
    assert plan["steps"] == 1 and plan["brightness_from"] == 40
    assert hub.calls == [{"power": "on", "brightness": 100, "color_temp": None}]


async def test_fade_steps_down_and_ends_with_power_off(monkeypatch) -> None:
    monkeypatch.setattr(sched, "MIN_STEP_SECONDS", 0.001)
    hub = FakeHub(brightness=3)
    fades = FadeRunner(hub)
    plan = await fades.fade("bedroom", 0, 0.001, wait=True)
    assert plan["steps"] == 3
    assert [call["brightness"] for call in hub.calls[:-1]] == [2, 1]
    assert hub.calls[-1] == {"power": "off", "brightness": None, "color_temp": None}
    assert fades.running() == []


async def test_color_change_adds_steps_and_interpolates(monkeypatch) -> None:
    monkeypatch.setattr(sched, "MIN_STEP_SECONDS", 0.001)
    hub = FakeHub(brightness=0)
    plan = await FadeRunner(hub).fade("bedroom", 5, 0.001, color_temp_to=3200, wait=True)
    # Five brightness points but 500 K of color: the color sets the step count.
    assert plan["steps"] == 10 and plan["color_temp_from"] == 2700
    assert [call["color_temp"] for call in hub.calls][:2] == [2750, 2800]
    assert hub.calls[-1] == {"power": None, "brightness": 5, "color_temp": 3200}


async def test_fade_starts_from_zero_when_light_is_off() -> None:
    hub = FakeHub(brightness=80, power=False)
    plan = await FadeRunner(hub).fade("bedroom", 60, 0, wait=True)
    assert plan["brightness_from"] == 0


async def test_new_fade_cancels_the_running_one(monkeypatch) -> None:
    monkeypatch.setattr(sched, "MIN_STEP_SECONDS", 0.001)
    hub = FakeHub(brightness=0)
    fades = FadeRunner(hub)
    await fades.fade("bedroom", 100, 5)  # 100 steps over five minutes
    await asyncio.sleep(0)
    assert len(fades.running()) == 1
    await fades.fade("bedroom", 0, 0, wait=True)
    assert fades.running() == []
    assert hub.calls[-1]["power"] == "off"


async def test_fade_survives_a_few_failures(monkeypatch) -> None:
    monkeypatch.setattr(sched, "MIN_STEP_SECONDS", 0.001)
    hub = FakeHub(brightness=0)
    hub.fail_times = 2
    await FadeRunner(hub).fade("bedroom", 4, 0.001, wait=True)
    assert [call["brightness"] for call in hub.calls] == [3, 4]


def database(tmp_path) -> Database:
    db = Database(tmp_path / "sched.db")
    db.initialize()
    return db


def clock_at(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(sched, "local_now", lambda: moment)


async def test_a_fade_is_saved_and_resumed_after_a_restart(tmp_path, monkeypatch) -> None:
    """A restart (or a power cut) must not leave the lamp half-dimmed: the
    fade continues from where the clock says it should be."""
    db = database(tmp_path)
    hub = FakeHub(brightness=40)
    clock_at(monkeypatch, NOW)
    fades = FadeRunner(hub, db)
    await fades.fade("bedroom", 0, 30)  # 40 steps, one every 45 s
    await asyncio.sleep(0)
    saved = db.list_fades()
    assert saved[0]["device_id"] == "matter-1" and saved[0]["ends_at"].startswith("2026-08-29T08:30")
    await fades.stop()  # shutdown keeps the row
    assert len(db.list_fades()) == 1

    # Back 15 minutes later: step 21 of 40 is due -> brightness 19, and on.
    hub2 = FakeHub(brightness=40)
    clock_at(monkeypatch, NOW + timedelta(minutes=15))
    fresh = FadeRunner(hub2, db)
    await fresh.resume()
    await asyncio.sleep(0.05)
    assert hub2.calls[0] == {"power": "on", "brightness": 19, "color_temp": None}
    assert fresh.running()[0]["resumed_at_step"] == 21
    await fresh.stop()

    # Back after it should have ended: straight to the final level, row gone.
    hub3 = FakeHub(brightness=40)
    clock_at(monkeypatch, NOW + timedelta(hours=1))
    late = FadeRunner(hub3, db)
    await late.resume()
    await asyncio.sleep(0.05)
    assert hub3.calls == [{"power": "off", "brightness": None, "color_temp": None}]
    assert db.list_fades() == [] and late.running() == []


async def test_a_finished_fade_forgets_itself(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sched, "MIN_STEP_SECONDS", 0.001)
    db = database(tmp_path)
    fades = FadeRunner(FakeHub(brightness=3), db)
    await fades.fade("bedroom", 0, 0.001, wait=True)
    assert db.list_fades() == []


async def test_a_late_light_schedule_catches_up(tmp_path, monkeypatch) -> None:
    db = database(tmp_path)
    hub = FakeHub(brightness=40)
    schedule = normalize_schedule("daily", "07:30", now=NOW - timedelta(hours=2))
    db.add_automation(
        "night dim", schedule,
        {"type": "fade", "device": "bedroom", "brightness_to": 0, "minutes": 30},
        "2026-08-29T07:30-06:00",
    )
    fades = FadeRunner(hub, db)
    scheduler = Scheduler(db, Announcer(), fades)

    # Ten minutes late: the fade joins in progress rather than starting over.
    clock_at(monkeypatch, NOW.replace(hour=7, minute=40))
    fired = await scheduler.tick(NOW.replace(hour=7, minute=40))
    assert "from step 14" in fired[0]["result"]
    await asyncio.sleep(0.05)
    assert hub.calls[0]["brightness"] == 26
    await fades.stop()

    # Two hours late (past its end, within the catch-up window): final level.
    hub.calls.clear()
    db.set_automation_enabled(1, True, "2026-08-30T07:30-06:00")
    later = (NOW + timedelta(days=1)).replace(hour=9, minute=30)
    clock_at(monkeypatch, later)
    fired = await scheduler.tick(later)
    assert fired[0]["result"].startswith("late by 120 min")
    await asyncio.sleep(0.05)
    assert hub.calls == [{"power": "off", "brightness": None, "color_temp": None}]

    # Seven hours late: too stale, marked missed.
    hub.calls.clear()
    db.set_automation_enabled(1, True, "2026-08-31T07:30-06:00")
    stale = (NOW + timedelta(days=2)).replace(hour=14, minute=45)
    clock_at(monkeypatch, stale)
    fired = await scheduler.tick(stale)
    assert fired[0]["result"].startswith("missed") and hub.calls == []


async def test_due_reminder_is_announced_and_one_off_ends(tmp_path) -> None:
    db = database(tmp_path)
    announcer = Announcer()
    schedule = normalize_schedule("once", "2026-08-29 08:00", now=NOW - timedelta(hours=1))
    db.add_automation("reminder: eggs", schedule, {"type": "say", "text": "Buy eggs"}, schedule["at"], "Alex")
    scheduler = Scheduler(db, announcer, FadeRunner(FakeHub()))
    assert await scheduler.tick(NOW - timedelta(minutes=1)) == []
    fired = await scheduler.tick(NOW)
    assert [item["result"] for item in fired] == ["announced"]
    assert [event["text"] for event in announcer.recent] == ["Buy eggs"]
    assert announcer.recent[0]["profile"] == "Alex"
    assert db.list_automations() == []
    assert db.list_automations(include_disabled=True)[0]["last_result"] == "announced"


async def test_late_repeating_run_is_skipped_and_rescheduled(tmp_path) -> None:
    db = database(tmp_path)
    announcer = Announcer()
    schedule = normalize_schedule("daily", "07:30", now=NOW)
    db.add_automation("wake", schedule, {"type": "say", "text": "Wake up"}, "2026-08-29T07:30-06:00")
    scheduler = Scheduler(db, announcer, FadeRunner(FakeHub()))
    fired = await scheduler.tick(NOW.replace(hour=15))
    assert fired[0]["result"].startswith("missed")
    assert announcer.recent == announcer.recent.__class__(maxlen=50)
    assert db.list_automations()[0]["next_run_at"] == "2026-08-30T07:30-06:00"
    # A few minutes late is fine.
    db.mark_automation_run(fired[0]["id"], NOW.isoformat(), "2026-08-30T07:30-06:00")
    fired = await scheduler.tick(NOW.replace(day=30, hour=7, minute=40))
    assert fired[0]["result"] == "announced"


async def test_ask_actions_go_through_the_agent(tmp_path) -> None:
    db = database(tmp_path)
    announcer = Announcer()
    questions: list[str] = []

    async def ask(question: str, quiet: bool) -> str:
        questions.append((question, quiet))
        return "Nothing to report." if quiet else "No rain expected before your meeting."

    schedule = normalize_schedule("once", "2026-08-29 08:00", now=NOW - timedelta(hours=1))
    db.add_automation("check", schedule, {"type": "ask", "text": "Will it rain at 2pm?"}, schedule["at"])
    scheduler = Scheduler(db, announcer, FadeRunner(FakeHub()), ask)
    await scheduler.tick(NOW)
    assert questions == [("Will it rain at 2pm?", False)]
    assert announcer.recent[0]["text"] == "No rain expected before your meeting."
    # A quiet heads-up with nothing to say stays silent.
    db.add_automation(
        "heads-up", schedule,
        {"type": "ask", "text": "Weather ok?", "quiet": True, "event_id": 7}, schedule["at"],
    )
    fired = await scheduler.tick(NOW)
    assert fired[0]["result"] == "checked; nothing to report"
    assert questions[-1] == ("Weather ok?", True) and len(announcer.recent) == 1


async def test_fade_and_device_actions_reach_the_hub(tmp_path) -> None:
    db = database(tmp_path)
    hub = FakeHub()
    schedule = normalize_schedule("daily", "08:00", now=NOW - timedelta(hours=1))
    db.add_automation(
        "sunrise", schedule,
        {"type": "fade", "device": "bedroom", "brightness_to": 100, "minutes": 0},
        "2026-08-29T08:00-06:00",
    )
    db.add_automation(
        "off", schedule, {"type": "device", "device": "bedroom", "power": "off"}, "2026-08-29T08:00-06:00"
    )
    scheduler = Scheduler(db, Announcer(), FadeRunner(hub))
    fired = await scheduler.tick(NOW)
    await asyncio.sleep(0.01)
    assert fired[0]["result"].startswith("fading Bedroom lamp 40% to 100%")
    assert fired[1]["result"] == "set"
    assert {"power": "off", "brightness": None, "color_temp": None} in hub.calls
    assert {"power": "on", "brightness": 100, "color_temp": None} in hub.calls


async def test_failed_action_is_recorded_not_raised(tmp_path) -> None:
    db = database(tmp_path)
    schedule = normalize_schedule("once", "2026-08-29 08:00", now=NOW - timedelta(hours=1))
    db.add_automation(
        "bad", schedule, {"type": "fade", "device": "garage", "brightness_to": 0, "minutes": 0}, schedule["at"]
    )
    fired = await Scheduler(db, Announcer(), FadeRunner(FakeHub())).tick(NOW)
    assert fired[0]["result"].startswith("failed: No device matches")


async def test_announcer_fans_out_and_replays() -> None:
    announcer = Announcer(backlog=2)
    queue = announcer.subscribe()
    announcer.publish("one")
    announcer.publish("two")
    announcer.publish("three")
    assert [event["text"] for event in announcer.since(1)] == ["two", "three"]
    assert queue.qsize() == 3 and announcer.listeners == 1
    announcer.unsubscribe(queue)
    assert announcer.listeners == 0


def test_weather_relevance_of_plans() -> None:
    yes = [
        ("Ana's party", "the park"), ("Concert", None), ("Softball game", None), ("Picnic", None),
        ("Beach day", None), ("Drive to work", None), ("Road trip to Denver", None),
        ("Flight to Vegas", None), ("Hiking with Sam", None), ("Dentist", "14 Main St"),
        ("Kids soccer practice", None), ("Farmers market", None), ("Dog walk", None),
        ("Coffee with Bob", "Starbucks on 5th"), ("Bus to campus", None), ("Trips", None), ("Train to Boston", None),
        ("Ski day", None), ("Camp out", None), ("Day at the zoo", None),
    ]
    no = [
        ("Call mom", None), ("Call mom", "phone"), ("Weekly standup", "Zoom"), ("Pay rent", None),
        ("Team sync", "https://meet.google.com/abc"), ("Dentist", None), ("Zoom with Ana", None),
        ("Video interview", None), ("Business review", "Zoom"), ("Campaign call", None),
        ("Roadmap sync", None), ("Skills training", "online"), ("Training session", "teams"),
    ]
    for title, location in yes:
        assert weather_relevant(title, location), (title, location)
    for title, location in no:
        assert not weather_relevant(title, location), (title, location)


async def test_announcements_addressed_to_a_conversation_stay_in_it() -> None:
    """Two people talking to the house at once: the light that joins one
    person's pairing window is announced on their screen only, while a
    reminder still reaches everyone."""
    announcer = Announcer()
    mine = announcer.subscribe("conv-a")
    theirs = announcer.subscribe("conv-b")
    anyone = announcer.subscribe()  # a screen with no conversation yet

    announcer.publish("A new light just joined.", conversation_id="conv-a")
    announcer.publish("Buy eggs")  # household-wide

    assert [event["text"] for event in _drain(mine)] == ["A new light just joined.", "Buy eggs"]
    assert [event["text"] for event in _drain(theirs)] == ["Buy eggs"]
    assert [event["text"] for event in _drain(anyone)] == ["Buy eggs"]
    # The reconnect backlog is filtered the same way.
    assert [e["text"] for e in announcer.since(0, "conv-b")] == ["Buy eggs"]
    assert [e["text"] for e in announcer.since(0, "conv-a")] == ["A new light just joined.", "Buy eggs"]
    announcer.unsubscribe(mine)
    assert announcer.listeners == 2


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_days_named_in_words_resolve_to_a_date() -> None:
    """Wednesday 23 September 2026, 10am."""
    now = datetime(2026, 9, 23, 10, 0)
    at = lambda text: parse_when(text, now)

    assert at("next Monday 9am") == datetime(2026, 9, 28, 9, 0)
    assert at("monday") == datetime(2026, 9, 28, 9, 0)  # no time: 9am, as a bare date is
    assert at("tomorrow at noon") == datetime(2026, 9, 24, 12, 0)
    assert at("today 3pm") == datetime(2026, 9, 23, 15, 0)
    assert at("tonight") == datetime(2026, 9, 23, 20, 0)
    assert at("this Friday 7pm") == datetime(2026, 9, 25, 19, 0)
    assert at("in 3 days") == datetime(2026, 9, 26, 9, 0)
    assert at("next week") == datetime(2026, 9, 30, 9, 0)
    assert at("the day after tomorrow") == datetime(2026, 9, 25, 9, 0)
    assert at("el próximo lunes") == datetime(2026, 9, 28, 9, 0)
    assert at("mañana 8am") == datetime(2026, 9, 24, 8, 0)

    # A weekday that is today means the one coming, unless they said "this".
    monday = datetime(2026, 9, 28, 10, 0)
    assert parse_when("monday 9am", monday) == datetime(2026, 10, 5, 9, 0)
    assert parse_when("this monday 9am", monday) == datetime(2026, 9, 28, 9, 0)

    # The forms that already worked still do.
    assert at("2026-09-28 09:00") == datetime(2026, 9, 28, 9, 0)
    assert at("09:00") == datetime(2026, 9, 24, 9, 0)  # next time that hour comes
