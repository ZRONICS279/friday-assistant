"""dd-cli's real answers, as captured from a signed-in account (trimmed and
with the household's details replaced), through Friday's parsers. These
pin the shapes the CLI actually uses, which its help text only hints at."""

import json
import re
import stat

from house_agent.config import DoorDashConfig
from house_agent.doordash import DoorDashService


def envelope(structured):
    return {
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "structuredContent": structured,
        "isError": False,
    }


MONEY = lambda cents: {"unit_amount": cents, "currency": "USD", "display_string": f"${cents / 100:.2f}", "decimal_places": 2, "sign": True, "symbol": "$"}  # noqa: E731

ADDRESSES = envelope({
    "addresses": [
        {"address_id": "1000000002", "printable_address": "Hotel, 808 Somewhere St, Seattle, WA 98101", "city": "Seattle", "state": "WA", "lat": 47.6, "lng": -122.3, "is_default": False, "label": None},
        {"address_id": "1000000001", "printable_address": "100 Main St, Springfield, IL 62701", "city": "Springfield", "state": "IL", "lat": 39.78, "lng": -89.65, "is_default": True, "label": None},
        {"address_id": "22", "printable_address": "1 Resort Dr, Winter Garden, FL", "city": "Winter Garden", "state": "FL", "is_default": False, "label": "disney hotel"},
    ],
    "trace_id": "x", "success": True, "message": None,
})
SEARCH = envelope({
    "title": 'Top picks for "mcdonalds" near you', "query": "mcdonalds",
    "stores": [
        {"store_id": "20000001", "name": "Corner Burger", "image_url": "https://x", "distance_meters": 1230.9, "distance": "0.8 mi", "delivery_time": "26 min", "rating": 4.3, "review_count": 127, "verified_name": "Corner Burger", "is_link_out": False},
        {"store_id": "1640535", "name": "JJ's In and Out", "distance_meters": 1313.3, "distance": "0.8 mi", "delivery_time": "30 min", "rating": 3.0, "review_count": 59, "is_link_out": False},
        {"store_id": "1234", "name": "McDonald's", "distance_meters": 1126.5, "distance": "0.7 mi", "delivery_time": "22 min", "rating": 4.1, "review_count": 2200, "is_link_out": False},
    ],
    "session_id": "", "delivery_address": "100 Main St, Springfield, IL 62701", "address_id": "1000000001",
    "search_radius": 8.0, "is_authenticated": True, "success": True, "message": "Restaurant list shown.",
})
NEARBY = envelope({"stores": [
    {"store_id": "1748693", "name": "Main St Grocery", "menu_id": "", "business_vertical_id": 68, "distance_meters": 1026.7, "delivery_time": "51 min", "availability_status": "available", "next_open_time_asap_delivery_ms": 1788613200000.0, "printable_address": None},
    {"store_id": "23826631", "name": "Grocery Outlet", "menu_id": "", "distance_meters": 1625.1, "delivery_time": "58 min", "availability_status": "closed", "next_open_time_asap_delivery_ms": 1788620400000.0},
], "success": True, "message": "Found 2 stores near consumer location"})
MENU = envelope({
    "menu_id": "67387347",
    "items": [
        {"item_id": "i_25474302471", "name": "AMERICANO", "description": "Smashburger with american cheese, lettuce, grilled onions, pickles, and burger sauce on a potato bun.", "image_url": "https://x", "price": 12.5, "price_varies": False, "extras": [], "is_orderable": True, "unavailability_reason": None, "is_popular": True, "popularity_rank": 1, "category_id": "246885488", "category_name": "Smashburgers", "applicable_promotion_ids": [], "has_modifiers": True, "has_required_modifiers": False},
        {"item_id": "i_25474302474", "name": "THE DOUBLE", "description": "Smash burger with double meat.", "price": 16.25, "price_varies": False, "is_orderable": True, "category_name": "Smashburgers", "applicable_promotion_ids": [], "has_modifiers": True, "has_required_modifiers": True},
        {"item_id": "i_25474302490", "name": "HORCHATA", "description": "", "price": 4.0, "price_varies": False, "is_orderable": False, "unavailability_reason": "Sold out", "category_name": "Drinks", "has_modifiers": False, "has_required_modifiers": False},
    ],
    "success": True, "message": "Retrieved 3 items for store ID 20000001", "store_is_open": True,
    "promotions": [{"title": "$5 Reward", "description": "Terms Apply.", "code": "319bce21"}],
    "store_id": "20000001", "store_name": "", "is_authenticated": True, "session_id": "", "categories": [],
})
ITEM = envelope({"item": {
    "item_id": "i_25474302471", "name": "AMERICANO", "description": "Smashburger with american cheese.", "price": 12.5, "price_varies": False,
    "extras": [
        {"extra_id": "e_9694689186", "title": "AMERICANO MODIFIERS", "min_num_options": 0, "max_num_options": 21, "num_free_options": 0,
         "options": [{"option_id": "o_44454020697", "name": "Small Fries", "price": 5.0, "extras": []}, {"option_id": "o_44454020698", "name": "Large Fries", "price": 8.75, "extras": []}]},
        {"extra_id": "e_1", "title": "Choice of Bun", "min_num_options": 1, "max_num_options": 1, "num_free_options": 1,
         "options": [{"option_id": "o_1", "name": "Potato", "price": 0, "extras": []}, {"option_id": "o_2", "name": "Lettuce wrap", "price": 0, "extras": []}]},
    ],
    "is_orderable": True, "category_name": "Smashburgers", "has_modifiers": True, "has_required_modifiers": True,
}, "success": True, "message": "Retrieved item"})
CARTS = envelope({"carts": [{
    "cart_uuid": "83d2aa94", "store_id": "35799129", "store_name": "Fred Meyer",
    "items": [
        {"id": "f087cf2f", "item_id": "30990583708", "name": "Yasso Mint Chocolate Chip Bars", "quantity": 1, "price": 6.79, "nested_options": []},
        {"id": "84182a29", "item_id": "30990583638", "name": "Yasso Fudge Bars", "quantity": 1, "price": 6.79, "nested_options": []},
    ],
    "items_count": 2, "created_at": 1786332204400, "updated_at": 1786332228794,
}, {
    "cart_uuid": "ae2a3b98", "store_id": "1027478", "store_name": "Menchie’s Frozen Yogurt",
    "items": [{"id": "6e3cdb7d", "item_id": "32524302806", "name": "32oz Large Cup", "quantity": 1, "price": 18.99,
               "nested_options": [{"id": "46846139626", "name": "Pure Chocolate FroYo", "quantity": 1}, {"id": "48328792488", "name": "Creamy Peanut Butter FroYo", "quantity": 1}]}],
    "items_count": 1, "created_at": 1786331934522, "updated_at": 1786331934567,
}], "success": True})
CART_SHOW = envelope({"cart": {
    "id": "83d2aa94", "store_id": "35799129", "store_name": "Fred Meyer",
    "items": [{"id": "f087cf2f", "item_id": "30990583708", "menu_id": "79849168", "name": "Yasso Mint Chocolate Chip Bars", "description": "", "quantity": 1.0, "price": 6.79, "nested_options": []}],
    "items_count": 1, "group_cart_url": None, "savings": None, "spend_limit_cents": None, "is_group_cart": False, "subcarts": [],
}, "cart_uuid": "83d2aa94", "success": True})
HISTORY = envelope({"orders": [
    {"order_uuid": "0e0f651e", "store_id": "543554", "store_name": "Chipotle Mexican Grill", "is_reorderable": True, "order_date": "2026-08-24T01:05:48.743Z", "order_fulfilled_at": "2026-08-24T01:23:23.173Z", "fulfillment_type": "FULFILLMENT_TYPE_DX_DELIVERY", "order_target": "ORDER_TARGET_RESTAURANT", "items": [{"item_id": "4382083075", "name": "Burrito Bowl", "quantity": 1}, {"item_id": "4382083075", "name": "Burrito Bowl", "quantity": 1}], "is_group_order": False},
    {"order_uuid": "0ae0432d", "store_id": "1081677", "store_name": "Robeks", "is_reorderable": True, "order_date": "2026-08-09T19:01:06.630Z", "fulfillment_type": "FULFILLMENT_TYPE_DX_DELIVERY", "order_target": "ORDER_TARGET_RESTAURANT", "items": [{"item_id": "1", "name": "800 LB Gorilla", "quantity": 1}], "is_group_order": False},
    {"order_uuid": "77", "store_id": "543554", "store_name": "Chipotle Mexican Grill", "is_reorderable": False, "order_date": "2026-07-01T01:05:48.743Z", "fulfillment_type": "FULFILLMENT_TYPE_PICKUP", "order_target": "ORDER_TARGET_RESTAURANT", "items": [{"name": "Chips", "quantity": 2}]},
], "success": True, "message": "Found 3 orders", "page_full": True})
STATUS = envelope({"result": {"status": "completed", "status_message": None, "status_updated_at": "2026-08-24T01:23:23.173Z", "action_required": False, "merchant_name": "Chipotle Mexican Grill", "is_pickup": False, "quoted_delivery_time": "2026-08-24T01:36:07Z", "actual_delivery_time": "2026-08-24T01:23:23.173Z", "eta_trend": None, "late_reason": None, "cancellation_reason": None}, "success": True, "message": "Order Complete"})
STATUS_LIVE = envelope({"result": {"status": "dasher_assigned", "status_message": "Your Dasher is heading to the store", "action_required": False, "merchant_name": "Zeeks Pizza", "is_pickup": False, "quoted_delivery_time": "2026-09-05T03:36:07Z", "eta_trend": "on_time"}, "success": True})
PREVIEW = envelope({
    "success": True, "cart_uuid": "83d2aa94", "items": [], "stores": [], "fulfillment_options": [], "error_message": "", "warning": "", "message": "Order preview generated successfully",
    "quote": {
        "line_items": [
            {"charge_id": "SUBTOTAL", "label": "Subtotal", "final_money": MONEY(1358)},
            {"charge_id": "CHARGE_ID_BAG_FEE", "label": "Estimated Bag Fee", "final_money": MONEY(8)},
            {"charge_id": "DELIVERY_FEE", "label": "Delivery Fee", "final_money": MONEY(0), "original_money": MONEY(599)},
            {"charge_id": "TOTAL", "label": "Total", "final_money": MONEY(1206)},
        ],
        "store_order_cart": {
            "is_consumer_pickup": False, "orders": [{"id": "o1", "order_items": [
                {"id": "f087cf2f", "unit_price_monetary_fields": MONEY(679), "item": {"id": "30990583708", "name": "Yasso Mint Chocolate Chip Bars", "price": 679}, "quantity": 1},
                {"id": "84182a29", "unit_price_monetary_fields": MONEY(679), "item": {"id": "30990583638", "name": "Yasso Fudge Bars", "price": 679}, "quantity": 1},
            ]}],
            "store": {"id": "35799129", "name": "Fred Meyer", "offers_pickup": False},
        },
        "delivery_availability": {"asap_available": True, "asap_minutes_range": [23, 39], "asap_pickup_available": False, "asap_minutes_range_string": "23–39 min", "asap_pickup_minutes_range_string": "3–13 min", "scheduled_delivery_available": True,
                                  "available_days": [{"day_timestamp": {"year": 2026, "month": 9, "day": 4}, "time_windows": [{"display_string": "8:00 PM-9:00 PM", "midpoint_timestamp": "2026-09-05T03:30:00Z", "range_min": "2026-09-05T03:00:00Z", "range_max": "2026-09-05T04:00:00Z"}]}]},
        "delivery_address": {"printable_address": "100 Main St, Springfield, IL 62701", "city": "Springfield"},
        "is_pre_tippable": True,
        "tips_suggestion_details": [{"type": "AMOUNT", "monetary_values": [MONEY(350), MONEY(400), MONEY(450)], "default_index": 1, "tip_recipient": "DASHER"}],
        "credit_details": {"total_credits_applied": MONEY(0)},
        "is_dashpass_applied": True,
        "discount_banner_details": [{"discount_details_message": "Saving $8.45 with DashPass", "required_action": "ADD_MORE_ITEMS"}],
        "net_total_before_tip": MONEY(1206), "total_savings": MONEY(845), "min_age_requirement": 0, "contains_alcohol_item": False,
    },
})
CARDS = envelope({"success": True, "message": "", "cards": [
    {"payment_method_id": "dff30bcf", "last4": "1596", "brand": "Visa", "exp_month": "02", "exp_year": "2023"},
    {"payment_method_id": "01ffc2e2", "last4": "4242", "brand": "MasterCard", "exp_month": "03", "exp_year": "2028"},
], "default_payment_method_id": "01ffc2e2"})
RECEIPT = envelope({
    "success": True, "store_name": "Chipotle - 2271",
    "line_items": [
        {"charge_id": "SUBTOTAL", "label": "Subtotal", "final_money": MONEY(3545)},
        {"charge_id": "DELIVERY_FEE", "label": "Delivery Fee", "final_money": MONEY(0), "original_money": MONEY(599)},
        {"charge_id": "TAX", "label": "Estimated Tax", "final_money": MONEY(370)},
        {"charge_id": "TIP", "label": "Dasher Tip", "final_money": MONEY(400)},
        {"charge_id": "TOTAL", "label": "Total", "final_money": MONEY(4791)},
    ],
    "orders": [{"id": "3000014905757880", "order_items": [
        {"special_instructions": "", "quantity": 1, "id": "1", "options": [{"quantity": 1, "id": "17604208109", "item_extra_option": {"quantity": 1, "id": "17604208109", "price_monetary_fields": MONEY(95), "name": "Carnitas", "description": ""}}], "item": {"id": "4382083075", "name": "Burrito Bowl", "price": 1275}, "unit_price_monetary_fields": MONEY(1370)},
        {"quantity": 2, "id": "2", "options": [], "item": {"id": "9", "name": "Chips & Guac", "price": 500}},
    ]}],
    "payment_charge_details": [{"count": 1, "data": [{"net_amount": MONEY(4791), "original_amount": MONEY(4791), "status": "succeeded", "created_at": "2026-08-24T01:05:50Z", "payment_method": {"dd_payment_method_id": "01ffc2e2", "last4": "4242", "brand": "MasterCard"}}]}],
    "credited_amount": {}, "receipt": {"line_items": []}, "message": "Receipt retrieved",
})
FIND_ITEMS = envelope({"results": {"milk": [
    {"item_id": "30990410681", "item_name": "Malk Organic Oat Milk Bottle (32 fl oz)", "main_price": 6.49, "main_display_price": "$6.49", "unit_price": None, "quantity_display": "32 fl oz", "quantity_increment": 1.0, "non_discounted_price": None, "non_discounted_display_price": None, "purchase_type": "PURCHASE_TYPE_UNIT", "taxonomy": "Dairy & Eggs > Milk Alternatives"},
    {"item_id": "30990409489", "item_name": "Fred Meyer Vitamin D Whole Milk (1 gal)", "main_price": 3.99, "main_display_price": "$3.99", "quantity_display": "1 gal", "non_discounted_display_price": "$4.49", "purchase_type": "PURCHASE_TYPE_UNIT", "taxonomy": "Dairy & Eggs > Milk"},
]}, "success": True})
GROCERY_ITEM = envelope({"id": "30990583708", "name": "Yasso Mint Chocolate Chip Bars 4 ct", "description": "default_description", "pricing_fields": MONEY(679), "variation": {"size": "3.5 oz x 4 ct", "enable_price_on_size": True}, "increment": {"decimal_places": 2, "unit_amount": 100}, "unit": "qty", "menu_id": "79849168", "store_id": "35799129", "details": "Put this mint where your mouth is.", "purchase_type": "PURCHASE_TYPE_UNIT", "success": True})
STORE = envelope({"store": {"store_id": "20000001", "name": "Corner Burger", "menu_id": "", "business_vertical_id": 0, "distance_meters": None, "delivery_time": "26 min", "availability_status": None, "printable_address": "1234 State St, Springfield, IL 62701"}, "success": True, "message": "Found store: Corner Burger"})


