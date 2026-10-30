"""Flights and hotels through LetsFG, with the network mocked out."""

import json
from datetime import date, timedelta

import httpx
import pytest

from house_agent import travel
from house_agent.config import TravelConfig
from house_agent.travel import (
    TravelError,
    TravelNotConfigured,
    TravelService,
    bearer_token,
)

SOON = (date.today() + timedelta(days=40)).isoformat()
LATER = (date.today() + timedelta(days=47)).isoformat()
CONFIG = TravelConfig(letsfg_token="tok_test", currency="USD", timeout_seconds=5)

NESTED_OFFER = {
    "id": "ws_off_1",
    "price": 612.4,
    "currency": "USD",
    "airlines": ["TAP Air Portugal"],
    "booking_url": "https://letsfg.co/book/ws_off_1",
    "outbound": {
        "total_duration_seconds": 42000,
        "stopovers": 1,
        "segments": [
            {"airline": "TP", "airline_name": "TAP Air Portugal", "flight_no": "TP234",
             "origin": "DEN", "destination": "EWR",
             "departure": f"{SOON}T09:10:00", "arrival": f"{SOON}T15:05:00"},
            {"airline": "TP", "airline_name": "TAP Air Portugal", "flight_no": "TP208",
             "origin": "EWR", "destination": "LIS",
             "departure": f"{SOON}T17:30:00", "arrival": f"{SOON}T22:50:00"},
        ],
    },
    "inbound": {
        "total_duration_seconds": 39600,
        "stopovers": 0,
        "segments": [
            {"airline": "TP", "airline_name": "TAP Air Portugal", "flight_no": "TP209",
             "origin": "LIS", "destination": "DEN",
             "departure": f"{LATER}T10:00:00", "arrival": f"{LATER}T15:00:00"},
        ],
    },
}
FLAT_OFFER = {
    "id": "ws_off_2",
    "price": 899.0,
    "currency": "USD",
    "airline": "United",
    "origin": "DEN",
    "destination": "LIS",
    "departure_time": f"{SOON}T18:45:00",
    "arrival_time": f"{SOON}T23:59:00",
    "duration_minutes": 615,
    "stops": 0,
}
HOTELS = {
    "found": True,
    "cityName": "Lisbon, Portugal",
    "currency": "EUR",
    "finished": True,
    "error": None,
    "hotels": [
        {"name": "EVOLUTION Lisboa Hotel", "starRating": 4, "guestRating": "Excellent",
         "reviewCount": 2355, "address": "Praça do Duque de Saldanha, 4,",
         "lat": 38.7335, "lon": -9.1442,
         "price": 637.26, "currency": "EUR", "roomName": "COOL, DOUBLE OR TWIN BED",
         "mealPlan": "Room Only", "freeCancellation": True, "freeUntil": "2026-10-08T23:59:59"},
        {"name": "Residencial Lar do Areeiro", "starRating": 3, "guestRating": "Good",
         "reviewCount": 410, "address": "Praca Francisco Sa Carneiro 4,",
         "lat": 38.78, "lon": -9.10,
         "price": 293.68, "currency": "EUR", "roomName": "Double", "mealPlan": "Breakfast",
         "freeCancellation": False, "freeUntil": None},
        {"name": "Ghost Listing", "price": 0, "currency": "EUR"},
    ],
}


def service(handler, config=CONFIG) -> TravelService:
    client = httpx.AsyncClient(
        base_url="https://letsfg.co", transport=httpx.MockTransport(handler)
    )
    return TravelService(config, client=client)


def flight_api(polls_before_done: int = 1, offers=None, late_merge: int = 0):
    """A LetsFG that answers `searching` a few times, then completes."""
    calls = {"search": [], "results": 0}
    offers = list(NESTED_OFFER for _ in range(1)) if offers is None else offers

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok_test"
        if request.url.path == "/api/search":
            calls["search"].append(json.loads(request.content))
            return httpx.Response(200, json={"search_id": "ws_1", "status": "searching"})
        if request.url.path == "/api/results/ws_1":
            calls["results"] += 1
            if calls["results"] <= polls_before_done:
                return httpx.Response(200, json={"status": "searching", "offers": []})
            pending = calls["results"] <= polls_before_done + late_merge
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "offers": offers,
                    "total_results": len(offers),
                    "split_ticket_pending": pending,
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    return handle, calls


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(travel, "_POLL_SECONDS", 0)
    monkeypatch.setattr(travel, "_SETTLE_SECONDS", 1)


