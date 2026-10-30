"""Weather for the house: Open-Meteo for conditions and the forecast, the
US National Weather Service for active alerts. Neither service needs an
API key, so this works out of the box with only a latitude and longitude."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

import httpx

from .config import WeatherConfig
from .db import Database


class WeatherError(RuntimeError):
    pass


class HomeLocationUnknown(WeatherError):
    """Nobody has said where the house is yet, so there is nothing to ask
    Open-Meteo about. The agent has to ask the household once and save it."""


# Where a household-supplied home location is kept. Set once, from what
# someone said out loud, and it outlives the conversation that taught it.
HOME_SETTING = "weather_home"


_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_ALERTS_URL = "https://api.weather.gov/alerts/active"

# NWS rejects requests without a User-Agent that identifies the caller.
_USER_AGENT = "house-agent/1.0 (Friday home assistant)"

# WMO codes as plain words: the agent speaks these aloud, so "light rain"
# beats "61" and beats "rain showers, slight intensity".
_CONDITIONS = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}

_UNITS = {
    "imperial": {
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    },
    "metric": {
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    },
}

_CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m,is_day"
)
_HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m"
)
_DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,precipitation_sum,wind_speed_10m_max,"
    "sunrise,sunset"
)


def describe(code: Any) -> str:
    try:
        return _CONDITIONS.get(int(code), "unsettled")
    except (TypeError, ValueError):
        return "unknown"


def _round(value: Any, places: int = 0) -> float | int | None:
    if value is None:
        return None
    try:
        number = round(float(value), places)
    except (TypeError, ValueError):
        return None
    return int(number) if places == 0 else number


class WeatherService:
    def __init__(
        self,
        config: WeatherConfig,
        database: Database | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self._injected = client

    def home(self) -> tuple[float, float, str] | None:
        """The house's own coordinates, or None if nobody has said yet.

        A saved location wins over the configured one: somebody telling the
        house where it lives is newer information than a config default, and
        it is the only way to correct coordinates without editing files."""
        saved = self._saved_home()
        if saved is not None:
            return saved
        if self.config.latitude is None or self.config.longitude is None:
            return None
        return self.config.latitude, self.config.longitude, self.config.place

    def _saved_home(self) -> tuple[float, float, str] | None:
        if self.database is None:
            return None
        raw = self.database.get_setting(HOME_SETTING)
        if not raw:
            return None
        try:
            saved = json.loads(raw)
            return (
                float(saved["latitude"]),
                float(saved["longitude"]),
                str(saved["place"]),
            )
        except (TypeError, ValueError, KeyError):
            return None  # corrupt setting: fall back to config, then to asking

    async def set_home(self, location: str) -> dict[str, Any]:
        """Remember where the house is, from a town or city someone named."""
        if self.database is None:
            raise WeatherError("No storage is available to save a home location.")
        try:
            async with self._client() as client:
                latitude, longitude, place = await self._geocode(client, location)
        except httpx.HTTPError as exc:
            raise WeatherError(f"Location lookup failed: {exc}") from exc
        home = {"latitude": latitude, "longitude": longitude, "place": place}
        self.database.set_setting(HOME_SETTING, json.dumps(home))
        return home

    def _units(self) -> dict[str, str]:
        return _UNITS.get(self.config.units, _UNITS["imperial"])

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """An injected client belongs to the caller, so never close it."""
        if self._injected is not None:
            yield self._injected
            return
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            yield client

    async def _place(
        self, client: httpx.AsyncClient, location: str | None
    ) -> tuple[float, float, str]:
        """Home by default; any named town when the user asks about elsewhere."""
        if not location:
            home = self.home()
            if home is None:
                raise HomeLocationUnknown(
                    "The house's own location is not known yet. Ask which town "
                    "or city they live in, then save it with weather_set_home."
                )
            return home
        return await self._geocode(client, location)

    async def _geocode(
        self, client: httpx.AsyncClient, location: str
    ) -> tuple[float, float, str]:
        response = await client.get(
            _GEOCODE_URL,
            params={"name": location, "count": 5, "language": "en", "format": "json"},
        )
        response.raise_for_status()
        matches = response.json().get("results") or []
        if not matches:
            # One word of the name is often the whole problem ("Bellingham WA",
            # "the Bellingham area"), so the longest word gets one more try
            # before the request is handed back.
            words = sorted(re.findall(r"[^\W\d_]{3,}", location), key=len, reverse=True)
            if words and words[0].casefold() != location.casefold():
                return await self._geocode(client, words[0])
            raise WeatherError(
                f"No place found matching '{location}'. Try the nearest larger town or "
                "city instead, or answer for home without a location."
            )
        match = matches[0]
        name = ", ".join(
            part
            for part in (match.get("name"), match.get("admin1"), match.get("country_code"))
            if part
        )
        return float(match["latitude"]), float(match["longitude"]), name

    async def _forecast(
        self,
        client: httpx.AsyncClient,
        latitude: float,
        longitude: float,
        days: int,
        current: bool,
        hourly: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": _DAILY_FIELDS,
            "forecast_days": days,
            "timezone": "auto",
            **self._units(),
        }
        if current:
            params["current"] = _CURRENT_FIELDS
        if hourly:
            params["hourly"] = _HOURLY_FIELDS
        response = await client.get(_FORECAST_URL, params=params)
        response.raise_for_status()
        return response.json()

    async def _alerts(
        self, client: httpx.AsyncClient, latitude: float, longitude: float
    ) -> list[dict[str, str]]:
        """Severe weather warnings. US-only, and never worth failing the
        whole forecast over, so any error here degrades to no alerts."""
        if not self.config.alerts_enabled:
            return []
        try:
            response = await client.get(
                _ALERTS_URL,
                params={"point": f"{latitude:.4f},{longitude:.4f}", "status": "actual"},
                headers={"Accept": "application/geo+json"},
            )
            response.raise_for_status()
            features = response.json().get("features") or []
        except (httpx.HTTPError, ValueError):
            return []
        alerts = []
        for feature in features[:5]:
            properties = feature.get("properties") or {}
            alerts.append(
                {
                    "event": str(properties.get("event", "Weather alert")),
                    "severity": str(properties.get("severity", "Unknown")),
                    "urgency": str(properties.get("urgency", "Unknown")),
                    "headline": str(properties.get("headline") or "")[:300],
                    "expires": str(properties.get("expires") or ""),
                }
            )
        return alerts

    def _day(self, daily: dict[str, Any], index: int) -> dict[str, Any]:
        def value(field: str) -> Any:
            column = daily.get(field) or []
            return column[index] if index < len(column) else None

        return {
            "date": value("time"),
            "condition": describe(value("weather_code")),
            "high": _round(value("temperature_2m_max")),
            "low": _round(value("temperature_2m_min")),
            "precipitation_chance_percent": _round(value("precipitation_probability_max")),
            "precipitation_total": _round(value("precipitation_sum"), 2),
            "wind_max": _round(value("wind_speed_10m_max")),
            "sunrise": value("sunrise"),
            "sunset": value("sunset"),
        }

    async def now(self, location: str | None = None) -> dict[str, Any]:
        """Conditions right now, plus today's range and any active alerts."""
        try:
            async with self._client() as client:
                latitude, longitude, place = await self._place(client, location)
                payload = await self._forecast(client, latitude, longitude, 1, True)
                alerts = await self._alerts(client, latitude, longitude)
        except httpx.HTTPError as exc:
            raise WeatherError(f"Weather lookup failed: {exc}") from exc

        current = payload.get("current") or {}
        units = payload.get("current_units") or {}
        return {
            "place": place,
            "observed_at": current.get("time"),
            "condition": describe(current.get("weather_code")),
            "code": current.get("weather_code"),
            "temperature": _round(current.get("temperature_2m")),
            "feels_like": _round(current.get("apparent_temperature")),
            "humidity_percent": _round(current.get("relative_humidity_2m")),
            "wind": _round(current.get("wind_speed_10m")),
            "wind_gusts": _round(current.get("wind_gusts_10m")),
            "precipitation_now": _round(current.get("precipitation"), 2),
            "daylight": bool(current.get("is_day")),
            "today": self._day(payload.get("daily") or {}, 0),
            "alerts": alerts,
            "units": {
                "temperature": units.get("temperature_2m", "°F"),
                "wind": units.get("wind_speed_10m", "mph"),
                "precipitation": units.get("precipitation", "inch"),
            },
        }

    async def forecast(self, days: int = 5, location: str | None = None) -> dict[str, Any]:
        """Day-by-day outlook starting today. Open-Meteo tops out at 16 days;
        7 is as far as a local forecast is worth repeating to someone."""
        days = max(1, min(days, 7))
        try:
            async with self._client() as client:
                latitude, longitude, place = await self._place(client, location)
                payload = await self._forecast(client, latitude, longitude, days, False)
                alerts = await self._alerts(client, latitude, longitude)
        except httpx.HTTPError as exc:
            raise WeatherError(f"Weather lookup failed: {exc}") from exc

        daily = payload.get("daily") or {}
        units = payload.get("daily_units") or {}
        return {
            "place": place,
            "days": [self._day(daily, index) for index in range(len(daily.get("time") or []))],
            "alerts": alerts,
            "units": {
                "temperature": units.get("temperature_2m_max", "°F"),
                "wind": units.get("wind_speed_10m_max", "mph"),
                "precipitation": units.get("precipitation_sum", "inch"),
            },
        }

    async def at(self, when: str, location: str | None = None) -> dict[str, Any]:
        """What to expect at a particular hour: the hour itself and the few
        around it, with anything worth a warning picked out. This is what
        makes "will it rain during the party at four" answerable, and what a
        heads-up before a plan is built on."""
        from .scheduler import ScheduleError, local_now, parse_when

        now = local_now()
        try:
            moment = parse_when(when, now)
        except ScheduleError as exc:
            raise WeatherError(str(exc)) from exc
        days_ahead = (moment.date() - now.date()).days
        if days_ahead < 0 or moment < now - timedelta(hours=1):
            raise WeatherError("That time has already passed; ask about the weather now instead.")
        if days_ahead > 6:
            raise WeatherError("Hour-by-hour forecasts only reach seven days out.")
        try:
            async with self._client() as client:
                latitude, longitude, place = await self._place(client, location)
                payload = await self._forecast(
                    client, latitude, longitude, days_ahead + 1, False, hourly=True
                )
                alerts = await self._alerts(client, latitude, longitude)
        except httpx.HTTPError as exc:
            raise WeatherError(f"Weather lookup failed: {exc}") from exc

        hourly = payload.get("hourly") or {}
        times: list[str] = list(hourly.get("time") or [])
        target = moment.strftime("%Y-%m-%dT%H:00")
        if target in times:
            index = times.index(target)
        else:
            index = min(
                range(len(times)), key=lambda i: abs(_hour_of(times[i]) - moment.replace(tzinfo=None)),
                default=-1,
            )
        if index < 0:
            raise WeatherError("No hourly forecast came back for that time.")
        hours = [
            self._hour(hourly, i) for i in range(max(0, index - 1), min(len(times), index + 4))
        ]
        units = payload.get("hourly_units") or {}
        concerns = self._concerns(hours, units)
        return {
            "place": place,
            "for": moment.strftime("%A %Y-%m-%d %H:%M"),
            "hours": hours,
            "concerns": concerns,
            "looks_bad": bool(concerns) or bool(alerts),
            "alerts": alerts,
            "units": {
                "temperature": units.get("temperature_2m", "°F"),
                "wind": units.get("wind_speed_10m", "mph"),
                "precipitation": units.get("precipitation", "inch"),
            },
        }

    def _hour(self, hourly: dict[str, Any], index: int) -> dict[str, Any]:
        def value(field: str) -> Any:
            column = hourly.get(field) or []
            return column[index] if index < len(column) else None

        return {
            "time": value("time"),
            "condition": describe(value("weather_code")),
            "weather_code": value("weather_code"),
            "temperature": _round(value("temperature_2m")),
            "feels_like": _round(value("apparent_temperature")),
            "precipitation_chance_percent": _round(value("precipitation_probability")),
            "precipitation": _round(value("precipitation"), 2),
            "wind": _round(value("wind_speed_10m")),
            "wind_gusts": _round(value("wind_gusts_10m")),
        }

    def _concerns(self, hours: list[dict[str, Any]], units: dict[str, Any]) -> list[str]:
        """Plain-language reasons someone should prepare, or nothing."""
        metric = "°C" in str(units.get("temperature_2m", "")) or self.config.units == "metric"
        hot, cold = (33, 0) if metric else (92, 32)
        gusty = 50 if metric else 30
        concerns: list[str] = []
        chance = max((h["precipitation_chance_percent"] or 0) for h in hours) if hours else 0
        codes = {h.get("weather_code") for h in hours}
        wet = {code for code in codes if isinstance(code, int) and code >= 51}
        if any(code >= 95 for code in wet):
            concerns.append("thunderstorms expected")
        elif any(71 <= code <= 77 or 85 <= code <= 86 for code in wet):
            concerns.append("snow expected")
        elif chance >= 40 or any(61 <= code <= 67 or 80 <= code <= 82 for code in wet):
            concerns.append(f"{int(chance)} percent chance of rain")
        elif any(code in (45, 48) for code in codes):
            concerns.append("fog")
        temps = [h["feels_like"] if h["feels_like"] is not None else h["temperature"] for h in hours]
        temps = [t for t in temps if t is not None]
        if temps and max(temps) >= hot:
            concerns.append(f"hot, feels like {int(max(temps))}")
        if temps and min(temps) <= cold:
            concerns.append(f"freezing, feels like {int(min(temps))}")
        gusts = [h["wind_gusts"] for h in hours if h["wind_gusts"] is not None]
        if gusts and max(gusts) >= gusty:
            concerns.append(f"gusts to {int(max(gusts))}")
        return concerns


def _hour_of(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min