class RealCLI:
    def __init__(self, table):
        self.table = table
        self.calls = []

    async def __call__(self, argv, env, timeout):
        args = argv[2:-2]
        self.calls.append(args)
        for length in range(len(args), 0, -1):
            payload = self.table.get(tuple(args[:length]))
            if payload is not None:
                return 0, json.dumps(payload).encode(), b""
        raise AssertionError(f"unexpected call {args}")


def service(tmp_path, table):
    binary = tmp_path / "dd-cli"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    cli = RealCLI(table)
    return DoorDashService(DoorDashConfig(binary=binary, timeout_seconds=5), runner=cli), cli


async def test_search_reads_the_envelope_and_puts_the_named_store_first(tmp_path) -> None:
    svc, cli = service(tmp_path, {("address", "list"): ADDRESSES, ("search",): SEARCH, ("cart", "list"): CARTS})
    result = await svc.search("mcdonalds", limit=3)
    args = cli.argv("search") if hasattr(cli, "argv") else next(a for a in cli.calls if a[0] == "search")
    assert args[args.index("--address-id") + 1] == "1000000001"  # the account's default, in Springfield
    assert args[args.index("--limit") + 1] == "10"  # more than asked, so a buried exact match still surfaces
    assert [s["name"] for s in result["stores"]] == ["McDonald's", "Corner Burger", "JJ's In and Out"]
    first = result["stores"][0]
    assert first["store_id"] == "1234" and first["miles"] == 0.7 and first["eta"] == "22 min"
    assert first["rating"] == 4.1 and first["reviews"] == 2200
    assert "open_cart" not in first