async def test_round_trip_polls_until_done_and_reports_the_cheapest_first() -> None:
    handle, calls = flight_api(polls_before_done=2, offers=[FLAT_OFFER, NESTED_OFFER])
    report = await service(handle).search_flights("den", "lis", SOON, LATER, adults=2)

    sent = calls["search"][0]
    assert sent["origin"] == "DEN" and sent["destination"] == "LIS"
    assert sent["date_from"] == SOON and sent["return_date"] == LATER
    assert sent["adults"] == 2 and sent["currency"] == "USD"
    assert calls["results"] == 3

    assert report["total_results"] == 2
    cheapest = report["offers"][0]
    assert cheapest["price"] == 612.4
    assert cheapest["airlines"] == ["TAP Air Portugal"]
    assert cheapest["stops"] == 1
    assert cheapest["outbound"]["departs"] == f"{SOON} 09:10"
    assert cheapest["outbound"]["arrives"] == f"{SOON} 22:50"
    assert cheapest["outbound"]["duration"] == "11 hours 40 minutes"
    assert cheapest["outbound"]["flights"] == ["TP234", "TP208"]
    assert cheapest["inbound"]["stops"] == 0
    assert cheapest["booking_url"].startswith("https://")
    # The flat, older shape is understood too.
    flat = report["offers"][1]
    assert flat["airlines"] == ["United"] and flat["inbound"] is None
    assert flat["outbound"]["duration"] == "10 hours 15 minutes"
    assert "per person" in report["note"]


async def test_a_late_split_ticket_merge_is_waited_for() -> None:
    handle, calls = flight_api(polls_before_done=1, late_merge=2)
    await service(handle).search_flights("DEN", "LIS", SOON)
    # 1 searching + 1 completed-but-pending + 1 still pending + 1 settled.
    assert calls["results"] == 4


async def test_a_search_that_never_finishes_says_so_instead_of_nothing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"search_id": "ws_1"})
        return httpx.Response(200, json={"status": "searching", "offers": []})

    config = TravelConfig(letsfg_token="tok_test", timeout_seconds=0.05)
    report = await service(handle, config).search_flights("DEN", "LIS", SOON)
    assert report["offers"] == []
    assert "ran out of time" in report["note"]


async def test_no_flights_is_a_finished_answer_not_an_error() -> None:
    handle, _ = flight_api(offers=[])
    report = await service(handle).search_flights("DEN", "LIS", SOON)
    assert report["offers"] == [] and "no flights" in report["note"]
    # Empty is nearly always a filter, not an empty sky: the result says which
    # one to let go of rather than leaving the trip with the user.
    assert "a day either side" in report["next_step"]
    assert "the other airport for that city" in report["next_step"]


async def test_a_filtered_out_flight_search_says_which_filter_to_drop() -> None:
    handle, _ = flight_api(offers=[])
    report = await service(handle).search_flights(
        "DEN", "LIS", SOON, max_stops=0, cabin="business"
    )
    assert "max_stops" in report["next_step"] and "cabin" in report["next_step"]


async def test_arguments_are_checked_before_any_request_is_made() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    svc = service(handle)
    with pytest.raises(ValueError, match="airport code"):
        await svc.search_flights("Denver", "LIS", SOON)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await svc.search_flights("DEN", "LIS", "next friday")
    with pytest.raises(ValueError, match="in the past"):
        await svc.search_flights("DEN", "LIS", "2020-01-01")
    with pytest.raises(ValueError, match="before the departure"):
        await svc.search_flights("DEN", "LIS", LATER, SOON)
    with pytest.raises(ValueError, match="same airport"):
        await svc.search_flights("DEN", "den", SOON)
    with pytest.raises(ValueError, match="check_out must be after"):
        await svc.search_hotels("Lisbon", SOON, SOON)


async def test_a_rejected_token_asks_for_letsfg_auth() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized", "code": "NO_SESSION"})

    with pytest.raises(TravelNotConfigured, match="letsfg auth"):
        await service(handle).search_flights("DEN", "LIS", SOON)


def test_the_token_letsfg_auth_saved_is_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LETSFG_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(travel, "_token_file", lambda: tmp_path / "config.json")
    config = TravelConfig()

    with pytest.raises(TravelNotConfigured, match="letsfg auth"):
        bearer_token(config)

    (tmp_path / "config.json").write_text(
        json.dumps({"pfs_auth": {"token": "tok_saved", "expires_at": 4102444800}})
    )
    assert bearer_token(config) == "tok_saved"

    (tmp_path / "config.json").write_text(
        json.dumps({"pfs_auth": {"token": "tok_old", "expires_at": 1000}})
    )
    with pytest.raises(TravelNotConfigured, match="expired"):
        bearer_token(config)

    monkeypatch.setenv("LETSFG_BEARER_TOKEN", "tok_env")
    assert bearer_token(config) == "tok_env"
    assert bearer_token(TravelConfig(letsfg_token="tok_cfg")) == "tok_cfg"


