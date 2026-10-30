import httpx
import pytest

from house_agent.config import DirectionsConfig
from house_agent.directions import (
    DirectionsError,
    DirectionsService,
    _main_roads,
    _spoken_duration,
)
from house_agent.weather import HomeLocationUnknown


HOME = (39.7817, -89.6501, "Springfield, Illinois")


def _handler(seconds: float = 6540, meters: float = 142900, roads=("I-5", "I-5", "Exit 165")):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": "47.6038",
                        "lon": "-122.3301",
                        "display_name": "Seattle, King County, Washington, United States",
                    }
                ],
            )
        if request.url.host == "routing.openstreetmap.de":
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "routes": [
                        {
                            "duration": seconds,
                            "distance": meters,
                            "legs": [{"steps": [{"name": name} for name in roads]}],
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected host {request.url.host}")

    return handle


def _service(handler, home=HOME, **config):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DirectionsService(
        DirectionsConfig(**config), home=lambda: home, client=client
    )


@pytest.mark.asyncio
async def test_travel_time_from_home_is_reported_in_miles() -> None:
    result = await _service(_handler()).travel_time("Seattle")

    assert result["origin"] == "Springfield, Illinois"
    assert result["destination"] == "Seattle, King County, Washington"
    assert result["minutes"] == 109
    assert result["duration_spoken"] == "1 hour 49 minutes"
    assert result["distance"] == 88.8
    assert result["distance_unit"] == "miles"


@pytest.mark.asyncio
async def test_metric_households_get_kilometres() -> None:
    result = await _service(_handler(), units="metric").travel_time("Seattle")

    assert result["distance"] == 142.9
    assert result["distance_unit"] == "km"


@pytest.mark.asyncio
async def test_each_mode_asks_its_own_routing_instance() -> None:
    """The car instance answers a walking request with car speeds, so the
    profile has to be in the URL, not a parameter."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "routing.openstreetmap.de":
            seen.append(request.url.path.split("/")[1])
        return _handler()(request)

    for mode in ("driving", "walking", "cycling"):
        await _service(handle).travel_time("Seattle", mode=mode)

    assert seen == ["routed-car", "routed-foot", "routed-bike"]


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(DirectionsError, match="driving, walking, or cycling"):
        await _service(_handler()).travel_time("Seattle", mode="teleport")


@pytest.mark.asyncio
async def test_a_named_origin_overrides_home() -> None:
    result = await _service(_handler()).travel_time("Seattle", origin="Portland")

    # Both ends were geocoded, so neither is the configured home label.
    assert result["origin"] == "Seattle, King County, Washington"


@pytest.mark.asyncio
async def test_no_home_yet_asks_rather_than_guessing() -> None:
    with pytest.raises(HomeLocationUnknown):
        await _service(_handler(), home=None).travel_time("Seattle")


@pytest.mark.asyncio
async def test_an_unfindable_destination_is_reported() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with pytest.raises(DirectionsError, match="No destination found"):
        await _service(handle).travel_time("Narnia")


@pytest.mark.asyncio
async def test_a_route_that_does_not_exist_is_reported() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "routing.openstreetmap.de":
            return httpx.Response(200, json={"code": "NoRoute", "routes": []})
        return _handler()(request)

    with pytest.raises(DirectionsError, match="No driving route"):
        await _service(handle).travel_time("Hawaii")


def test_repeated_road_names_collapse_into_a_route_summary() -> None:
    assert _main_roads(["I-5", "I-5", "I-5", "SR-20", "", "SR-20", "Main St"]) == [
        "I-5",
        "SR-20",
        "Main St",
    ]


def test_durations_are_phrased_the_way_they_are_spoken() -> None:
    assert _spoken_duration(1) == "1 minute"
    assert _spoken_duration(45) == "45 minutes"
    assert _spoken_duration(60) == "1 hour"
    assert _spoken_duration(90) == "1 and a half hours"
    assert _spoken_duration(109) == "1 hour 49 minutes"
    assert _spoken_duration(120) == "2 hours"


def _tomtom_handler(travel=1620, free_flow=1200, delay=420, meters=18500):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return _handler()(request)
        if request.url.host == "api.tomtom.com":
            assert request.url.params["traffic"] == "true"
            assert request.url.params["computeTravelTimeFor"] == "all"
            return httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "summary": {
                                "lengthInMeters": meters,
                                "travelTimeInSeconds": travel,
                                "noTrafficTravelTimeInSeconds": free_flow,
                                "trafficDelayInSeconds": delay,
                                "arrivalTime": "2026-08-29T17:42:00-07:00",
                            }
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected host {request.url.host}")

    return handle


@pytest.mark.asyncio
async def test_a_tomtom_key_turns_on_live_traffic() -> None:
    result = await _service(_tomtom_handler(), tomtom_key="k").travel_time("Seattle")

    assert result["source"] == "TomTom (live traffic)"
    assert result["minutes"] == 27
    assert result["traffic_delay_minutes"] == 7
    assert result["free_flow_minutes"] == 20
    assert result["arrival_time"].startswith("2026-08-29T17:42")
    assert result["note"] == "Includes live traffic."


@pytest.mark.asyncio
async def test_walking_stays_on_the_keyless_router_even_with_a_key() -> None:
    """Traffic is meaningless on foot, so a key must not divert walking."""
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.tomtom.com":
            return _tomtom_handler()(request)
        return _handler()(request)

    await _service(handle, tomtom_key="k").travel_time("Seattle", mode="walking")

    assert "api.tomtom.com" not in hosts
    assert "routing.openstreetmap.de" in hosts


@pytest.mark.asyncio
async def test_without_a_key_the_result_warns_it_excludes_traffic() -> None:
    result = await _service(_handler()).travel_time("Seattle")

    assert result["source"] == "OpenStreetMap routing"
    assert "without live traffic" in result["note"]
    assert "traffic_delay_minutes" not in result
