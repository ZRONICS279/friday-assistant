"""Calendars kept elsewhere, read into Friday.

Google, iCloud, Outlook and Nextcloud all publish a calendar as an iCalendar
feed at a private URL. Friday fetches each one every few minutes, expands
recurring events for the next couple of months, and keeps the result in
SQLite beside the plans people said out loud, so "what's on tomorrow" and
the weather heads-up before a meeting work the same for both.

Read-only by design: Friday never writes to anyone's Google calendar, and
the feed URL is the only credential involved.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import icalendar
import recurring_ical_events

from .config import CalendarConfig
from .db import Database
from .scheduler import local_now, weather_relevant

log = logging.getLogger(__name__)

HEADS_UP_LEAD = timedelta(minutes=60)
HEADS_UP_WINDOW = timedelta(hours=36)
MAX_BYTES = 5_000_000


class CalendarSyncError(RuntimeError):
    pass


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _local(value: Any, now: datetime) -> tuple[datetime, bool]:
    """A DTSTART/DTEND as an aware local datetime, plus whether it was a
    bare date (an all-day event)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=now.tzinfo)
        return value.astimezone(now.tzinfo), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=now.tzinfo), True
    raise CalendarSyncError(f"Unreadable date in feed: {value!r}")


def parse_feed(text: str, source_hint: str, now: datetime, horizon_days: int) -> tuple[str, list[dict[str, Any]]]:
    """The calendar's own name and its events between yesterday and the
    horizon, recurrences expanded and exceptions applied."""
    try:
        calendar = icalendar.Calendar.from_ical(text)
    except Exception as exc:  # noqa: BLE001 - library raises a grab-bag
        raise CalendarSyncError(f"Not an iCalendar feed: {exc}") from exc
    name = _text(calendar.get("X-WR-CALNAME")) or source_hint
    window_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=horizon_days + 1)
    events: list[dict[str, Any]] = []
    for occurrence in recurring_ical_events.of(calendar).between(window_start, window_end):
        start_value = occurrence.get("DTSTART")
        if start_value is None:
            continue
        start, all_day = _local(start_value.dt, now)
        end = None
        if occurrence.get("DTEND") is not None:
            end, _ = _local(occurrence["DTEND"].dt, now)
        elif occurrence.get("DURATION") is not None:
            end = start + occurrence["DURATION"].dt
        if str(occurrence.get("STATUS", "")).upper() == "CANCELLED":
            continue
        uid = _text(occurrence.get("UID")) or f"{name}-{start.isoformat()}"
        events.append(
            {
                "key": f"{name}:{uid}:{start.isoformat(timespec='minutes')}",
                "uid": uid,
                "title": _text(occurrence.get("SUMMARY")) or "(untitled)",
                "starts_at": start.isoformat(timespec="minutes"),
                "ends_at": end.isoformat(timespec="minutes") if end else None,
                "all_day": all_day,
                "location": _text(occurrence.get("LOCATION")),
                "notes": (_text(occurrence.get("DESCRIPTION")) or "")[:500] or None,
            }
        )
    events.sort(key=lambda item: item["starts_at"])
    return name, events


class CalendarSync:
    def __init__(
        self,
        config: CalendarConfig,
        database: Database,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self._injected = client
        self._task: asyncio.Task[None] | None = None
        self.status: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.subscriptions)

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="calendar-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - keep syncing
                log.exception("calendar sync failed")
            await asyncio.sleep(self.config.refresh_minutes * 60)

    async def _fetch(self, url: str) -> str:
        fetch_url = url.replace("webcal://", "https://", 1)
        if self._injected is not None:
            response = await self._injected.get(fetch_url)
        else:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "house-agent/1.0 (Friday home assistant)"},
            ) as client:
                response = await client.get(fetch_url)
        response.raise_for_status()
        return response.text[:MAX_BYTES]

    async def refresh(self) -> list[dict[str, Any]]:
        """Fetch every subscription once. One bad feed never blocks the rest."""
        results: list[dict[str, Any]] = []
        now = local_now()
        for index, url in enumerate(self.config.subscriptions, start=1):
            hint = f"calendar {index}"
            try:
                text = await self._fetch(url)
                name, events = parse_feed(text, hint, now, self.config.horizon_days)
                change = self.database.replace_external_events(name, events)
                heads_ups = self._schedule_heads_ups(name, events, now) if self.config.weather_heads_up else 0
                entry = {
                    "name": name,
                    "ok": True,
                    "events": change["total"],
                    "added": len(change["added"]),
                    "removed": len(change["removed"]),
                    "heads_ups_added": heads_ups,
                    "synced_at": now.isoformat(timespec="seconds"),
                }
                log.info("calendar %s: %d events (+%d/-%d)", name, change["total"], len(change["added"]), len(change["removed"]))
            except (httpx.HTTPError, CalendarSyncError) as exc:
                entry = {"name": hint, "ok": False, "error": str(exc), "synced_at": now.isoformat(timespec="seconds")}
                log.warning("calendar %s: %s", hint, exc)
            self.status[entry["name"]] = entry
            results.append(entry)
        return results

    def _schedule_heads_ups(self, source: str, events: list[dict[str, Any]], now: datetime) -> int:
        """A quiet weather check an hour before each timed outdoor or
        travel event in the next day and a half, once per occurrence. Calls,
        video meetings and other indoor items get none."""
        added = 0
        for event in events:
            if event["all_day"] or not weather_relevant(event["title"], event.get("location")):
                continue
            start = datetime.fromisoformat(event["starts_at"])
            check_at = start - HEADS_UP_LEAD
            if not (now + timedelta(minutes=2) < check_at <= now + HEADS_UP_WINDOW):
                continue
            if self.database.has_automation_for_external(event["key"]):
                continue
            where = f" at {event['location']}" if event.get("location") else ""
            question = (
                f"Heads-up for {event['title']}{where} at {start.strftime('%-I:%M %p')} today, "
                f"from the {source} calendar: call weather_at for that hour and say "
                "whether the weather will be a problem — rain, storms, snow, heat, cold, "
                "wind, or an alert — and how to prepare, in one or two sentences. If it "
                "looks fine, reply exactly: Nothing to report."
            )
            self.database.add_automation(
                f"weather heads-up: {event['title']}"[:60],
                {"repeat": "once", "at": check_at.isoformat(timespec="minutes")},
                {"type": "ask", "text": question, "quiet": True, "external_key": event["key"]},
                check_at.isoformat(timespec="minutes"),
                None,
            )
            added += 1
        return added
