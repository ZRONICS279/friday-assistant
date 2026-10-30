"""DoorDash ordering through dd-cli, with the binary faked out."""

import json
import os
import stat

import pytest

from house_agent.config import DoorDashConfig
from house_agent.doordash import (
    DoorDashError,
    DoorDashNotConfigured,
    DoorDashService,
    find_binary,
    intent_for,
)

ADDRESSES = {
    "addresses": [
        {"address_id": "addr-work", "label": "Work", "printable_address": "1 Office Way, Seattle, WA", "is_default": False},
        {"address_id": "addr-home", "label": "Home", "printable_address": "12 Elm St, Springfield, IL", "is_default": True},
    ]
}
CARDS = {
    "default_payment_method_id": "pm-1",
    "cards": [
        {"payment_method_id": "pm-2", "brand": "Mastercard", "last4": "9999"},
        {"payment_method_id": "pm-1", "brand": "Visa", "last4": "1234"},
    ],
}
SEARCH = {
    "stores": [
        {"store_id": 928163, "name": "Zeeks Pizza", "delivery_time": "25-35 min", "distance_meters": 2414, "rating": 4.7},
        {"store_id": 111, "name": "Pizza Hut", "delivery_time": "40-50 min"},
    ]
}
MENU = {
    "menu_id": "m-77",
    "store_name": "Zeeks Pizza",
    "promotions": [{"title": "20% off $30+", "code": "SAVE20"}],
    "categories": [
        {"name": "Pizzas", "items": [
            {"item_id": "i_1001", "name": "Large Pepperoni", "price": "$24.00", "description": "Classic pepperoni on a hand-tossed crust"},
            {"item_id": "i_1002", "name": "Margherita", "price": "$22.00"},
        ]},
        {"name": "Drinks", "items": [{"item_id": "i_2001", "name": "Coke", "price": "$2.50"}]},
    ],
}
PREVIEW = {
    "success": True,
    "quote": {
        "net_total_before_tip": {"display_string": "$31.20", "unit_amount": 3120},
        "line_items": [
            {"label": "Subtotal", "final_money": {"display_string": "$24.00"}},
            {"label": "Delivery fee", "final_money": {"display_string": "$3.99"}},
        ],
        "store_order_cart": {
            "store_name": "Zeeks Pizza",
            "fulfillment_type": "DELIVERY",
            "orders": [{"order_items": [
                {"item": {"name": "Large Pepperoni"}, "quantity": 1, "unit_price_monetary_fields": {"display_string": "$24.00"}},
            ]}],
        },
        "delivery_address": {"printable_address": "12 Elm St, Springfield, IL"},
        "delivery_availability": {"asap_available": True, "asap_delivery_minutes_range_string": "30-40 min"},
        "is_pre_tippable": True,
        "tips_suggestion_details": [{
            "default_index": 1,
            "percentage_values": [10, 15, 20],
            "percentage_to_amount_monetary_values": [{"unit_amount": 240}, {"unit_amount": 360}, {"unit_amount": 480}],
        }],
        "dropoff_options": [{"proof_of_delivery_type": "PIN_CODE"}],
    },
}


class FakeCLI:
    """Answers dd-cli calls from a table keyed by the leading arguments,
    recording every argv so the tests can check the flags."""

    def __init__(self, responses: dict[tuple[str, ...], object]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv, env, timeout):
        assert argv[1] == "--json-output"
        assert argv[-2] == "--intent" and argv[-1].startswith("Summary: ")
        args = argv[2:-2]
        self.calls.append(args)
        for length in range(len(args), 0, -1):
            key = tuple(args[:length])
            if key in self.responses:
                response = self.responses[key]
                if callable(response):
                    response = response(args)
                if isinstance(response, tuple):
                    code, payload, stderr = response
                    return code, json.dumps(payload).encode(), stderr.encode()
                return 0, json.dumps(response).encode(), b""
        raise AssertionError(f"unexpected dd-cli call: {args}")

    def argv(self, *leading: str) -> list[str]:
        for args in self.calls:
            if tuple(args[: len(leading)]) == leading:
                return args
        raise AssertionError(f"no call starting {leading}; calls: {self.calls}")


