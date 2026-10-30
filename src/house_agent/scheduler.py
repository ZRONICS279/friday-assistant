"""Things Friday does on its own clock.

Everything else in the house is request and response: someone speaks, a tool
runs, a reply comes back. Reminders, a light that brightens itself at 7:30, and
"tell me if it will rain before my meeting" all need the opposite -- an action
with nobody asking for it -- so this module owns the clock and the channel
that carries an unasked-for sentence back to whoever is listening.

Three pieces:

* :class:`Announcer` -- a fan-out of events to every connected browser (served
  as Server-Sent Events by the API). A short backlog lets a tab that was closed
  when a reminder fired catch up when it reopens.
* :class:`FadeRunner` -- gradual brightness and color changes for a light, run
  as a background task so a sunrise over thirty minutes never blocks a turn.
* :class:`Scheduler` -- polls the automations table and runs whatever is due.

Times are local. Timestamps are stored with the house's UTC offset so string
comparison in SQLite orders them the way a person would expect.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, time as dtime, timedelta
from typing import Any, Awaitable, Callable

from .smarthome import SmartHomeError, SmartHomeHub

log = logging.getLogger(__name__)

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPEATS = ("once", "daily", "weekdays", "weekends", "weekly")
ACTION_TYPES = ("say", "ask", "fade", "device")
# A repeating automation that comes due while the server is down is not worth
# running hours late: nobody wants a sunrise at three in the afternoon. One-off
# reminders still fire late, because late is better than lost.
LATE_GRACE = timedelta(minutes=45)
# A light change that came due while the server was off is still worth
# applying for this long afterwards: "off at bedtime" found at midnight is
# still lights-out, while a sunrise found the next evening is not.
LIGHT_CATCH_UP = timedelta(hours=6)
# Times people name without a clock. Bedtime is 10:20 PM in this house; a
# profile note or an explicit time overrides any of these.
VAGUE_TIMES: dict[str, dtime] = {
    "bedtime": dtime(22, 20),
    "bed time": dtime(22, 20),
    "night": dtime(22, 0),
    "nighttime": dtime(22, 0),
    "evening": dtime(19, 0),
    "dinner": dtime(18, 0),
    "dinnertime": dtime(18, 0),
    "dinner time": dtime(18, 0),
    "afternoon": dtime(15, 0),
    "lunch": dtime(12, 0),
    "lunchtime": dtime(12, 0),
    "lunch time": dtime(12, 0),
    "morning": dtime(8, 0),
    "breakfast": dtime(7, 30),
    "wake up": dtime(7, 0),
    "wake-up": dtime(7, 0),
    "wakeup": dtime(7, 0),
}
MIN_STEP_SECONDS = 5.0
MAX_FADE_MINUTES = 12 * 60


class ScheduleError(ValueError):
    pass


# Plans where the weather changes what you wear, bring, or whether you go:
# anything outdoors, and anything that means travelling somewhere. Matched
# as word stems, so "hiking", "hike" and "hikes" all count.
_OUTDOOR_STEMS = (
    "concert", "festival", "fair", "carnival", "parade", "firework", "tailgat",
    "marathon", "tournament", "practice", "playoff",
    "soccer", "football", "baseball", "softball", "golf", "tennis", "rugby",
    "lacrosse", "cricket", "stadium", "ballpark", "track",
    "picnic", "bbq", "barbecue", "barbeque", "cookout", "grill", "bonfire", "campfire",
    "beach", "lake", "river", "pool", "ocean", "coast", "shore", "boat", "sail", "kayak",
    "canoe", "raft", "paddle", "surf", "swim", "fish", "snorkel",
    "park", "garden", "yard", "lawn", "mow", "farm", "orchard", "trail", "hik",
    "camping", "hunt", "climb", "skiing", "snowboard", "sled", "skat", "jog",
    "bik", "cycl", "horse",
    "drive", "driv", "roadtrip", "travel", "commut", "flight", "flying",
    "airport", "ferry", "cruise", "errand", "pickup", "dropoff",
    "outdoor", "outside", "patio", "rooftop", "porch", "deck", "terrace",
    "wedding", "graduation", "ceremony", "funeral", "burial", "market",
    "amusement", "waterpark", "playground", "rally", "protest", "cleanup", "volunteer",
    "moving", "delivery", "carwash", "photoshoot",
)
# Short words that only count whole (with a plural): "zoo" must not catch
# "zoom", "bus" must not catch "business", "camp" not "campaign".
_OUTDOOR_WORDS = {
    "zoo", "bus", "fly", "run", "ride", "road", "dive", "field", "game",
    "match", "race", "trip", "walk", "camp", "ski", "hike", "train",
}
_OUTDOOR_PHRASES = ("road trip", "day trip", "theme park", "water park", "farmers market",
                    "car wash", "photo shoot", "drop off", "pick up", "field trip")
# Plans that happen through a screen or a phone, or that plainly stay indoors.
_INDOOR_WORDS = {
    "call", "phone", "facetime", "zoom", "teams", "meet", "video", "online",
    "virtual", "webinar", "skype", "discord", "chat", "email", "text", "remote",
    "stream", "podcast", "interview", "standup", "sync", "1:1", "check-in",
}


def weather_relevant(title: str, location: str | None = None) -> bool:
    """Whether a plan deserves a weather heads-up beforehand.

    Outdoor words and travel words say yes. Calls and anything virtual say
    no. A plain title with a physical place attached ("dentist at 14 Main
    St") means going somewhere, so it gets one too; a plain title with no
    place ("call mom", "pay rent") does not.
    """
    text = f"{title} {location or ''}".casefold()
    words = set(re.findall(r"[a-z0-9:'-]+", text))
    if any(phrase in text for phrase in _OUTDOOR_PHRASES):
        return True
    if any(word.startswith(stem) for word in words for stem in _OUTDOOR_STEMS):
        return True
    if any(word in _OUTDOOR_WORDS or word.rstrip("s") in _OUTDOOR_WORDS for word in words):
        return True
    if words & _INDOOR_WORDS or "http" in text:
        return False
    return bool((location or "").strip())


def local_now() -> datetime:
    return datetime.now().astimezone()


def _vague_words(text: str) -> str:
    words = " ".join(text.strip().lower().replace(".", "").split())
    for lead in ("at ", "around ", "about ", "in the ", "the ", "my "):
        if words.startswith(lead):
            words = words[len(lead):]
    return words


def assumed_time(text: str) -> str | None:
    """What a vague time was taken to mean, for the confirmation: 'bedtime
    taken as 10:20 PM'. None for a real clock time."""
    words = _vague_words(text)
    if words in VAGUE_TIMES:
        return f"{words} taken as {VAGUE_TIMES[words].strftime('%-I:%M %p')}"
    return None


def _parse_time(text: str) -> dtime:
    text = text.strip().lower().replace(".", "")
    if _vague_words(text) in VAGUE_TIMES:
        return VAGUE_TIMES[_vague_words(text)]
    if text in {"noon", "midday", "12 noon"}:
        return dtime(12, 0)
    if text == "midnight":
        return dtime(0, 0)
    suffix = None
    for marker in ("am", "pm"):
        if text.endswith(marker):
            suffix = marker
            text = text[: -len(marker)].strip()
    try:
        if ":" in text:
            hour_text, minute_text = text.split(":", 1)
            hour, minute = int(hour_text), int(minute_text[:2])
        else:
            hour, minute = int(text), 0
    except ValueError:
        raise ScheduleError(f"Not a time of day: {text!r} (use HH:MM, 24-hour)") from None
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"Not a time of day: {text!r}")
    return dtime(hour, minute)


