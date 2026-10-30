"""Friday's events as an iCalendar feed that any calendar app can subscribe to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _stamp(value: str | datetime) -> str:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    """RFC 5545 limits lines to 75 octets; continuation lines start with a space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    pieces: list[str] = []
    while encoded:
        chunk = encoded[:74]
        # Never split inside a multi-byte character.
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        pieces.append(chunk.decode("utf-8"))
        encoded = encoded[len(chunk):]
    return "\r\n ".join(pieces)


def render_ics(events: list[dict[str, Any]]) -> str:
    now = datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Friday//House Agent//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Friday",
    ]
    for event in events:
        start = datetime.fromisoformat(event["starts_at"])
        end = (
            datetime.fromisoformat(event["ends_at"])
            if event.get("ends_at")
            else start + timedelta(hours=1)
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:friday-{event['id']}@friday.local",
            f"DTSTAMP:{_stamp(now)}",
            f"DTSTART:{_stamp(start)}",
            f"DTEND:{_stamp(end)}",
            f"SUMMARY:{_escape(str(event['title']))}",
        ]
        if event.get("location"):
            lines.append(f"LOCATION:{_escape(str(event['location']))}")
        if event.get("notes"):
            lines.append(f"DESCRIPTION:{_escape(str(event['notes']))}")
        # An alert half an hour before, so a subscribed phone rings for it.
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(str(event['title']))}",
            "TRIGGER:-PT30M",
            "END:VALARM",
        ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