def fake_binary(tmp_path):
    binary = tmp_path / "dd-cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def service(tmp_path, responses, home=None) -> tuple[DoorDashService, FakeCLI]:
    cli = FakeCLI(responses)
    config = DoorDashConfig(binary=fake_binary(tmp_path), timeout_seconds=5)
    return DoorDashService(config, home=home, runner=cli), cli


def test_intent_carries_the_users_words_in_doordashs_two_line_shape() -> None:
    intent = intent_for("  order me a   pizza ")
    lines = intent.split("\n")
    assert lines[0].startswith("Summary: Help a household member order")
    assert lines[1] == 'user prompt/purpose: "order me a pizza"'


def test_binary_is_found_in_local_bin_when_not_on_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_binary() is None
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    installed = local / "dd-cli"
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    assert find_binary() == installed


def test_not_installed_is_a_setup_problem_not_a_crash(tmp_path) -> None:
    config = DoorDashConfig(binary=tmp_path / "missing")
    missing = DoorDashService(config, runner=FakeCLI({}))
    assert not missing.available


async def test_not_signed_in_is_reported_as_setup(tmp_path) -> None:
    svc, _ = service(tmp_path, {
        ("address", "list"): (1, {}, "Error: Failed to execute command due to missing credentials. Sign in with dd-cli login"),
    })
    with pytest.raises(DoorDashNotConfigured, match="not signed in"):
        await svc.addresses()


async def test_search_uses_the_default_saved_address_never_the_cli_fallback(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("address", "list"): ADDRESSES,
        ("search",): SEARCH,
        ("cart", "list"): {"carts": [{"cart_uuid": "c-1", "store_id": 928163, "store_name": "Zeeks Pizza", "items": [{"name": "Coke", "quantity": 2}]}]},
    })
    result = await svc.search("pizza", limit=5, purpose="find pizza")
    args = cli.argv("search")
    assert args[args.index("--query") + 1] == "pizza"
    assert args[args.index("--address-id") + 1] == "addr-home"
    assert "--lat" not in args
    assert result["near"] == "Home"
    first = result["stores"][0]
    assert first["store_id"] == "928163" and first["miles"] == 1.5 and first["eta"] == "25-35 min"
    assert first["open_cart"] == {"cart_uuid": "c-1", "items": [{"name": "Coke", "quantity": 2}]}
    assert "open_cart" not in result["stores"][1]


async def test_search_near_a_labeled_address_and_the_house_when_none_saved(tmp_path) -> None:
    svc, cli = service(tmp_path, {("address", "list"): ADDRESSES, ("search",): SEARCH, ("cart", "list"): {"carts": []}})
    await svc.search("sushi", address="work")
    assert cli.argv("search")[cli.argv("search").index("--address-id") + 1] == "addr-work"

    svc2, cli2 = service(
        tmp_path, {("address", "list"): {"addresses": []}, ("search",): SEARCH, ("cart", "list"): {"carts": []}},
        home=lambda: (39.7817, -89.6501, "Springfield"),
    )
    result = await svc2.search("sushi")
    args = cli2.argv("search")
    assert args[args.index("--lat") + 1] == "39.78170" and args[args.index("--lng") + 1] == "-89.65010"
    assert result["near"] == "Springfield"


async def test_an_unknown_address_label_lists_the_known_ones(tmp_path) -> None:
    svc, _ = service(tmp_path, {("address", "list"): ADDRESSES})
    with pytest.raises(DoorDashError, match="Home, Work"):
        await svc.search("tacos", address="the cabin")


async def test_grocery_search_goes_through_nearby_stores(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("address", "list"): ADDRESSES,
        ("find-nearby-stores",): {"stores": [
            {"store_id": 5, "name": "Fred Meyer", "distance_meters": 3200, "delivery_time": "45 min"},
            {"store_id": 6, "name": "Main St Grocery", "distance_meters": 800},
        ]},
        ("cart", "list"): {"carts": []},
    })
    result = await svc.search("fred meyer", kind="grocery")
    args = cli.argv("find-nearby-stores")
    assert args[args.index("--vertical") + 1] == "grocery"
    assert [s["name"] for s in result["stores"]] == ["Fred Meyer"]
    everything = await svc.search("", kind="grocery")
    assert [s["name"] for s in everything["stores"]] == ["Fred Meyer", "Main St Grocery"]