async def test_an_address_label_still_resolves_among_many_unlabeled_addresses(tmp_path) -> None:
    svc, cli = service(tmp_path, {("address", "list"): ADDRESSES, ("search",): SEARCH, ("cart", "list"): CARTS})
    result = await svc.search("pizza", address="disney")
    args = next(a for a in cli.calls if a[0] == "search")
    assert args[args.index("--address-id") + 1] == "22" and result["near"] == "disney hotel"


async def test_nearby_grocery_stores_say_whether_they_are_open(tmp_path) -> None:
    svc, _ = service(tmp_path, {("address", "list"): ADDRESSES, ("find-nearby-stores",): NEARBY, ("cart", "list"): CARTS})
    result = await svc.search("", kind="grocery")
    haggen, outlet = result["stores"]
    assert haggen["name"] == "Main St Grocery" and haggen["open"] is True and haggen["miles"] == 0.6
    assert outlet["open"] is False and outlet["opens_at"]


async def test_menu_items_come_out_with_bare_ids_prices_and_what_needs_choosing(tmp_path) -> None:
    svc, _ = service(tmp_path, {("menu",): MENU})
    result = await svc.menu("20000001")
    assert result["menu_id"] == "67387347" and result["item_count"] == 3
    assert result["categories"] == ["Smashburgers", "Drinks"]
    americano, double, horchata = result["items"]
    assert americano == {"item_id": "25474302471", "name": "AMERICANO", "price": "$12.50", "description": "Smashburger with american cheese, lettuce, grilled onions, pickles, and burger sauce on a potato bun.", "category": "Smashburgers", "has_options": True}
    assert double["needs_choices"] is True and double["price"] == "$16.25"
    assert horchata["unavailable"] is True and horchata["reason"] == "Sold out"
    assert result["promotions"] == [{"title": "$5 Reward", "description": "Terms Apply.", "code": "319bce21"}]
    burgers = await svc.menu("20000001", query="double")
    assert [i["name"] for i in burgers["items"]] == ["THE DOUBLE"]


