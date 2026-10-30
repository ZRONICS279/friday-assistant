"""Ordering in one call, and the working memory that carries a chat's
DoorDash context from turn to turn.

The real failure, 2026-09-04: asked for "a cheeseburger from McDonald's",
the model searched, listed three branches with their store ids, and stopped;
next turn it had forgotten them and searched again — near a hotel label it
had never been told about.
"""

import copy
import json
import stat

import numpy as np

from house_agent.config import AppConfig, DoorDashConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.doordash import _pick_item, mentioned
from house_agent.llm import LocalAgent
from house_agent.tools import CURRENT_REQUEST, CURRENT_SPEAKER, FoodSession, ToolRegistry
from house_agent.voice import VoiceIdentity
from tests.test_doordash import ADDRESSES, CARDS, FakeCLI, PREVIEW, service

MCD_SEARCH = {
    "stores": [
        {"store_id": 1, "name": "Burger King", "delivery_time": "20 min", "distance_meters": 800},
        {"store_id": 685416, "name": "McDonald's", "delivery_time": "39 min", "distance_meters": 4200, "availability_status": "available"},
        {"store_id": 877843, "name": "McDonald's", "delivery_time": "35 min", "distance_meters": 5500, "availability_status": "available"},
        {"store_id": 685898, "name": "McDonald's", "delivery_time": "15 min", "distance_meters": 900, "availability_status": "closed"},
    ]
}
MCD_MENU = {
    "menu_id": "m-mcd",
    "store_name": "McDonald's",
    "categories": [
        {"name": "Burgers", "items": [
            {"item_id": "i_1", "name": "Double Cheeseburger", "price": "$4.49"},
            {"item_id": "i_2", "name": "Cheeseburger", "price": "$2.99"},
            {"item_id": "i_3", "name": "Bacon Cheeseburger", "price": "$3.99", "is_orderable": False},
        ]},
        {"name": "Sides", "items": [{"item_id": "i_4", "name": "World Famous Fries", "price": "$3.29", "has_required_modifiers": True}]},
    ],
}


MCD_PREVIEW = copy.deepcopy(PREVIEW)
MCD_PREVIEW["quote"]["store_order_cart"]["store_name"] = "McDonald's"
MCD_PREVIEW["quote"]["store_order_cart"]["orders"][0]["order_items"] = [
    {"item": {"name": "Cheeseburger"}, "quantity": 1, "unit_price_monetary_fields": {"display_string": "$2.99"}},
]


def added_ok(args):
    sent = json.loads(args[args.index("--items-json") + 1])
    return {"success": True, "cart_uuid": "c-mcd", "cart": {
        "store_name": "McDonald's",
        "items": [{"id": f"line-{i}", "name": s["item_name"], "quantity": s["quantity"]} for i, s in enumerate(sent)],
    }}


def mcd_responses(carts=None):
    return {
        ("address", "list"): ADDRESSES,
        ("search",): MCD_SEARCH,
        ("menu",): MCD_MENU,
        ("cart", "list"): {"carts": carts or []},
        ("cart", "add-items"): added_ok,
        ("order", "preview"): MCD_PREVIEW,
        ("payment-method", "list"): CARDS,
        ("cart", "delete"): {"success": True},
    }


async def test_a_cheeseburger_from_mcdonalds_is_one_call_to_a_priced_cart(tmp_path) -> None:
    svc, cli = service(tmp_path, mcd_responses())
    result = await svc.order_items("McDonald's", [{"name": "a cheeseburger", "quantity": 1}], purpose="order")
    # The fastest open branch, not the closed one next door and not Burger King.
    assert result["store"]["store_id"] == "877843" and result["store"]["name"] == "McDonald's"
    assert "branch_note" in result["store"]
    assert cli.argv("menu")[cli.argv("menu").index("--store-id") + 1] == "877843"
    sent = json.loads(cli.argv("cart", "add-items")[cli.argv("cart", "add-items").index("--items-json") + 1])
    assert sent == [{"item_id": "2", "item_name": "Cheeseburger", "quantity": 1}]
    assert result["added"] == [{"name": "Cheeseburger", "quantity": 1, "price": "$2.99", "instead_of": ["Double Cheeseburger"]}]
    assert result["total"] == "$31.20" and result["default_card_spoken"].startswith("Visa")
    assert result["cart_uuid"] == "c-mcd" and "ask whether to place it" in result["next"]