async def test_menu_is_filtered_to_the_dish_with_clean_item_ids(tmp_path) -> None:
    svc, _ = service(tmp_path, {("menu",): MENU})
    result = await svc.menu("928163", query="pepperoni")
    assert result["menu_id"] == "m-77"
    assert result["items"] == [{
        "item_id": "1001", "name": "Large Pepperoni", "price": "$24.00",
        "description": "Classic pepperoni on a hand-tossed crust", "category": "Pizzas",
    }]
    assert result["item_count"] == 3 and result["categories"] == ["Pizzas", "Drinks"]
    assert result["promotions"] == [{"title": "20% off $30+", "code": "SAVE20"}]
    # Words that are not an item name used to come back as an empty list,
    # which the model read as "the menu cannot be seen" and said so out loud.
    # The menu is in hand either way, so it comes back as a sample to offer.
    nothing = await svc.menu("928163", query="sushi")
    assert nothing["no_match"] == "sushi"
    assert [item["name"] for item in nothing["items"]] == ["Large Pepperoni", "Margherita", "Coke"]
    assert "cannot be seen" in nothing["note"]

    # A phrase nobody put on a menu still finds the dish inside it, best first.
    loose = await svc.menu("928163", query="large pepperoni pizza meal")
    assert [item["name"] for item in loose["items"]][:1] == ["Large Pepperoni"]
    assert "no_match" not in loose and "closest" in loose["note"]


async def test_adding_joins_the_open_cart_at_that_store_and_reports_choices_needed(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("cart", "list"): {"carts": [{"cart_uuid": "c-9", "store_id": "928163", "store_name": "Zeeks Pizza",
                                       "items": [{"name": "Coke", "quantity": 1}], "updated_at": 1762963200000}]},
        ("cart", "add-items"): {
            "success": False, "cart_uuid": "c-9",
            "cart": {"store_name": "Zeeks Pizza", "items": [{"id": "line-1", "name": "Coke", "quantity": 1, "price": 2.5},
                                                          {"id": "line-2", "name": "Large Pepperoni", "quantity": 1, "price": 24}]},
            "item_errors": [{"item_id": "1002", "item_name": "Margherita", "error_message": "Required option missing",
                             "required_options": [{"name": "Size", "min_num_options": 1, "max_num_options": 1,
                                                   "options": [{"id": "s1", "name": "Small"}, {"id": "s2", "name": "Large"}]}]}],
        },
    })
    result = await svc.cart_add("928163", [
        {"item_id": "i_1001", "name": "Large Pepperoni", "quantity": 1},
        {"item_id": "1002", "name": "Margherita"},
    ], menu_id="m-77")
    args = cli.argv("cart", "add-items")
    assert args[args.index("--cart-uuid") + 1] == "c-9"
    sent = json.loads(args[args.index("--items-json") + 1])
    assert sent == [
        {"item_id": "1001", "item_name": "Large Pepperoni", "quantity": 1},
        {"item_id": "1002", "item_name": "Margherita", "quantity": 1},
    ]
    assert result["added"] == ["Large Pepperoni"]
    assert result["cart"]["cart_uuid"] == "c-9"
    assert result["item_errors"][0]["required_choices"][0]["choices"] == [{"id": "s1", "name": "Small"}, {"id": "s2", "name": "Large"}]
    assert result["added_to_existing_cart"]["already_had"] == ["1× Coke"]
    assert "required_choices" in result["note"]


async def test_options_are_sent_as_nested_options(tmp_path) -> None:
    svc, cli = service(tmp_path, {("cart", "list"): {"carts": []}, ("cart", "add-items"): {"success": True, "cart_uuid": "c-2", "cart": {}}})
    await svc.cart_add("1", [{"item_id": "7", "name": "Boba", "options": [{"id": "o1", "name": "Less ice"}, "o2"]}], menu_id="m")
    sent = json.loads(cli.argv("cart", "add-items")[cli.argv("cart", "add-items").index("--items-json") + 1])
    assert sent[0]["nested_options"] == [
        {"id": "o1", "name": "Less ice", "quantity": 1},
        {"id": "o2", "name": "o2", "quantity": 1},
    ]
    assert "--cart-uuid" not in cli.argv("cart", "add-items")


