from __future__ import annotations

import array
import math
import re
from typing import Any

import httpx

from .config import TextToSpeechConfig


CONTENT_TYPES = {
    "aac": "audio/aac",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "pcm": "audio/L16",
    "wav": "audio/wav",
}


_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BULLET = re.compile(r"^[ \t]*[-*+\u2022][ \t]+", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_FENCE = re.compile(r"^[ \t]*`{3,}.*$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def speakable(text: str) -> str:
    """Strip Markdown so the voice reads words, not "asterisk asterisk"."""
    text = text.replace("\u202f", " ").replace("\u00a0", " ").replace("\u2009", " ")
    text = _FENCE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    for _ in range(3):
        stripped = _EMPHASIS.sub(r"\2", text)
        if stripped == text:
            break
        text = stripped
    text = text.replace("`", "").replace("*", "")
    text = _BLANK_RUN.sub("\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


# Every sentence is synthesized as its own clip, and the engine's output
# level drifts between renders. Normalizing each PCM16 WAV to one target
# loudness keeps consecutive clips from audibly stepping up and down.
TARGET_RMS = 0.13
PEAK_LIMIT = 0.95


def normalize_wav(data: bytes) -> bytes:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    position = 12
    format_chunk = None
    data_position = None
    data_length = 0
    while position + 8 <= len(data):
        chunk_id = data[position : position + 4]
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        if chunk_id == b"fmt ":
            format_chunk = data[position + 8 : position + 8 + size]
        elif chunk_id == b"data":
            data_position = position + 8
            data_length = min(size, len(data) - data_position)
            break
        position += 8 + size + (size & 1)
    if format_chunk is None or data_position is None or len(format_chunk) < 16:
        return data
    audio_format = int.from_bytes(format_chunk[0:2], "little")
    bits_per_sample = int.from_bytes(format_chunk[14:16], "little")
    if audio_format != 1 or bits_per_sample != 16:
        return data
    samples = array.array("h")
    samples.frombytes(data[data_position : data_position + data_length - (data_length % 2)])
    if not samples:
        return data
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    if rms < 1e-4:
        return data
    gain = TARGET_RMS / rms
    peak = max(-min(samples), max(samples)) / 32768.0
    if peak * gain > PEAK_LIMIT:
        gain = PEAK_LIMIT / peak
    if abs(gain - 1.0) < 0.03:
        return data
    scaled = array.array(
        "h", (max(-32768, min(32767, int(s * gain))) for s in samples)
    )
    return (
        data[:data_position]
        + scaled.tobytes()
        + data[data_position + data_length :]
    )


# Friday speaks English and Spanish. Kokoro voices are single-language —
# a Spanish voice mangles English and vice versa — so every clip picks the
# voice matching its own text. Cheap detection is enough: Spanish-only
# letters, or a heavy share of unmistakably Spanish words.
_SPANISH_CHARS = set("¿¡áéíóúñÁÉÍÓÚÑ")
_SPANISH_WORDS = frozenset(
    """el los las una unas del que qué es está están estás soy eres son
    para por con sin como cómo más muy sí tu tus su sus te le lo mi mis al
    se ya hay este esta esto ese esa eso aquí ahora luego también dónde
    cuándo cuál quién hola gracias bueno buenas noches días tarde puedo
    puedes quiero quieres tengo tienes hacer ver poner pon apaga enciende
    luz luces casa película películas serie series algo nada todo bien
    cuarto dormitorio cocina sala baño y o pero cuando donde porque""".split()
)
_WORD = re.compile(r"[a-záéíóúñü]+")


def looks_spanish(text: str) -> bool:
    if any(ch in _SPANISH_CHARS for ch in text):
        return True
    words = _WORD.findall(text.casefold())
    if not words:
        return False
    hits = sum(1 for word in words if word in _SPANISH_WORDS)
    if len(words) <= 3:
        return hits == len(words)
    return hits >= 2 and hits / len(words) >= 0.3


def _described(item: dict[str, Any]) -> dict[str, str]:
    voice_id = str(item["id"])
    language, accent = VOICE_LANGUAGES.get(voice_id[:1], ("Other", ""))
    return {
        "id": voice_id,
        "language": language,
        "accent": accent,
        "gender": {"f": "female", "m": "male"}.get(voice_id[1:2], ""),
        # "American female — warm, natural" -> "warm, natural"
        "name": voice_id.split("_", 1)[-1].replace("-", " ").title(),
        "description": str(item.get("description") or "").partition("—")[2].strip(),
    }


class TextToSpeechError(RuntimeError):
    pass


# Kokoro names a voice by language and gender: af_heart is an American
# female, bm_george a British male, ef_dora a Spanish female. The prefix is
# all the grouping a picker needs.
VOICE_LANGUAGES = {
    "a": ("English", "American"),
    "b": ("English", "British"),
    "e": ("Spanish", "Spanish"),
    "f": ("French", "French"),
    "h": ("Hindi", "Hindi"),
    "i": ("Italian", "Italian"),
    "j": ("Japanese", "Japanese"),
    "p": ("Portuguese", "Brazilian"),
    "z": ("Chinese", "Mandarin"),
}
# What a voice reads out when someone auditions it. Short enough to
# synthesize in a moment, long enough to hear the character of the voice.
SAMPLE_LINE = "Hi, I'm Friday. The kitchen lights are on and you have one thing on today."
SPANISH_SAMPLE_LINE = "Hola, soy Friday. Las luces de la cocina están encendidas."


class TextToSpeech:
    def __init__(
        self,
        config: TextToSpeechConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        # The household can change these in Settings; config is only the
        # starting point, so they are not read off the frozen config again.
        self.voice = config.voice
        self.spanish_voice = config.spanish_voice

    def use_voice(self, voice: str | None = None, spanish_voice: str | None = None) -> None:
        if voice:
            self.voice = voice
        if spanish_voice:
            self.spanish_voice = spanish_voice

    async def voices(self) -> list[dict[str, str]]:
        """Every voice the server can speak in, grouped by language."""
        endpoint = f"{self.config.base_url.rstrip('/')}/voices"
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.get(
                endpoint, headers={"Authorization": f"Bearer {self.config.api_key}"}
            )
            response.raise_for_status()
            listed = response.json().get("voices") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise TextToSpeechError(f"Could not list voices: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        return [_described(item) for item in listed if isinstance(item, dict) and item.get("id")]

    async def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, str]:
        text = speakable(text)
        if not text:
            raise ValueError("Speech text cannot be empty")
        if len(text) > 4_096:
            raise ValueError("Speech text exceeds the 4,096 character limit")

        endpoint = f"{self.config.base_url.rstrip('/')}/audio/speech"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "model": self.config.model,
            "voice": voice or (self.spanish_voice if looks_spanish(text) else self.voice),
            "input": text,
            "response_format": self.config.response_format,
            "speed": self.config.speed,
        }
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            response = await client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TextToSpeechError(f"Text-to-speech endpoint failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if not response.content:
            raise TextToSpeechError("Text-to-speech endpoint returned no audio")
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not content_type.startswith("audio/"):
            content_type = CONTENT_TYPES.get(self.config.response_format, "audio/mpeg")
        content = response.content
        if content_type in {"audio/wav", "audio/x-wav"} or content[:4] == b"RIFF":
            content = normalize_wav(content)
        return content, content_type
