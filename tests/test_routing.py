"""Each turn gets the tools and guidance it needs, and the whole prompt
fits the model's window.

Ollama's log for 4-14 September: 57 turns arrived over 16k tokens and were
cut to the last 8k — the system prompt and most tool definitions gone.
Those were the turns where Friday forgot who it was.
"""

import json

import numpy as np

from house_agent.config import AppConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.routing import (
    CORE_TOOLS, DOMAINS, Router, domain_of_tool, domains_mentioned, estimate_tokens, fit_history, paragraph_domains,
    wants_saving,
)
from house_agent.tools import ToolRegistry
from house_agent.voice import VoiceIdentity


def agent_for(tmp_path, script, **llm):
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "agent.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(think_complex=False, **llm),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    agent = LocalAgent(config.llm, database, tools)
    seen: list[tuple[list[dict], list[dict]]] = []

    async def fake_stream(client, messages, effort, allow_tools=True):
        seen.append((list(messages), list(agent._turn_schemas or [])))
        yield "message", script[len(seen) - 1]

    agent._stream_completion = fake_stream
    return agent, tools, seen


async def run(agent, message, conversation_id="c1"):
    return [e async for e in agent.chat_stream(message, conversation_id)]


def test_every_tool_is_core_or_owned_by_exactly_one_domain(tmp_path) -> None:
    config = AppConfig(storage=StorageConfig(database_path=tmp_path / "a.db"))
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    names = [s["function"]["name"] for s in tools.schemas]
    for name in names:
        owners = [d.name for d in DOMAINS if d.owns_tool(name)]
        core = any(name == t or (t.endswith("_") and name.startswith(t)) for t in CORE_TOOLS)
        assert core or len(owners) == 1, f"{name}: core={core} owners={owners}"
    assert domain_of_tool("doordash_order") == "doordash" and domain_of_tool("inbox_send") is None


def test_requests_point_at_their_domains() -> None:
    assert "doordash" in domains_mentioned("Now from McDonald's, order me a cheeseburger")
    assert {"devices", "tv"} <= domains_mentioned("turn off the bedroom lights")
    assert "content" in domains_mentioned("put on Frasier")
    assert "schedule" in domains_mentioned("remind me at 6pm to call mom")
    assert "house" in domains_mentioned("what is the ip address saved for the tv")
    assert "travel" in domains_mentioned("book me a flight to vegas")
    # A misspelt calendar used to route nowhere, leaving the turn with
    # inbox_send as its nearest tool: it wrote a note and called it a plan.
    assert "calendar" in domains_mentioned("add to my calander for next Monday")
    assert "calendar" in domains_mentioned("put it on my calender for friday")
    assert "calendar" in domains_mentioned("dentist on the 3rd")
    assert "travel" in domains_mentioned("buy a plane ticket to Mexico")
    assert "schedule" in domains_mentioned("don't let me forget the bins")
    # Mid-order, the restaurant's name has already been said and the words
    # left are about the food. These used to route nowhere, and the turn
    # answered "I can't access real-time menu information".
    assert "doordash" in domains_mentioned("what cheeseburger meals do they have?")
    assert "doordash" in domains_mentioned("what do they have")
    assert "doordash" in domains_mentioned("a small combo and a shake")
    assert domains_mentioned("hola") == set()
    assert "doordash" in domains_mentioned("tengo hambre, quiero pedir comida")


def test_a_request_to_save_something_always_reaches_a_tool_that_can(tmp_path) -> None:
    """Keywords will always miss some phrasing. When a request asks for
    something to be kept until a time, the two domains that can keep it ride
    along anyway, rather than the turn falling back to inbox_send."""
    assert wants_saving("add to my calander for next Monday")
    assert wants_saving("pencil something in for thursday")
    assert not wants_saving("add milk to the shopping list")
    assert not wants_saving("what is on tomorrow")

    agent, _, _ = agent_for(tmp_path, [])
    routed = agent.router.route("please jot the mexico thing down for the 14th", [])
    names = {s["function"]["name"] for s in routed.schemas}
    assert {"calendar", "schedule"} <= set(routed.domains)
    assert "calendar_add" in names and "reminder_add" in names
    # Not at the cost of what the message actually named.
    lights = agent.router.route("remind me to turn the porch light off tomorrow", [])
    assert {"devices", "schedule"} <= set(lights.domains)


def test_guidance_paragraphs_follow_their_topic() -> None:
    assert paragraph_domains("Weather comes from weather_now for current conditions.") == {"weather"}
    assert paragraph_domains("Smart lights are controlled with device_set; tv_press is not for lights.") == {"devices"}
    assert paragraph_domains("Be direct and friendly. Outside a title list, keep replies short.") == set()
    assert paragraph_domains("Use tools for device facts. Never claim an action happened.") == set()
    assert paragraph_domains("Food is ordered from DoorDash with doordash_order.") == {"doordash"}