async def test_hotels_are_best_rated_first_with_a_per_night_price() -> None:
    sent = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/hotels/search"
        assert "Authorization" not in request.headers
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=HOTELS)

    report = await service(handle).search_hotels("Lisbon", SOON, LATER, adults=2)
    assert sent == {"place": "Lisbon", "checkIn": SOON, "checkOut": LATER,
                    "adults": 2, "currency": "USD", "limit": 60}
    assert report["place"] == "Lisbon, Portugal"
    assert report["nights"] == 7 and report["currency"] == "EUR"
    assert report["sorted_by"] == "guest rating and stars"
    # The zero-priced ghost listing is dropped, not offered.
    assert report["total_results"] == 2
    first, second = report["hotels"]
    # A budget is a ceiling, not a target: the 4-star "Excellent" leads.
    assert first["name"] == "EVOLUTION Lisboa Hotel"
    assert first["stars"] == 4 and first["guest_rating"] == "Excellent"
    assert first["free_cancellation_until"] == "2026-10-08 23:59"
    assert first["address"] == "Praça do Duque de Saldanha, 4"
    assert second["price_total"] == 293.68 and second["price_per_night"] == 41.95
    assert second["free_cancellation_until"] is None
    assert "distance" not in first and "_lat" not in first


async def test_cheapest_is_available_when_asked_for() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=HOTELS)

    report = await service(handle).search_hotels("Lisbon", SOON, LATER, sort="cheapest")
    assert report["sorted_by"] == "lowest price"
    assert [h["name"] for h in report["hotels"]] == [
        "Residencial Lar do Areeiro", "EVOLUTION Lisboa Hotel"
    ]
    with pytest.raises(ValueError, match="sort must be"):
        await service(handle).search_hotels("Lisbon", SOON, LATER, sort="fanciest")


async def test_a_nightly_ceiling_filters_hotels() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=HOTELS)

    svc = service(handle)
    report = await svc.search_hotels("Lisbon", SOON, LATER, max_price_per_night=50)
    assert [h["name"] for h in report["hotels"]] == ["Residencial Lar do Areeiro"]
    report = await svc.search_hotels("Lisbon", SOON, LATER, max_price_per_night=20)
    assert report["hotels"] == [] and "No rooms under 20" in report["note"]
    assert "max_price_per_night" in report["next_step"]


def hotels_and_geocoder(geocode):
    """LetsFG for hotels plus Nominatim for the area, on one transport."""
    asked = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            asked.update(dict(request.url.params))
            assert request.headers["User-Agent"].startswith("house-agent/")
            return httpx.Response(200, json=geocode)
        return httpx.Response(200, json=HOTELS)

    return handle, asked


async def test_near_keeps_hotels_within_a_walk_and_says_how_far() -> None:
    handle, asked = hotels_and_geocoder(
        [{"lat": "38.7330", "lon": "-9.1440", "display_name": "Saldanha, Lisbon"}]
    )
    report = await service(handle).search_hotels("Lisbon", SOON, LATER, near="Saldanha")
    # The area is looked up inside the city the search resolved, not bare.
    assert asked["q"] == "Saldanha, Lisbon, Portugal"
    assert report["near"] == "Saldanha" and report["within"] == "1.9 miles"
    assert [h["name"] for h in report["hotels"]] == ["EVOLUTION Lisboa Hotel"]
    assert report["hotels"][0]["distance"] == "0 miles from Saldanha"

    metric = TravelConfig(units="metric")
    report = await service(handle, metric).search_hotels("Lisbon", SOON, LATER, near="Saldanha")
    assert report["within"] == "3 km"


async def test_nothing_near_the_area_says_where_the_rest_are() -> None:
    handle, _ = hotels_and_geocoder(
        [{"lat": "38.6979", "lon": "-9.4207", "display_name": "Cascais"}]
    )
    report = await service(handle).search_hotels("Lisbon", SOON, LATER, near="Cascais")
    assert report["hotels"] == []
    assert "within 1.9 miles of Cascais" in report["note"]
    assert "2 elsewhere in the city" in report["note"]
    # And says to go and get them rather than reporting nothing.
    assert "Search again without near" in report["next_step"]


async def test_an_area_the_geocoder_cannot_find_is_an_error() -> None:
    handle, _ = hotels_and_geocoder([])
    with pytest.raises(TravelError, match="Could not find 'Nowhere'"):
        await service(handle).search_hotels("Lisbon", SOON, LATER, near="Nowhere")


async def test_hotel_service_errors_are_reported_not_hidden() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"found": False, "hotels": [], "error": "dates_required"})

    with pytest.raises(TravelError, match="dates_required"):
        await service(handle).search_hotels("Lisbon", SOON, LATER)


async def test_an_unreachable_service_is_an_error_not_an_empty_list() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(TravelError, match="could not be reached"):
        await service(handle).search_hotels("Lisbon", SOON, LATER)
