import httpx

from house_agent.config import SpeechConfig
from house_agent.speech import SpeechToText, keep_segment


def test_keep_segment_drops_whispers_own_non_speech_and_filler() -> None:
    assert keep_segment({"text": " Find me a comedy", "no_speech_prob": 0.05})
    assert not keep_segment({"text": " Thanks for watching!", "no_speech_prob": 0.8})
    assert not keep_segment({"text": " Thank you.", "no_speech_prob": 0.1})
    assert not keep_segment({"text": "  ", "no_speech_prob": 0.0})


async def test_transcribe_returns_only_real_speech(monkeypatch) -> None:
    payloads = [
        {
            "text": "Thanks for watching!",
            "segments": [{"text": " Thanks for watching!", "no_speech_prob": 0.79}],
        },
        {
            "text": "Play Frasier. you",
            "segments": [
                {"text": " Play Frasier.", "no_speech_prob": 0.02},
                {"text": " you", "no_speech_prob": 0.7},
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"verbose_json" in request.content
        return httpx.Response(200, json=payloads.pop(0))

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class PatchedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedClient)
    stt = SpeechToText(SpeechConfig(base_url="http://stt/v1", api_key="k"))
    assert await stt.transcribe(b"thump", "a.webm", "audio/webm") == ""
    assert await stt.transcribe(b"speech", "a.webm", "audio/webm") == "Play Frasier."


def test_spanish_whisper_hallucinations_are_dropped() -> None:
    from house_agent.speech import keep_segment

    for text in [
        "Subtítulos por la comunidad de Amara.org",
        "Gracias por ver el vídeo.",
        "Suscríbete al canal",
    ]:
        assert not keep_segment({"text": text, "no_speech_prob": 0.1})
    assert keep_segment({"text": "Enciende la luz", "no_speech_prob": 0.1})