# Days named the way people say them. The tool schemas ask the model to do
# the arithmetic and hand over a YYYY-MM-DD, and a small model gets it wrong
# often enough to matter — "next Monday" came back as tomorrow. Resolving the
# words here means the date is right whether or not the model worked it out.
_WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "lunes": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "martes": 1,
    "wednesday": 2, "wed": 2, "weds": 2, "miercoles": 2, "miércoles": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "jueves": 3,
    "friday": 4, "fri": 4, "viernes": 4,
    "saturday": 5, "sat": 5, "sabado": 5, "sábado": 5,
    "sunday": 6, "sun": 6, "domingo": 6,
}
_DAY_PHRASE = re.compile(
    r"^\s*(?:on\s+)?(?P<phrase>"
    r"today|tonight|tomorrow|hoy|ma[ñn]ana|"
    r"(?:the\s+day\s+after\s+tomorrow)|"
    r"(?:in\s+(?P<count>\d+|a|an)\s+(?P<unit>day|days|week|weeks))|"
    r"(?:(?P<qualifier>next|this|coming|el\s+pr[óo]ximo|pr[óo]ximo)\s+)?(?P<weekday>"
    + "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
    + r")|"
    r"(?:next\s+week)"
    r")\b[\s,]*(?:at\s+)?",
    re.IGNORECASE,
)