async def test_item_details_list_option_groups_with_bare_option_ids(tmp_path) -> None:
    svc, _ = service(tmp_path, {("restaurant-item-details",): ITEM})
    result = await svc.item_details("20000001", "i_25474302471", menu_id="67387347")
    assert result["item_id"] == "25474302471" and result["price"] == "$12.50"
    assert result["required_choices"] == ["Choice of Bun"]
    fries, bun = result["options"]
    assert fries["group"] == "AMERICANO MODIFIERS" and fries["required"] is False
    assert fries["choices"][0] == {"id": "44454020697", "name": "Small Fries", "price": "$5.00"}
    assert bun["required"] is True and bun["free_choices"] == 1
    assert bun["choices"] == [{"id": "1", "name": "Potato"}, {"id": "2", "name": "Lettuce wrap"}]


async def test_carts_show_whole_quantities_chosen_options_and_their_menu(tmp_path) -> None:
    svc, _ = service(tmp_path, {("cart", "list"): CARTS, ("cart", "show"): CART_SHOW})
    carts = await svc.carts()
    assert carts[0]["store_name"] == "Fred Meyer" and carts[0]["item_count"] == 2
    assert carts[0]["items"][0] == {"name": "Yasso Mint Chocolate Chip Bars", "quantity": 1, "price": "$6.79", "cart_item_id": "f087cf2f"}
    assert carts[1]["items"][0]["options"] == ["Pure Chocolate FroYo", "Creamy Peanut Butter FroYo"]
    assert carts[0]["updated"]
    shown = await svc.cart_show("83d2aa94")
    assert shown["menu_id"] == "79849168" and shown["items"][0]["quantity"] == 1


