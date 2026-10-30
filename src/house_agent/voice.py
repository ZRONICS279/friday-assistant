"""Who is talking: voiceprints and per-conversation speaker identity.

Each conversation locks onto one speaker from its first couple of sentences.
Audio is only held in memory until enough speech has accumulated to compute
one voiceprint; after that the audio is dropped and only the 256-number
embedding remains, in memory for the chat and in the database once enrolled.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import VoiceConfig
from .db import Database

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# How voiceprints are built. Bump this whenever the audio reaching the model
# changes shape, because a stored print is only comparable with prints made
# the same way: scoring across a change looks exactly like a stranger's voice.
#   1  the whole clip, silence and room tone included
#   2  speech frames only
EMBED_SCHEME = 2
FRAME = 400  # 25 ms energy frames for the speech-seconds estimate
SPEECH_RMS = 0.012

Embedder = Callable[[np.ndarray], np.ndarray]

STATUS_LISTENING = "listening"
STATUS_IDENTIFIED = "identified"
STATUS_UNKNOWN = "unknown"
STATUS_ANONYMOUS = "anonymous"


class VoiceError(RuntimeError):
    pass


def decode_audio(data: bytes) -> np.ndarray:
    """Any browser recording (webm/opus, ogg, wav, mp4) -> 16 kHz mono float32."""
    import av  # bundles its own ffmpeg; no system install needed

    chunks: list[np.ndarray] = []
    try:
        with av.open(io.BytesIO(data)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise VoiceError("Recording has no audio stream")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    except (av.FFmpegError, ValueError, OSError) as exc:
        raise VoiceError(f"Could not decode recording: {exc}") from exc
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32)


def speech_seconds(wav: np.ndarray) -> float:
    if wav.size < FRAME:
        return 0.0
    frames = wav[: wav.size - wav.size % FRAME].reshape(-1, FRAME)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return float(np.count_nonzero(rms > SPEECH_RMS) * FRAME / SAMPLE_RATE)


def speech_only(wav: np.ndarray, *, pad: int = 2) -> np.ndarray:
    """Drop silence and room tone, keeping only the frames that carry voice.

    The model embeds whatever is in the clip, so half a recording of a quiet
    room is half a voiceprint of that room. Everyone here records in the same
    rooms through the same microphones, which makes that shared part the very
    thing that pulls two different people together. Keep the loud frames plus
    a couple either side, so word onsets and tails survive the cut.
    """
    if wav.size < FRAME:
        return wav
    frames = wav[: wav.size - wav.size % FRAME].reshape(-1, FRAME)
    loud = np.sqrt(np.mean(frames * frames, axis=1)) > SPEECH_RMS
    if not loud.any():
        return wav
    keep = loud.copy()
    for shift in range(1, pad + 1):
        keep[shift:] |= loud[:-shift]
        keep[:-shift] |= loud[shift:]
    return frames[keep].reshape(-1)


# The average voiceprint of many different speakers (28 English voices,
# same model, same opus path). Subtracting it before comparing removes what
# every voice shares and leaves what distinguishes them: on that set,
# different voices fall from up to 0.80 similarity to at most 0.58 while the
# same voice stays above 0.66.
_GENERIC_CENTRE_PATH = Path(__file__).with_name("assets") / "voice_centre.npy"


def generic_centre(dim: int) -> np.ndarray:
    try:
        centre = np.load(_GENERIC_CENTRE_PATH).astype(np.float32).reshape(-1)
        if centre.size == dim:
            return centre
    except (OSError, ValueError):
        pass
    return np.zeros(dim, dtype=np.float32)


def _unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def _sherpa_embedder(model_path: Path, threads: int) -> Embedder:
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(model_path), num_threads=threads, provider="cpu"
    )
    if not config.validate():
        raise VoiceError(f"Speaker model is not usable: {model_path}")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    lock = threading.Lock()

    def embed(wav: np.ndarray) -> np.ndarray:
        with lock:
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, wav)
            stream.input_finished()
            return np.array(extractor.compute(stream), dtype=np.float32)

    return embed


class VoiceIdentity:
    def __init__(
        self,
        config: VoiceConfig,
        database: Database,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self._embedder = embedder
        self._pending: dict[str, list[np.ndarray]] = {}
        self._lock = threading.Lock()
        self._load_failed = False

    @property
    def first_run(self) -> bool:
        """Brand new: voice recognition is on but nobody is usably enrolled."""
        return self.available and not self.usable_profiles()

    def usable_profiles(self) -> list[str]:
        """Enrolled voices that can still be matched against today's audio."""
        return sorted(self.database.list_voiceprint_samples(EMBED_SCHEME))

    def stale_profiles(self) -> list[str]:
        """Names whose recordings predate a change in how voiceprints are built.

        They are not deleted -- the name and its notes are still theirs -- but
        nothing in their bank can be compared with today's audio, so they are
        reported rather than quietly scored against the wrong scale.
        """
        usable = set(self.usable_profiles())
        return sorted(
            str(item["profile"])
            for item in self.database.list_voiceprints()
            if str(item["profile"]) not in usable
        )

    @property
    def available(self) -> bool:
        if not self.config.enabled or self._load_failed:
            return False
        return self._embedder is not None or Path(self.config.model_path).exists()

    def _embed(self, wav: np.ndarray) -> np.ndarray:
        if self._embedder is None:
            try:
                self._embedder = _sherpa_embedder(
                    Path(self.config.model_path), self.config.threads
                )
            except Exception as exc:  # noqa: BLE001 - surface once, then disable
                self._load_failed = True
                raise VoiceError(f"Voice recognition unavailable: {exc}") from exc
        return _unit(self._embedder(wav))

    # -- per-utterance observation -------------------------------------------------

    async def observe(self, conversation_id: str, audio: bytes) -> dict[str, Any]:
        """Feed one utterance; identification happens once enough speech is in."""
        if not self.available:
            return {"status": "disabled"}
        return await asyncio.to_thread(self._observe_sync, conversation_id, audio)

    # Decision pacing. A clear verdict should land on the very first
    # sentence: from QUICK_SECONDS of speech, an obvious match (or an
    # obvious stranger) is decided immediately; only borderline scores wait
    # for the full min_speech_seconds. An unknown chat keeps absorbing
    # speech up to REFINE_SPEECH_CAP so its voiceprint sharpens — a
    # borderline known voice can still resolve, and an enrollment that
    # follows is built on far more audio than the first utterance.
    QUICK_SECONDS = 1.2
    QUICK_CONFIDENT = 0.12  # above threshold by this much = identify early
    QUICK_REJECT = 0.10  # below threshold by this much = clearly a new voice
    REFINE_SPEECH_CAP = 8.0

    # Breaking a lock. A settled conversation is never re-opened on a
    # borderline score -- that would flap between people on noise alone. It
    # re-opens only when a later utterance carries enough speech to judge and
    # is clearly not the locked speaker, which is what happens when somebody
    # else picks up the microphone part-way through a conversation.
    RECHECK_SECONDS = 2.0

    # The shortest recording worth keeping. Below about three seconds a
    # voiceprint is still mostly noise -- measured on this model, one- and
    # two-second clips separate two speakers roughly half as well as a
    # five-second one. Identification can still happen on a short sentence;
    # what it must not do is file that sentence away as evidence, because a
    # bank is matched on its closest entry and a noisy entry is what a
    # stranger gets lucky against.
    MIN_BANK_SECONDS = 3.0

    def _observe_sync(self, conversation_id: str, audio: bytes) -> dict[str, Any]:
        state = self.database.get_conversation_speaker(conversation_id)
        status_before = str(state["status"]) if state else STATUS_LISTENING
        if status_before == STATUS_IDENTIFIED and not self._still_them(state, audio):
            self.database.save_conversation_speaker(
                conversation_id, STATUS_LISTENING, None, None, None, 0.0
            )
            with self._lock:
                self._pending.pop(conversation_id, None)
            state, status_before = None, STATUS_LISTENING
        elif status_before not in (STATUS_LISTENING, STATUS_UNKNOWN):
            return self.speaker(conversation_id) or {"status": STATUS_LISTENING}
        previous_spoken = float(state["speech_seconds"] or 0.0) if state else 0.0
        if status_before == STATUS_UNKNOWN and previous_spoken >= self.REFINE_SPEECH_CAP:
            return self.speaker(conversation_id) or {"status": STATUS_UNKNOWN}
        wav = decode_audio(audio)
        with self._lock:
            buffer = self._pending.setdefault(conversation_id, [])
            buffer.append(wav)
            total = np.concatenate(buffer) if len(buffer) > 1 else wav
            spoken = speech_seconds(total)
            waited = total.size / SAMPLE_RATE
            full = (
                spoken >= self.config.min_speech_seconds
                or waited >= self.config.max_wait_seconds
            )
            if spoken < self.QUICK_SECONDS and not full:
                if status_before == STATUS_LISTENING:
                    self.database.save_conversation_speaker(
                        conversation_id, STATUS_LISTENING, None, None, None, spoken
                    )
                return self.speaker(conversation_id) or {
                    "status": STATUS_LISTENING,
                    "speech_seconds": round(spoken, 1),
                }
        window = speech_only(total)[-SAMPLE_RATE * 20 :]
        embedding = self._embed(window)
        heard = window.size / SAMPLE_RATE  # pure speech, silence already cut
        pending_name = str(state["pending_name"]) if state and state.get("pending_name") else None
        if pending_name:
            # They gave their name before enough speech was in; finish that now.
            self._bank(pending_name, embedding, heard)
            profile, score, status = pending_name, 1.0, STATUS_IDENTIFIED
        else:
            profile, score = self._match(embedding)
            if profile is not None and (
                full or score >= self.config.threshold + self.QUICK_CONFIDENT
            ):
                status = STATUS_IDENTIFIED
                if (
                    self.config.adapt
                    and score >= self.config.threshold + 0.1
                    and heard >= self.MIN_BANK_SECONDS
                ):
                    # Only a confident match is banked: a doubtful one would
                    # teach this person's voice to answer to someone else's.
                    self._bank(profile, embedding, heard)
            elif full or score <= self.config.threshold - self.QUICK_REJECT:
                status, profile = STATUS_UNKNOWN, None
            elif status_before == STATUS_UNKNOWN:
                status, profile = STATUS_UNKNOWN, None  # refreshed print, no verdict change
            else:
                # Early and borderline: keep the audio and wait for more.
                self.database.save_conversation_speaker(
                    conversation_id, STATUS_LISTENING, None, score, None, spoken
                )
                return {"status": STATUS_LISTENING, "speech_seconds": round(spoken, 1)}
        with self._lock:
            if status == STATUS_UNKNOWN and spoken < self.REFINE_SPEECH_CAP:
                pass  # keep the buffer: later utterances sharpen this voiceprint
            else:
                self._pending.pop(conversation_id, None)
        spoken = max(spoken, previous_spoken)
        self.database.save_conversation_speaker(
            conversation_id, status, profile, score, embedding.tobytes(), spoken,
            asked=True if pending_name else None,
        )
        if pending_name:
            self.database.set_speaker_pending_name(conversation_id, None)
        log.info(
            "voice: conversation %s -> %s%s (score %.2f, %.1fs speech)",
            conversation_id[:8],
            status,
            f" as {profile}" if profile else "",
            score,
            spoken,
        )
        return self.speaker(conversation_id) or {"status": status}

    def _still_them(self, state: Any, audio: bytes) -> bool:
        """Is the locked speaker plausibly still the one talking?

        The voiceprint computed to answer that is also the best material the
        bank will see: a settled conversation is where the long, relaxed
        sentences are, while identification happens on the short opening one.
        So a confident, long enough utterance is banked here on the way past.
        """
        profile = str(state["profile"]) if state and state["profile"] else None
        if profile is None:
            return True
        try:
            wav = speech_only(decode_audio(audio))
        except VoiceError:
            return True  # undecodable clip is no evidence against them
        heard = speech_seconds(wav)
        if heard < self.RECHECK_SECONDS:
            return True  # too little to judge on; leave the lock alone
        embedding = self._embed(wav)
        theirs = dict(self._scores(embedding)).get(profile)
        if theirs is None or theirs > self.config.threshold - self.QUICK_REJECT:
            if (
                self.config.adapt
                and theirs is not None
                and theirs >= self.config.threshold + 0.1
                and heard >= self.MIN_BANK_SECONDS
            ):
                self._bank(profile, embedding, heard)
            return True
        log.info(
            "voice: locked speaker %s no longer matches (score %.2f) -- re-identifying",
            profile,
            theirs,
        )
        return False

    def _scores(self, embedding: np.ndarray) -> list[tuple[str, float]]:
        """Every enrolled voice scored against this voiceprint, best first."""
        banked = {
            name: [vec for vec in vectors if vec.size == embedding.size]
            for name, vectors in self.database.list_voiceprint_samples(EMBED_SCHEME).items()
        }
        banked = {name: vectors for name, vectors in banked.items() if vectors}
        if not banked:
            return []
        # Every recording in a home shares the same microphones, rooms, and
        # speakers, and the model hears that channel as much as the voice.
        # Subtract the average voice: a generic one to start, blended with
        # the household's own mean once two or more people are enrolled.
        centre = generic_centre(embedding.size)
        if len(banked) >= 2:
            household = np.mean(
                [np.mean(vectors, axis=0) for vectors in banked.values()], axis=0
            )
            centre = 0.5 * centre + 0.5 * household
        probe = _unit(embedding - centre)
        # A person is as close as their closest recording. Averaging the whole
        # bank into one vector would blur the very variation -- tired, distant,
        # hurried -- that keeping several recordings exists to cover.
        return sorted(
            (
                (name, max(float(np.dot(_unit(vec - centre), probe)) for vec in vectors))
                for name, vectors in banked.items()
            ),
            key=lambda item: -item[1],
        )

    def _bank(self, profile: str, embedding: np.ndarray, seconds: float) -> dict[str, Any]:
        return self.database.bank_voiceprint(
            profile,
            embedding,
            seconds=seconds,
            scheme=EMBED_SCHEME,
            cap_seconds=self.config.max_banked_seconds,
        )

    def _match(self, embedding: np.ndarray) -> tuple[str | None, float]:
        scored = self._scores(embedding)
        if not scored:
            return None, 0.0
        best_name, best = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else None
        # With a single enrolled voice there is no rival to beat, so the
        # margin never bites and an absolute cosine decides alone -- and the
        # absolute value is the part that travels worst between microphones,
        # rooms and moods. Until a second person is enrolled, make the one
        # profile clear the bar it would have had to clear against a rival.
        needed = self.config.threshold
        if runner_up is None:
            needed += self.config.margin
        elif best - runner_up < self.config.margin:
            return None, best
        if best >= needed:
            return best_name, best
        return None, best

    # -- state and actions ---------------------------------------------------------

    def speaker(self, conversation_id: str | None) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        state = self.database.get_conversation_speaker(conversation_id)
        if state is None:
            return None
        return {
            "status": state["status"],
            "profile": state["profile"],
            # Who the screen should name: the voice match, or the name someone
            # gave (typed or said) before enough of their voice was heard.
            "name": state["profile"] if state["status"] == STATUS_IDENTIFIED else state.get("pending_name"),
            "confidence": round(float(state["confidence"] or 0.0), 2),
            "asked": bool(state["asked"]),
            "ask_attempts": int(state["asked"] or 0),
        }

    def enroll(self, conversation_id: str, name: str) -> dict[str, Any]:
        profile = " ".join(name.strip().split())
        if not profile or len(profile) > 40:
            raise ValueError("A name of at most 40 characters is required")
        state = self.database.get_conversation_speaker(conversation_id)
        if state is None or not state["embedding"]:
            # Not enough speech yet. Remember the name; the voiceprint is saved
            # under it automatically as soon as the next utterances provide it.
            if state is None:
                self.database.save_conversation_speaker(
                    conversation_id, STATUS_LISTENING, None, None, None, 0.0, asked=True
                )
            self.database.set_speaker_pending_name(conversation_id, profile)
            self.database.mark_speaker_asked(conversation_id)
            return {
                "ok": True,
                "profile": profile,
                "pending": True,
                "note": (
                    f"Name noted, but not enough of {profile}'s voice has been heard "
                    "yet to save it. In your reply, ask them one open question they "
                    "will answer in a full sentence or two — what they last watched "
                    "and enjoyed, or what they are in the mood for — rather than "
                    "asking them to repeat a set phrase. A few seconds of ordinary "
                    "talking is what their voice saves from, and it saves "
                    "automatically once they say it."
                ),
            }
        embedding = np.frombuffer(state["embedding"], dtype=np.float32)
        record = self._bank(profile, embedding, float(state["speech_seconds"] or 0.0))
        self.database.save_conversation_speaker(
            conversation_id, STATUS_IDENTIFIED, profile, 1.0, state["embedding"],
            float(state["speech_seconds"] or 0.0), asked=True,
        )
        return {
            "ok": True,
            "profile": profile,
            "samples": record["samples"],
            # Without this the model gets a bare result, finds nothing telling
            # it what to say, and falls back to re-answering the question from
            # before -- so the person who just gave their name is never
            # acknowledged and hears their last answer twice.
            "note": (
                f"{profile}'s voice is saved. Reply now, in your own words: greet "
                f"{profile} by name and say you will know their voice from now on. "
                "If they named something they love, say one warm line about it. "
                "Call no further tools, and do not repeat or re-answer anything "
                "you already told them earlier in this conversation."
            ),
        }

    def stay_anonymous(self, conversation_id: str) -> dict[str, Any]:
        state = self.database.get_conversation_speaker(conversation_id)
        # The chat's voiceprint stays with the chat (never stored as a profile)
        # so a later "actually, I'm Sam" can still enroll without re-recording.
        self.database.save_conversation_speaker(
            conversation_id, STATUS_ANONYMOUS, None, None,
            state["embedding"] if state else None,
            float(state["speech_seconds"] or 0.0) if state else 0.0, asked=True,
        )
        self._pending.pop(conversation_id, None)
        return {
            "ok": True,
            "status": STATUS_ANONYMOUS,
            "note": (
                "Reply now, in your own words: acknowledge briefly that they can "
                "stay anonymous and that you will not ask again. Call no further "
                "tools, and do not re-answer anything you already told them."
            ),
        }

    # The question Friday asks an unrecognized voice. It asks for a favourite
    # as well as a name, for two reasons: a bare name is half a second of
    # audio, which is far too little to build a first voiceprint from, while
    # a name and a film together runs three or four seconds -- and the answer
    # is worth knowing anyway, so nothing is asked purely to fill time.
    NAME_QUESTION = {
        False: (
            " By the way, I don't recognize your voice yet — what's your name, "
            "and what's a film or show you love? You can stay anonymous if "
            "you'd rather not say."
        ),
        True: (
            " Por cierto, todavía no reconozco tu voz. ¿Cómo te llamas, y qué "
            "película o serie te encanta? Puedes quedarte en el anonimato si "
            "lo prefieres."
        ),
    }

    # Whether a reply already raised the subject. Asking is not always phrased
    # as a question -- "I'll need to ask for your name" is a statement, and
    # treating it as no ask is what put the question in a reply twice.
    _NAME_CUE = re.compile(
        r"your name|call you|who you are"
        r"|tu nombre|c[oó]mo te llamas|qui[eé]n eres",
        re.IGNORECASE,
    )
    _NOT_RECOGNIZED = re.compile(
        r"(?:don'?t|do not|not)\s+(?:yet\s+)?recognize|didn'?t recognize"
        r"|not sure who|who'?s speaking|who i'?m (?:talking|speaking)"
        r"|no (?:te )?reconozco|no s[eé] qui[eé]n",
        re.IGNORECASE,
    )

    @classmethod
    def sounds_like_a_name_question(cls, reply: str) -> bool:
        if cls._NOT_RECOGNIZED.search(reply):
            return True
        return ("?" in reply or "¿" in reply) and bool(cls._NAME_CUE.search(reply))

    def name_question(self, *, spanish: bool) -> str:
        return self.NAME_QUESTION[spanish]

    def mark_asked(self, conversation_id: str) -> int:
        return self.database.mark_speaker_asked(conversation_id)

    def should_ask(self, conversation_id: str | None) -> bool:
        """Does this voice still need to be asked its name?

        Asked once, never again — but "asked" means the question actually
        reached the person, not that the model was told to put it.
        """
        state = self.speaker(conversation_id)
        return bool(state and state["status"] == STATUS_UNKNOWN and not state["asked"])

    def context(
        self, conversation_id: str | None, *, new_conversation: bool = False
    ) -> str | None:
        """The Speaker line the model sees this turn; None when nothing is known."""
        state = self.speaker(conversation_id)
        if state is None or state["status"] == STATUS_LISTENING:
            if new_conversation and conversation_id and self.first_run:
                # The very first exchange with a new household: the onboarding
                # greeting doubles as the (one and only) name question.
                self.mark_asked(conversation_id)
            return None
        status = state["status"]
        if status == STATUS_IDENTIFIED:
            return (
                f"Speaker: {state['profile']} (recognized by voice). Use this profile "
                "for taste memory, recommendations, and feedback. Address them by "
                "name now and then where it feels natural — a greeting, a warm "
                "moment — never in most replies."
            )
        if status == STATUS_ANONYMOUS:
            return (
                "Speaker: a guest who chose to stay anonymous. Never ask for their name; "
                "do not record personal taste feedback."
            )
        if state["asked"]:
            return (
                "Speaker: unrecognized voice; you already asked for a name — do not ask "
                "again. If they now give a name, call voice_enroll; if they decline, "
                "call voice_stay_anonymous."
            )
        return (
            "Speaker: unrecognized voice, not yet asked. Answer their request exactly "
            "as you would for anyone else. Do NOT mention their name, their voice, or "
            "that you do not recognize them: the question is added to the end of your "
            "reply for you, once you have finished answering. When they give a name "
            "call voice_enroll; if they decline call voice_stay_anonymous."
        )

    def forget(self, profile: str) -> bool:
        return self.database.delete_voiceprint(profile)

    def profiles(self) -> list[dict[str, Any]]:
        stale = set(self.stale_profiles())
        return [
            {
                "profile": item["profile"],
                "samples": item["samples"],
                "updated_at": item["updated_at"],
                "seconds": round(float(item["seconds"] or 0.0), 1),
                "needs_reenrollment": str(item["profile"]) in stale,
            }
            for item in self.database.list_voiceprints()
        ]