def _day_phrase(text: str, now: datetime) -> tuple[datetime, str] | None:
    """A leading day word resolved to that date, with whatever is left of the
    string (usually a time). None when the text does not start with one."""
    match = _DAY_PHRASE.match(text)
    if not match:
        return None
    rest = text[match.end():].strip()
    phrase = match.group("phrase").lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if phrase in {"today", "hoy"}:
        return midnight, rest
    if phrase == "tonight":
        # An evening by name: 8pm unless they gave a time.
        return midnight, rest or "20:00"
    if phrase in {"tomorrow", "manana", "mañana"}:
        return midnight + timedelta(days=1), rest
    if phrase == "the day after tomorrow":
        return midnight + timedelta(days=2), rest
    if phrase == "next week":
        return midnight + timedelta(days=7), rest
    if match.group("unit"):
        count = match.group("count").lower()
        number = 1 if count in {"a", "an"} else int(count)
        step = timedelta(weeks=number) if match.group("unit").startswith("week") else timedelta(days=number)
        return midnight + step, rest

    # A weekday: the next one to come around. "this Friday" can still mean
    # today; "next Friday" said on a Friday means the one after this.
    target = _WEEKDAYS[match.group("weekday").lower()]
    qualifier = (match.group("qualifier") or "").lower()
    ahead = (target - midnight.weekday()) % 7
    if ahead == 0:
        ahead = 0 if qualifier.startswith("this") else 7
    return midnight + timedelta(days=ahead), rest


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """A moment from 'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM', a day named in
    words ('next Monday 9am', 'tomorrow at noon', 'in 3 days'), or a bare
    'HH:MM' meaning the next time that clock reading comes around. A named
    day with no time lands at 9am, as a bare date does."""
    now = now or local_now()
    text = text.strip()
    if not text:
        raise ScheduleError("A time is required")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        date_part, _, time_part = text[:10], text[10:11], text[11:].strip()
        try:
            year, month, day = (int(part) for part in date_part.split("-"))
            moment = datetime(year, month, day, tzinfo=now.tzinfo)
        except ValueError:
            raise ScheduleError(f"Not a date: {date_part!r}") from None
        at = _parse_time(time_part) if time_part else dtime(9, 0)
        return moment.replace(hour=at.hour, minute=at.minute)
    named = _day_phrase(text, now)
    if named is not None:
        day, remainder = named
        at = _parse_time(remainder) if remainder else dtime(9, 0)
        return day.replace(hour=at.hour, minute=at.minute)
    at = _parse_time(text)
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def normalize_schedule(
    repeat: str, when: str, days: list[str] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """The stored form of a schedule, validated.

    once     -> {"repeat": "once", "at": "<ISO local datetime>"}
    daily    -> {"repeat": "daily", "time": "HH:MM"}
    weekdays / weekends -> same shape as daily
    weekly   -> {"repeat": "weekly", "time": "HH:MM", "days": ["mon", ...]}
    """
    now = now or local_now()
    repeat = (repeat or "once").strip().lower()
    if repeat not in REPEATS:
        raise ScheduleError(f"repeat must be one of {', '.join(REPEATS)}")
    if repeat == "once":
        moment = parse_when(when, now)
        if moment <= now:
            raise ScheduleError(
                f"{moment.strftime('%Y-%m-%d %H:%M')} has already passed; "
                "give a future time"
            )
        return {"repeat": "once", "at": moment.isoformat(timespec="minutes")}
    # Repeating: only the clock reading matters. Accept a full datetime too,
    # and take its time.
    if len(when.strip()) >= 10 and when.strip()[4] == "-":
        at = parse_when(when, now).timetz()
    else:
        at = _parse_time(when)
    schedule: dict[str, Any] = {"repeat": repeat, "time": f"{at.hour:02d}:{at.minute:02d}"}
    if repeat == "weekly":
        chosen = []
        for day in days or []:
            key = str(day).strip().lower()[:3]
            if key not in DAY_NAMES:
                raise ScheduleError(f"Unknown day: {day!r}")
            if key not in chosen:
                chosen.append(key)
        if not chosen:
            raise ScheduleError("weekly needs at least one day, such as mon or sat")
        schedule["days"] = sorted(chosen, key=DAY_NAMES.index)
    return schedule


def _days_for(schedule: dict[str, Any]) -> set[int]:
    repeat = schedule["repeat"]
    if repeat == "daily":
        return set(range(7))
    if repeat == "weekdays":
        return set(range(5))
    if repeat == "weekends":
        return {5, 6}
    return {DAY_NAMES.index(day) for day in schedule.get("days", [])}


def next_run(schedule: dict[str, Any], after: datetime) -> datetime | None:
    """The first firing strictly after the given moment, or None when the
    schedule has nothing left (a one-off that already ran)."""
    if schedule["repeat"] == "once":
        moment = datetime.fromisoformat(schedule["at"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=after.tzinfo)
        return moment if moment > after else None
    at = _parse_time(schedule["time"])
    days = _days_for(schedule)
    for offset in range(8):
        day = (after + timedelta(days=offset)).replace(
            hour=at.hour, minute=at.minute, second=0, microsecond=0
        )
        if day.weekday() in days and day > after:
            return day
    return None


def describe_schedule(schedule: dict[str, Any]) -> str:
    repeat = schedule["repeat"]
    if repeat == "once":
        moment = datetime.fromisoformat(schedule["at"])
        return moment.strftime("%A %B %-d at %-I:%M %p")
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    clock = dtime(hour, minute).strftime("%-I:%M %p")
    if repeat == "weekly":
        return f"{', '.join(day.title() for day in schedule['days'])} at {clock}"
    return f"{repeat} at {clock}"


def normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    kind = str(action.get("type", "")).strip().lower()
    if kind not in ACTION_TYPES:
        raise ScheduleError(f"action type must be one of {', '.join(ACTION_TYPES)}")
    if kind in ("say", "ask"):
        text = " ".join(str(action.get("text", "")).split())
        if not text or len(text) > 700:
            raise ScheduleError(f"A {kind} action needs text under 700 characters")
        out: dict[str, Any] = {"type": kind, "text": text}
        if kind == "ask" and action.get("quiet"):
            # Only worth interrupting anyone for if there is something to say.
            out["quiet"] = True
        if action.get("event_id") is not None:
            out["event_id"] = int(action["event_id"])
        if action.get("external_key"):
            out["external_key"] = str(action["external_key"])
        return out
    device = str(action.get("device", "")).strip()
    if not device:
        raise ScheduleError(f"A {kind} action needs a device")
    if kind == "device":
        out: dict[str, Any] = {"type": "device", "device": device}
        if action.get("power") in ("on", "off"):
            out["power"] = action["power"]
        if action.get("brightness") is not None:
            out["brightness"] = max(0, min(100, int(action["brightness"])))
        if action.get("color_temp") is not None:
            out["color_temp"] = max(1500, min(6500, int(action["color_temp"])))
        if len(out) == 2:
            raise ScheduleError("A device action needs power, brightness, or color_temp")
        return out
    minutes = float(action.get("minutes", 0) or 0)
    if not 0 <= minutes <= MAX_FADE_MINUTES:
        raise ScheduleError(f"minutes must be between 0 and {MAX_FADE_MINUTES}")
    if action.get("brightness_to") is None:
        raise ScheduleError("A fade needs brightness_to")
    out = {
        "type": "fade",
        "device": device,
        "brightness_to": max(0, min(100, int(action["brightness_to"]))),
        "minutes": minutes,
    }
    # A room or "all the lights": every light fades together. `device` stays
    # the first of them, so automations saved before this still read.
    devices = [str(d).strip() for d in action.get("devices") or [] if str(d).strip()]
    if len(devices) > 1:
        out["devices"] = devices
    if action.get("label"):
        out["label"] = " ".join(str(action["label"]).split())[:120]
    if action.get("brightness_from") is not None:
        out["brightness_from"] = max(0, min(100, int(action["brightness_from"])))
    for key in ("color_temp_from", "color_temp_to"):
        if action.get(key) is not None:
            out[key] = max(1500, min(6500, int(action[key])))
    return out


def describe_action(action: dict[str, Any]) -> str:
    kind = action["type"]
    if kind == "say":
        return f"say: {action['text']}"
    if kind == "ask":
        prefix = "heads-up if needed" if action.get("quiet") else "check and report"
        return f"{prefix}: {action['text']}"
    if kind == "device":
        parts = [f"{key} {value}" for key, value in action.items() if key not in ("type", "device")]
        return f"set {action['device']} to {', '.join(parts)}"
    start = action.get("brightness_from")
    origin = f"from {start}%" if start is not None else "from current level"
    tail = f" over {action['minutes']:g} minutes" if action["minutes"] else ""
    return f"fade {action.get('label') or action['device']} {origin} to {action['brightness_to']}%{tail}"


class Announcer:
    """Unprompted messages to whoever is listening: reminders, the answer to
    a scheduled question, a fade that finished. Every connected client gets
    every household-wide event; a client that reconnects gets the ones it
    missed. An event published with a conversation_id belongs to ONE
    conversation — the person pairing a light hears about their light, and
    nobody else's screen does. Several people can talk to the house at once
    without their sessions bleeding into each other."""

    def __init__(self, backlog: int = 50) -> None:
        self._queues: dict[asyncio.Queue[dict[str, Any]], str | None] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=backlog)
        self._counter = 0

    @staticmethod
    def _addressed_to(event: dict[str, Any], conversation_id: str | None) -> bool:
        target = event.get("conversation_id")
        return target is None or target == conversation_id

    def publish(self, text: str, **extra: Any) -> dict[str, Any]:
        self._counter += 1
        event = {
            "id": self._counter,
            "type": "announce",
            "text": text,
            "at": local_now().isoformat(timespec="seconds"),
            **extra,
        }
        self.recent.append(event)
        for queue, conversation_id in list(self._queues.items()):
            if self._addressed_to(event, conversation_id):
                queue.put_nowait(event)
        log.info("announce: %s", text)
        return event

    def since(self, last_id: int, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return [
            event
            for event in self.recent
            if event["id"] > last_id and self._addressed_to(event, conversation_id)
        ]

    def subscribe(self, conversation_id: str | None = None) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[queue] = conversation_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.pop(queue, None)

    @property
    def listeners(self) -> int:
        return len(self._queues)


class FadeRunner:
    """Gradual brightness and color-temperature changes.

    One fade per device: starting a new one on a light cancels the fade it
    was in the middle of, so "actually, lights out now" wins over a sunrise
    still under way.
    """

    def __init__(self, hub: SmartHomeHub, database: Any = None) -> None:
        self.hub = hub
        self.database = database  # fades are written here so a restart resumes them
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._plans: dict[str, dict[str, Any]] = {}

    async def fade(
        self,
        device_query: str,
        brightness_to: int,
        minutes: float,
        brightness_from: int | None = None,
        color_temp_from: int | None = None,
        color_temp_to: int | None = None,
        wait: bool = False,
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Start a fade -- or, given started_at in the past, pick it up from
        where the clock says it should be by now (a schedule that came due
        while the server was down, a fade cut short by a restart). Past its
        end, that means applying the final level at once."""
        device = self.hub.find(device_query)
        self.cancel(device.key)
        state = device.state or {}
        if brightness_from is None:
            brightness_from = (
                int(state.get("brightness") or 0) if state.get("power", True) else 0
            )
        if color_temp_to is not None and color_temp_from is None:
            color_temp_from = int(state.get("color_temp_kelvin") or color_temp_to)
        total_seconds = max(0.0, float(minutes)) * 60
        span = abs(brightness_to - brightness_from)
        if color_temp_to is not None and color_temp_from is not None:
            span = max(span, abs(color_temp_to - color_temp_from) // 50)
        steps = 1 if total_seconds <= 0 else max(1, min(span or 1, int(total_seconds / MIN_STEP_SECONDS)))
        now = local_now()
        started = started_at or now
        elapsed = max(0.0, (now - started).total_seconds())
        first_index = 1
        if elapsed > 0 and total_seconds > 0:
            first_index = min(steps, int(elapsed / (total_seconds / steps)) + 1)
        plan = {
            "device": device.name,
            "device_id": device.key,
            "brightness_from": brightness_from,
            "brightness_to": brightness_to,
            "color_temp_from": color_temp_from,
            "color_temp_to": color_temp_to,
            "minutes": minutes,
            "steps": steps,
            "started_at": started.isoformat(timespec="seconds"),
        }
        if first_index > 1:
            plan["resumed_at_step"] = first_index
        if self.database is not None:
            ends = started + timedelta(seconds=total_seconds)
            self.database.save_fade(
                device.key, plan, plan["started_at"], ends.isoformat(timespec="seconds")
            )
        task = asyncio.create_task(self._run(device.key, plan, total_seconds, first_index))
        self._tasks[device.key] = task
        self._plans[device.key] = plan
        task.add_done_callback(lambda _: self._forget(device.key, task))
        if wait:
            await task
        return plan

    def _forget(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._plans.pop(key, None)

    async def _run(
        self, key: str, plan: dict[str, Any], total_seconds: float, first_index: int = 1
    ) -> None:
        steps = plan["steps"]
        interval = total_seconds / steps if steps else 0
        start, end = plan["brightness_from"], plan["brightness_to"]
        cfrom, cto = plan["color_temp_from"], plan["color_temp_to"]
        failures = 0
        for index in range(first_index, steps + 1):
            fraction = index / steps
            level = round(start + (end - start) * fraction)
            color = (
                round(cfrom + (cto - cfrom) * fraction) if cfrom is not None and cto is not None else None
            )
            try:
                if level <= 0 and index == steps:
                    await self.hub.control(key, power="off")
                else:
                    await self.hub.control(
                        key,
                        power="on" if level > 0 and index == first_index else None,
                        brightness=level,
                        color_temp_kelvin=color,
                    )
                failures = 0
            except SmartHomeError as exc:
                failures += 1
                log.warning("fade %s step %d/%d failed: %s", plan["device"], index, steps, exc)
                if failures >= 5:
                    log.error("fade %s abandoned after repeated failures", plan["device"])
                    break
            if index < steps:
                await asyncio.sleep(interval)
        # Finished or given up: nothing to resume. (A cancelled fade skips
        # this on purpose -- a replacement rewrites the row, and a shutdown
        # leaves it for the next start to pick up.)
        if self.database is not None:
            self.database.delete_fade(key)

    async def _device_back(self, key: str, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                self.hub.find(key)
                return True
            except SmartHomeError:
                if asyncio.get_running_loop().time() >= deadline:
                    return False
                await asyncio.sleep(3)

    async def resume(self, wait_for_devices: float = 90.0) -> None:
        """Pick up fades a previous run left unfinished, once their lights
        are reachable again (the controllers connect after startup)."""
        if self.database is None:
            return
        for saved in self.database.list_fades():
            plan, key = saved["plan"], saved["device_id"]
            started = datetime.fromisoformat(saved["started_at"])
            if not await self._device_back(key, wait_for_devices):
                log.warning("fade %s not resumed: %s never came back", plan["device"], key)
                continue
            resumed = await self.fade(
                key,
                int(plan["brightness_to"]),
                float(plan["minutes"]),
                brightness_from=plan.get("brightness_from"),
                color_temp_from=plan.get("color_temp_from"),
                color_temp_to=plan.get("color_temp_to"),
                started_at=started,
            )
            log.info(
                "fade %s resumed at step %s/%s (started %s)",
                plan["device"], resumed.get("resumed_at_step", 1), resumed["steps"], saved["started_at"],
            )

    def cancel(self, key: str) -> bool:
        task = self._tasks.pop(key, None)
        self._plans.pop(key, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def running(self) -> list[dict[str, Any]]:
        return [dict(plan) for plan in self._plans.values()]

    async def stop(self) -> None:
        for key in list(self._tasks):
            self.cancel(key)


# (question, quiet) -> spoken answer. A quiet ask answers "Nothing to report"
# when there is nothing worth saying, and that answer is never announced.
Asker = Callable[[str, bool], Awaitable[str]]
NOTHING_TO_REPORT = "nothing to report"


class Scheduler:
    """Runs whatever the automations table says is due.

    ``ask`` answers a question the way a spoken turn would (through the
    agent and its tools), so a scheduled "will it rain during my meeting" is
    looked up at the moment it fires, not when it was set.
    """

    def __init__(
        self,
        database: Any,
        announcer: Announcer,
        fades: FadeRunner,
        ask: Asker | None = None,
        tick_seconds: float = 15.0,
    ) -> None:
        self.database = database
        self.announcer = announcer
        self.fades = fades
        self.ask = ask
        self.tick_seconds = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._resume: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="automations")
            self._resume = asyncio.create_task(self.fades.resume(), name="fade-resume")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.fades.stop()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the clock must keep ticking
                log.exception("automation tick failed")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or local_now()
        fired: list[dict[str, Any]] = []
        for automation in self.database.due_automations(now.isoformat(timespec="minutes")):
            fired.append(await self._fire(automation, now))
        return fired

    async def _fire(self, automation: dict[str, Any], now: datetime) -> dict[str, Any]:
        schedule = automation["schedule"]
        following = next_run(schedule, now)
        scheduled_for = datetime.fromisoformat(automation["next_run_at"])
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=now.tzinfo)
        late = now - scheduled_for
        action = automation["action"]
        # A light change is still worth making for a while after it was due:
        # a fade picks up from where it should be by now, or lands on its
        # final level; a plain set just happens. Something to say is not --
        # a 7:30 "wake up" at three in the afternoon is noise.
        allowed_late = LATE_GRACE
        if action["type"] in {"fade", "device"}:
            allowed_late = LIGHT_CATCH_UP + timedelta(minutes=float(action.get("minutes") or 0))
        if schedule["repeat"] != "once" and late > allowed_late:
            result = f"missed (came due at {scheduled_for.strftime('%H:%M')} while the server was off)"
        else:
            try:
                result = await self.run_action(automation, scheduled_for=scheduled_for)
                if late > LATE_GRACE:
                    result = f"late by {int(late.total_seconds() // 60)} min; {result}"
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
                log.exception("automation %s failed", automation["name"])
                result = f"failed: {exc}"
        self.database.mark_automation_run(
            automation["id"],
            now.isoformat(timespec="seconds"),
            following.isoformat(timespec="minutes") if following else None,
            result[:300],
        )
        return {"id": automation["id"], "name": automation["name"], "result": result}

    async def run_action(
        self, automation: dict[str, Any], scheduled_for: datetime | None = None
    ) -> str:
        action = automation["action"]
        kind = action["type"]
        source = automation["name"]
        if kind == "say":
            self.announcer.publish(action["text"], source=source, profile=automation.get("profile"))
            return "announced"
        if kind == "ask":
            if self.ask is None:
                raise ScheduleError("No agent is available to answer scheduled questions")
            quiet = bool(action.get("quiet"))
            reply = await self.ask(action["text"], quiet)
            if quiet and NOTHING_TO_REPORT in reply.casefold()[:120]:
                return "checked; nothing to report"
            self.announcer.publish(reply, source=source, profile=automation.get("profile"))
            return f"asked; answered: {reply}"
        if kind == "device":
            await self.fades.hub.control(
                action["device"],
                power=action.get("power"),
                brightness=action.get("brightness"),
                color_temp_kelvin=action.get("color_temp"),
            )
            return "set"
        results = []
        for device in action.get("devices") or [action["device"]]:
            try:
                plan = await self.fades.fade(
                    device,
                    action["brightness_to"],
                    action["minutes"],
                    brightness_from=action.get("brightness_from"),
                    color_temp_from=action.get("color_temp_from"),
                    color_temp_to=action.get("color_temp_to"),
                    started_at=scheduled_for,
                )
            except Exception as exc:  # noqa: BLE001 - one missing light must not stop the others
                if len(action.get("devices") or []) < 2:
                    raise
                results.append(f"{device} failed: {exc}")
                continue
            where = f" from step {plan['resumed_at_step']}" if plan.get("resumed_at_step") else ""
            results.append(
                f"fading {plan['device']} {plan['brightness_from']}% to {plan['brightness_to']}% "
                f"in {plan['steps']} steps{where}"
            )
        return "; ".join(results)