async def test_history_dates_kinds_and_usual_places_from_the_real_shape(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "history"): HISTORY})
    result = await svc.order_history()
    first = result["orders"][0]
    assert first["store_name"] == "Chipotle Mexican Grill"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M", first["when"])  # local clock, not UTC ISO
    assert first["items"] == ["Burrito Bowl", "Burrito Bowl"] and first["fulfillment"] == "delivery" and first["kind"] == "restaurant"
    assert "total" not in first  # history carries no totals; the receipt does
    assert result["orders"][2]["fulfillment"] == "pickup" and result["orders"][2]["reorderable"] is False
    assert result["usual_places"][0] == {"store_name": "Chipotle Mexican Grill", "store_id": "543554", "times": 2, "last_order_uuid": "0e0f651e", "last_items": ["Burrito Bowl", "Burrito Bowl"]}
    assert result["more_available"] is True


async def test_status_is_read_from_result_with_a_spoken_eta(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "status"): STATUS})
    done = await svc.order_status("0e0f651e")
    assert done["status"] == "completed" and done["terminal"] is True and done["store_name"] == "Chipotle Mexican Grill"
    assert "eta" not in done and done["fulfillment"] == "delivery"
    svc, _ = service(tmp_path, {("order", "status"): STATUS_LIVE})
    live = await svc.order_status("x")
    assert live["status"] == "dasher_assigned" and live["placed"] is True and live["terminal"] is False
    assert live["eta"].endswith("M") and ":" in live["eta"]  # a local clock time, e.g. 8:36 PM
    assert live["detail"] == "Your Dasher is heading to the store" and live["eta_trend"] == "on_time"


