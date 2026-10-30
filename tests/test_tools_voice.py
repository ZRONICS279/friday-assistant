import numpy as np

from house_agent.config import AppConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry
from house_agent.voice import VoiceIdentity


def registry(tmp_path) -> ToolRegistry:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        voice=VoiceConfig(),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return tools


async def test_recognized_speaker_profile_fills_profile_tools(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    result = await tools.execute(
        "taste_feedback", {"title": "Monk", "status": "liked", "media_type": "series"}
    )
    assert result["ok"] is True
    assert result["preference"]["profile"] == "Alex"
    memory = await tools.execute("taste_memory_get", {})
    assert memory["profile"] == "Alex"
    # An explicit profile from the model still wins.
    memory = await tools.execute("taste_memory_get", {"profile": "default"})
    assert memory["profile"] == "default"


async def test_anonymous_guest_feedback_is_not_recorded(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "anonymous", "profile": None, "conversation_id": "c1"})
    result = await tools.execute(
        "taste_feedback", {"title": "Monk", "status": "liked", "media_type": "series"}
    )
    assert result["ok"] is True and "anonymous" in result["skipped"]
    assert tools.database.list_preferences("default") == []


async def test_voice_tools_enroll_and_decline_for_the_current_chat(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.database.save_conversation_speaker(
        "c1", "unknown", None, 0.3, np.ones(8, dtype=np.float32).tobytes(), 3.0
    )
    CURRENT_SPEAKER.set({"status": "unknown", "profile": None, "conversation_id": "c1"})
    result = await tools.execute("voice_enroll", {"name": "Sam"})
    assert result["ok"] is True and result["profile"] == "Sam" and result["samples"] == 1
    # The result has to tell the model what to say, or it falls back to
    # re-answering whatever came before and never acknowledges the name.
    assert "Sam" in result["note"] and "no further tools" in result["note"]
    assert CURRENT_SPEAKER.get()["profile"] == "Sam"
    assert tools.voices.speaker("c1")["status"] == "identified"

    tools.database.save_conversation_speaker("c2", "unknown", None, 0.2, None, 3.0)
    CURRENT_SPEAKER.set({"status": "unknown", "profile": None, "conversation_id": "c2"})
    # Declining is only possible once the question has actually been asked.
    result = await tools.execute("voice_stay_anonymous", {})
    assert result["ok"] is False and "not asked" in result["error"]
    CURRENT_SPEAKER.set(
        {"status": "unknown", "profile": None, "asked": True, "conversation_id": "c2"}
    )
    result = await tools.execute("voice_stay_anonymous", {})
    assert result["status"] == "anonymous"
    assert "no further tools" in result["note"].lower()
    assert tools.voices.speaker("c2")["asked"] is True

    CURRENT_SPEAKER.set(None)
    result = await tools.execute("voice_enroll", {"name": "Nobody"})
    assert result["ok"] is False


async def test_profile_notes_default_to_the_speaker_and_household_alias(tmp_path) -> None:
    tools = registry(tmp_path)
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": "c1"})
    saved = await tools.execute("profile_note_add", {"note": "loves slow-burn sci-fi"})
    assert saved["profile"] == "Alex"
    saved = await tools.execute(
        "profile_note_add", {"profile": "household", "note": "no horror, kids around"}
    )
    assert saved["profile"] == tools.config.household_profile
    saved = await tools.execute("profile_note_add", {"profile": "Sam", "note": "likes anime"})
    assert saved["profile"] == "Sam"
    memory = await tools.execute("taste_memory_get", {})
    assert [n["note"] for n in memory["notes"]] == ["loves slow-burn sci-fi"]
    assert [n["note"] for n in memory["household_notes"]] == ["no horror, kids around"]
    assert memory["household_members"] == ["Alex", "Sam"]
    removed = await tools.execute("profile_note_remove", {"note_id": memory["notes"][0]["id"]})
    assert removed == {"ok": True}
    assert (await tools.execute("taste_memory_get", {}))["notes"] == []
    assert (await tools.execute("profile_note_remove", {"note_id": 999}))["ok"] is False

    CURRENT_SPEAKER.set({"status": "anonymous", "profile": None, "conversation_id": "c2"})
    result = await tools.execute("profile_note_add", {"note": "likes westerns"})
    assert "anonymous" in result["skipped"]
    result = await tools.execute("profile_note_add", {"profile": "household", "note": "quiet after ten"})
    assert result["ok"] is True

    status = await tools.execute("household_setup", {"status": "done"})
    assert status == {"ok": True, "status": "done"}
    assert tools.database.get_setting("setup_status") == "done"


async def test_a_favourite_given_with_the_name_is_saved(tmp_path) -> None:
    """Enrolling ends the turn, so the favourite has to be caught here or not at all."""
    tools = registry(tmp_path)
    tools.database.save_conversation_speaker(
        "c1", "unknown", None, 0.3, np.ones(8, dtype=np.float32).tobytes(), 4.0
    )
    CURRENT_SPEAKER.set({"status": "unknown", "profile": None, "conversation_id": "c1"})

    result = await tools.execute(
        "voice_enroll", {"name": "Lisa", "favorite": "The Princess Bride"}
    )
    assert result["ok"] is True and result["favorite_saved"] == "The Princess Bride"
    notes = [item["note"] for item in tools.database.list_profile_notes("Lisa")]
    assert notes == ["likes The Princess Bride"]
    # The reply is told to say something about it rather than just confirm.
    assert "love" in result["note"]


async def test_enrolling_without_a_favourite_still_works(tmp_path) -> None:
    tools = registry(tmp_path)
    tools.database.save_conversation_speaker(
        "c1", "unknown", None, 0.3, np.ones(8, dtype=np.float32).tobytes(), 4.0
    )
    CURRENT_SPEAKER.set({"status": "unknown", "profile": None, "conversation_id": "c1"})
    result = await tools.execute("voice_enroll", {"name": "Lisa"})
    assert result["ok"] is True and "favorite_saved" not in result
    assert tools.database.list_profile_notes("Lisa") == []
