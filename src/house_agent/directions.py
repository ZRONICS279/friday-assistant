"""Travel time and distance, with no API key anywhere.

Addresses are resolved by Nominatim and routes by OSRM, both run by the
OpenStreetMap community and both open without registration. Their fair-use
policies suit a household asking a few times a day; they are not for bulk
traffic. Set an OpenRouteService key if you ever want a service with an
actual availability guarantee — the same tool then uses it instead."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Callable

import httpx

from .config import DirectionsConfig
from .weather import HomeLocationUnknown


class DirectionsError(RuntimeError):
    pass


_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# One OSRM instance per travel mode. The better-known demo server at
# router.project-osrm.org hosts only the car profile and answers a foot or
# bike request with car routing — 23 mph "walking" — so it is not used.
_OSRM_URL = "https://routing.openstreetmap.de"
_ORS_URL = "https://api.openrouteservice.org/v2/directions"
_TOMTOM_URL = "https://api.tomtom.com/routing/1/calculateRoute"

# Both services ask callers to identify themselves.
_USER_AGENT = "house-agent/1.0 (Friday home assistant)"

# How the model names a mode -> the profile each router calls it.
_PROFILES = {
    "driving": {"osrm": "routed-car", "ors": "driving-car"},
    "walking": {"osrm": "routed-foot", "ors": "foot-walking"},
    "cycling": {"osrm": "routed-bike", "ors": "cycling-regular"},
}

_METERS_PER_MILE = 1609.344


class DirectionsService:
    def __init__(
        self,
        config: DirectionsConfig,
        home: Callable[[], tuple[float, float, str] | None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        # Where home is lives in one place for the whole house; directions
        # reads the same answer the weather does rather than keeping its own.
        self._home = home
        self._injected = client

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._injected is not None:
            yield self._injected
            return
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            yield client

    async def _place(
        self, client: httpx.AsyncClient, query: str | None, what: str
    ) -> tuple[float, float, str]:
        """A missing query means home; anything else is geocoded as typed, so
        street addresses and business names both work."""
        if not query:
            home = self._home() if self._home else None
            if home is None:
                raise HomeLocationUnknown(
                    "I do not know where this house is yet."
                )
            return home
        response = await client.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 0},
        )
        response.raise_for_status()
        matches = response.json()
        if not matches:
            raise DirectionsError(f"No {what} found matching '{query}'.")
        match = matches[0]
        label = str(match.get("display_name") or query)
        # Nominatim labels run to six comma-separated parts; the first three
        # are what a person would actually say back.
        short = ", ".join(part.strip() for part in label.split(",")[:3])
        return float(match["lat"]), float(match["lon"]), short

    def _distance(self, meters: float) -> tuple[float, str]:
        if self.config.units == "metric":
            return round(meters / 1000, 1), "km"
        return round(meters / _METERS_PER_MILE, 1), "miles"

    async def _route_osrm(
        self,
        client: httpx.AsyncClient,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: str,
    ) -> dict[str, Any]:
        profile = _PROFILES[mode]["osrm"]
        path = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        # The trailing "driving" is an OSRM quirk: each instance serves one
        # profile and ignores this segment.
        response = await client.get(
            f"{_OSRM_URL}/{profile}/route/v1/driving/{path}",
            params={"overview": "false", "steps": "true"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise DirectionsError(
                f"No {mode} route found between those two places."
            )
        route = payload["routes"][0]
        steps = route.get("legs", [{}])[0].get("steps") or []
        return {
            "seconds": float(route["duration"]),
            "meters": float(route["distance"]),
            "roads": _main_roads(step.get("name", "") for step in steps),
            "via": "OpenStreetMap routing",
        }

    async def _route_ors(
        self,
        client: httpx.AsyncClient,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: str,
    ) -> dict[str, Any]:
        profile = _PROFILES[mode]["ors"]
        response = await client.post(
            f"{_ORS_URL}/{profile}",
            headers={"Authorization": self.config.openrouteservice_key or ""},
            json={
                "coordinates": [
                    [origin[1], origin[0]],
                    [destination[1], destination[0]],
                ]
            },
        )
        response.raise_for_status()
        routes = response.json().get("routes") or []
        if not routes:
            raise DirectionsError(f"No {mode} route found between those two places.")
        summary = routes[0].get("summary") or {}
        return {
            "seconds": float(summary.get("duration", 0)),
            "meters": float(summary.get("distance", 0)),
            "roads": [],
            "via": "OpenRouteService",
        }

    async def _route_tomtom(
        self,
        client: httpx.AsyncClient,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: str,
    ) -> dict[str, Any]:
        """The only routed source here with live traffic. Asking for
        computeTravelTimeFor=all also returns the free-flow time, so the
        result can say how much of the journey is congestion."""
        path = f"{origin[0]},{origin[1]}:{destination[0]},{destination[1]}"
        response = await client.get(
            f"{_TOMTOM_URL}/{path}/json",
            params={
                "key": self.config.tomtom_key or "",
                "traffic": "true",
                "computeTravelTimeFor": "all",
                "travelMode": "car",
            },
        )
        response.raise_for_status()
        routes = response.json().get("routes") or []
        if not routes:
            raise DirectionsError(f"No {mode} route found between those two places.")
        summary = routes[0].get("summary") or {}
        delay = summary.get("trafficDelayInSeconds")
        return {
            "seconds": float(summary.get("travelTimeInSeconds", 0)),
            "meters": float(summary.get("lengthInMeters", 0)),
            "roads": [],
            "via": "TomTom (live traffic)",
            "traffic_delay_minutes": (
                round(float(delay) / 60) if delay is not None else None
            ),
            "free_flow_minutes": (
                round(float(summary["noTrafficTravelTimeInSeconds"]) / 60)
                if summary.get("noTrafficTravelTimeInSeconds")
                else None
            ),
            "arrival_time": summary.get("arrivalTime"),
        }

    def _router(self, mode: str) -> Any:
        """Live traffic only matters for driving, and only TomTom has it."""
        if self.config.tomtom_key and mode == "driving":
            return self._route_tomtom
        if self.config.openrouteservice_key:
            return self._route_ors
        return self._route_osrm

    async def travel_time(
        self,
        destination: str,
        origin: str | None = None,
        mode: str = "driving",
    ) -> dict[str, Any]:
        mode = str(mode).strip().lower()
        if mode not in _PROFILES:
            raise DirectionsError(
                f"Unsupported mode '{mode}'. Use driving, walking, or cycling."
            )
        if not destination or not destination.strip():
            raise DirectionsError("A destination is required.")
        try:
            async with self._client() as client:
                start = await self._place(client, origin, "starting point")
                end = await self._place(client, destination, "destination")
                route = await self._router(mode)(client, start[:2], end[:2], mode)
        except httpx.HTTPError as exc:
            raise DirectionsError(f"Directions lookup failed: {exc}") from exc

        distance, unit = self._distance(route["meters"])
        minutes = max(1, round(route["seconds"] / 60))
        return {
            "origin": start[2],
            "destination": end[2],
            "mode": mode,
            "minutes": minutes,
            "duration_spoken": _spoken_duration(minutes),
            "distance": distance,
            "distance_unit": unit,
            "roads": route["roads"],
            "source": route["via"],
            **{
                key: route[key]
                for key in ("traffic_delay_minutes", "free_flow_minutes", "arrival_time")
                if route.get(key) is not None
            },
            "note": (
                "Includes live traffic."
                if route.get("traffic_delay_minutes") is not None
                else "Typical travel time without live traffic; add margin at rush hour."
            ),
        }


def _main_roads(names: Any, limit: int = 4) -> list[str]:
    """The named roads a route actually uses, in order and without the
    repeats OSRM emits for every manoeuvre on the same street."""
    roads: list[str] = []
    for name in names:
        cleaned = str(name).strip()
        if cleaned and (not roads or roads[-1] != cleaned):
            roads.append(cleaned)
    return roads[:limit]


def _spoken_duration(minutes: int) -> str:
    """Replies are read aloud, so 95 minutes should say an hour and a half."""
    if minutes < 60:
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    hours, remainder = divmod(minutes, 60)
    hour_word = "hour" if hours == 1 else "hours"
    if remainder == 0:
        return f"{hours} {hour_word}"
    if remainder == 30:
        # Always plural: it is "one and a half hours", never "hour".
        return f"{hours} and a half hours"
    return f"{hours} {hour_word} {remainder} minutes"