async def test_plural_words_and_sold_out_items(tmp_path) -> None:
    svc, cli = service(tmp_path, mcd_responses())
    result = await svc.order_items("mcdonalds", [{"name": "two cheeseburgers", "quantity": 2}, {"name": "bacon cheeseburger"}, {"name": "sushi"}])
    sent = json.loads(cli.argv("cart", "add-items")[cli.argv("cart", "add-items").index("--items-json") + 1])
    assert sent == [{"item_id": "2", "item_name": "Cheeseburger", "quantity": 2}]
    assert result["not_found"] == [
        {"asked": "bacon cheeseburger", "found": "Bacon Cheeseburger", "unavailable": True, "reason": None},
        {"asked": "sushi", "categories": ["Burgers", "Sides"]},
    ]


async def test_a_dish_already_waiting_in_the_cart_is_not_added_twice(tmp_path) -> None:
    svc, cli = service(tmp_path, mcd_responses(carts=[
        {"cart_uuid": "c-mcd", "store_id": "877843", "store_name": "McDonald's", "items": [{"name": "Cheeseburger", "quantity": 1}]},
    ]))
    result = await svc.order_items("McDonald's", [{"name": "cheeseburger"}])
    assert result["already_in_cart"] == ["Cheeseburger"]
    assert not any(args[:2] == ["cart", "add-items"] for args in cli.calls)
    assert result["total"] == "$31.20"  # priced as it stands


async def test_several_different_places_or_none_means_ask_not_guess(tmp_path) -> None:
    responses = mcd_responses()
    responses[("search",)] = {"stores": [
        {"store_id": 7, "name": "Uptown Burger", "delivery_time": "22 min"},
        {"store_id": 8, "name": "Corner Burger", "delivery_time": "23 min"},
    ]}
    svc, cli = service(tmp_path, responses)
    result = await svc.order_items("burger place", [{"name": "cheeseburger"}])
    assert [s["name"] for s in result["choose_store"]] == ["Uptown Burger", "Corner Burger"]
    assert result["store"] is None and "ask which one" in result["note"]
    nothing = await svc.order_items("Dick's Drive-In", [{"name": "deluxe"}])
    assert nothing["store"] is None and [s["name"] for s in nothing["stores"]] == ["Uptown Burger", "Corner Burger"]
    assert not any(args[:1] == ["menu"] for args in cli.calls)


async def test_required_choices_come_back_as_a_question(tmp_path) -> None:
    responses = mcd_responses()
    responses[("cart", "add-items")] = {
        "success": False, "cart_uuid": "c-mcd", "cart": {"store_name": "McDonald's", "items": []},
        "item_errors": [{"item_id": "4", "item_name": "World Famous Fries", "error_message": "Required option missing",
                         "required_options": [{"name": "Size", "min_num_options": 1, "max_num_options": 1,
                                               "options": [{"id": "s", "name": "Small"}, {"id": "l", "name": "Large"}]}]}],
    }
    svc, _ = service(tmp_path, responses)
    result = await svc.order_items(None, [{"name": "fries"}], store_id="877843")
    assert result["needs_choices"] is True and result["added"] == []
    assert result["item_errors"][0]["required_choices"][0]["choices"][1] == {"id": "l", "name": "Large"}
    assert "total" not in result


def test_the_plain_item_wins_over_its_variants() -> None:
    items = [
        {"item_id": "1", "name": "Double Cheeseburger"},
        {"item_id": "2", "name": "Cheeseburger Deluxe"},
        {"item_id": "3", "name": "Cheeseburger"},
    ]
    picked, others = _pick_item(items, "the cheeseburger")
    assert picked["item_id"] == "3" and {o["item_id"] for o in others} == {"1", "2"}
    picked, _ = _pick_item(items, "double cheeseburgers")
    assert picked["item_id"] == "1"
    assert _pick_item(items, "fries") == (None, [])


def test_an_address_label_counts_only_when_the_user_said_it() -> None:
    assert mentioned("disney hotel", "order a cheeseburger to the disney hotel")
    assert mentioned("Work", "send it to work please")
    assert not mentioned("disney hotel", "order me a cheeseburger from McDonald's")
    assert not mentioned("the cabin", "is the coke in the cart")  # 'the' is not a word of the label


