from __future__ import annotations

import re

import httpx

from .config import SpeechConfig


class SpeechError(RuntimeError):
    pass


# Whisper's own estimate that a segment is not speech at all. A thump, a
# cough, or a phone set on a table comes back around 0.8 with invented words.
NO_SPEECH_MAX = 0.6
# Filler Whisper invents for near-silent audio, never something a person
# says to a house assistant.
HALLUCINATIONS = re.compile(
    r"^(?:thanks?(?: you)? for watching|thank you(?: very much)?|subtitles? by|"
    r"please subscribe|bye|you|"
    r"gracias por ver(?: el v[íi]deo)?|subt[íi]tulos (?:por|realizados por).*|"
    r"suscr[íi]bete(?: al canal)?|"
    r"\.)[.! ]*$",
    re.IGNORECASE,
)


def keep_segment(segment: dict) -> bool:
    text = str(segment.get("text") or "").strip()
    if not text:
        return False
    if float(segment.get("no_speech_prob") or 0.0) > NO_SPEECH_MAX:
        return False
    if HALLUCINATIONS.match(text):
        return False
    return True


class SpeechToText:
    def __init__(self, config: SpeechConfig) -> None:
        self.config = config

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        """The words actually spoken; an empty string when the audio held
        none (noise, a bump), so callers can ignore it rather than act on
        Whisper's guess."""
        if not audio:
            raise ValueError("Audio is empty")
        if len(audio) > 25 * 1024 * 1024:
            raise ValueError("Audio exceeds the 25 MB limit")
        endpoint = f"{self.config.base_url.rstrip('/')}/audio/transcriptions"
        data = {
            "model": self.config.model,
            "response_format": "verbose_json",
        }
        # "auto" (or empty) lets Whisper detect the language itself, so
        # Spanish speech is transcribed as Spanish instead of forced into
        # English.
        if self.config.language and self.config.language != "auto":
            data["language"] = self.config.language
        files = {"file": (filename, audio, content_type)}
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(endpoint, data=data, files=files, headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SpeechError(f"Speech endpoint failed: {exc}") from exc
        segments = body.get("segments")
        if isinstance(segments, list) and segments:
            kept = [str(item["text"]).strip() for item in segments if keep_segment(item)]
            return " ".join(kept).strip()
        text = str(body.get("text") or "").strip()
        return "" if HALLUCINATIONS.match(text) else text