async def test_grocery_items_get_their_menu_id_from_item_details(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("item-details",): {"item_id": "44", "name": "Milk", "menu_id": "gm-1"},
        ("cart", "list"): {"carts": []},
        ("cart", "add-items"): {"success": True, "cart_uuid": "c-3", "cart": {"items": [{"name": "Milk", "quantity": 2}]}},
    })
    result = await svc.cart_add("5", [{"item_id": "44", "name": "Milk", "quantity": 2}])
    assert cli.argv("cart", "add-items")[cli.argv("cart", "add-items").index("--menu-id") + 1] == "gm-1"
    assert result["cart"]["items"] == [{"name": "Milk", "quantity": 2}]


async def test_removing_by_name_finds_the_cart_line(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("cart", "show"): {"cart": {"id": "c-9", "store_name": "Zeeks", "items": [
            {"id": "line-1", "item_id": "2001", "name": "Coke", "quantity": 1},
            {"id": "line-2", "item_id": "1001", "name": "Large Pepperoni", "quantity": 1},
        ]}},
        ("cart", "remove-item"): {"success": True, "cart": {"id": "c-9", "items": [{"id": "line-2", "name": "Large Pepperoni", "quantity": 1}]}},
    })
    result = await svc.cart_remove("c-9", item_name="the coke")
    args = cli.argv("cart", "remove-item")
    assert args[args.index("--cart-item-id") + 1] == "line-1"
    assert [i["name"] for i in result["cart"]["items"]] == ["Large Pepperoni"]
    with pytest.raises(DoorDashError, match="Nothing called 'fries'"):
        await svc.cart_remove("c-9", item_name="fries")


async def test_history_names_the_usual_places(tmp_path) -> None:
    svc, cli = service(tmp_path, {("order", "history"): {"orders": [
        {"order_uuid": "o-3", "store_id": 928163, "store_name": "Zeeks Pizza", "created_at": 1762963200000,
         "items": [{"name": "Large Pepperoni", "quantity": 1}], "total": {"display_string": "$31.20"},
         "is_reorderable": True, "fulfillment_type": "FULFILLMENT_TYPE_DX_DELIVERY"},
        {"order_uuid": "o-2", "store_id": 42, "store_name": "Thai House", "items": ["Pad Thai"], "is_reorderable": False},
        {"order_uuid": "o-1", "store_id": 928163, "store_name": "Zeeks Pizza", "items": [{"name": "Coke", "quantity": 2}]},
    ], "page_full": False}})
    result = await svc.order_history(days=30, limit=2)
    args = cli.argv("order", "history")
    assert args[args.index("--days") + 1] == "30"
    assert result["orders"][0]["store_name"] == "Zeeks Pizza"
    assert result["orders"][0]["total"] == "$31.20" and result["orders"][0]["fulfillment"] == "delivery"
    assert result["orders"][2:] == [] and result["order_count"] == 3
    assert result["usual_places"][0]["store_name"] == "Zeeks Pizza"
    assert result["usual_places"][0]["times"] == 2
    assert result["usual_places"][0]["last_order_uuid"] == "o-3"
    assert result["orders"][1]["items"] == ["Pad Thai"] and result["orders"][1]["reorderable"] is False


async def test_reorder_by_store_name_previews_and_reports_dropped_items(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("order", "history"): {"orders": [
            {"order_uuid": "o-3", "store_id": 928163, "store_name": "Zeeks Pizza", "is_reorderable": True,
             "items": [{"name": "Large Pepperoni", "quantity": 1}, {"name": "Garlic Knots", "quantity": 1}]},
        ]},
        ("cart", "list"): {"carts": [{"cart_uuid": "old", "store_id": "928163", "items": [{"name": "Coke", "quantity": 1}]}]},
        ("cart", "delete"): {"success": True},
        ("order", "reorder"): {"success": True, "cart_uuid": "c-new"},
        ("order", "preview"): PREVIEW,
        ("payment-method", "list"): CARDS,
    })
    result = await svc.reorder(store_name="zeeks")
    assert cli.argv("order", "reorder")[-1] == "o-3"
    assert cli.argv("cart", "delete")[-1] == "old"
    assert result["cart_uuid"] == "c-new"
    assert result["total"] == "$31.20"
    assert result["dropped_items"] == ["Garlic Knots"]
    assert result["replaced_cart_items"] == ["1× Coke"]
    assert result["default_card_spoken"] == "Visa ending in 1234"