def registry(tmp_path) -> ToolRegistry:
    binary = tmp_path / "dd-cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(think_complex=False),
        doordash=DoorDashConfig(binary=binary, timeout_seconds=5),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return tools


async def test_an_address_the_user_never_said_is_dropped_for_home(tmp_path) -> None:
    tools = registry(tmp_path)
    cli = FakeCLI(mcd_responses())
    tools.doordash._runner = cli
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    CURRENT_REQUEST.set("Now from McDonald's, order me a cheeseburger")
    result = await tools.execute("doordash_order", {"store": "McDonald's", "items": [{"name": "cheeseburger"}], "address": "disney hotel"})
    assert result["ok"] and result["near"] == "Home"
    assert cli.argv("search")[cli.argv("search").index("--address-id") + 1] == "addr-home"
    assert "'disney hotel' was not in the user's request" in result["address_ignored"]

    CURRENT_REQUEST.set("send a cheeseburger to work")
    result = await tools.execute("doordash_search", {"query": "cheeseburger", "address": "work"})
    assert result["ok"] and result["near"] == "Work" and "address_ignored" not in result


async def test_the_chat_remembers_its_doordash_context_in_memory_only(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.doordash._runner = FakeCLI(mcd_responses())
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    CURRENT_REQUEST.set("any mcdonalds near me?")
    await tools.execute("doordash_search", {"query": "mcdonalds"})
    line = tools.food_context("c1")
    assert "last search 'mcdonalds' near Home found: McDonald's (store_id 685416); McDonald's (store_id 877843)" in line
    assert tools.food_context("other-chat") is None

    CURRENT_REQUEST.set("order me a cheeseburger from there")
    await tools.execute("doordash_order", {"store_id": "877843", "items": [{"name": "cheeseburger"}]})
    line = tools.food_context("c1")
    assert "store in play: McDonald's (store_id 877843)" in line
    assert "cart at McDonald's: 1× Cheeseburger; already priced at $31.20 on Visa ending in 1234, suggested tip $3.60" in line
    assert "leave cart_uuid out" in line and "nothing has to be previewed first" in line
    assert "c-mcd" not in line  # the model never has to copy the id
    assert "never say an id aloud" in line

    await tools.execute("doordash_cart_delete", {})
    assert "cart at McDonald's" not in tools.food_context("c1")
    # Nothing of it reached the database.
    assert tools.database.get_messages("c1") == []
    assert FoodSession().line() is None


async def test_the_model_sees_the_doordash_line_next_turn(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.doordash._runner = FakeCLI(mcd_responses())
    agent = LocalAgent(tools.config.llm, tools.database, tools)
    seen: list[list[dict]] = []
    script = [
        {"tool_calls": [{"id": "1", "function": {"name": "doordash_search", "arguments": json.dumps({"query": "mcdonalds"})}}]},
        {"content": "There are three McDonald's near you."},
        {"content": "Which branch?"},
    ]

    async def fake_stream(client, messages, effort, allow_tools=True):
        seen.append(list(messages))
        reply = script[len(seen) - 1]
        yield "message", reply

    agent._stream_completion = fake_stream
    async for _ in agent.chat_stream("any mcdonalds?", "c1"):
        pass
    async for _ in agent.chat_stream("order a cheeseburger from there", "c1"):
        pass
    lines = [m["content"] for m in seen[-1] if m.get("role") == "system" and "DoorDash so far" in str(m.get("content"))]
    assert len(lines) == 1 and "McDonald's (store_id 877843)" in lines[0]
    # It sits right before the user's words, where a small model reads it.
    assert seen[-1][-1]["role"] == "user" and "DoorDash so far" in seen[-1][-2]["content"]
    # And never in the saved trace.
    saved = tools.database.get_messages("c1")
    assert len(saved) == 4 and all("877843" not in json.dumps(m, default=str) for m in saved)


async def test_the_same_call_twice_in_one_turn_runs_once(tmp_path) -> None:
    tools = registry(tmp_path)
    agent = LocalAgent(tools.config.llm, tools.database, tools)
    ran: list[str] = []
    original = tools.execute

    async def counting(name, arguments):
        ran.append(name)
        return await original(name, arguments)

    tools.execute = counting
    show = {"id": "1", "function": {"name": "list_show", "arguments": json.dumps({"name": "shopping"})}}
    script = [
        {"tool_calls": [show, {**show, "id": "2"}]},
        {"tool_calls": [{**show, "id": "3"}]},
        {"content": "Your list is empty."},
    ]
    seen: list[list[dict]] = []

    async def fake_stream(client, messages, effort, allow_tools=True):
        seen.append(list(messages))
        yield "message", script[len(seen) - 1]

    agent._stream_completion = fake_stream
    events = [e async for e in agent.chat_stream("what's on my list?", "c1")]
    assert ran == ["list_show"]
    repeats = [m for m in seen[-1] if m.get("role") == "tool" and "already ran this turn" in m["content"]]
    assert len(repeats) == 2
    assert events[-1]["type"] == "done"


def placing_responses():
    responses = mcd_responses()
    responses[("order", "submit")] = {"success": True, "order_uuid": "ord-9"}
    responses[("order", "status")] = {"status": "pending"}
    return responses


async def test_a_yes_places_the_chats_cart_with_no_id_and_the_suggested_tip(tmp_path) -> None:
    """The 2026-09-04 chats: 'yes, place the order' failed three ways — no
    cart_uuid at all, a garbled one, and a tip the model made up in cents."""
    tools = registry(tmp_path)
    cli = FakeCLI(placing_responses())
    tools.doordash._runner = cli
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    CURRENT_REQUEST.set("order me a big mac from mcdonalds")
    await tools.execute("doordash_order", {"store": "McDonald's", "items": [{"name": "cheeseburger"}]})

    CURRENT_REQUEST.set("the suggested tip is perfect, yes place it")
    result = await tools.execute("doordash_order_place", {"confirmed": True})
    assert result["ok"] and result["accepted"] and result["order_uuid"] == "ord-9"
    assert result["tip"] == "$3.60" and result["tip_source"] == "suggested"
    assert result["cart_source"] == "this chat's cart"
    args = cli.argv("order", "submit")
    assert args[args.index("--cart-uuid") + 1] == "c-mcd" and args[args.index("--tip-cents") + 1] == "360"
    assert "order sent: McDonald's" in tools.food_context("c1")


async def test_a_wrong_cart_id_from_the_model_is_replaced_by_the_chats_cart(tmp_path) -> None:
    tools = registry(tmp_path)
    cli = FakeCLI(placing_responses())
    tools.doordash._runner = cli
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    CURRENT_REQUEST.set("a cheeseburger from mcdonalds")
    await tools.execute("doordash_order", {"store": "McDonald's", "items": [{"name": "cheeseburger"}]})
    CURRENT_REQUEST.set("yes, no tip")
    result = await tools.execute("doordash_order_place", {"cart_uuid": "c-mcd-garbled", "confirmed": True, "tip": "none"})
    assert result["ok"] and result["cart_source"] == "this chat's cart (the id passed was not it)"
    args = cli.argv("order", "submit")
    assert args[args.index("--cart-uuid") + 1] == "c-mcd" and args[args.index("--tip-cents") + 1] == "0"


async def test_placing_with_no_cart_in_the_chat_says_how_to_get_one(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.doordash._runner = FakeCLI(placing_responses())
    CURRENT_SPEAKER.set({"conversation_id": "fresh"})
    CURRENT_REQUEST.set("yes place it")
    result = await tools.execute("doordash_order_place", {"confirmed": True})
    assert result["ok"] is False and "Build one with doordash_order" in result["error"]
    result = await tools.execute("doordash_order_preview", {})
    assert result["ok"] is False and "No cart in this chat yet" in result["error"]


def test_tips_in_words() -> None:
    from house_agent.doordash import _resolve_tip
    assert _resolve_tip(None, 225) == (225, "suggested")
    assert _resolve_tip("suggested", 225) == (225, "suggested")
    assert _resolve_tip("none", 225) == (0, "none")
    assert _resolve_tip("$3", 225) == (300, "amount")
    assert _resolve_tip("2.50", 225) == (250, "amount")
    assert _resolve_tip("4 dollars", 225) == (400, "amount")
    assert _resolve_tip("5", 225) == (500, "amount")
    assert _resolve_tip(400, 225) == (400, "cents")
    assert _resolve_tip("400", 225) == (400, "cents")
    assert _resolve_tip("300 cents", 225) == (300, "cents")
    assert _resolve_tip(None, None) == (None, "missing")
