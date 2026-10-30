"""Settling someone's name ends the turn: no second answer to the old question."""

import json

import numpy as np

from house_agent.config import AppConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.tools import ToolRegistry
from house_agent.voice import VoiceIdentity


def agent_for(tmp_path, script):
    """An agent whose model replies from `script`, one entry per round."""
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "agent.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(think_complex=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    agent = LocalAgent(config.llm, database, tools)

    rounds = []

    async def fake_stream(client, messages, effort, allow_tools=True):
        rounds.append(allow_tools)
        yield "message", script[len(rounds) - 1]

    agent._stream_completion = fake_stream
    return agent, tools, rounds


def call(tool, **arguments):
    return {
        "tool_calls": [
            {"id": "1", "function": {"name": tool, "arguments": json.dumps(arguments)}}
        ]
    }


async def run(agent, message, conversation_id):
    async for event in agent.chat_stream(message, conversation_id):
        if event["type"] == "done":
            return event
    raise AssertionError("the turn never finished")


async def test_enrolling_ends_the_turn(tmp_path) -> None:
    script = [
        call("voice_enroll", name="Lisa"),
        {"content": "Good to meet you, Lisa — I'll know your voice now."},
        # A third round would be the bug: re-answering the earlier question.
        call("weather_forecast"),
    ]
    agent, tools, rounds = agent_for(tmp_path, script)
    tools.database.save_conversation_speaker(
        "c1", "unknown", None, 0.3, np.ones(8, dtype=np.float32).tobytes(), 3.0, asked=True
    )

    done = await run(agent, "My name is Lisa.", "c1")

    # Two rounds only, and the second was not allowed to call anything.
    assert rounds == [True, False]
    assert [record["tool"] for record in done["trace"]] == ["voice_enroll"]
    assert "Lisa" in done["message"]
    assert tools.voices.speaker("c1")["profile"] == "Lisa"


async def test_staying_anonymous_ends_the_turn(tmp_path) -> None:
    script = [
        call("voice_stay_anonymous"),
        {"content": "No problem — I won't ask again."},
    ]
    agent, tools, rounds = agent_for(tmp_path, script)
    tools.database.save_conversation_speaker("c1", "unknown", None, 0.3, None, 3.0, asked=True)

    done = await run(agent, "I'd rather not say.", "c1")
    assert rounds == [True, False]
    assert tools.voices.speaker("c1")["status"] == "anonymous"


async def test_ordinary_tools_do_not_end_the_turn(tmp_path) -> None:
    script = [call("weather_now"), {"content": "It's 59 degrees."}]
    agent, tools, rounds = agent_for(tmp_path, script)
    await run(agent, "What's the weather?", "c1")
    # Still free to keep working: only the name tools close a turn.
    assert rounds == [True, True]