async def test_reorder_from_an_unknown_store_says_which_are_usual(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "history"): {"orders": [
        {"order_uuid": "o-1", "store_name": "Thai House", "is_reorderable": True, "items": []},
    ]}})
    with pytest.raises(DoorDashError, match="Usual places: Thai House"):
        await svc.reorder(store_name="zeeks")


async def test_preview_reads_the_quote_the_tip_and_the_card(tmp_path) -> None:
    svc, cli = service(tmp_path, {("order", "preview"): PREVIEW, ("payment-method", "list"): CARDS})
    result = await svc.preview("c-1", work_benefits=True)
    assert "--include-work-benefits" in cli.argv("order", "preview")
    assert result["items"] == [{"name": "Large Pepperoni", "quantity": 1, "unit_price": "$24.00"}]
    assert result["total"] == "$31.20"
    assert result["breakdown"][1] == {"label": "Delivery fee", "amount": "$3.99"}
    assert result["eta"] == "30-40 min" and result["fulfillment"] == "delivery"
    assert result["deliver_to"] == "12 Elm St, Springfield, IL"
    assert result["tip"] == {"pre_tippable": True, "suggested_cents": 360, "suggested": "$3.60", "suggested_percent": 15, "choices": ["$2.40", "$3.60", "$4.80"]}
    assert result["pin_required_at_handoff"] is True
    assert result["default_card"] == {"brand": "Visa", "last4": "1234", "default": True}
    assert "ask whether to place it" in result["next"]


async def test_a_wallet_default_is_explained_not_invented(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "preview"): PREVIEW, ("payment-method", "list"): {"cards": [], "default_payment_method_id": None}})
    result = await svc.preview("c-1")
    assert "default_card" not in result and "wallet" in result["card_note"]


async def test_placing_refuses_without_a_preview_a_yes_and_a_tip(tmp_path) -> None:
    svc, cli = service(tmp_path, {("order", "preview"): PREVIEW, ("payment-method", "list"): CARDS})
    with pytest.raises(DoorDashError, match="say yes first"):
        await svc.place("c-1", confirmed=False, tip_cents=300)
    with pytest.raises(DoorDashError, match="not been previewed"):
        await svc.place("c-1", confirmed=True, tip_cents=300)
    # Without a suggested tip in the quote, a delivery still needs one agreed.
    svc.preview_fixture = None
    await svc.preview("c-1")
    svc._previews["c-1"]["suggested_tip_cents"] = None
    with pytest.raises(DoorDashError, match="tip has not been agreed"):
        await svc.place("c-1", confirmed=True)
    assert not any(args[:2] == ["order", "submit"] for args in cli.calls)


async def test_placing_submits_exactly_what_was_previewed(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("order", "preview"): PREVIEW,
        ("payment-method", "list"): CARDS,
        ("order", "submit"): {"success": True, "order_uuid": "ord-1"},
        ("order", "status"): {"status": "pending"},
    })
    await svc.preview("c-1", scheduled_time="2026-09-05T01:00:00Z", apply_credits=False)
    result = await svc.place("c-1", confirmed=True, tip_cents=360)
    args = cli.argv("order", "submit")
    assert "--yes" in args
    assert args[args.index("--tip-cents") + 1] == "360"
    assert args[args.index("--scheduled-time") + 1] == "2026-09-05T01:00:00Z"
    assert "--no-apply-credits" in args and args[args.index("--fulfillment") + 1] == "delivery"
    assert result["accepted"] is True and result["order_uuid"] == "ord-1"
    assert result["placed"] is False and result["status"] == "pending"
    assert result["card"] == "Visa ending in 1234" and result["tip"] == "$3.60"
    assert svc.last_order["order_uuid"] == "ord-1"
    # The cart is spent: a second place needs a fresh preview.
    with pytest.raises(DoorDashError, match="not been previewed"):
        await svc.place("c-1", confirmed=True, tip_cents=360)


