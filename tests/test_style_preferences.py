"""Standing instructions about how Friday writes, given out loud."""

import numpy as np

from house_agent.config import AppConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry
from house_agent.voice import VoiceIdentity


def parts(tmp_path):
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "style.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return LocalAgent(config.llm, database, tools), tools


async def test_a_preference_is_saved_against_the_person_speaking(tmp_path) -> None:
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})

    result = await tools.execute(
        "style_preference_add", {"preference": "just the titles, skip the years"}
    )
    assert result["ok"] is True and result["profile"] == "Alex"

    line = agent._style_line({"profile": "Alex"})
    assert "just the titles, skip the years" in line
    assert "(Alex)" in line
    # It has to outrank the built-in formatting or it changes nothing.
    assert "override" in line

    # Someone else is unaffected by it.
    assert agent._style_line({"profile": "Nikki"}) is None


async def test_a_household_preference_reaches_everyone(tmp_path) -> None:
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    await tools.execute(
        "style_preference_add",
        {"profile": "household", "preference": "be warmer and less clipped"},
    )
    for who in ({"profile": "Nikki"}, {"profile": None}, {}):
        line = agent._style_line(who)
        assert line is not None and "be warmer and less clipped" in line
        assert "(everyone)" in line


async def test_both_a_personal_and_a_household_preference_apply(tmp_path) -> None:
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    await tools.execute("style_preference_add", {"preference": "add the rating"})
    await tools.execute(
        "style_preference_add", {"profile": "household", "preference": "keep it short"}
    )
    line = agent._style_line({"profile": "Alex"})
    assert "add the rating" in line and "keep it short" in line


async def test_a_preference_can_be_taken_back(tmp_path) -> None:
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    saved = await tools.execute("style_preference_add", {"preference": "skip the years"})

    memory = await tools.execute("taste_memory_get", {})
    ids = [item["id"] for item in memory["style_preferences"]]
    assert saved["id"] in ids  # visible, so the model can find the one to drop

    assert (await tools.execute("style_preference_remove", {"id": saved["id"]}))["ok"] is True
    assert agent._style_line({"profile": "Alex"}) is None
    assert (await tools.execute("style_preference_remove", {"id": saved["id"]}))["ok"] is False


async def test_style_and_taste_stay_separate(tmp_path) -> None:
    """A preference about wording must not leak into what gets recommended."""
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    await tools.execute("style_preference_add", {"preference": "skip the years"})
    await tools.execute("profile_note_add", {"note": "loves slow-burn sci-fi"})

    memory = await tools.execute("taste_memory_get", {})
    notes = [item["note"] for item in memory["notes"]]
    styles = [item["note"] for item in memory["style_preferences"]]
    assert notes == ["loves slow-burn sci-fi"]
    assert styles == ["skip the years"]


async def test_an_anonymous_guest_sets_nothing(tmp_path) -> None:
    agent, tools = parts(tmp_path)
    CURRENT_SPEAKER.set({"status": "anonymous", "profile": None, "conversation_id": "c1"})
    result = await tools.execute("style_preference_add", {"preference": "be brief"})
    assert "anonymous" in result["skipped"]
    assert agent._style_line({}) is None
