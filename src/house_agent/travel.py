"""Flights and hotels, searched live through LetsFG (letsfg.co).

LetsFG is a hosted search engine built for agents: every search runs on
their servers and comes back as structured JSON — price, airline, stops,
duration, booking link — which is what a spoken answer needs. Nothing runs
on this machine beyond two HTTPS calls, so the GPUs never notice it.

Flights need a free Bearer token: `letsfg auth` once, which is a zero-amount
card setup (nothing is charged, searches are free), and the token lands in
~/.letsfg/config.json where this module finds it. Hotels go through the
site's own search route, which asks for no token at all. Neither is a
booking: the result carries the link to book with, and that is the user's
job in a browser."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .config import TravelConfig
from .keys import MissingKey
from .directions import _spoken_duration


class TravelError(RuntimeError):
    pass


class TravelNotConfigured(TravelError, MissingKey):
    """No usable LetsFG token: flight search cannot run until `letsfg auth`."""

    def __init__(self, message: str) -> None:
        MissingKey.__init__(self, message, feature="flight search", key="letsfg auth", group="flights")


_BASE_URL = "https://letsfg.co"
# The site's Cloudflare front rejects library default agents outright; a
# browser-shaped one is what its own pages send.
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0"
_IATA = re.compile(r"^[A-Za-z]{3}$")
_CABINS = ("economy", "premium_economy", "business", "first")

# "Near the Strip" or "downtown" is resolved by Nominatim, the OpenStreetMap
# geocoder the directions tool already relies on, and hotels are kept when
# they fall within a walk of that point.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_AGENT = "house-agent/1.0 (Friday home assistant)"
_NEAR_RADIUS_KM = 3.0
_METERS_PER_MILE = 1609.344
# How the supplier grades guest reviews, best first. A hotel nobody has
# reviewed ranks below a "good" one rather than beside it.
_GUEST_RANK = {"excellent": 4, "very good": 3, "good": 2, "fair": 1, "poor": 0}
_UNRATED_RANK = 1.5
_SORTS = ("best", "cheapest")

# A flight search is asynchronous on their side: POST returns a search id,
# and the results endpoint reports `searching` until the airline fan-out
# finishes. Anything else is terminal.
_IN_PROGRESS = {"pending", "running", "searching"}
_POLL_SECONDS = 2.0
# The offer set can still grow briefly after `completed` (a split-ticket
# probe lands late); the cheapest fare is often in that late batch, so wait
# a little for the flags to clear — but only a little.
_SETTLE_SECONDS = 20.0
_MAX_RESULTS = 5


def _token_file() -> Path:
    return Path.home() / ".letsfg" / "config.json"


def bearer_token(config: TravelConfig) -> str:
    """The token `letsfg auth` saved, or the one configured explicitly."""
    explicit = config.letsfg_token or os.environ.get("LETSFG_BEARER_TOKEN")
    if explicit:
        return explicit.strip()
    saved: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        saved = json.loads(_token_file().read_text())
    auth = saved.get("pfs_auth") if isinstance(saved, dict) else None
    token = (auth or {}).get("token")
    expires_at = (auth or {}).get("expires_at") or 0
    if token and expires_at and time.time() > float(expires_at) - 3600:
        raise TravelNotConfigured(
            "The LetsFG flight-search token has expired. Run `letsfg auth` "
            "on the house computer to renew it; hotel search still works."
        )
    if token:
        return str(token)
    raise TravelNotConfigured(
        "Flight search is not set up on this house yet: run `letsfg auth` "
        "once on the house computer (a free, zero-amount card setup). Hotel "
        "search works without it."
    )


def _iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a date written YYYY-MM-DD") from exc
    if parsed < date.today():
        raise ValueError(f"{label} {parsed.isoformat()} is in the past")
    return parsed


def _airport(value: str, label: str) -> str:
    code = str(value).strip().upper()
    if not _IATA.match(code):
        raise ValueError(
            f"{label} must be a three-letter airport code such as DEN or LHR, not {value!r}"
        )
    return code


def _clock(stamp: Any) -> str:
    """2026-10-12T06:25:00 -> 2026-10-12 06:25, which reads aloud cleanly."""
    text = str(stamp or "")
    if "T" in text:
        day, _, clock = text.partition("T")
        return f"{day} {clock[:5]}"
    return text


def _unique(values: list[Any]) -> list[str]:
    seen: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _leg(route: dict[str, Any]) -> dict[str, Any]:
    segments = [s for s in route.get("segments") or [] if isinstance(s, dict)]
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    seconds = int(route.get("total_duration_seconds") or 0)
    return {
        "from": first.get("origin", ""),
        "to": last.get("destination", ""),
        "departs": _clock(first.get("departure")),
        "arrives": _clock(last.get("arrival")),
        "duration": _spoken_duration(seconds // 60) if seconds else "",
        "stops": int(route.get("stopovers") or max(len(segments) - 1, 0)),
        "airlines": _unique([s.get("airline_name") or s.get("airline") for s in segments]),
        "flights": _unique([s.get("flight_no") for s in segments]),
    }


def _flight(offer: dict[str, Any], currency: str) -> dict[str, Any]:
    """One offer in the shape a reply needs, whichever shape the API used.
    Results carry nested outbound/inbound routes; older and partial ones a
    flat airline/departure_time/stops record."""
    if isinstance(offer.get("outbound"), dict):
        outbound = _leg(offer["outbound"])
        inbound = _leg(offer["inbound"]) if isinstance(offer.get("inbound"), dict) else None
        airlines = _unique(offer.get("airlines") or []) or outbound["airlines"]
    else:
        minutes = int(offer.get("duration_minutes") or 0)
        outbound = {
            "from": offer.get("origin", ""),
            "to": offer.get("destination", ""),
            "departs": _clock(offer.get("departure_time")),
            "arrives": _clock(offer.get("arrival_time")),
            "duration": _spoken_duration(minutes) if minutes else "",
            "stops": int(offer.get("stops") or 0),
            "airlines": _unique([offer.get("airline") or offer.get("airline_code")]),
            "flights": [],
        }
        inbound = None
        airlines = outbound["airlines"]
    return {
        "price": round(float(offer.get("price") or 0), 2),
        "currency": str(offer.get("currency") or currency),
        "airlines": airlines,
        "stops": outbound["stops"] + (inbound["stops"] if inbound else 0),
        "outbound": outbound,
        "inbound": inbound,
        "booking_url": str(offer.get("booking_url") or ""),
    }


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _spoken_distance(km: float, units: str) -> str:
    if units == "metric":
        return f"{round(km, 1):g} km"
    return f"{round(km * 1000 / _METERS_PER_MILE, 1):g} miles"


def _quality(hotel: dict[str, Any]) -> tuple[float, float, float, float]:
    """Best first: guest verdict, then stars, then how many people said so,
    then price. A 4-star "Excellent" beats a 2-star "Fair" whatever they cost."""
    verdict = str(hotel.get("guest_rating") or "").strip().lower()
    return (
        -_GUEST_RANK.get(verdict, _UNRATED_RANK),
        -float(hotel.get("stars") or 0),
        -float(hotel.get("reviews") or 0),
        float(hotel["price_total"]),
    )


def _hotel(entry: dict[str, Any], nights: int, currency: str) -> dict[str, Any]:
    total = round(float(entry.get("price") or 0), 2)
    return {
        "name": entry.get("name", ""),
        "stars": entry.get("starRating"),
        "guest_rating": entry.get("guestRating"),
        "reviews": entry.get("reviewCount"),
        "address": str(entry.get("address") or "").strip(" ,"),
        "price_total": total,
        "price_per_night": round(total / nights, 2) if nights else total,
        "currency": str(entry.get("currency") or currency),
        "room": entry.get("roomName"),
        "meal_plan": entry.get("mealPlan"),
        "free_cancellation_until": (
            _clock(entry.get("freeUntil")) if entry.get("freeCancellation") else None
        ),
        "_lat": entry.get("lat"),
        "_lon": entry.get("lon"),
    }


class TravelService:
    def __init__(self, config: TravelConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=15),
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            yield client

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TravelError(f"The travel search service could not be reached: {exc}") from exc
        if response.status_code in (401, 402, 403) and token:
            raise TravelNotConfigured(
                "LetsFG rejected the flight-search token. Run `letsfg auth` on "
                "the house computer to renew it; hotel search still works."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise TravelError(
                f"The travel search service answered {response.status_code} "
                "with something that was not a result."
            ) from exc
        if response.status_code >= 400:
            detail = data.get("error") or data.get("message") or response.status_code
            raise TravelError(f"Travel search failed: {detail}")
        return data if isinstance(data, dict) else {}

    async def _locate(
        self, client: httpx.AsyncClient, near: str, city: str
    ) -> tuple[float, float, str]:
        try:
            response = await client.get(
                _NOMINATIM_URL,
                params={"q": f"{near}, {city}", "format": "json", "limit": 1},
                headers={"User-Agent": _NOMINATIM_AGENT},
            )
            response.raise_for_status()
            hits = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TravelError(f"Could not look up where {near!r} is: {exc}") from exc
        if not hits:
            raise TravelError(f"Could not find {near!r} in {city}; try a landmark or street.")
        hit = hits[0]
        return float(hit["lat"]), float(hit["lon"]), str(hit.get("display_name") or near)

    async def search_flights(
        self,
        origin: str,
        destination: str,
        depart: str,
        return_date: str | None = None,
        adults: int = 1,
        cabin: str | None = None,
        max_stops: int | None = None,
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]:
        origin_code = _airport(origin, "origin")
        destination_code = _airport(destination, "destination")
        if origin_code == destination_code:
            raise ValueError("origin and destination are the same airport")
        depart_on = _iso_date(depart, "depart")
        back_on = _iso_date(return_date, "return_date") if return_date else None
        if back_on and back_on < depart_on:
            raise ValueError("return_date is before the departure date")
        if cabin and cabin not in _CABINS:
            raise ValueError(f"cabin must be one of {', '.join(_CABINS)}")
        adults = max(1, min(int(adults), 9))
        limit = max(1, min(int(limit), _MAX_RESULTS))
        token = bearer_token(self.config)

        body: dict[str, Any] = {
            "origin": origin_code,
            "destination": destination_code,
            "date_from": depart_on.isoformat(),
            "adults": adults,
            "currency": self.config.currency,
            "limit": 40,
        }
        if back_on:
            body["return_date"] = back_on.isoformat()
        if cabin:
            body["cabin_class"] = cabin
        if max_stops is not None:
            body["max_stopovers"] = max(0, int(max_stops))

        async with self._session() as client:
            started = await self._request(client, "POST", "/api/search", token=token, body=body)
            search_id = started.get("search_id") or started.get("id")
            if not search_id:
                results = started
            else:
                results = await self._await_results(client, str(search_id), token)

        offers = [
            _flight(o, self.config.currency)
            for o in results.get("offers") or []
            if isinstance(o, dict) and float(o.get("price") or 0) > 0
        ]
        offers.sort(key=lambda o: o["price"])
        unfinished = results.get("status", "") in _IN_PROGRESS
        report: dict[str, Any] = {
            "origin": origin_code,
            "destination": destination_code,
            "depart": depart_on.isoformat(),
            "return_date": back_on.isoformat() if back_on else None,
            "adults": adults,
            "currency": self.config.currency,
            "total_results": len(offers),
            "offers": offers[:limit],
            "note": (
                "Live fares per person at search time; they change by the hour. "
                "Nothing is booked: the user books through booking_url themselves."
            ),
        }
        if not offers:
            report["note"] = (
                "The search finished with no flights for those airports and dates."
                if not unfinished
                else "The search ran out of time before any flights came back; try again."
            )
            # Empty almost always means a filter, not an empty sky. Say which
            # one to let go of rather than handing the trip back to the user.
            loosen = [
                name
                for name, tight in (
                    ("max_stops (a connection is normal on most routes)", max_stops is not None),
                    ("cabin", (cabin or "economy") != "economy"),
                    ("a day either side of those dates", True),
                    ("the other airport for that city", True),
                )
                if tight
            ]
            report["next_step"] = (
                "Search again yourself before reporting nothing — drop or widen "
                + "; then ".join(loosen)
                + ". Only when a plainer search is also empty do you say there are no "
                "flights, and say which airports and dates you tried."
            )
        elif unfinished:
            report["note"] += " The search was still running; cheaper fares may appear on a retry."
        return report

    async def _await_results(
        self, client: httpx.AsyncClient, search_id: str, token: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        path = f"/api/results/{search_id}"
        data = await self._request(client, "GET", path, token=token)
        while data.get("status", "") in _IN_PROGRESS and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_SECONDS)
            data = await self._request(client, "GET", path, token=token)
        settle_until = min(deadline, time.monotonic() + _SETTLE_SECONDS)
        while (
            data.get("status", "") not in _IN_PROGRESS
            and (data.get("split_ticket_pending") or data.get("gf_enrich_pending"))
            and time.monotonic() < settle_until
        ):
            await asyncio.sleep(_POLL_SECONDS)
            fresh = await self._request(client, "GET", path, token=token)
            if fresh.get("status", "") not in _IN_PROGRESS:
                data = fresh
        return data

    async def search_hotels(
        self,
        place: str,
        check_in: str,
        check_out: str,
        adults: int = 2,
        max_price_per_night: float | None = None,
        near: str | None = None,
        sort: str = "best",
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]:
        place = str(place).strip()
        if not place:
            raise ValueError("place cannot be empty")
        near = str(near).strip() if near else None
        if sort not in _SORTS:
            raise ValueError(f"sort must be one of {', '.join(_SORTS)}")
        arrive = _iso_date(check_in, "check_in")
        leave = _iso_date(check_out, "check_out")
        nights = (leave - arrive).days
        if nights < 1:
            raise ValueError("check_out must be after check_in")
        adults = max(1, min(int(adults), 8))
        limit = max(1, min(int(limit), _MAX_RESULTS))

        body = {
            "place": place,
            "checkIn": arrive.isoformat(),
            "checkOut": leave.isoformat(),
            "adults": adults,
            "currency": self.config.currency,
            # The site's own page asks for 60; fewer and a whole district can
            # be missing from what there is to choose from.
            "limit": 60,
        }
        async with self._session() as client:
            data = await self._request(client, "POST", "/api/hotels/search", body=body)
            if data.get("error") and not data.get("hotels"):
                raise TravelError(f"Hotel search failed: {data['error']}")
            city = str(data.get("cityName") or place)
            anchor = await self._locate(client, near, city) if near else None

        currency = str(data.get("currency") or self.config.currency)
        hotels = [
            _hotel(h, nights, currency)
            for h in data.get("hotels") or []
            if isinstance(h, dict) and float(h.get("price") or 0) > 0
        ]
        found = len(hotels)
        if anchor:
            lat, lon, _ = anchor
            placed: list[dict[str, Any]] = []
            for hotel in hotels:
                if hotel["_lat"] is None or hotel["_lon"] is None:
                    continue
                km = _km_between(lat, lon, float(hotel["_lat"]), float(hotel["_lon"]))
                if km <= _NEAR_RADIUS_KM:
                    hotel["distance"] = f"{_spoken_distance(km, self.config.units)} from {near}"
                    placed.append(hotel)
            hotels = placed
        for hotel in hotels:
            del hotel["_lat"], hotel["_lon"]
        if max_price_per_night is not None:
            ceiling = float(max_price_per_night)
            hotels = [h for h in hotels if h["price_per_night"] <= ceiling]
        hotels.sort(key=(lambda h: h["price_total"]) if sort == "cheapest" else _quality)

        report: dict[str, Any] = {
            "place": city,
            "check_in": arrive.isoformat(),
            "check_out": leave.isoformat(),
            "nights": nights,
            "adults": adults,
            "currency": currency,
            "sorted_by": "lowest price" if sort == "cheapest" else "guest rating and stars",
            "total_results": len(hotels),
            "hotels": hotels[:limit],
            "note": (
                "Live bookable rates: price_total is for the whole stay in the "
                "room shown. Nothing is booked; booking is done at letsfg.co."
            ),
        }
        if anchor:
            report["near"] = near
            report["within"] = _spoken_distance(_NEAR_RADIUS_KM, self.config.units)
        if not hotels:
            where = f"within {report['within']} of {near} in {city}" if anchor else f"in {city}"
            report["note"] = (
                f"No rooms under {max_price_per_night:g} a night {where} for those dates"
                if max_price_per_night is not None
                else f"No hotels {where} for those dates"
            ) + (f"; {found} elsewhere in the city." if anchor and found else ".")
            report["next_step"] = (
                (
                    "Search again without near — there are rooms in the city, just not in that "
                    "part of it — and tell the user how far out they are."
                    if anchor and found
                    else "Search again with the filters let go one at a time: no near, then no "
                    "max_price_per_night. Only when a plain city search is also empty do you "
                    "say there is nothing, and say which city and dates you tried."
                )
            )
        return report
