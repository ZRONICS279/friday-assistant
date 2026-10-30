import httpx
import pytest

from house_agent.config import WeatherConfig
from house_agent.db import Database
from house_agent.weather import (
    HOME_SETTING,
    HomeLocationUnknown,
    WeatherError,
    WeatherService,
    describe,
)


HOME = WeatherConfig(latitude=39.7392, longitude=-104.9903, place="home")


def _forecast_payload(days: int = 1, current: bool = True) -> dict:
    payload = {
        "daily": {
            "time": [f"2026-08-{29 + index}" for index in range(days)],
            "weather_code": [61] * days,
            "temperature_2m_max": [84.2] * days,
            "temperature_2m_min": [59.7] * days,
            "precipitation_probability_max": [40] * days,
            "precipitation_sum": [0.132] * days,
            "wind_speed_10m_max": [12.4] * days,
            "sunrise": ["2026-08-29T06:20"] * days,
            "sunset": ["2026-08-29T19:45"] * days,
        },
        "daily_units": {"temperature_2m_max": "°F"},
    }
    if current:
        payload["current"] = {
            "time": "2026-08-29T14:00",
            "temperature_2m": 78.6,
            "apparent_temperature": 80.1,
            "relative_humidity_2m": 43,
            "precipitation": 0.0,
            "weather_code": 2,
            "wind_speed_10m": 8.3,
            "wind_gusts_10m": 15.9,
            "is_day": 1,
        }
        payload["current_units"] = {"temperature_2m": "°F", "wind_speed_10m": "mph"}
    return payload


def _handler(alerts: list | None = None, days: int = 1):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(
                200, json=_forecast_payload(days, "current" in request.url.params)
            )
        if request.url.host == "api.weather.gov":
            return httpx.Response(200, json={"features": alerts or []})
        raise AssertionError(f"unexpected host {request.url.host}")

    return handle


@pytest.mark.asyncio
async def test_now_reports_rounded_conditions_for_the_house() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        report = await WeatherService(HOME, client=client).now()

    assert report["place"] == "home"
    assert report["condition"] == "partly cloudy"
    assert report["temperature"] == 79
    assert report["feels_like"] == 80
    assert report["today"]["high"] == 84
    assert report["today"]["low"] == 60
    assert report["today"]["precipitation_chance_percent"] == 40
    assert report["alerts"] == []


@pytest.mark.asyncio
async def test_now_surfaces_active_severe_weather_alerts() -> None:
    alerts = [
        {
            "properties": {
                "event": "Severe Thunderstorm Warning",
                "severity": "Severe",
                "urgency": "Immediate",
                "headline": "Severe Thunderstorm Warning until 4 PM",
                "expires": "2026-08-29T16:00:00-06:00",
            }
        }
    ]
    transport = httpx.MockTransport(_handler(alerts=alerts))
    async with httpx.AsyncClient(transport=transport) as client:
        report = await WeatherService(HOME, client=client).now()

    assert report["alerts"][0]["event"] == "Severe Thunderstorm Warning"
    assert report["alerts"][0]["severity"] == "Severe"


@pytest.mark.asyncio
async def test_alert_outage_does_not_fail_the_forecast() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            return httpx.Response(500)
        return httpx.Response(200, json=_forecast_payload(3, False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await WeatherService(HOME, client=client).forecast(3)

    assert len(report["days"]) == 3
    assert report["alerts"] == []


@pytest.mark.asyncio
async def test_forecast_clamps_days_to_a_week() -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.open-meteo.com":
            requested.append(request.url.params["forecast_days"])
            return httpx.Response(200, json=_forecast_payload(7, False))
        return httpx.Response(200, json={"features": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await WeatherService(HOME, client=client).forecast(30)

    assert requested == ["7"]


@pytest.mark.asyncio
async def test_named_location_is_geocoded_before_the_forecast() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Boulder",
                            "admin1": "Colorado",
                            "country_code": "US",
                            "latitude": 40.01,
                            "longitude": -105.27,
                        }
                    ]
                },
            )
        if request.url.host == "api.open-meteo.com":
            assert request.url.params["latitude"] == "40.01"
            return httpx.Response(200, json=_forecast_payload(1, True))
        return httpx.Response(200, json={"features": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await WeatherService(HOME, client=client).now("Boulder")

    assert report["place"] == "Boulder, Colorado, US"


@pytest.mark.asyncio
async def test_unknown_place_is_reported_not_guessed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WeatherError, match="No place found"):
            await WeatherService(HOME, client=client).now("Nowheresville")


def _geocoder(name: str = "Springfield", admin1: str = "Illinois"):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": name,
                            "admin1": admin1,
                            "country_code": "US",
                            "latitude": 39.7817,
                            "longitude": -89.6501,
                        }
                    ]
                },
            )
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=_forecast_payload(1, True))
        return httpx.Response(200, json={"features": []})

    return handle


