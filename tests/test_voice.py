import io
import wave

import numpy as np

from house_agent.config import VoiceConfig
from house_agent.db import Database
from house_agent.voice import (
    EMBED_SCHEME,
    VoiceIdentity,
    decode_audio,
    speech_only,
    speech_seconds,
)

RATE = 16_000


def tone(frequency: float, seconds: float, amplitude: float = 0.3) -> bytes:
    """A WAV clip; each frequency stands in for a different voice."""
    t = np.arange(int(RATE * seconds)) / RATE
    samples = (np.sin(2 * np.pi * frequency * t) * amplitude * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def spectral_embedder(wav: np.ndarray) -> np.ndarray:
    """Stand-in for the speaker model: a coarse spectrum, so different tones
    map to different, and the same tone to identical, voiceprints."""
    spectrum = np.abs(np.fft.rfft(wav[: RATE * 2], n=RATE * 2))
    bins = spectrum[:4000].reshape(40, 100).sum(axis=1)
    return bins / (np.linalg.norm(bins) or 1.0)


def age(identity: VoiceIdentity, profile: str) -> None:
    """Rewrite a profile's recordings as if an older extractor made them."""
    with identity.database._connect() as connection:
        for table in ("voiceprints", "voiceprint_samples"):
            connection.execute(
                f"UPDATE {table} SET scheme = ? WHERE profile = ?",
                (EMBED_SCHEME - 1, profile),
            )
        connection.commit()


def make_identity(tmp_path, **overrides) -> VoiceIdentity:
    database = Database(tmp_path / "voice.db")
    database.initialize()
    config = VoiceConfig(min_speech_seconds=2.0, max_wait_seconds=10.0, **overrides)
    return VoiceIdentity(config, database, embedder=spectral_embedder)


def test_decode_and_speech_seconds_from_wav() -> None:
    wav = decode_audio(tone(220, 1.5))
    assert wav.dtype == np.float32
    assert abs(wav.size - RATE * 1.5) < RATE * 0.05
    assert 1.3 < speech_seconds(wav) <= 1.5
    assert speech_seconds(np.zeros(RATE, dtype=np.float32)) == 0.0


async def test_waits_for_enough_speech_then_flags_a_new_voice(tmp_path) -> None:
    identity = make_identity(tmp_path)
    first = await identity.observe("chat-1", tone(220, 1.0))
    assert first["status"] == "listening"
    second = await identity.observe("chat-1", tone(220, 1.5))
    assert second["status"] == "unknown"
    assert second["asked"] is False
    # The audio is kept while the chat is unknown so later utterances keep
    # sharpening the voiceprint (until the refinement cap).
    assert "chat-1" in identity._pending
    assert "not yet asked" in identity.context("chat-1")
    # Once enough total speech is in, refinement stops and the audio drops.
    third = await identity.observe("chat-1", tone(220, 8.0))
    assert third["status"] == "unknown"
    assert "chat-1" not in identity._pending


async def test_enrolled_voice_is_recognized_in_a_later_chat(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    result = identity.enroll("chat-1", "  Alex  ")
    assert result["ok"] is True and result["profile"] == "Alex" and result["samples"] == 1
    assert "Alex" in result["note"]
    assert identity.speaker("chat-1")["status"] == "identified"
    assert identity.context("chat-1").startswith("Speaker: Alex")

    later = await identity.observe("chat-2", tone(220, 3.0))
    assert later["status"] == "identified"
    assert later["profile"] == "Alex"
    assert later["confidence"] >= 0.99

    stranger = await identity.observe("chat-3", tone(660, 3.0))
    assert stranger["status"] == "unknown"
    assert identity.profiles()[0]["profile"] == "Alex"


async def test_the_lock_holds_against_a_glimpse_of_another_voice(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    # Too little speech to judge on: the lock is left alone rather than
    # flapping to someone else on a stray noise or a one-word reply.
    later = await identity.observe("chat-1", tone(660, 1.0))
    assert later["profile"] == "Alex"


async def test_someone_else_taking_over_the_chat_re_identifies(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    # A clearly different voice, with enough speech to be sure, re-opens the
    # chat instead of answering as Alex for the rest of the conversation.
    later = await identity.observe("chat-1", tone(660, 3.0))
    assert later["status"] == "unknown"
    assert later["profile"] is None


async def test_anonymous_guest_is_never_asked_again(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(330, 3.0))
    identity.mark_asked("chat-1")
    assert "do not ask again" in identity.context("chat-1")
    identity.stay_anonymous("chat-1")
    state = identity.speaker("chat-1")
    assert state["status"] == "anonymous" and state["asked"] is True
    assert "anonymous" in identity.context("chat-1")
    assert identity.profiles() == []


async def test_early_name_enrolls_once_enough_speech_arrives(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 0.5))
    result = identity.enroll("chat-1", "Alex")
    assert result["pending"] is True and result["profile"] == "Alex"
    assert identity.profiles() == []
    assert identity.speaker("chat-1")["name"] == "Alex"  # shown before the voice is saved
    state = await identity.observe("chat-1", tone(220, 3.0))
    assert state["status"] == "identified" and state["profile"] == "Alex"
    assert identity.profiles()[0]["profile"] == "Alex"
    assert identity.context("chat-1").startswith("Speaker: Alex")
    assert identity.forget("Alex") is True
    assert identity.forget("Alex") is False


async def test_anonymous_chat_can_still_enroll_later(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.stay_anonymous("chat-1")
    assert identity.speaker("chat-1")["status"] == "anonymous"
    result = identity.enroll("chat-1", "Alex")
    assert result["ok"] is True and result["profile"] == "Alex"
    assert identity.speaker("chat-1")["status"] == "identified"


def test_disabled_without_model(tmp_path) -> None:
    database = Database(tmp_path / "voice.db")
    database.initialize()
    identity = VoiceIdentity(
        VoiceConfig(model_path=tmp_path / "missing.onnx"), database
    )
    assert identity.available is False


async def test_first_run_greets_once_then_behaves_normally(tmp_path) -> None:
    identity = make_identity(tmp_path)
    assert identity.available and identity.first_run
    assert identity.context("chat-1", new_conversation=True) is None
    # The onboarding greeting is itself the name question: no second ask.
    assert identity.speaker("chat-1")["asked"] is True
    assert identity.context("chat-1", new_conversation=False) is None
    await identity.observe("chat-1", tone(220, 3.0))
    assert "do not ask again" in identity.context("chat-1")
    identity.enroll("chat-1", "Alex")
    assert identity.first_run is False


async def test_clear_stranger_is_flagged_from_the_first_sentence(tmp_path) -> None:
    identity = make_identity(tmp_path)
    # Enroll one known voice so matching has something to compare against.
    await identity.observe("known", tone(220, 3.0))
    identity.enroll("known", "Alex")
    # A very different voice: one short sentence (1.5s) is decided
    # immediately as unknown, without waiting for min_speech_seconds (2.0).
    result = await identity.observe("guest", tone(700, 1.5))
    assert result["status"] == "unknown"


async def test_known_voice_is_recognized_from_the_first_sentence(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("known", tone(300, 3.0))
    identity.enroll("known", "Alex")
    await identity.observe("other", tone(700, 3.0))
    identity.enroll("other", "Lisa")
    result = await identity.observe("later", tone(300, 1.5))
    assert result["status"] == "identified" and result["profile"] == "Alex"


def test_speech_only_drops_the_quiet_room_between_words() -> None:
    speech = decode_audio(tone(220, 1.0))
    quiet = np.zeros(RATE * 3, dtype=np.float32)
    clip = np.concatenate([quiet, speech, quiet])
    kept = speech_only(clip)
    # Only the second of speech survives, so the voiceprint is built from the
    # voice rather than three quarters room tone.
    assert 0.9 * RATE < kept.size < 1.3 * RATE
    assert speech_seconds(kept) > 0.9
    # Nothing to gate on: the clip is passed through rather than emptied.
    assert speech_only(quiet).size == quiet.size


def test_a_lone_profile_must_clear_the_margin_too(tmp_path) -> None:
    identity = make_identity(tmp_path, threshold=0.6, margin=0.1)
    embedding = np.zeros(40, dtype=np.float32)
    embedding[0] = 1.0
    identity.database.bank_voiceprint(
        "Alex", embedding, seconds=3.0, scheme=EMBED_SCHEME, cap_seconds=30.0
    )

    # A score between the threshold and threshold + margin. With nobody to
    # compare against there is no relative evidence, so it is not a match.
    probe = np.zeros(40, dtype=np.float32)
    probe[0], probe[1] = 0.65, np.sqrt(1 - 0.65**2)
    name, score = identity._match(probe)
    assert 0.6 <= score < 0.7 and name is None

    # Comfortably clear of the bar: still recognized.
    assert identity._match(embedding)[0] == "Alex"


async def test_answering_the_name_stops_the_question_immediately(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(330, 3.0))
    identity.mark_asked("chat-1")
    identity.enroll("chat-1", "Mom")
    assert identity.should_ask("chat-1") is False
    assert identity.context("chat-1").startswith("Speaker: Mom")


def test_recognizing_a_reply_that_already_asked() -> None:
    asked = VoiceIdentity.sounds_like_a_name_question
    # The wording Friday actually used when it did ask, unprompted.
    assert asked("I don't recognize your voice yet—what's your name? "
                 "You can stay anonymous if you prefer.")
    assert asked("¿Cómo te llamas?")
    assert asked("Sorry, who am I speaking to — what should I call you?")
    # A statement can raise it just as well as a question — missing that is
    # what once put the question in a reply twice, top and bottom.
    assert asked("I'm not sure who's speaking yet, so I'll need to ask for your name.")
    assert asked("I don't recognize your voice yet.")
    assert asked("Todavía no te reconozco.")
    # Ordinary replies, including ones that end in a question, have not asked.
    assert not asked("Hi! How can I help you today?")
    assert not asked("It's clear right now, about 59 degrees.")
    assert not asked("Want details on any, or should I put one on?")
    # A statement about names is not a question.
    assert not asked("Your name is saved.")


async def test_a_voiceprint_from_an_older_extractor_is_not_matched(tmp_path) -> None:
    """A print built the old way scores like a stranger, so it must not be used.

    Silently comparing across a change in how prints are built is what turned a
    recognized voice into an unrecognized one with no explanation.
    """
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    assert identity.speaker("chat-1")["status"] == "identified"

    # Age the recordings: they now predate the current extractor.
    age(identity, "Alex")

    assert identity.stale_profiles() == ["Alex"]
    assert identity.usable_profiles() == []
    assert identity.profiles()[0]["needs_reenrollment"] is True
    # The same voice is now treated as new, so the person is asked and
    # re-enrolled, rather than scored against an incomparable print.
    assert (await identity.observe("chat-2", tone(220, 3.0)))["status"] == "unknown"


async def test_re_enrolling_replaces_a_stale_print_instead_of_blending(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    age(identity, "Alex")

    await identity.observe("chat-2", tone(220, 3.0))
    record = identity.enroll("chat-2", "Alex")
    # Replaced, not averaged with the old-scheme print: samples restarts at 1.
    assert record["samples"] == 1
    assert identity.stale_profiles() == []
    assert (await identity.observe("chat-3", tone(220, 3.0)))["profile"] == "Alex"


async def test_a_voice_is_learned_from_every_confident_match(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    first = identity.profiles()[0]
    assert first["samples"] == 1

    # Each later conversation he is recognized in adds to the bank, so the
    # profile keeps improving without anyone being asked to record anything.
    for index in range(4):
        await identity.observe(f"later-{index}", tone(220, 3.0))
    grown = identity.profiles()[0]
    assert grown["samples"] == 5
    assert grown["seconds"] > first["seconds"]


async def test_the_bank_stops_at_the_configured_cap(tmp_path) -> None:
    identity = make_identity(tmp_path, max_banked_seconds=8.0)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    for index in range(10):
        await identity.observe(f"later-{index}", tone(220, 3.0))
    banked = identity.profiles()[0]
    assert banked["seconds"] <= 8.0
    # It keeps several recordings rather than collapsing back to one.
    assert 1 < banked["samples"] <= 4


async def test_a_doubtful_match_is_never_banked(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    before = identity.profiles()[0]["samples"]
    # A different voice is not recognized, so nothing about it is learned.
    assert (await identity.observe("stranger", tone(700, 3.0)))["status"] == "unknown"
    assert identity.profiles()[0]["samples"] == before


async def test_a_voice_matches_its_closest_recording(tmp_path) -> None:
    """The bank exists so an unusual utterance still matches the right person."""
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    await identity.observe("chat-2", tone(700, 3.0))
    identity.enroll("chat-2", "Nikki")

    # Bank a second, quite different recording for Alex directly, as a run of
    # confident matches would over time.
    identity._bank("Alex", spectral_embedder(decode_audio(tone(300, 3.0))), 3.0)
    banked = {item["profile"]: item for item in identity.profiles()}
    assert banked["Alex"]["samples"] == 2 and banked["Nikki"]["samples"] == 1

    # That utterance now lands on Alex, where a single averaged print would
    # have sat between his two recordings and matched neither well.
    assert (await identity.observe("chat-3", tone(300, 3.0)))["profile"] == "Alex"


async def test_forgetting_a_profile_clears_its_recordings(tmp_path) -> None:
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 3.0))
    identity.enroll("chat-1", "Alex")
    assert identity.forget("Alex") is True
    assert identity.usable_profiles() == [] and identity.profiles() == []
    assert (await identity.observe("chat-2", tone(220, 3.0)))["status"] == "unknown"


async def test_a_clip_too_short_to_trust_is_not_banked(tmp_path) -> None:
    """Recognizing someone on a short sentence is fine; filing it is not.

    Below about three seconds a voiceprint is mostly noise, and the bank is
    matched on its closest entry -- so one noisy entry is a permanent extra
    chance for a stranger to score well.
    """
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 4.0))
    identity.enroll("chat-1", "Alex")
    before = identity.profiles()[0]["samples"]

    # A short sentence still identifies him...
    quick = await identity.observe("chat-2", tone(220, 1.5))
    assert quick["profile"] == "Alex"
    # ...but adds nothing to the bank.
    assert identity.profiles()[0]["samples"] == before

    # A full-length one does.
    assert (await identity.observe("chat-3", tone(220, 4.0)))["profile"] == "Alex"
    assert identity.profiles()[0]["samples"] == before + 1


async def test_a_long_utterance_later_in_a_chat_is_banked(tmp_path) -> None:
    """The relaxed sentences come after the greeting, not in it."""
    identity = make_identity(tmp_path)
    await identity.observe("chat-1", tone(220, 4.0))
    identity.enroll("chat-1", "Alex")
    await identity.observe("chat-2", tone(220, 4.0))
    before = identity.profiles()[0]["samples"]

    # Still the same locked speaker, talking at length: worth keeping.
    later = await identity.observe("chat-2", tone(220, 5.0))
    assert later["profile"] == "Alex"
    assert identity.profiles()[0]["samples"] == before + 1


def test_a_name_typed_in_a_text_chat_is_shown(tmp_path) -> None:
    identity = make_identity(tmp_path)
    assert identity.enroll("text-chat", "Alex")["pending"] is True
    speaker = identity.speaker("text-chat")
    assert speaker["name"] == "Alex" and speaker["profile"] is None
