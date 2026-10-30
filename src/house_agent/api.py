from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field

from .config import AppConfig, load_config
from .catalog import DEFAULT_SUBSCRIPTIONS, PROVIDER_DISPLAY_NAMES, CatalogError, canonical_provider
from .containers import Containers
from .content import ContentError, PlaybackService
from .db import Database
from .keys import KEYED_FEATURES, WEB_FALLBACK_SETTING, fallback_setting, web_fallback_enabled
from . import providers
from .assistant_model import AssistantModel
from .llm import LLMError, LocalAgent
from .roku import RokuError, discover_roku_devices
from .recommendations import RecommendationService
from .routines import execute_routine, validate_actions
from .calendar_sync import CalendarSync
from .scheduler import Announcer, Scheduler
from .settings import HouseSettings, SettingsError
from .speech import SpeechError, SpeechToText
from .tools import ToolError, ToolRegistry
from .tv import TVError
from . import tts as tts_module
from .tts import TextToSpeech, TextToSpeechError
from .voice import VoiceError
from .news import NewsError, NewsService
from .weather import HomeLocationUnknown, WeatherError


timing_log = logging.getLogger("house_agent.timing")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    conversation_id: str | None = Field(default=None, max_length=100)
    # "voice" when the words came from the microphone. A typed turn may say
    # who is typing: the person this screen last recognised by voice.
    mode: str = Field(default="text", pattern=r"^(text|voice)$")
    person: str | None = Field(default=None, max_length=40)


class ChatCancelRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)


class SpeechSynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_096)
    # Auditioning a voice in Settings: speak this one instead of the saved
    # one, without changing anything.
    voice: str | None = Field(default=None, max_length=60)


class VoiceChoiceRequest(BaseModel):
    voice: str | None = Field(default=None, max_length=60)
    spanish_voice: str | None = Field(default=None, max_length=60)


class KeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    count: int = Field(default=1, ge=1, le=10)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=256)


class AppRequest(BaseModel):
    app: str = Field(min_length=1, max_length=100)


class InputRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100)


class RoutineRequest(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,79}$")
    device_id: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    actions: list[dict[str, Any]]


class ListItemsRequest(BaseModel):
    items: list[str] = Field(min_length=1, max_length=40)


class EventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    when: str = Field(min_length=1, max_length=40)
    ends: str | None = Field(default=None, max_length=40)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)
    weather_heads_up: bool | None = None


class AnnounceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class DoorDashCallbackRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)
    callback: str = Field(min_length=1, max_length=4000)


class InboxReadRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)
    ids: list[int] | None = Field(default=None, max_length=200)


class WatchRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(pattern=r"^(planned|started|watched|abandoned)$")
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)


class ModelDownloadRequest(BaseModel):
    model: str = Field(min_length=1, max_length=80)


class WebFallbackRequest(BaseModel):
    enabled: bool
    feature: str | None = Field(default=None, max_length=40)


class SubscriptionRequest(BaseModel):
    providers: list[str] = Field(min_length=1, max_length=30)


class ContentLinkRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=100)
    media_type: str = Field(pattern=r"^(movie|episode|season|series|special|live)$")
    app_name: str = Field(min_length=1, max_length=100)
    content_id: str = Field(min_length=1, max_length=512)
    source_url: str | None = Field(default=None, max_length=2_000)
    source: str = Field(default="manual", max_length=80)
    verified: bool = False


class ContentPlayRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    provider: str | None = Field(default=None, max_length=100)
    media_type: str | None = Field(
        default=None, pattern=r"^(movie|episode|season|series|special|live)$"
    )
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)
    profile: str | None = Field(default=None, min_length=1, max_length=80)
    allow_unverified: bool = False


class ContentPlanRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    provider: str | None = Field(default=None, max_length=100)
    media_type: str | None = Field(
        default=None, pattern=r"^(movie|episode|season|series|special|live)$"
    )
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)


class ResolverVerificationRequest(BaseModel):
    verified: bool
    notes: str = Field(default="", max_length=500)


class PreferenceRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(pattern=r"^(liked|disliked|deferred)$")
    media_type: str = Field(default="unknown", pattern=r"^(movie|series|unknown)$")
    profile: str | None = Field(default=None, min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)


class RecommendationRequest(BaseModel):
    profile: str | None = Field(default=None, min_length=1, max_length=80)
    media_type: str = Field(default="any", pattern=r"^(any|movie|series)$")
    genres: list[str] = Field(default_factory=list, max_length=5)
    year_min: int | None = Field(default=None, ge=1900, le=2100)
    year_max: int | None = Field(default=None, ge=1900, le=2100)
    user_rating_min: float = Field(default=7.0, ge=0, le=10)
    critic_score_min: int | None = Field(default=None, ge=0, le=100)
    sort_by: str = Field(
        default="popularity_desc",
        pattern=r"^(popularity_desc|release_date_desc|relevance_desc)$",
    )
    limit: int = Field(default=5, ge=1, le=10)


class DevicePairRequest(BaseModel):
    network: str = Field(pattern=r"^(matter|zigbee)$")
    code: str | None = Field(default=None, max_length=120)


class DeviceRemoveRequest(BaseModel):
    device: str = Field(min_length=1, max_length=120)


class DeviceSetRequest(BaseModel):
    device: str = Field(min_length=1, max_length=120)
    power: str | None = Field(default=None, pattern=r"^(on|off|toggle)$")
    brightness: int | None = Field(default=None, ge=0, le=100)
    # Device state reports it as color_temp_kelvin; either spelling works.
    color_temp: int | None = Field(
        default=None, ge=1500, le=6500, validation_alias=AliasChoices("color_temp", "color_temp_kelvin")
    )
    color: str | None = Field(default=None, min_length=1, max_length=30)


