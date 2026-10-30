import json

import httpx
import pytest

from house_agent.config import TextToSpeechConfig
from house_agent.tts import TextToSpeech, TextToSpeechError


@pytest.mark.asyncio
async def test_synthesizes_with_configured_voice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["Authorization"] == "Bearer secret"
        assert json.loads(request.content) == {
            "model": "tts-1",
            "voice": "bf_isabella",
            "input": "Good evening.",
            "response_format": "mp3",
            "speed": 1.25,
        }
        return httpx.Response(
            200,
            content=b"synthetic audio",
            headers={"content-type": "audio/mpeg"},
        )

    config = TextToSpeechConfig(api_key="secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        audio, content_type = await TextToSpeech(config, client=client).synthesize(
            " Good evening. "
        )

    assert audio == b"synthetic audio"
    assert content_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_rejects_empty_speech() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        await TextToSpeech(TextToSpeechConfig()).synthesize("   ")


@pytest.mark.asyncio
async def test_reports_empty_audio_response() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b""))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TextToSpeechError, match="returned no audio"):
            await TextToSpeech(TextToSpeechConfig(), client=client).synthesize("Hello")


def test_speakable_strips_markdown_the_voice_would_read_aloud() -> None:
    from house_agent.tts import speakable

    spoken = speakable(
        "Here are some comedies:\n\n"
        "1. **Ted Lasso** (2020) — *Prime Video*\n"
        "- **The Simpsons** (1989) — *Hulu*\n"
        "### Notes\n"
        "See [the list](http://example.com) or `code`.\n"
    )

    assert "*" not in spoken
    assert "#" not in spoken
    assert "`" not in spoken
    assert "http" not in spoken
    assert "Ted Lasso (2020) — Prime Video" in spoken
    assert "The Simpsons (1989) — Hulu" in spoken
    assert "See the list or code." in spoken


def test_speakable_rejects_text_that_is_only_markup() -> None:
    from house_agent.tts import speakable

    assert speakable("**  **") == ""


def test_normalize_wav_levels_quiet_clips_to_target() -> None:
    import array as array_module
    import math
    import struct

    from house_agent.tts import TARGET_RMS, normalize_wav

    quiet = array_module.array(
        "h", (int(3000 * math.sin(i / 8)) for i in range(24_000))
    )
    body = quiet.tobytes()
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
        + b"data" + struct.pack("<I", len(body))
    )
    normalized = normalize_wav(header + body)

    samples = array_module.array("h")
    samples.frombytes(normalized[44:])
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    assert abs(rms - TARGET_RMS) < 0.01
    assert normalized[:44] == header


def test_leaked_planner_fragment_is_stripped_from_replies() -> None:
    from house_agent.llm import strip_planner_fragment

    assert strip_planner_fragment("Need list.A few comedies:\n**Frasier** (1993)") == (
        "A few comedies:\n**Frasier** (1993)"
    )
    assert strip_planner_fragment("Provide answer. Done.") == "Done."
    # Real replies that happen to start with these words survive.
    assert strip_planner_fragment("Now playing Frasier on the living room TV.") == (
        "Now playing Frasier on the living room TV."
    )
    assert strip_planner_fragment("Done.") == "Done."


def test_looks_spanish_detects_language() -> None:
    from house_agent.tts import looks_spanish

    assert looks_spanish("Enciende la luz del dormitorio")
    assert looks_spanish("¿Qué películas hay esta noche?")  # Spanish punctuation
    assert looks_spanish("Hola")
    assert not looks_spanish("Turn on the bedroom light")
    assert not looks_spanish("Here are a few easy comedies for tonight")
    assert not looks_spanish("Playing La La Land on Netflix now")


def _spoke(request: httpx.Request) -> str:
    return json.loads(request.content)["voice"]


@pytest.mark.asyncio
async def test_a_chosen_voice_outlives_the_frozen_config() -> None:
    """Settings changes the voice at runtime; config is only the default it
    started from, and the dataclass is frozen."""
    heard: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        heard.append(_spoke(request))
        return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})

    config = TextToSpeechConfig(api_key="secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tts = TextToSpeech(config, client=client)
        await tts.synthesize("Good evening.")
        tts.use_voice("am_onyx")
        await tts.synthesize("Good evening.")
        # Spanish still picks the Spanish voice, and has its own setting.
        await tts.synthesize("Hola, ¿qué tal?")
        tts.use_voice(spanish_voice="em_alex")
        await tts.synthesize("Hola, ¿qué tal?")
        # Auditioning one in Settings changes nothing.
        await tts.synthesize("Good evening.", "bf_lily")
        await tts.synthesize("Good evening.")

    assert heard == ["bf_isabella", "am_onyx", "ef_dora", "em_alex", "bf_lily", "am_onyx"]
    assert config.voice == "bf_isabella"  # the frozen default is untouched


@pytest.mark.asyncio
async def test_voices_are_grouped_by_the_language_their_name_encodes() -> None:
    listing = {
        "voices": [
            {"id": "af_heart", "description": "American female — warm, natural"},
            {"id": "bm_george", "description": "British male — authoritative"},
            {"id": "ef_dora", "description": "Spanish female"},
            {"id": "", "description": "ignored"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voices"
        return httpx.Response(200, json=listing)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        voices = await TextToSpeech(TextToSpeechConfig(), client=client).voices()

    assert [voice["id"] for voice in voices] == ["af_heart", "bm_george", "ef_dora"]
    assert voices[0] == {
        "id": "af_heart",
        "language": "English",
        "accent": "American",
        "gender": "female",
        "name": "Heart",
        "description": "warm, natural",
    }
    assert voices[1]["accent"] == "British" and voices[1]["gender"] == "male"
    # Nothing after the dash: the description would only repeat the language.
    assert voices[2]["language"] == "Spanish" and voices[2]["description"] == ""


@pytest.mark.asyncio
async def test_a_speech_server_that_cannot_list_voices_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TextToSpeechError, match="Could not list voices"):
            await TextToSpeech(TextToSpeechConfig(), client=client).voices()
