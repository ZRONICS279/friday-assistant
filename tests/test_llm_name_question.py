"""Friday asks an unrecognized voice for a name, whatever the model does."""

from types import SimpleNamespace

from house_agent.config import VoiceConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.voice import VoiceIdentity

from test_voice import spectral_embedder, tone


def agent_for(tmp_path):
    """Just enough agent to exercise the reply check."""
    database = Database(tmp_path / "voice.db")
    database.initialize()
    voices = VoiceIdentity(
        VoiceConfig(min_speech_seconds=2.0, max_wait_seconds=10.0),
        database,
        embedder=spectral_embedder,
    )
    return SimpleNamespace(tools=SimpleNamespace(voices=voices)), voices


async def unknown_speaker(voices, conversation_id="chat-1"):
    await voices.observe(conversation_id, tone(330, 3.0))
    assert voices.speaker(conversation_id)["status"] == "unknown"


async def test_the_question_is_added_when_the_model_ignores_it(tmp_path) -> None:
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    # Exactly what happened in the real conversation: the model was told to
    # ask, and replied with a bare greeting instead.
    added = LocalAgent._missing_name_question(agent, "chat-1", "Hi! How can I help you today?")
    assert "what's your name" in added.lower()
    assert "anonymous" in added.lower()
    # Asked once, and only once: the next reply gets nothing appended.
    assert voices.should_ask("chat-1") is False
    assert LocalAgent._missing_name_question(agent, "chat-1", "Sure thing.") == ""


async def test_nothing_is_added_when_the_model_asked_itself(tmp_path) -> None:
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    reply = "It's 59 degrees. I don't recognize your voice yet — what's your name?"
    assert LocalAgent._missing_name_question(agent, "chat-1", reply) == ""
    # Still recorded as asked, so it is not asked a second time.
    assert voices.should_ask("chat-1") is False


async def test_a_spanish_reply_is_asked_in_spanish(tmp_path) -> None:
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    added = LocalAgent._missing_name_question(agent, "chat-1", "Está despejado, 15 grados.")
    assert "Cómo te llamas" in added


async def test_a_known_voice_is_never_asked(tmp_path) -> None:
    agent, voices = agent_for(tmp_path)
    await voices.observe("chat-1", tone(220, 3.0))
    voices.enroll("chat-1", "Nikki")
    assert LocalAgent._missing_name_question(agent, "chat-1", "Hello there.") == ""


async def test_an_anonymous_guest_is_never_asked(tmp_path) -> None:
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    voices.stay_anonymous("chat-1")
    assert LocalAgent._missing_name_question(agent, "chat-1", "Hello there.") == ""


async def test_the_question_lands_after_the_answer(tmp_path) -> None:
    """It is appended, so it can only ever come once the answer is finished."""
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    reply = "About 1 hour 49 minutes."
    added = LocalAgent._missing_name_question(agent, "chat-1", reply)
    whole = reply + added
    assert whole.startswith("About 1 hour 49 minutes.")
    assert whole.index("your name") > whole.index("49 minutes")


async def test_a_model_that_raises_it_anyway_is_not_doubled(tmp_path) -> None:
    """The real double-ask: the model opened with it, in statement form."""
    agent, voices = agent_for(tmp_path)
    await unknown_speaker(voices)
    reply = (
        "I'm not sure who's speaking yet, so I'll need to ask for your name. "
        "You can also stay anonymous if you prefer.\n\n"
        "**The Marvelous Mrs. Maisel** (2017)\n**Girls** (2012)"
    )
    assert LocalAgent._missing_name_question(agent, "chat-1", reply) == ""
    assert voices.should_ask("chat-1") is False