class AssistantModelRequest(BaseModel):
    provider: str = Field(pattern=r"^(local|openai|anthropic|openrouter|custom)$")
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=400)
    # The custom endpoint's address, e.g. http://192.168.0.20:8000/v1
    base_url: str | None = Field(default=None, max_length=300, pattern=r"^https?://\S+$")
    # Cloud models see everything said to Friday; the Settings page makes
    # the household tick that they understand before it will switch.
    accept_cloud: bool = False


class DeviceFlashRequest(BaseModel):
    device: str = Field(min_length=1, max_length=120)
    times: int = Field(default=3, ge=1, le=10)


class DeviceRenameRequest(BaseModel):
    device: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    room: str | None = Field(default=None, min_length=1, max_length=60)
    replace: bool = False


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="rule", pattern=r"^(rule|style|taste)$")


class NoteEditRequest(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class TVAddRequest(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    platform: str = Field(default="roku", min_length=1, max_length=30)
    mac: str | None = Field(default=None, max_length=40)


class TVEditRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)


class WifiRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class DoorDashLoginRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)


_OPEN_PATHS = frozenset({"/api/health"})


def _pairing_code(config: AppConfig) -> str:
    """The code the web app has to present. Configured, or minted once and
    kept in .env so it survives restarts; printed so the first screen can
    be paired from the startup log."""
    token = config.server.api_token
    saved = ""
    if not token:
        token = secrets.token_urlsafe(9)  # 12 characters: typed on a phone once
        if config.config_path.exists():
            try:
                HouseSettings(config.config_path).set_env("API_TOKEN", token, secret=True)
                saved = " (saved to .env as API_TOKEN)"
            except (SettingsError, OSError) as exc:
                saved = f" (could not save it to .env: {exc}; set API_TOKEN yourself)"
    logging.getLogger("house_agent").warning("Pairing code for the web app: %s%s", token, saved)
    return token


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or load_config()
    database = Database(app_config.storage.database_path)
    tools = ToolRegistry(app_config, database)
    agent = LocalAgent(app_config.llm, database, tools)
    assistant_model = AssistantModel(agent, database, HouseSettings(app_config.config_path))
    speech = SpeechToText(app_config.speech)
    text_to_speech = TextToSpeech(app_config.text_to_speech)
    playback = PlaybackService(app_config, database)
    recommendations = RecommendationService(app_config, database)
    voices = tools.voices

    announcer = Announcer()
    # Background work in the tool layer (the zigbee join watcher) speaks
    # through the same channel as reminders.
    tools.announcer = announcer

    async def ask(question: str, quiet: bool = False) -> str:
        """A scheduled check answered the way a spoken turn would be, so it
        uses the same tools and the same voice."""
        prompt = (
            "Scheduled check: this was set up earlier and is due now; nobody is "
            "speaking. Answer it with your tools in one or two spoken sentences, "
            "addressed to the person who set it. "
        )
        if quiet:
            prompt += (
                "This is a heads-up: if there is nothing they need to know or do, "
                "reply with exactly the words: Nothing to report. "
            )
        result = await agent.chat(prompt + question)
        return result.message

    scheduler = Scheduler(database, announcer, tools.fades, ask)
    calendar_sync = CalendarSync(app_config.calendar, database)
    containers = Containers(
        app_config.docker.compose_files if app_config.docker.manage else (),
        command=app_config.docker.command,
        cwd=app_config.config_path.resolve().parent,
        start_timeout=app_config.docker.start_timeout_seconds,
        stop_timeout=app_config.docker.stop_timeout_seconds,
    )

    async def zigbee_watchdog() -> None:
        """Zigbee2MQTT down while the broker is up means every Zigbee light
        is deaf. Give it 90 seconds to come back by itself (Docker restarts
        it), then recreate its container -- no more than every ten minutes,
        since a pulled dongle is not something a new container fixes."""
        last_attempt = 0.0
        while True:
            await asyncio.sleep(30)
            down = tools.smarthome.zigbee_bridge_down_for()
            if down is None or down < 90 or time.monotonic() - last_attempt < 600:
                continue
            last_attempt = time.monotonic()
            logging.getLogger("house_agent").warning(
                "Zigbee2MQTT has been offline for %.0fs; recreating its container", down
            )
            await containers.recreate("zigbee2mqtt")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        database.seed_subscriptions(DEFAULT_SUBSCRIPTIONS)
        assistant_model.apply_saved()
        text_to_speech.use_voice(
            database.get_setting("tts_voice"),
            database.get_setting("tts_spanish_voice"),
        )
        # Before the backends, so mosquitto and matter-server are on their way
        # up by the time their connect loops start knocking.
        await containers.start()
        await assistant_model.settle_local()
        await agent.probe_context()
        tools.smarthome.start()
        scheduler.start()
        calendar_sync.start()
        watchdog = asyncio.create_task(zigbee_watchdog()) if containers.enabled else None
        warm = asyncio.create_task(assistant_model.keep_warm()) if app_config.llm.keep_warm else None
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if warm is not None:
                warm.cancel()
            await calendar_sync.stop()
            await scheduler.stop()
            await tools.close()
            await tools.smarthome.stop()
            # Last, so the backends have already disconnected cleanly.
            await containers.stop()

    app = FastAPI(
        title="House Agent",
        version="0.1.0",
        description="Local LLM tool gateway and home-device command center",
        lifespan=lifespan,
    )
    app.state.config = app_config
    app.state.database = database
    api_token = _pairing_code(app_config)
    app.state.api_token = api_token

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        offered = header[7:] if header.lower().startswith("bearer ") else ""
        offered = offered or request.query_params.get("token", "") or request.cookies.get("house_token", "")
        return bool(offered) and secrets.compare_digest(offered, api_token)

    @app.middleware("http")
    async def require_pairing_code(request: Request, call_next):
        # Everything that reads or moves the house needs the code. The app
        # shell, the health line, and the calendar feed (its own address is
        # the secret) do not.
        path = request.url.path
        if path.startswith("/api/") and path not in _OPEN_PATHS and not authorized(request):
            return _json_error(401, "Pairing code required: enter the code from Friday's startup log.")
        return await call_next(request)
    app.state.tools = tools
    app.state.agent = agent
    app.state.assistant_model = assistant_model
    app.state.speech = speech
    app.state.text_to_speech = text_to_speech
    app.state.playback = playback
    app.state.recommendations = recommendations
    app.state.announcer = announcer
    app.state.scheduler = scheduler
    app.state.calendar_sync = calendar_sync

    active_turns: dict[str, asyncio.Task[None]] = {}
    app.state.active_turns = active_turns

    @app.exception_handler(RokuError)
    async def roku_error(_: Request, exc: RokuError):
        return _json_error(502, str(exc))

    @app.exception_handler(TVError)
    async def tv_error(_: Request, exc: TVError):
        return _json_error(502, str(exc))

    @app.exception_handler(ToolError)
    async def tool_error(_: Request, exc: ToolError):
        return _json_error(400, str(exc))

    @app.exception_handler(CatalogError)
    async def catalog_error(_: Request, exc: CatalogError):
        return _json_error(503, str(exc))

    @app.exception_handler(ContentError)
    async def content_error(_: Request, exc: ContentError):
        return _json_error(400, str(exc))

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "paired": authorized(request),
            "model": agent.config.model,
            "llm_provider": agent.provider,
            "llm_cloud": agent.provider != "local",
            "llm_label": providers.PROVIDERS[agent.provider].label,
            "llm_endpoint": app_config.llm.base_url,
            "tts_voice": text_to_speech.voice,
            "tv_devices": len(tools.devices),
            "voice_recognition": voices.available,
            "first_run": voices.first_run,
            "setup_status": database.get_setting("setup_status", "none"),
            "exact_episode_playback_enabled": (
                app_config.catalog.exact_episode_playback_enabled
            ),
            "smart_home": tools.smarthome.status(),
            "containers": containers.status(),
            "calendar": {
                "subscriptions": len(app_config.calendar.subscriptions),
                "synced": list(calendar_sync.status.values()),
                "feed": "/calendar.ics",
            },
        }

    @app.get("/api/events")
    async def events(
        request: Request, since: int = 0, conversation: str | None = None
    ) -> StreamingResponse:
        """Server-Sent Events carrying everything Friday says unprompted:
        reminders, scheduled checks, fades finishing. A reconnecting browser
        sends Last-Event-ID and gets what it missed from the backlog. Pass
        the conversation id to also receive events meant for that
        conversation alone (a light joining the pairing window it opened)."""
        header = request.headers.get("last-event-id")
        try:
            last_id = int(header) if header else int(since)
        except ValueError:
            last_id = 0

        def sse(event: dict[str, Any]) -> str:
            return (
                f"id: {event['id']}\nevent: {event['type']}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            )

        async def stream() -> Any:
            yield ":" + " " * 2048 + "\n\n"
            for event in announcer.since(last_id, conversation or None):
                yield sse(event)
            queue = announcer.subscribe(conversation or None)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=25)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield sse(event)
            finally:
                announcer.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/announce")
    async def announce(body: AnnounceRequest) -> dict[str, Any]:
        """Say something on every connected screen now. Handy for testing the
        push channel, and for other programs on the LAN that want a word."""
        return announcer.publish(body.text, source="api")

    @app.get("/api/inbox")
    async def inbox(conversation: str | None = None, limit: int = 50) -> dict[str, Any]:
        """What Friday has put on this chat's screen rather than said: links,
        sign-in pages, step-by-step instructions. Newest first, with how many
        nobody has opened yet. Each conversation has its own board."""
        return {
            "messages": database.list_inbox(conversation or None, max(1, min(int(limit), 200))),
            "unread": database.unread_inbox_count(conversation or None),
        }

    # ----------------------------------------------------------- the app page

    @app.get("/api/conversations")
    async def conversations(limit: int = 100) -> dict[str, Any]:
        """Past chats, newest first, so they can be reopened or forgotten."""
        return {"conversations": database.list_conversations(max(1, min(int(limit), 500)))}

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str) -> dict[str, Any]:
        messages = database.conversation_messages(conversation_id)
        if not messages:
            raise HTTPException(status_code=404, detail="Unknown conversation")
        return {
            "id": conversation_id,
            "messages": messages,
            "speaker": voices.speaker(conversation_id),
        }

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict[str, Any]:
        task = active_turns.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
        if not database.delete_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="Unknown conversation")
        return {"ok": True}

    def known_person(name: str | None) -> str | None:
        """A typed-in name, only if the house knows that person."""
        if not name or not name.strip():
            return None
        wanted = name.strip().casefold()
        known = {*database.known_profiles(), *database.note_counts()}
        if voices.available:
            known |= {item["profile"] for item in voices.profiles()}
        return next((person for person in known if str(person).casefold() == wanted), None)

    @app.get("/api/profiles")
    async def profiles() -> dict[str, Any]:
        """Everyone the house knows, with their voice enrollment and how much
        has been written down about them. The household entry is what applies
        to everyone."""
        household = app_config.household_profile
        by_voice = {item["profile"]: item for item in voices.profiles()} if voices.available else {}
        counts = database.note_counts()
        names = sorted(
            {*database.known_profiles(), *by_voice, *counts} - {household},
            key=str.casefold,
        )

        def entry(name: str, is_household: bool) -> dict[str, Any]:
            kinds = counts.get(name, {})
            return {
                "profile": name,
                "household": is_household,
                "voice": by_voice.get(name),
                "rules": kinds.get("rule", 0),
                "style": kinds.get("style", 0),
                "taste": kinds.get("taste", 0),
            }

        return {
            "household": household,
            "voice_recognition": voices.available,
            "profiles": [entry(household, True), *(entry(name, False) for name in names)],
        }

    @app.get("/api/profiles/{profile}/notes")
    async def profile_notes(profile: str) -> dict[str, Any]:
        return {"profile": profile, "notes": database.list_notes(profile)}

    @app.post("/api/profiles/{profile}/notes")
    async def add_profile_note(profile: str, body: NoteRequest) -> dict[str, Any]:
        profile = " ".join(profile.split())
        if not profile or len(profile) > 80:
            raise HTTPException(status_code=400, detail="Profile name is empty or too long")
        return {"note": database.add_profile_note(profile, body.note, kind=body.kind)}

    @app.put("/api/notes/{note_id}")
    async def edit_note(note_id: int, body: NoteEditRequest) -> dict[str, Any]:
        note = database.update_profile_note(note_id, body.note)
        if note is None:
            raise HTTPException(status_code=404, detail="Unknown note")
        return {"note": note}

    @app.delete("/api/notes/{note_id}")
    async def delete_note(note_id: int) -> dict[str, Any]:
        if not database.delete_profile_note(note_id):
            raise HTTPException(status_code=404, detail="Unknown note")
        return {"ok": True}

    @app.get("/api/tvs")
    async def tvs() -> dict[str, Any]:
        return {
            "tvs": await tools.tv_overview(),
            "input_aliases": dict(tools.config.roku_input_aliases),
        }

    @app.post("/api/tvs")
    async def add_tv(body: TVAddRequest) -> dict[str, Any]:
        return await tools.add_tv(body.id, body.name, body.host, body.port, body.platform, body.mac)

    @app.put("/api/tvs/{device_id}")
    async def edit_tv(device_id: str, body: TVEditRequest) -> dict[str, Any]:
        if device_id not in tools.devices:
            raise HTTPException(status_code=404, detail="Unknown TV")
        results: dict[str, Any] = {}
        if body.name:
            results["name"] = await tools.execute(
                "house_config_set", {"setting": "tv_name", "device": device_id, "name": body.name}
            )
        if body.host or body.port:
            arguments: dict[str, Any] = {
                "setting": "tv_address",
                "device": device_id,
                "host": body.host or tools.devices[device_id].host,
            }
            if body.port:
                arguments["port"] = body.port
            results["address"] = await tools.execute("house_config_set", arguments)
        failed = [value for value in results.values() if not value.get("ok")]
        if failed:
            raise HTTPException(status_code=400, detail=str(failed[0].get("error") or "Could not save"))
        device = tools.devices[device_id]
        return {"ok": True, "device": asdict(device), "results": results}

    @app.delete("/api/tvs/{device_id}")
    async def remove_tv(device_id: str) -> dict[str, Any]:
        return await tools.remove_tv(device_id)

    @app.post("/api/tvs/discover")
    async def discover_tvs() -> dict[str, Any]:
        return await tools.execute("tv_discover", {})

    @app.get("/api/doordash/status")
    async def doordash_status(probe: bool = False) -> dict[str, Any]:
        return await tools.doordash_status(probe=probe)

    @app.post("/api/doordash/login")
    async def doordash_login(body: DoorDashLoginRequest) -> dict[str, Any]:
        return await tools.start_doordash_login(body.conversation_id)

    @app.get("/api/house")
    async def house() -> dict[str, Any]:
        """What the house knows about itself, for the settings page."""
        return {
            "wifi": {
                "ssid": tools.config.matter.wifi_ssid,
                "password_saved": bool(tools.config.matter.wifi_password),
            },
            "files": [tools.settings.config_path.name, tools.settings.env_path.name],
            "household": app_config.household_profile,
            "llm": {"model": app_config.llm.model, "endpoint": app_config.llm.base_url},
            "speech": {"tts_voice": app_config.text_to_speech.voice},
            "smart_home": tools.smarthome.status(),
            "doordash": tools.doordash.status(),
            "calendar": {
                "subscriptions": len(app_config.calendar.subscriptions),
                "synced": list(calendar_sync.status.values()),
                "feed": "/calendar.ics",
            },
            "web_fallback": {
                "default": web_fallback_enabled(database, ""),
                "features": tools.keyed_features(),
            },
        }

    @app.put("/api/house/web-fallback")
    async def set_web_fallback(body: WebFallbackRequest) -> dict[str, Any]:
        """Whether Friday answers from a web search when a feature's API key
        is missing (and says so), or just says which key it needs. Per
        feature, or with no feature the default for all of them."""
        value = "on" if body.enabled else "off"
        if body.feature:
            if body.feature not in KEYED_FEATURES:
                raise HTTPException(status_code=404, detail="Unknown feature")
            database.set_setting(fallback_setting(body.feature), value)
        else:
            database.set_setting(WEB_FALLBACK_SETTING, value)
        return {"ok": True, "features": tools.keyed_features()}

    # ---- which model answers ------------------------------------------------
    @app.get("/api/assistant-model")
    async def get_assistant_model() -> dict[str, Any]:
        return assistant_model.status()

    @app.get("/api/assistant-model/models")
    async def list_assistant_models(provider: str) -> dict[str, Any]:
        if provider not in providers.PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown provider")
        try:
            return {"models": await assistant_model.models(provider)}
        except Exception as exc:  # noqa: BLE001 - shown in the page as-is
            return {"models": list(providers.PROVIDERS[provider].suggested), "error": str(exc)[:300]}

    @app.post("/api/assistant-model/download")
    async def download_local_model(body: ModelDownloadRequest) -> dict[str, Any]:
        """Download one of the offered models to this computer (Ollama)."""
        try:
            return assistant_model.start_download(body.model)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/assistant-model/download")
    async def download_progress() -> dict[str, Any]:
        return assistant_model.download

    @app.put("/api/assistant-model")
    async def set_assistant_model(body: AssistantModelRequest) -> dict[str, Any]:
        cloud = providers.is_cloud(body.provider)
        if cloud and not body.accept_cloud:
            raise HTTPException(
                status_code=400,
                detail="Confirm that conversations will be sent to the provider before switching.",
            )
        key = (body.api_key or "").strip() or None
        custom = body.provider == providers.CUSTOM
        base_url = (body.base_url or "").strip() or None
        if custom and not (base_url or assistant_model.base_url(body.provider)):
            raise HTTPException(status_code=400, detail="Give the endpoint's address first.")
        if cloud and not custom and not (key or assistant_model.api_key(body.provider)):
            raise HTTPException(status_code=400, detail="Paste an API key first.")
        model = body.model.strip()
        if body.provider == providers.LOCAL:
            model = await assistant_model.house_build(model)
        try:
            reply = await assistant_model.test(body.provider, model, key, base_url)
        except Exception as exc:  # noqa: BLE001 - the provider's complaint, shown to the user
            raise HTTPException(status_code=400, detail=f"That did not work: {str(exc)[:300]}") from exc
        if key:
            assistant_model.save_key(body.provider, key)
        previous = (agent.provider, agent.config.model)
        assistant_model.choose(body.provider, model, base_url)
        if previous[0] == providers.LOCAL and (agent.provider, agent.config.model) != previous:
            await assistant_model.unload(previous[1])
        # The new model's real window, so turns are trimmed to fit it.
        await agent.probe_context()
        return {"ok": True, "reply": reply.strip()[:80], **assistant_model.status()}

    # Whichever voice the household picked outlives a restart; config is only
    # the default it started from. Read in the lifespan, once the settings
    # table exists.
    VOICE_SETTING = "tts_voice"
    SPANISH_VOICE_SETTING = "tts_spanish_voice"

    @app.get("/api/tts/voices")
    async def list_voices() -> dict[str, Any]:
        body: dict[str, Any] = {
            "voice": text_to_speech.voice,
            "spanish_voice": text_to_speech.spanish_voice,
            "sample": tts_module.SAMPLE_LINE,
            "spanish_sample": tts_module.SPANISH_SAMPLE_LINE,
        }
        try:
            body["voices"] = await text_to_speech.voices()
        except TextToSpeechError as exc:
            body["voices"] = []
            body["error"] = str(exc)[:300]
        return body

    @app.put("/api/tts/voice")
    async def set_voice(body: VoiceChoiceRequest) -> dict[str, Any]:
        if not body.voice and not body.spanish_voice:
            raise HTTPException(status_code=400, detail="Give a voice to switch to.")
        if body.voice:
            database.set_setting(VOICE_SETTING, body.voice)
        if body.spanish_voice:
            database.set_setting(SPANISH_VOICE_SETTING, body.spanish_voice)
        text_to_speech.use_voice(body.voice, body.spanish_voice)
        return {
            "ok": True,
            "voice": text_to_speech.voice,
            "spanish_voice": text_to_speech.spanish_voice,
        }

    @app.delete("/api/assistant-model/key")
    async def forget_assistant_key(provider: str) -> dict[str, Any]:
        if provider not in providers.PROVIDERS or not providers.is_cloud(provider):
            raise HTTPException(status_code=404, detail="Unknown provider")
        assistant_model.forget_key(provider)
        if agent.provider == provider:
            assistant_model.choose(providers.LOCAL, agent.local_config.model)
        return {"ok": True, **assistant_model.status()}

    @app.put("/api/house/wifi")
    async def set_wifi(body: WifiRequest) -> dict[str, Any]:
        result = await tools.execute(
            "house_config_set", {"setting": "wifi", "ssid": body.ssid, "password": body.password}
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=str(result.get("error") or "Could not save"))
        return result

    @app.post("/api/doordash/callback")
    async def doordash_callback(body: DoorDashCallbackRequest) -> dict[str, Any]:
        """The address a phone landed on after the DoorDash sign-in, pasted
        into the chat box: its code goes to the waiting `dd-cli login`."""
        return await tools.finish_doordash_login(body.callback, body.conversation_id)

    @app.post("/api/inbox/read")
    async def inbox_read(body: InboxReadRequest) -> dict[str, Any]:
        """The panel was opened: the listed messages (or all on this chat's
        board) are read."""
        return {
            "ok": True,
            "unread": database.mark_inbox_read(body.conversation_id or None, body.ids),
        }

    @app.get("/api/automations")
    async def list_automations(all: bool = False) -> dict[str, Any]:
        return {
            "automations": database.list_automations(include_disabled=all),
            "fades_in_progress": tools.fades.running(),
        }

    @app.post("/api/automations/{automation_id}/run")
    async def run_automation(automation_id: int) -> dict[str, Any]:
        """Fire one now, leaving its schedule alone."""
        automation = database.get_automation(automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Unknown automation")
        return {"ok": True, "result": await scheduler.run_action(automation)}

    @app.put("/api/automations/{automation_id}/enabled")
    async def set_automation_enabled(automation_id: int, enabled: bool = True) -> dict[str, Any]:
        from .scheduler import local_now, next_run

        automation = database.get_automation(automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="Unknown automation")
        following = next_run(automation["schedule"], local_now()) if enabled else None
        database.set_automation_enabled(
            automation_id, enabled, following.isoformat(timespec="minutes") if following else None
        )
        return {"ok": True, "enabled": enabled}

    @app.delete("/api/automations/{automation_id}")
    async def delete_automation(automation_id: int) -> dict[str, Any]:
        if not database.delete_automation(automation_id):
            raise HTTPException(status_code=404, detail="Unknown automation")
        return {"ok": True}

    @app.get("/api/lists")
    async def lists() -> dict[str, Any]:
        return {name: [row["item"] for row in database.list_items(name)] for name in database.list_names()}

    @app.get("/api/lists/{name}")
    async def list_items(name: str) -> dict[str, Any]:
        return {"list": name, "items": database.list_items(name)}

    @app.post("/api/lists/{name}")
    async def add_list_items(name: str, body: ListItemsRequest) -> dict[str, Any]:
        return {"list": name, "added": database.add_list_items(name, body.items)}

    @app.delete("/api/lists/{name}")
    async def clear_list(name: str, item: str | None = None) -> dict[str, Any]:
        if item:
            return {"list": name, "removed": database.remove_list_items(name, [item])}
        return {"list": name, "cleared": database.clear_list(name)}

    @app.get("/api/calendar")
    async def calendar(days: int = 7) -> dict[str, Any]:
        return await tools.execute("calendar_events", {"days": days})

    @app.post("/api/calendar")
    async def add_calendar_event(body: EventRequest) -> dict[str, Any]:
        return await tools.execute("calendar_add", body.model_dump())

    @app.get("/api/calendar/sources")
    async def calendar_sources() -> dict[str, Any]:
        return {
            "subscriptions": len(app_config.calendar.subscriptions),
            "status": list(calendar_sync.status.values()),
            "stored": database.external_sources(),
        }

    @app.post("/api/calendar/refresh")
    async def calendar_refresh() -> dict[str, Any]:
        if not calendar_sync.enabled:
            raise HTTPException(
                status_code=400,
                detail="No calendar subscriptions configured (CALENDAR_ICS_URLS or [calendar] in config.toml).",
            )
        return {"results": await calendar_sync.refresh()}

    @app.delete("/api/calendar/{event_id}")
    async def delete_calendar_event(event_id: int) -> dict[str, Any]:
        if not database.delete_event(event_id):
            raise HTTPException(status_code=404, detail="Unknown event")
        return {"ok": True}

    @app.get("/calendar.ics", include_in_schema=False)
    async def calendar_feed() -> Response:
        """Friday's calendar as an iCalendar feed. Phone and desktop calendar
        apps can subscribe to this URL, so events said out loud at home show
        up beside everything else — no account involved."""
        from .calendar_feed import render_ics

        return Response(
            render_ics(database.all_events()),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="friday.ics"'},
        )

    @app.get("/api/recipes")
    async def recipes(q: str | None = None) -> dict[str, Any]:
        return {"recipes": database.list_recipes(q)}

    @app.get("/api/devices")
    async def smart_devices() -> dict[str, Any]:
        return await tools.execute("device_list", {})

    @app.post("/api/devices/pair")
    async def pair_device(body: DevicePairRequest) -> dict[str, Any]:
        return await tools.execute("device_pair", body.model_dump(exclude_none=True))

    @app.post("/api/devices/remove")
    async def remove_device(body: DeviceRemoveRequest) -> dict[str, Any]:
        return await tools.execute("device_remove", body.model_dump())

    @app.post("/api/devices/set")
    async def set_device(body: DeviceSetRequest) -> dict[str, Any]:
        return await tools.execute("device_set", body.model_dump(exclude_none=True))

    @app.post("/api/devices/flash")
    async def flash_device(body: DeviceFlashRequest) -> dict[str, Any]:
        return await tools.execute("device_flash", body.model_dump(exclude_none=True))

    @app.put("/api/devices/alias")
    async def rename_device(body: DeviceRenameRequest) -> dict[str, Any]:
        return await tools.execute("device_rename", body.model_dump(exclude_none=True))

    # The glance card beside the chat polls this; the forecast moves slowly,
    # so one lookup serves every screen for ten minutes.
    weather_cache: dict[str, Any] = {"at": 0.0, "body": None}

    @app.get("/api/weather")
    async def weather_now() -> dict[str, Any]:
        if weather_cache["body"] is not None and time.monotonic() - weather_cache["at"] < 600:
            return weather_cache["body"]
        try:
            body: dict[str, Any] = {"known": True, **await tools.weather.now()}
        except HomeLocationUnknown:
            return {"known": False}  # not cached: it shows up once the home is set
        except WeatherError as exc:
            return {"known": True, "error": str(exc)}
        weather_cache.update(at=time.monotonic(), body=body)
        return body

    news = NewsService(app_config.news)
    news_cache: dict[str, Any] = {"at": 0.0, "body": None}

    @app.get("/api/news")
    async def headlines() -> dict[str, Any]:
        """Feeds move slowly and every screen asks for the same stories, so
        one pull every ten minutes serves the whole house."""
        if news_cache["body"] is not None and time.monotonic() - news_cache["at"] < 600:
            return news_cache["body"]
        try:
            body = await news.headlines()
        except NewsError as exc:
            return {"enabled": True, "stories": [], "error": str(exc)}
        # A failed round leaves the cache alone so the next screen retries.
        if body.get("stories"):
            news_cache.update(at=time.monotonic(), body=body)
        return body

    @app.get("/api/roku/devices")
    async def devices() -> dict[str, Any]:
        return {"devices": [asdict(device) for device in tools.devices.values()]}

    @app.post("/api/roku/discover")
    async def discover() -> dict[str, Any]:
        return {"devices": await asyncio.to_thread(discover_roku_devices)}

    @app.get("/api/roku/{device_id}/status")
    async def roku_status(device_id: str) -> dict[str, Any]:
        return await tools.client_for(device_id).status()

    @app.get("/api/roku/{device_id}/apps")
    async def roku_apps(device_id: str) -> dict[str, Any]:
        applications = await tools.client_for(device_id).apps()
        return {"apps": [asdict(application) for application in applications]}

    # App artwork barely changes, so each icon is fetched from the TV once.
    app_icons: dict[tuple[str, str], tuple[bytes, str]] = {}

    @app.get("/api/roku/{device_id}/apps/{app_id}/icon")
    async def roku_app_icon(device_id: str, app_id: str) -> Response:
        key = (device_id, app_id)
        if key not in app_icons:
            if tools.client_for(device_id).platform != "roku":
                raise HTTPException(status_code=404, detail="No icons for this TV")
            try:
                app_icons[key] = await tools.roku_client_for(device_id).app_icon(app_id)
            except RokuError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        content, media_type = app_icons[key]
        return Response(content, media_type=media_type, headers={"Cache-Control": "private, max-age=604800"})

    @app.post("/api/roku/{device_id}/key")
    async def roku_key(device_id: str, body: KeyRequest) -> dict[str, Any]:
        return await tools.execute(
            "roku_press", {"device_id": device_id, "key": body.key, "count": body.count}
        )

    @app.post("/api/roku/{device_id}/text")
    async def roku_text(device_id: str, body: TextRequest) -> dict[str, Any]:
        return await tools.execute(
            "roku_type_text", {"device_id": device_id, "text": body.text}
        )

    @app.post("/api/roku/{device_id}/launch")
    async def roku_launch(device_id: str, body: AppRequest) -> dict[str, Any]:
        return await tools.execute(
            "roku_launch_app", {"device_id": device_id, "app": body.app}
        )

    @app.post("/api/roku/{device_id}/input")
    async def roku_input(device_id: str, body: InputRequest) -> dict[str, Any]:
        return await tools.execute(
            "roku_switch_input", {"device_id": device_id, "input": body.input}
        )

    @app.get("/api/routines")
    async def list_routines() -> dict[str, Any]:
        return {"routines": database.list_routines()}

    @app.put("/api/routines/{name}")
    async def save_routine(name: str, body: RoutineRequest) -> dict[str, Any]:
        if name != body.name:
            raise HTTPException(status_code=400, detail="Routine path and body names must match")
        tools.roku_client_for(body.device_id)
        try:
            actions = validate_actions(body.actions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        database.save_routine(body.name, body.device_id, body.description, actions)
        return {"ok": True, "routine": database.get_routine(body.name)}

    @app.post("/api/routines/{name}/run")
    async def run_routine(name: str) -> dict[str, Any]:
        routine = database.get_routine(name)
        if routine is None:
            raise HTTPException(status_code=404, detail="Routine not found")
        trace = await execute_routine(tools.roku_client_for(routine["device_id"]), routine["actions"])
        return {
            "ok": True,
            "trace": trace,
            "verification": "Roku accepted the commands; in-app screen state is not observable.",
        }

    @app.post("/api/watch-history")
    async def record_watch(body: WatchRequest) -> dict[str, bool]:
        database.record_watch(**body.model_dump())
        return {"ok": True}

    @app.get("/api/watch-history/{profile}")
    async def watch_history(
        profile: str, provider: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        return {
            "history": database.find_watch_history(profile, provider, title),
            "complete": False,
            "source": "local user-maintained ledger",
        }

    @app.get("/api/subscriptions")
    async def subscriptions() -> dict[str, Any]:
        return {"subscriptions": database.list_subscriptions(enabled_only=True)}

    @app.put("/api/subscriptions")
    async def set_subscriptions(body: SubscriptionRequest) -> dict[str, Any]:
        providers: list[tuple[str, str]] = []
        for name in body.providers:
            provider = canonical_provider(name)
            display_name = PROVIDER_DISPLAY_NAMES.get(provider, name.strip())
            providers.append((provider, display_name))
        database.replace_subscriptions(providers)
        return {"subscriptions": database.list_subscriptions(enabled_only=True)}

    @app.get("/api/catalog/search")
    async def catalog_search(q: str) -> dict[str, Any]:
        if not q.strip() or len(q) > 300:
            raise HTTPException(status_code=400, detail="Search query is empty or too long")
        return await playback.search(q.strip())

    @app.get("/api/content-links")
    async def content_links() -> dict[str, Any]:
        return {"links": database.list_content_links()}

    @app.get("/api/taste/{profile}")
    async def taste_memory(profile: str) -> dict[str, Any]:
        return recommendations.memory(profile)

    @app.put("/api/taste")
    async def taste_feedback(body: PreferenceRequest) -> dict[str, Any]:
        return recommendations.record_feedback(**body.model_dump())

    @app.post("/api/recommendations")
    async def recommend_content(body: RecommendationRequest) -> dict[str, Any]:
        return await recommendations.recommend(**body.model_dump())

    @app.post("/api/content-links")
    async def save_content_link(body: ContentLinkRequest) -> dict[str, Any]:
        provider = canonical_provider(body.provider)
        return {
            "link": database.save_content_link(
                **body.model_dump(exclude={"provider"}), provider=provider
            )
        }

    @app.get("/api/provider-resolvers")
    async def provider_resolvers() -> dict[str, Any]:
        return {"resolvers": database.list_provider_resolvers()}

    @app.put("/api/provider-resolvers/{provider}")
    async def verify_provider_resolver(
        provider: str, body: ResolverVerificationRequest
    ) -> dict[str, Any]:
        normalized = canonical_provider(provider)
        if normalized not in {item["provider"] for item in database.list_subscriptions()}:
            raise HTTPException(status_code=400, detail="Provider is not an enabled subscription")
        return {
            "resolver": database.set_provider_resolver_verified(
                normalized, body.verified, body.notes
            )
        }

    @app.post("/api/content/plan")
    async def plan_content(body: ContentPlanRequest) -> dict[str, Any]:
        return await playback.plan(
            body.title,
            body.provider,
            body.media_type,
            body.season,
            body.episode,
        )

    @app.post("/api/content/play")
    async def play_content(body: ContentPlayRequest) -> dict[str, Any]:
        return await playback.play(**body.model_dump())

    @app.get("/api/recommendations/log")
    async def recommendations_log() -> dict[str, Any]:
        """Every recommendation ever surfaced, with its status and timestamps."""
        profile = app_config.household_profile
        return {
            "profile": profile,
            "recommendations": database.list_recommendations(profile, limit=200),
        }

    @app.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        try:
            result = await agent.chat(body.message, body.conversation_id)
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return asdict(result)

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatRequest) -> StreamingResponse:
        """Newline-delimited JSON events, so the reply can be shown and
        spoken sentence by sentence instead of after the whole turn.

        The turn runs as its own task: if the client walks away (an
        interrupted stream), the turn still finishes and its outcome lands
        in history — so device actions are recorded once, never retried.
        Only /api/chat/cancel stops it mid-flight.
        """
        conversation_id = body.conversation_id or database.new_conversation_id()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def sse(event: dict[str, Any]) -> str:
            return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        async def run_turn() -> None:
            try:
                queue.put_nowait(sse({"type": "meta", "conversation_id": conversation_id}))
                async for event in agent.chat_stream(
                    body.message,
                    conversation_id,
                    spoken=body.mode == "voice",
                    typed_by=known_person(body.person) if body.mode == "text" else None,
                ):
                    queue.put_nowait(sse(event))
            except (LLMError, ValueError) as exc:
                queue.put_nowait(sse({"type": "error", "detail": str(exc)}))
            except asyncio.CancelledError:
                queue.put_nowait(sse({"type": "error", "detail": "Turn cancelled."}))
                raise
            except Exception as exc:  # noqa: BLE001 - never end a turn in silence
                # Anything unexpected used to kill the task quietly: the
                # stream just closed, nothing was spoken, nothing was logged.
                logging.getLogger(__name__).exception("chat turn failed: %s", exc)
                queue.put_nowait(sse({"type": "error", "detail": f"Something went wrong on my end ({type(exc).__name__}). Try that again?"}))
            finally:
                queue.put_nowait(None)
                if active_turns.get(conversation_id) is turn_task:
                    active_turns.pop(conversation_id, None)

        turn_task = asyncio.create_task(run_turn())
        active_turns[conversation_id] = turn_task

        async def events() -> Any:
            # Proxies (ngrok among them) buffer unknown streaming formats
            # but pass Server-Sent Events through live; the 2KB comment
            # preamble also pushes WebKit past its small-chunk hold-back.
            yield ":" + " " * 2048 + "\n\n"
            while True:
                try:
                    # A quiet stretch (a slow model round, a tool mid-blink)
                    # still sends heartbeats, so the client can tell a busy
                    # turn from a dead one and give up on the latter.
                    item = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": working\n\n"
                    continue
                if item is None:
                    return
                yield item

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/chat/cancel")
    async def cancel_chat(body: ChatCancelRequest) -> dict[str, Any]:
        """Explicit stop for a running turn — the user said "no, wait"."""
        task = active_turns.pop(body.conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}

    @app.post("/api/transcribe")
    async def transcribe_only(
        audio: Annotated[UploadFile, File()],
        conversation_id: Annotated[str | None, Form(max_length=100)] = None,
    ) -> dict[str, Any]:
        """Transcribe without running the agent, so the client can show the
        user's own words immediately instead of after the whole turn. The same
        audio feeds speaker identification for the conversation, in parallel."""
        payload = await audio.read()
        started = time.monotonic()

        async def identify() -> dict[str, Any] | None:
            if not conversation_id or not voices.available:
                return None
            try:
                return await voices.observe(conversation_id, payload)
            except VoiceError as exc:
                logging.getLogger(__name__).warning("voice identification failed: %s", exc)
                return None

        try:
            transcript, speaker = await asyncio.gather(
                speech.transcribe(
                    payload,
                    audio.filename or "recording.webm",
                    audio.content_type or "application/octet-stream",
                ),
                identify(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SpeechError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not transcript:
            timing_log.info("TIMING transcribe: no speech in %d audio bytes", len(payload))
        timing_log.info(
            "TIMING transcribe: %.2fs (%d audio bytes -> %d chars, speaker %s)",
            time.monotonic() - started,
            len(payload),
            len(transcript),
            (speaker or {}).get("status", "off"),
        )
        return {"transcript": transcript, "speaker": speaker}

    @app.get("/api/voice/speaker")
    async def voice_speaker(conversation_id: str) -> dict[str, Any]:
        return {"speaker": voices.speaker(conversation_id)}

    @app.get("/api/voice/profiles")
    async def voice_profiles() -> dict[str, Any]:
        return {"enabled": voices.available, "profiles": voices.profiles()}

    @app.delete("/api/voice/profiles/{profile}")
    async def voice_forget(profile: str) -> dict[str, Any]:
        if not voices.forget(profile):
            raise HTTPException(status_code=404, detail="No voiceprint for that profile")
        return {"ok": True}

    @app.post("/api/voice")
    async def voice(
        audio: Annotated[UploadFile, File()],
        conversation_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        try:
            transcript = await speech.transcribe(
                await audio.read(),
                audio.filename or "recording.webm",
                audio.content_type or "application/octet-stream",
            )
            if not transcript:
                raise HTTPException(status_code=422, detail="No speech was heard")
            result = await agent.chat(transcript, conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (SpeechError, LLMError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"transcript": transcript, **asdict(result)}

    @app.post("/api/speech")
    async def synthesize_speech(body: SpeechSynthesisRequest) -> Response:
        started = time.monotonic()
        try:
            audio, content_type = await text_to_speech.synthesize(body.text, body.voice)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TextToSpeechError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        timing_log.info(
            "TIMING tts: %.2fs (%d chars -> %d bytes) %r",
            time.monotonic() - started,
            len(body.text),
            len(audio),
            body.text[:40],
        )
        return Response(
            content=audio,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


def _json_error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


app = create_app()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    config = load_config()
    ssl: dict[str, Any] = {}
    cert, key = config.server.ssl_certfile, config.server.ssl_keyfile
    if cert and key:
        if cert.is_file() and key.is_file():
            ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        else:
            # A fresh install before scripts/make-cert.sh: serve plain HTTP
            # rather than refuse to start. Phones need HTTPS for the mic.
            logging.getLogger(__name__).warning(
                "No certificate at %s; serving plain HTTP. Run scripts/make-cert.sh for HTTPS.", cert
            )
    scheme = "https" if ssl else "http"
    where = config.server.public_host if config.server.host in {"0.0.0.0", "::"} else config.server.host
    logging.getLogger(__name__).info("Friday is at %s://%s:%s", scheme, where, config.server.port)
    uvicorn.run(
        "house_agent.api:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
        # A browser keeps /api/events open for good; without this uvicorn
        # waits for it forever and SIGTERM never finishes.
        timeout_graceful_shutdown=5,
        **ssl,
    )