async def test_preview_reads_the_real_quote(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "preview"): PREVIEW, ("payment-method", "list"): CARDS})
    result = await svc.preview("83d2aa94")
    assert result["store_name"] == "Fred Meyer" and result["fulfillment"] == "delivery"
    assert result["items"] == [
        {"name": "Yasso Mint Chocolate Chip Bars", "quantity": 1, "unit_price": "$6.79"},
        {"name": "Yasso Fudge Bars", "quantity": 1, "unit_price": "$6.79"},
    ]
    assert result["total"] == "$12.06"
    assert result["breakdown"][0] == {"label": "Subtotal", "amount": "$13.58"}
    assert result["eta"] == "23–39 min" and result["asap_available"] is True
    assert result["deliver_to"] == "100 Main St, Springfield, IL 62701"
    assert result["tip"] == {"pre_tippable": True, "suggested_cents": 400, "suggested": "$4.00", "choices": ["$3.50", "$4.00", "$4.50"]}
    assert result["dashpass"] is True and result["total_savings"] == "$8.45"
    assert result["savings"] == ["Saving $8.45 with DashPass"]
    assert "credits_applied" not in result and "pin_required_at_handoff" not in result and "age_restricted" not in result
    assert result["default_card_spoken"] == "MasterCard ending in 4242"


