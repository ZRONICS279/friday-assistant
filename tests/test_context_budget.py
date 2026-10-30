"""The prompt must fit, however long the turn runs.

Ollama's answer to an over-long prompt is to keep the *last* half, which
throws away the system prompt and every tool schema — the failure the routing
module was written to prevent. History is fitted before the turn starts, but
the turn then appends a tool call and a result per round, up to ten of them at
2000 tokens each, and nothing was holding that against the context. A full
history plus ten big results overran a 32k context by more than it held.
"""

import json

import numpy as np

from house_agent.config import AppConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import (
    SHRUNK_RESULT,
    TOOL_WORK_RESERVE,
    LocalAgent,
    shrink_to_fit,
)
from house_agent.routing import estimate_tokens
from house_agent.tools import ToolRegistry
from house_agent.voice import VoiceIdentity


def test_the_oldest_results_are_emptied_until_the_prompt_fits() -> None:
    fat = json.dumps({"items": ["x" * 40 for _ in range(100)]})  # ~1100 tokens
    messages = [
        {"role": "system", "content": "the prompt that must never be dropped"},
        {"role": "user", "content": "what is on the menu"},
        *[
            item
            for index in range(6)
            for item in (
                {"role": "assistant", "tool_calls": [{"id": f"c{index}", "function": {"name": "t"}}]},
                {"role": "tool", "tool_call_id": f"c{index}", "content": fat},
            )
        ],
    ]
    before = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
    dropped, after = shrink_to_fit(messages, before // 2)

    assert dropped and after <= before // 2
    # The oldest go first, the newest survive, and every tool_call still has a
    # result beside it — the API rejects a call without one.
    contents = [m["content"] for m in messages if m["role"] == "tool"]
    assert contents[0] == SHRUNK_RESULT and contents[-1] == fat
    assert len([m for m in messages if m["role"] == "tool"]) == 6
    assert messages[0]["content"] == "the prompt that must never be dropped"
    # Already-emptied results are not counted again on a second pass.
    assert shrink_to_fit(messages, after)[0] == 0


def test_nothing_is_dropped_while_the_prompt_fits() -> None:
    messages = [{"role": "system", "content": "short"}, {"role": "tool", "content": "also short"}]
    assert shrink_to_fit(messages, 10_000) == (0, estimate_tokens("short") + estimate_tokens("also short"))
    assert messages[1]["content"] == "also short"


async def test_history_leaves_room_for_the_turns_own_tool_results(tmp_path) -> None:
    """A long history must not eat the space the results will need."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "ctx.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(context_tokens=32768, think_complex=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    agent = LocalAgent(config.llm, database, tools)
    for index in range(300):
        database.add_message("c1", "user", f"{index} " + "a long thing said out loud. " * 30)
        database.add_message("c1", "assistant", f"{index} " + "a long answer given back. " * 30)

    sent: list[list[dict]] = []

    async def capture(client, messages, effort, *args, **kwargs):
        sent.append(list(messages))
        yield "message", {"content": "Done."}

    agent._stream_completion = capture
    async for _ in agent.chat_stream("what's the weather", "c1"):
        pass

    prompt = sum(estimate_tokens(str(m.get("content") or "")) for m in sent[0])
    # History is long enough to have filled the context on its own; what is
    # left free is the room the turn's own tool results will need.
    free = config.llm.context_tokens - 1200 - 400 - prompt
    assert free >= TOOL_WORK_RESERVE, f"only {free} tokens left for tool results"