def test_a_turn_carries_only_its_domains_within_budget(tmp_path) -> None:
    agent, tools, _ = agent_for(tmp_path, [])
    routed = agent.router.route("order me a cheeseburger from McDonald's", [])
    names = {s["function"]["name"] for s in routed.schemas}
    assert routed.domains == ["doordash"]
    assert "doordash_order" in names and "inbox_send" in names and "list_add" in names
    assert not any(n.startswith(("tv_", "device_", "content_", "flight_")) for n in names)
    assert "DoorDash" in routed.prompt and "Smart lights" not in routed.prompt
    assert "Be direct and friendly" in routed.prompt  # style guidance is always there
    assert routed.schema_tokens + routed.prompt_tokens < 8000

    quiet = agent.router.route("hola", [])
    assert quiet.domains == ["tv", "devices", "weather"]

    tight = Router(agent.system_prompt, tools.schemas, 2500)
    squeezed = tight.route("turn off the lights and put on Frasier and order pizza", [])
    assert squeezed.domains and squeezed.dropped, (squeezed.domains, squeezed.dropped)
    assert squeezed.schema_tokens <= 2500 + tight._cost[squeezed.domains[0]]


def test_history_and_progress_keep_a_domain_offered(tmp_path) -> None:
    agent, tools, _ = agent_for(tmp_path, [])
    history = [{"role": "user", "content": "any thai places?"}, {"role": "assistant", "content": "Wanida Thai is 0.1 miles away."}]
    routed = agent.router.route("yes the first one", history)
    assert "doordash" in routed.domains
    routed = agent.router.route("ok", [], active=["devices"], recent=["calendar"])
    assert routed.domains[:2] == ["devices", "calendar"]


async def test_the_model_receives_routed_tools_and_prompt(tmp_path) -> None:
    agent, tools, seen = agent_for(tmp_path, [{"content": "Sure."}, {"content": "Done."}])
    await run(agent, "remind me at 6pm to call mom")
    messages, schemas = seen[0]
    names = {s["function"]["name"] for s in schemas}
    assert "reminder_add" in names and "tv_press" not in names
    assert "Reminders and schedules" in messages[0]["content"] and "Smart lights" not in messages[0]["content"]
    # Off: everything, as before.
    agent2, _, seen2 = agent_for(tmp_path / "b", [{"content": "Sure."}], route_tools=False)
    await run(agent2, "remind me at 6pm to call mom")
    assert len(seen2[0][1]) == len(agent2.tools.schemas)


async def test_a_used_domain_stays_for_the_next_turns(tmp_path) -> None:
    call = {"tool_calls": [{"id": "1", "function": {"name": "list_show", "arguments": json.dumps({"name": "shopping"})}}]}
    agent, tools, seen = agent_for(tmp_path, [call, {"content": "Empty."}, {"content": "Okay."}])
    await run(agent, "what's on my shopping list")
    tools._domains_used["c1"] = {"recipes": tools._domains_used.get("c1", {}).get("recipes", 0) or 1e12}
    await run(agent, "and the second one?")
    assert "recipes" in agent.router.domains_for("and the second one?", [], recent=tools.recent_domains("c1"))


async def test_history_is_trimmed_to_the_window_never_the_prompt(tmp_path) -> None:
    agent, tools, seen = agent_for(tmp_path, [{"content": "Hi."}] * 3, context_tokens=6000)
    long = "x" * 4000  # ~1000 tokens each
    for i in range(6):
        tools.database.add_message("c1", "user", long)
        tools.database.add_message("c1", "assistant", "ok")
    await run(agent, "hello")
    messages, _ = seen[0]
    assert messages[0]["role"] == "system" and "You are Friday" in messages[0]["content"]
    total = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
    assert total < 6000 - 1200
    assert sum(1 for m in messages if m.get("content") == long) < 6


def test_fit_history_drops_oldest_first() -> None:
    history = [{"role": "user", "content": "a" * 400}, {"role": "assistant", "content": "b" * 400}, {"role": "user", "content": "c" * 40}]
    kept, dropped = fit_history(history, 120)  # 100 + 100 + 10 tokens
    assert dropped == 1 and [h["content"][0] for h in kept] == ["b", "c"]
    kept, dropped = fit_history(history, 10, keep_last=2)
    assert dropped == 1 and len(kept) == 2  # the last two stay even when over
    kept, dropped = fit_history(history, 10, keep_last=1)
    assert dropped == 2 and [h["content"][0] for h in kept] == ["c"]


async def test_the_context_probe_lowers_a_configured_window_it_cannot_have(tmp_path, monkeypatch) -> None:
    import httpx
    agent, _, _ = agent_for(tmp_path, [], context_tokens=32768)

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"parameters": "num_ctx 16384\ntemperature 0.6"}

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            assert url.endswith("/api/show") and json == {"name": agent.config.model}
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert await agent.probe_context() == 16384
    assert agent.context_tokens == 16384


def test_resume_phrasings_reach_the_tv_tools() -> None:
    from house_agent.routing import domains_mentioned

    for message in ("Can you put my show back on?", "resume please", "unpause it", "keep watching", "pick up where we left off"):
        assert "tv" in domains_mentioned(message), message