async def test_pickup_never_carries_a_dasher_tip(tmp_path) -> None:
    pickup = json.loads(json.dumps(PREVIEW))
    pickup["quote"]["store_order_cart"]["fulfillment_type"] = "PICKUP"
    svc, cli = service(tmp_path, {
        ("order", "preview"): pickup, ("payment-method", "list"): CARDS,
        ("order", "submit"): {"success": True, "order_uuid": "ord-2"},
        ("order", "status"): {"status": "placed"},
    })
    await svc.preview("c-1")
    result = await svc.place("c-1", confirmed=True)
    args = cli.argv("order", "submit")
    assert args[args.index("--tip-cents") + 1] == "0"
    assert result["placed"] is True and "placed" in result["note"]


async def test_age_restricted_carts_hand_over_a_checkout_link(tmp_path) -> None:
    svc, _ = service(tmp_path, {
        ("order", "preview"): PREVIEW, ("payment-method", "list"): CARDS,
        ("order", "submit"): {"success": False, "error_reason": "AGENTIC_RESTRICTED_ITEM_NOT_ALLOWED", "message": "Age-restricted items"},
        ("order", "checkout-url"): {"checkout_url": "https://www.doordash.com/checkout/abc"},
    })
    await svc.preview("c-1")
    result = await svc.place("c-1", confirmed=True, tip_cents=0)
    assert result["accepted"] is False and result["restricted_items"] is True
    assert result["checkout_url"] == "https://www.doordash.com/checkout/abc"


async def test_status_is_normalized_and_terminal_states_flagged(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "status"): {"order": {"status": "ORDER_STATUS_DASHER_NEARBY", "eta": "6:45 PM"}}})
    result = await svc.order_status("ord-1")
    assert result == {"order_uuid": "ord-1", "status": "dasher_nearby", "store_name": None, "eta": "6:45 PM", "terminal": False, "placed": True}
    with pytest.raises(DoorDashError, match="No order has been placed"):
        await svc.order_status()


async def test_grocery_list_resolves_items_and_flags_what_was_not_found(tmp_path) -> None:
    svc, cli = service(tmp_path, {("build-grocery-list",): {
        "store_name": "Fred Meyer", "menu_id": "gm-1", "delivery_time": "45-60 min",
        "items": [
            {"id": "a1", "name": "Whole Milk, 1 gal", "price": 4.29, "quantity": 2, "store_id": 5},
            {"id": "a2", "name": "Ground Beef", "price": 6.99, "quantity": 0.5, "measurement_unit": "lb", "store_id": 5},
        ],
        "available_stores": [{"store_id": 5, "name": "Fred Meyer", "delivery_time": "45-60 min"}, {"store_id": 6, "name": "Main St Grocery"}],
        "message": "couldn't find: saffron",
    }})
    result = await svc.grocery_list([{"name": "milk", "quantity": 2}, "ground beef", {"name": "saffron"}], store_name="Fred Meyer")
    sent = json.loads(cli.argv("build-grocery-list")[cli.argv("build-grocery-list").index("--items-json") + 1])
    assert sent == [{"name": "milk", "quantity": 2}, {"name": "ground beef", "quantity": 1}, {"name": "saffron", "quantity": 1}]
    assert cli.argv("build-grocery-list")[cli.argv("build-grocery-list").index("--desired-mx-name") + 1] == "Fred Meyer"
    assert result["store_id"] == "5" and result["menu_id"] == "gm-1"
    assert result["items"][1]["unit"] == "lb" and result["items"][1]["quantity"] == 0.5
    assert result["possibly_missing"] == ["saffron"]
    assert result["other_stores"][1]["name"] == "Main St Grocery"


