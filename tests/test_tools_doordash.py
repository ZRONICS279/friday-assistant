"""DoorDash tools as the model sees them: advertised only when dd-cli is
there, guarded before money moves, and kept out of the database."""

import asyncio
import json
import stat

from house_agent.config import AppConfig, DoorDashConfig, LLMConfig, StorageConfig
from house_agent.db import Database
from house_agent.doordash import DoorDashError, DoorDashService
from house_agent.llm import LocalAgent, _compact_trace
from house_agent.tools import CURRENT_REQUEST, CURRENT_SPEAKER, ToolRegistry


def registry(tmp_path, installed: bool = True) -> ToolRegistry:
    binary = tmp_path / "dd-cli"
    if installed:
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        doordash=DoorDashConfig(binary=binary, timeout_seconds=5),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    return ToolRegistry(config, database)


def test_doordash_tools_appear_only_when_dd_cli_is_installed(tmp_path) -> None:
    with_cli = {s["function"]["name"] for s in registry(tmp_path).schemas}
    assert {
        "doordash_search", "doordash_menu", "doordash_cart_add", "doordash_order_history",
        "doordash_reorder", "doordash_order_preview", "doordash_order_place", "doordash_favorite",
        "doordash_grocery_list", "doordash_order_status",
    } <= with_cli
    without = {s["function"]["name"] for s in registry(tmp_path / "other", installed=False).schemas}
    assert not any(name.startswith("doordash_") for name in without)


def test_the_prompt_explains_ordering_or_its_absence(tmp_path) -> None:
    tools = registry(tmp_path)
    agent = LocalAgent(LLMConfig(), tools.database, tools)
    assert "doordash_order_place with confirmed true" in agent.system_prompt
    missing = registry(tmp_path / "other", installed=False)
    agent = LocalAgent(LLMConfig(), missing.database, missing)
    assert "Food delivery is not set up" in agent.system_prompt
    assert "doordash_order_place" not in agent.system_prompt


async def test_placing_without_a_yes_is_refused_before_dd_cli_is_even_run(tmp_path) -> None:
    tools = registry(tmp_path)
    calls = []

    async def runner(argv, env, timeout):
        calls.append(argv)
        return 0, b"{}", b""

    tools.doordash._runner = runner
    result = await tools.execute("doordash_order_place", {"cart_uuid": "c-1", "confirmed": False, "tip_cents": 0})
    assert result["ok"] is False and "say yes first" in result["error"]
    assert calls == []


async def test_not_signed_in_comes_back_as_needs_setup_with_the_link_sent(tmp_path) -> None:
    from test_inbox import FakeProcess, LOGIN_URL

    tools = registry(tmp_path)
    process = FakeProcess()
    process.say("Open in browser: " + LOGIN_URL)

    async def runner(argv, env, timeout):
        return 1, b"", b"Error: Failed to execute command due to missing credentials. Sign in with dd-cli login"

    async def spawn(*argv, **kwargs):
        return process

    tools.doordash._runner = runner
    tools.doordash._spawn = spawn
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    result = await tools.execute("doordash_order_history", {})
    assert result["ok"] is False and result["needs_setup"] is True
    assert result["sign_in_link_sent"] is True and "not signed in" in result["error"]
    assert tools.database.list_inbox("c1")[0]["url"] == LOGIN_URL
    await tools.close()


async def test_the_users_words_become_the_intent(tmp_path) -> None:
    tools = registry(tmp_path)
    seen = []

    async def runner(argv, env, timeout):
        seen.append(argv)
        return 0, json.dumps({"addresses": [], "stores": [], "carts": []}).encode(), b""

    tools.doordash._runner = runner
    CURRENT_REQUEST.set("find me some thai food")
    await tools.execute("doordash_search", {"query": "thai"})
    assert any('user prompt/purpose: "find me some thai food"' in argv[-1] for argv in seen)