async def test_receipt_has_items_with_options_totals_and_the_card(tmp_path) -> None:
    svc, _ = service(tmp_path, {("order", "receipt"): RECEIPT})
    result = await svc.receipt("0e0f651e")
    assert result["store_name"] == "Chipotle - 2271"
    assert result["items"] == ["Burrito Bowl (Carnitas)", "2× Chips & Guac"]
    assert result["subtotal"] == "$35.45" and result["tip"] == "$4.00" and result["total"] == "$47.91"
    assert result["card"] == "MasterCard ending in 4242"
    assert [b["label"] for b in result["breakdown"]] == ["Subtotal", "Delivery Fee", "Estimated Tax", "Dasher Tip", "Total"]


async def test_grocery_search_and_item_details_from_the_real_shapes(tmp_path) -> None:
    svc, cli = service(tmp_path, {("find-items",): FIND_ITEMS, ("item-details",): GROCERY_ITEM})
    found = await svc.menu("35799129", query="milk", grocery=True)
    assert found["items"][0] == {"item_id": "30990410681", "name": "Malk Organic Oat Milk Bottle (32 fl oz)", "price": "$6.49", "category": "Dairy & Eggs > Milk Alternatives", "size": "32 fl oz", "matched": "milk"}
    assert found["items"][1]["original_price"] == "$4.49"
    detail = await svc.item_details("35799129", "30990583708")
    assert detail["item_id"] == "30990583708" and detail["price"] == "$6.79" and detail["size"] == "3.5 oz x 4 ct"
    assert detail["menu_id"] == "79849168" and detail["description"] == "Put this mint where your mouth is."
    assert detail["options"] == [] and detail["required_choices"] == []


async def test_store_details_carry_the_street_address(tmp_path) -> None:
    svc, _ = service(tmp_path, {("store-details",): STORE})
    result = await svc.store_info("20000001")
    assert result["name"] == "Corner Burger" and result["address"] == "1234 State St, Springfield, IL 62701"
    assert result["eta"] == "26 min" and "open" not in result


async def test_an_error_envelope_is_an_error(tmp_path) -> None:
    import pytest
    from house_agent.doordash import DoorDashError

    failing = {"content": [{"type": "text", "text": "Store not found"}], "structuredContent": None, "isError": True}
    svc, _ = service(tmp_path, {("store-details",): failing})
    with pytest.raises(DoorDashError, match="Store not found"):
        await svc.store_info("0")
