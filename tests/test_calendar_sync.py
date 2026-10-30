from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx

from house_agent.calendar_sync import CalendarSync, CalendarSyncError, parse_feed
from house_agent.config import CalendarConfig
from house_agent.db import Database
from house_agent.scheduler import local_now


def feed(start_local) -> str:
    denver = start_local.astimezone(ZoneInfo("America/Denver"))
    day = denver.strftime("%Y%m%d")
    stamp = denver.strftime("%Y%m%dT%H%M%S")
    stamp_end = (denver + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
X-WR-CALNAME:Work
BEGIN:VEVENT
UID:standup
DTSTART;TZID=America/Denver:{stamp}
DTEND;TZID=America/Denver:{stamp_end}
SUMMARY:Weekly standup
LOCATION:Zoom
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:softball
DTSTART;TZID=America/Denver:{stamp}
DTEND;TZID=America/Denver:{stamp_end}
SUMMARY:Softball game
LOCATION:Riverside park
END:VEVENT
BEGIN:VEVENT
UID:trip
DTSTART;VALUE=DATE:{(start_local + timedelta(days=2)).strftime("%Y%m%d")}
DTEND;VALUE=DATE:{(start_local + timedelta(days=3)).strftime("%Y%m%d")}
SUMMARY:Day off
END:VEVENT
BEGIN:VEVENT
UID:gone
DTSTART:{day}T200000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def test_parse_expands_recurrences_and_all_day_events() -> None:
    now = local_now()
    start = (now + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
    name, events = parse_feed(feed(start), "calendar 1", now, 30)
    assert name == "Work"
    standups = [event for event in events if event["uid"] == "standup"]
    assert len(standups) == 3
    assert standups[0]["title"] == "Weekly standup" and standups[0]["location"] == "Zoom"
    assert standups[0]["all_day"] is False
    assert standups[0]["key"].startswith("Work:standup:")
    assert standups[1]["key"] != standups[0]["key"]
    # Denver 16:00 expressed in the house's own offset, minute precision.
    assert standups[0]["ends_at"] > standups[0]["starts_at"]
    day_off = next(event for event in events if event["uid"] == "trip")
    assert day_off["all_day"] is True and day_off["starts_at"].endswith("T00:00" + now.strftime("%z")[:3] + ":" + now.strftime("%z")[3:])
    assert not any(event["uid"] == "gone" for event in events)


def test_garbage_is_reported_not_stored() -> None:
    try:
        parse_feed("<html>login page</html>", "calendar 1", local_now(), 30)
    except CalendarSyncError as exc:
        assert "Not an iCalendar feed" in str(exc)
    else:
        raise AssertionError("expected CalendarSyncError")


async def test_refresh_stores_events_and_schedules_heads_ups(tmp_path) -> None:
    db = Database(tmp_path / "sync.db")
    db.initialize()
    now = local_now()
    start = (now + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
    text = feed(start)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "bad.example":
            return httpx.Response(500)
        return httpx.Response(200, text=text)

    config = CalendarConfig(subscriptions=("webcal://cal.example/feed.ics", "https://bad.example/x.ics"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sync = CalendarSync(config, db, client=client)
        results = await sync.refresh()
    assert calls[0].startswith("https://cal.example/")  # webcal:// rewritten
    assert results[0]["ok"] and results[0]["name"] == "Work" and results[0]["events"] == 5
    assert results[0]["added"] == 5 and results[0]["heads_ups_added"] == 1  # the game, not the Zoom standup
    assert results[1]["ok"] is False and "500" in results[1]["error"]
    assert sync.status["Work"]["events"] == 5

    # Friday's own plans and subscribed ones come back together, in order.
    db.add_event("Dinner", (start + timedelta(hours=3)).isoformat(timespec="minutes"), None, None, None, "Alex")
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    listed = db.list_events(today.isoformat(timespec="minutes"), (today + timedelta(days=2)).isoformat(timespec="minutes"))
    assert [(item["title"], item["source"]) for item in listed][:3] == [
        ("Softball game", "Work"), ("Weekly standup", "Work"), ("Dinner", "friday"),
    ]

    heads_up = db.list_automations()[0]
    assert heads_up["name"] == "weather heads-up: Softball game"
    assert heads_up["action"]["quiet"] is True and heads_up["action"]["external_key"].startswith("Work:softball:")
    assert heads_up["next_run_at"] == (start - timedelta(hours=1)).isoformat(timespec="minutes")
    assert "Riverside park" in heads_up["action"]["text"]

    # A second refresh neither duplicates events nor heads-ups.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        results = await CalendarSync(config, db, client=client).refresh()
    assert results[0]["added"] == 0 and results[0]["heads_ups_added"] == 0
    assert len(db.list_automations()) == 1

    # An event that disappears from the feed takes its heads-up with it.
    text = text.replace("UID:softball", "UID:other").replace("SUMMARY:Softball game", "SUMMARY:Beach day")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        results = await CalendarSync(config, db, client=client).refresh()
    assert results[0]["removed"] == 1 and results[0]["heads_ups_added"] == 1
    assert [a["action"]["external_key"] for a in db.list_automations()] == [f"Work:other:{start.isoformat(timespec='minutes')}"]
    assert db.external_sources()[0]["events"] == 5


def test_config_reads_urls_from_env_and_toml(tmp_path, monkeypatch) -> None:
    from house_agent.config import load_config

    (tmp_path / "config.toml").write_text(
        '[calendar]\nsubscriptions = ["https://a.example/a.ics", {url = "https://b.example/b.ics"}]\nrefresh_minutes = 5\n'
    )
    (tmp_path / ".env").write_text("CALENDAR_ICS_URLS=https://c.example/c.ics, https://a.example/a.ics\n")
    monkeypatch.delenv("CALENDAR_ICS_URLS", raising=False)
    config = load_config(tmp_path / "config.toml")
    assert config.calendar.subscriptions == (
        "https://c.example/c.ics", "https://a.example/a.ics", "https://b.example/b.ics"
    )
    assert config.calendar.refresh_minutes == 5 and config.calendar.weather_heads_up is True
    (tmp_path / "empty").mkdir()
    assert load_config(tmp_path / "empty" / "config.toml").calendar.subscriptions == ()