async def test_campaign_promos_are_applied_with_their_ids(tmp_path) -> None:
    svc, cli = service(tmp_path, {
        ("cart", "show"): {"cart": {"id": "c-1", "store_id": 928163, "items": []}},
        ("promo", "list"): {"promotions": [{"code": "SAVE20", "campaign_id": "camp", "ad_group_id": "grp", "ad_id": "ad"}]},
        ("promo", "apply"): {"success": True, "discount_cents": 480, "subtotal_cents": 2400},
    })
    result = await svc.promo_apply("c-1", "save20")
    args = cli.argv("promo", "apply")
    assert args[args.index("--campaign-id") + 1] == "camp" and args[args.index("--ad-id") + 1] == "ad"
    assert result == {"success": True, "code": "save20", "action": "apply", "discount": "$4.80", "subtotal": "$24.00"}


async def test_setting_the_default_address_passes_yes_for_the_non_tty_prompt(tmp_path) -> None:
    svc, cli = service(tmp_path, {("address", "list"): ADDRESSES, ("address", "set"): {"success": True}})
    result = await svc.set_default_address("work")
    args = cli.argv("address", "set")
    assert args[args.index("--address-id") + 1] == "addr-work" and "--yes" in args
    assert result["default"]["label"] == "Work"


async def test_cli_errors_surface_their_message(tmp_path) -> None:
    svc, _ = service(tmp_path, {("cart", "delete"): (2, {"success": False, "message": "Cart not found"}, "")})
    with pytest.raises(DoorDashError, match="Cart not found"):
        await svc.cart_delete("nope")


async def test_a_search_nobody_matched_is_asked_again_with_fewer_words(tmp_path) -> None:
    """The real failure (2026-09-25): "is there a special Baja blast for Olivia
    Rodrigo" searched for all five words, matched no store, and the turn ended
    there — while "baja blast" finds the Taco Bell that sells it."""
    def by_query(args: list[str]) -> dict:
        asked = args[args.index("--query") + 1]
        return SEARCH if asked == "baja blast" else {"stores": []}

    svc, cli = service(tmp_path, {
        ("address", "list"): ADDRESSES,
        ("search",): by_query,
        ("cart", "list"): {"carts": []},
    })
    result = await svc.search("special baja blast olivia rodrigo", limit=5)

    assert [args[args.index("--query") + 1] for args in cli.calls if args[0] == "search"] == [
        "special baja blast olivia rodrigo",
        "baja blast",
    ]
    assert result["widened_to"] == "baja blast"
    assert result["stores"][0]["name"] == "Zeeks Pizza"
    assert "did not find that exact thing" in result["note"]


async def test_a_search_that_finds_stores_says_they_are_not_menu_items(tmp_path) -> None:
    svc, _ = service(tmp_path, {
        ("address", "list"): ADDRESSES, ("search",): SEARCH, ("cart", "list"): {"carts": []},
    })
    result = await svc.search("baja blast")
    assert "stores, not menu items" in result["stores_not_items"]


async def test_what_flavors_asks_the_menu_for_its_sections(tmp_path) -> None:
    """'flavors' is not on any menu. Queried for it, the store's sections are
    the answer — not "there are no items named flavors"."""
    svc, _ = service(tmp_path, {("address", "list"): ADDRESSES, ("menu",): MENU})
    result = await svc.menu("928163", "flavors", 10)

    assert result["categories"] == ["Pizzas", "Drinks"]
    assert [item["name"] for item in result["items"]] == ["Large Pepperoni", "Margherita", "Coke"]
    assert "no item called 'flavors'" in result["note"]
    assert "no_match" not in result


async def test_a_matched_query_says_to_read_out_every_version(tmp_path) -> None:
    svc, _ = service(tmp_path, {("address", "list"): ADDRESSES, ("menu",): MENU})
    result = await svc.menu("928163", "pizza", 10)
    assert [item["name"] for item in result["items"]] == ["Large Pepperoni", "Margherita"]
    assert "name every one of them" in result["note"]


def test_a_store_id_from_nowhere_is_not_a_store(tmp_path) -> None:
    svc, _ = service(tmp_path, {})
    assert svc.known_store("928163") is False
    svc._remember_stores([{"store_id": "928163", "name": "Zeeks Pizza"}])
    assert svc.known_store("928163") is True
    assert svc.known_store("Olivia Rodrigo") is False
    assert svc.known_store(None) is False