async def test_favorites_are_the_households_words_only(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    added = await tools.execute("doordash_favorite", {"action": "add", "name": "Zeeks Pizza", "usual_order": "large pepperoni"})
    assert added["ok"] and added["favorite"]["added_by"] == "Alex"
    again = await tools.execute("doordash_favorite", {"action": "add", "name": "zeeks pizza", "note": "extra ranch"})
    assert again["favorite"]["updated"] is True and again["favorite"]["usual_order"] == "large pepperoni"
    listed = await tools.execute("doordash_favorite", {"action": "list"})
    assert [f["name"] for f in listed["favorites"]] == ["zeeks pizza"]
    agent = LocalAgent(LLMConfig(), tools.database, tools)
    assert "Favorite food places: zeeks pizza (usual: large pepperoni)" in agent._house_state()
    removed = await tools.execute("doordash_favorite", {"action": "remove", "name": "zeeks"})
    assert removed == {"ok": True, "removed": "zeeks"}
    assert tools.database.list_food_favorites() == []


def test_doordash_results_are_redacted_from_the_saved_trace() -> None:
    trace = [
        {"tool": "doordash_menu", "arguments": {"store_id": "928163", "query": "pepperoni", "limit": 5},
         "result": {"ok": True, "items": [{"name": "Large Pepperoni", "price": "$24.00"}]}},
        {"tool": "doordash_order_place", "arguments": {"cart_uuid": "c-1", "confirmed": True, "tip_cents": 360},
         "result": {"ok": True, "accepted": True, "order_uuid": "ord-1", "status": "pending", "total": "$31.20"}},
        {"tool": "weather_now", "arguments": {}, "result": {"ok": True, "temperature": 61}},
    ]
    compact = _compact_trace(trace)
    assert compact[0]["arguments"] == {"query": "pepperoni", "limit": 5}
    assert "Pepperoni" not in compact[0]["result"] and "not kept" in compact[0]["result"]
    assert compact[1]["arguments"] == {"confirmed": True, "tip_cents": 360}
    assert "ord-1" not in compact[1]["result"] and "$31.20" not in compact[1]["result"]
    assert json.loads(compact[1]["result"])["accepted"] is True
    assert json.loads(compact[2]["result"]) == {"ok": True, "temperature": 61}


class FakeAnnouncer:
    def __init__(self) -> None:
        self.events = []

    def publish(self, text, **extra):
        self.events.append((text, extra))
        return {"text": text, **extra}


async def test_a_placed_order_is_watched_and_announced_to_the_house(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.announcer = FakeAnnouncer()
    tools.order_watch_poll = 0.01
    tools.order_watch_slow = 0.01
    statuses = iter(["pending", "placed", "dasher_assigned", "dasher_nearby", "completed"])

    async def status(order_uuid, purpose=None):
        state = next(statuses)
        return {"order_uuid": order_uuid, "status": state, "eta": "6:45 PM" if state == "placed" else None,
                "terminal": state == "completed", "placed": state != "pending"}

    tools.doordash.order_status = status
    tools.doordash.last_order = {"order_uuid": "ord-1", "fulfillment": "delivery"}
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    assert tools._start_order_watch("ord-1", "Zeeks Pizza") is True
    await asyncio.wait_for(tools._order_watchers["ord-1"], 5)
    said = [text for text, _ in tools.announcer.events]
    assert said == [
        "Your Zeeks Pizza order is confirmed; it should arrive around 6:45 PM.",
        "Your Zeeks Pizza order is almost here — the Dasher is nearby.",
        "Your Zeeks Pizza order has been delivered.",
    ]
    # Food arriving is for the whole house, and the ordering conversation remembers it.
    assert all(extra["conversation_id"] is None and extra["source"] == "doordash" for _, extra in tools.announcer.events)
    recorded = [m["content"] for m in tools.database.get_messages("c1")]
    assert "Your Zeeks Pizza order has been delivered." in recorded
    assert "ord-1" not in tools._order_watchers


async def test_close_cancels_order_watchers(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.announcer = FakeAnnouncer()
    tools.order_watch_poll = 60

    async def never(order_uuid, purpose=None):
        raise DoorDashError("no")

    tools.doordash.order_status = never
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    tools._start_order_watch("ord-9", None)
    task = tools._order_watchers["ord-9"]
    await tools.close()
    assert task.cancelled() or task.done()
    assert tools._order_watchers == {}


async def test_a_store_id_the_model_invented_is_refused_before_dd_cli_runs(tmp_path) -> None:
    """The real failure (2026-09-25): asked what flavors a store had, the model
    passed store_id 7002352, an id nobody had given it. DoorDash answered "this
    store has no menu", and the user was told the menu could not be seen."""
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    ran: list[list[str]] = []

    async def runner(argv, env, timeout):
        ran.append(argv)
        raise AssertionError("dd-cli must not be run for an id from nowhere")

    tools.doordash._runner = runner
    result = await tools.execute("doordash_menu", {"store_id": "7002352", "query": "baja blast"})

    assert result["ok"] is False and not ran
    assert "has come up in this chat" in result["error"]
    assert "doordash_search" in result["next_step"]
    # An id that came from a search is fine.
    tools.doordash._remember_stores([{"store_id": "7002352", "name": "Taco Bell"}])
    assert tools._dd_known_store({"store_id": "7002352"}) is None


async def test_a_menu_without_the_item_points_at_the_stores_not_yet_opened(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"conversation_id": "c1"})
    session = tools._food_session()
    session.stores = [("1", "Taco Bell"), ("2", "Jalapeños"), ("3", "Taco Time NW")]
    tools.doordash._remember_stores([{"store_id": "1", "name": "Taco Bell"}])

    async def menu(*args, **kwargs):
        return {"store_id": "1", "items": [{"name": "Crunchwrap"}], "no_match": "baja blast"}

    tools.doordash.menu = menu
    result = await tools.execute("doordash_menu", {"store_id": "1", "query": "baja blast"})

    assert result["other_stores"] == ["Jalapeños", "Taco Time NW"]
    assert "have not been looked at" in result["next_step"]