@pytest.mark.asyncio
async def test_unknown_home_asks_instead_of_explaining_env_vars(tmp_path) -> None:
    """The agent has to be told to ask the household, not to report a
    misconfiguration it cannot fix and the user cannot act on."""
    database = Database(tmp_path / "house.db")
    database.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        service = WeatherService(WeatherConfig(), database, client=client)
        assert service.home() is None
        with pytest.raises(HomeLocationUnknown, match="weather_set_home"):
            await service.now()


@pytest.mark.asyncio
async def test_home_told_once_is_used_for_every_later_question(tmp_path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    transport = httpx.MockTransport(_geocoder())
    async with httpx.AsyncClient(transport=transport) as client:
        service = WeatherService(WeatherConfig(), database, client=client)
        saved = await service.set_home("Springfield, Illinois")
        assert saved["place"] == "Springfield, Illinois, US"

        # A fresh service, as a later conversation would build: still knows.
        later = WeatherService(WeatherConfig(), database, client=client)
        assert later.home() == (39.7817, -89.6501, "Springfield, Illinois, US")
        report = await later.forecast(3)

    assert report["place"] == "Springfield, Illinois, US"


@pytest.mark.asyncio
async def test_saved_home_overrides_configured_coordinates(tmp_path) -> None:
    """Someone saying where they live is newer than a config default, and is
    the only way to correct the house's location without editing files."""
    database = Database(tmp_path / "house.db")
    database.initialize()
    transport = httpx.MockTransport(_geocoder())
    async with httpx.AsyncClient(transport=transport) as client:
        service = WeatherService(HOME, database, client=client)
        assert service.home() == (39.7392, -104.9903, "home")
        await service.set_home("Springfield, Illinois")
        assert service.home() == (39.7817, -89.6501, "Springfield, Illinois, US")


@pytest.mark.asyncio
async def test_corrupt_saved_home_falls_back_to_configuration(tmp_path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    database.set_setting(HOME_SETTING, "not json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        service = WeatherService(HOME, database, client=client)
        assert service.home() == (39.7392, -104.9903, "home")
        report = await service.now()

    assert report["place"] == "home"


@pytest.mark.asyncio
async def test_unknown_home_town_is_not_saved(tmp_path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = WeatherService(WeatherConfig(), database, client=client)
        with pytest.raises(WeatherError, match="No place found"):
            await service.set_home("Nowheresville")

    assert database.get_setting(HOME_SETTING) is None


def test_weather_codes_become_spoken_words() -> None:
    assert describe(0) == "clear"
    assert describe(95) == "thunderstorms"
    assert describe(None) == "unknown"
    assert describe(7) == "unsettled"


@pytest.mark.asyncio
async def test_a_place_name_with_extra_words_is_tried_shorter() -> None:
    """"the Bellingham area" geocodes to nothing, and Bellingham is right there
    inside it. One shorter try before the question goes back to the user."""
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            asked.append(request.url.params["name"])
            if asked[-1] != "Bellingham":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Bellingham",
                            "admin1": "Washington",
                            "country_code": "US",
                            "latitude": 48.75,
                            "longitude": -122.48,
                        }
                    ]
                },
            )
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=_forecast_payload(1, True))
        return httpx.Response(200, json={"features": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await WeatherService(HOME, client=client).now("the Bellingham area")

    assert asked == ["the Bellingham area", "Bellingham"]
    assert report["place"] == "Bellingham, Washington, US"


@pytest.mark.asyncio
async def test_a_name_that_is_nowhere_says_what_to_try_instead() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WeatherError, match="nearest larger town"):
            await WeatherService(HOME, client=client).now("Nowheresville")
