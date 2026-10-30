from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .catalog import CatalogError
from .keys import KEYED_FEATURES, MissingKey, web_fallback_enabled
from .config import AppConfig, TVDeviceConfig
from .content import ContentError, PlaybackService
from .db import Database
from .roku import RokuClient
from .recipes import RecipeError, get_recipe, search_recipes
from .recommendations import RecommendationService
from .routines import execute_routine
from .scheduler import (
    VAGUE_TIMES,
    assumed_time,
    FadeRunner,
    ScheduleError,
    parse_when,
    describe_action,
    describe_schedule,
    local_now,
    next_run,
    normalize_action,
    normalize_schedule,
    weather_relevant,
)
from .smarthome import SmartHomeError, SmartHomeHub
from .voice import VoiceIdentity
from .directions import DirectionsError, DirectionsService
from .travel import TravelError, TravelNotConfigured, TravelService
from .doordash import DoorDashError, DoorDashNotConfigured, DoorDashNotSignedIn, DoorDashService, mentioned
from .routing import domain_of_tool
from .settings import HouseSettings, SettingsError, clean_host, clean_port
from .weather import HomeLocationUnknown, WeatherError, WeatherService
from .tv import CANONICAL_KEYS, TVError, TVUnsupported
from .tv.base import MEDIA_STATUS, VOLUME, WAKE_ON_LAN
from .tv.wol import wake as wake_on_lan
from .tv import client_for as tv_client_for
from .tv import discover as tv_discover
from .tv.registry import PLATFORMS as TV_PLATFORMS
from .websearch import WebSearchError, fetch_page, web_search

# Who is talking in the turn being processed. Set per turn by the agent;
# contextvars keep concurrent conversations from seeing each other's speaker.
CURRENT_SPEAKER: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "house_agent_speaker", default=None
)
# The user's words for the turn being processed. DoorDash's CLI asks for the
# request behind every call it makes, verbatim, as its --intent.
CURRENT_REQUEST: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "house_agent_request", default=None
)


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

log = logging.getLogger(__name__)

# What small models pass as a device when they mean "the light we are setting
# up": tool-result key names, schema hints, and plain deixis. All of them
# resolve deterministically instead of erroring back and forth. LAST_WORDS
# mean the light most recently flashed; NEXT_WORDS mean the first light
# still waiting for a name.
LAST_WORDS = {
    "last", "last flashed", "flashed", "the flashed light",
    "it", "this", "this one", "that one",
}
NEXT_WORDS = {
    "setup_pending", "pending", "next", "new", "new light", "first",
    "unnamed", "unnamed light",
}
SETUP_PLACEHOLDERS = LAST_WORDS | NEXT_WORDS


# "turn off all the lights", "every light", "lights off everywhere". A small
# model sometimes narrows that to whatever room the chat was just about, so
# the user's own words decide instead. "all the lights in the bedroom" is
# still the bedroom: a named room in the request keeps its scope.
WHOLE_HOUSE = re.compile(
    r"\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+|my\s+|our\s+)?(?:lights?|lamps?|bulbs?)\b"
    r"|\blights?\s+(?:off|on|out)\s+everywhere\b|\b(?:whole|entire)\s+house\b"
    r"|\btodas?\s+las\s+luces\b",
    re.IGNORECASE,
)


# Titles that are really "whatever was on": my show, the movie, it, that.
VAGUE_TITLE = re.compile(
    r"(?:(?:my|the|our|that|this)\s+)?(?:show|movie|film|program|programme|episode|series|thing)"
    r"|it|that|this|what i was watching|what we were watching",
)


def _join_names(names: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


class ToolError(RuntimeError):
    pass


@dataclasses.dataclass
class FoodSession:
    """What a chat has done on DoorDash so far, kept only in memory.

    The saved trace deliberately drops everything DoorDash returned (its
    terms forbid keeping it), and the model only ever sees its own earlier
    prose again — so by the next turn it has forgotten which stores it found
    and which cart it built, and starts over. This is the working memory
    that lets "now from McDonald's" land on the McDonald's it already saw.
    Gone when the process exits; never written to disk."""

    updated: float = 0.0
    near: str | None = None
    query: str | None = None
    stores: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    store: tuple[str, str] | None = None
    cart_uuid: str | None = None
    cart_store: str | None = None
    cart_items: list[str] = dataclasses.field(default_factory=list)
    total: str | None = None
    card: str | None = None
    tip: str | None = None
    order: str | None = None

    TTL = 3 * 3600.0

    def line(self, now: float | None = None) -> str | None:
        if (now or time.monotonic()) - self.updated > self.TTL:
            return None
        parts: list[str] = []
        if self.query and self.stores:
            listed = "; ".join(f"{name} (store_id {sid})" for sid, name in self.stores[:6])
            parts.append(f"last search '{self.query}' near {self.near or 'home'} found: {listed}")
        if self.store:
            parts.append(f"store in play: {self.store[1]} (store_id {self.store[0]})")
        if self.cart_uuid:
            items = ", ".join(self.cart_items[:8]) or "empty"
            cart = f"cart at {self.cart_store or 'that store'}: {items}"
            if self.total:
                cart += f"; already priced at {self.total}"
                if self.card:
                    cart += f" on {self.card}"
                if self.tip:
                    cart += f", suggested tip {self.tip}"
                cart += (
                    ". The house holds this cart's id: leave cart_uuid out of every doordash call. "
                    "If the user now says yes, go ahead, or agrees to the tip, that is the go-ahead: "
                    "call doordash_order_place with confirmed true and tip 'suggested' (or the "
                    "amount they named) — nothing has to be previewed first"
                )
            parts.append(cart)
        if self.order:
            parts.append(f"order sent: {self.order}")
        if not parts:
            return None
        return (
            "DoorDash so far in this chat — " + ". ".join(parts) + ". Continue from here: "
            "pass these ids to the doordash tools instead of searching again, and never say "
            "an id aloud. A new dish from the store in play is doordash_order with its store_id."
        )


class ToolRegistry:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.playback = PlaybackService(config, database)
        self.recommendations = RecommendationService(config, database)
        self.voices = VoiceIdentity(config.voice, database)
        self.smarthome = SmartHomeHub(config, database)
        self.fades = FadeRunner(self.smarthome, self.database)
        self.weather = WeatherService(config.weather, database)
        self.directions = DirectionsService(config.directions, self.weather.home)
        self.travel = TravelService(config.travel)
        self.doordash = DoorDashService(config.doordash, home=self.weather.home)
        self.settings = HouseSettings(config.config_path)
        self._food: dict[str, FoodSession] = {}
        # conversation_id -> {domain: last time a tool of it ran}, so the
        # tool groups a chat has been using stay offered to the model.
        self._domains_used: dict[str, dict[str, float]] = {}
        # Orders placed this session, watched until they land so the house
        # can say when the store confirms and when the Dasher is close.
        self._order_watchers: dict[str, asyncio.Task[None]] = {}
        self.order_watch_poll = 15.0  # while DoorDash is still processing it
        self.order_watch_slow = 300.0  # once placed, until it arrives
        self.order_watch_seconds = 3 * 3600.0
        self.devices = {device.id: device for device in config.tv_devices}
        # Android TV keeps its pairing socket open between begin and complete,
        # so the client that started an exchange has to finish it.
        self._pairing: dict[str, Any] = {}
        self._zigbee_windows: set[str] = set()  # conversations that opened a join window
        # Set by the app after startup; lets background work (the zigbee join
        # watcher) speak to every connected client unprompted.
        self.announcer: Any | None = None
        # The light most recently flashed, per conversation: what "it" means
        # when the user answers with a room and a name.
        self._last_flashed: dict[str, str] = {}
        self._renames = 0  # counts successful renames, so watchers can tell
        self._join_watchers: dict[str, asyncio.Task[None]] = {}
        self.join_watch_seconds = 200.0  # the 3-minute window, plus slack
        self.join_watch_poll = 3.0
        self._handlers: dict[str, ToolHandler] = {
            "tv_list_devices": self._list_devices,
            "tv_get_status": self._get_status,
            "tv_list_apps": self._list_apps,
            "tv_press": self._press,
            "tv_switch_input": self._switch_input,
            "tv_set_volume": self._set_volume,
            "tv_type_text": self._type_text,
            "tv_launch_app": self._launch_app,
            "tv_deep_link": self._deep_link,
            "tv_discover": self._tv_discover,
            "tv_pair": self._tv_pair,
            "tv_list_users": self._list_users,
            "tv_switch_user": self._switch_user,
            "tv_list_routines": self._list_routines,
            "tv_run_routine": self._run_routine,
            "watch_history_find": self._watch_history_find,
            "watch_history_record": self._watch_history_record,
            "household_list_subscriptions": self._list_subscriptions,
            "catalog_search": self._catalog_search,
            "taste_memory_get": self._taste_memory,
            "taste_feedback": self._taste_feedback,
            "content_recommend": self._recommend,
            "content_details": self._content_details,
            "content_episodes": self._content_episodes,
            "web_search": self._web_search,
            "web_fetch": self._web_fetch,
            "weather_now": self._weather_now,
            "weather_forecast": self._weather_forecast,
            "directions_travel_time": self._travel_time,
            "weather_set_home": self._weather_set_home,
            "flight_search": self._flight_search,
            "hotel_search": self._hotel_search,
            "content_plan_playback": self._content_plan,
            "content_play": self._content_play,
            "voice_enroll": self._voice_enroll,
            "voice_stay_anonymous": self._voice_stay_anonymous,
            "profile_note_add": self._profile_note_add,
            "style_preference_add": self._style_preference_add,
            "style_preference_remove": self._style_preference_remove,
            "profile_note_remove": self._profile_note_remove,
            "household_setup": self._household_setup,
            "device_list": self._device_list,
            "device_set": self._device_set,
            "device_flash": self._device_flash,
            "device_pair": self._device_pair,
            "device_rename": self._device_rename,
            "device_remove": self._device_remove,
            "reminder_add": self._reminder_add,
            "light_schedule_add": self._light_schedule_add,
            "light_fade": self._light_fade,
            "automation_list": self._automation_list,
            "automation_delete": self._automation_delete,
            "list_add": self._list_add,
            "list_show": self._list_show,
            "list_remove": self._list_remove,
            "list_clear": self._list_clear,
            "recipe_search": self._recipe_search,
            "recipe_get": self._recipe_get,
            "recipe_save": self._recipe_save,
            "recipe_list": self._recipe_list,
            "recipe_forget": self._recipe_forget,
            "calendar_add": self._calendar_add,
            "calendar_events": self._calendar_events,
            "calendar_update": self._calendar_update,
            "calendar_delete": self._calendar_delete,
            "weather_at": self._weather_at,
            "calendar_setup_help": self._calendar_setup_help,
            "inbox_send": self._inbox_send,
            "house_config_show": self._house_config_show,
            "house_config_set": self._house_config_set,
            "doordash_login": self._dd_login,
            "doordash_search": self._dd_search,
            "doordash_order": self._dd_order,
            "doordash_store_info": self._dd_store_info,
            "doordash_menu": self._dd_menu,
            "doordash_item_details": self._dd_item_details,
            "doordash_cart_add": self._dd_cart_add,
            "doordash_cart_show": self._dd_cart_show,
            "doordash_cart_remove": self._dd_cart_remove,
            "doordash_cart_delete": self._dd_cart_delete,
            "doordash_grocery_list": self._dd_grocery_list,
            "doordash_order_history": self._dd_order_history,
            "doordash_reorder": self._dd_reorder,
            "doordash_order_preview": self._dd_order_preview,
            "doordash_order_place": self._dd_order_place,
            "doordash_order_status": self._dd_order_status,
            "doordash_checkout_link": self._dd_checkout_link,
            "doordash_receipt": self._dd_receipt,
            "doordash_account": self._dd_account,
            "doordash_promo": self._dd_promo,
            "doordash_favorite": self._dd_favorite,
        }
        # Superseded names from when this only spoke Roku. Still dispatched, so
        # a model working from older context never hits an unknown tool, but
        # deliberately absent from the advertised schema list.
        self._handlers |= {
            f"roku_{suffix}": self._handlers[f"tv_{suffix}"]
            for suffix in (
                "list_devices",
                "get_status",
                "list_apps",
                "press",
                "switch_input",
                "type_text",
                "launch_app",
                "deep_link",
                "list_routines",
                "run_routine",
            )
        }
        self._profile_tools = {
            schema["function"]["name"]
            for schema in self.schemas
            if "profile" in schema["function"]["parameters"]["properties"]
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        episode_guidance = (
            " Exact season-and-episode playback is enabled."
            if self.config.catalog.exact_episode_playback_enabled
            else " Exact season-and-episode playback is disabled; do not provide season or episode."
        )
        device_property = {
            "type": "string",
            "description": "Configured device ID, such as living-room. List devices when uncertain.",
        }
        return [
            _tool(
                "tv_list_devices",
                "List the configured televisions with their brand platform and what "
                "each one can do. Use this before acting if the target is ambiguous, "
                "or to check whether a TV supports apps, deep links, or typing.",
                {},
            ),
            _tool(
                "tv_get_status",
                "Read a TV's power state and the app in the foreground.",
                {"device_id": device_property},
                ["device_id"],
            ),
            _tool(
                "tv_list_apps",
                "List the apps available on a TV, with the IDs used to launch them.",
                {"device_id": device_property},
                ["device_id"],
            ),
            _tool(
                "tv_press",
                "Press a remote key one or more times. The same key words work on "
                "every brand. Prefer launching an app or a deep link over walking "
                "the menus with arrow keys.",
                {
                    "device_id": device_property,
                    "key": {
                        "type": "string",
                        "enum": list(CANONICAL_KEYS),
                        "description": "Remote key. Not every TV has every key; the tool says so if it is missing.",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 1,
                    },
                },
                ["device_id", "key"],
            ),
            _tool(
                "tv_switch_input",
                "Switch the TV to a named console or video input. Use this for Xbox, "
                "Nintendo Switch, HDMI, the TV's own home screen, antenna, or live TV.",
                {
                    "device_id": device_property,
                    "input": {
                        "type": "string",
                        "description": "Configured name or direct input, such as xbox, switch, tv, live tv, HDMI 2, or HDMI 3.",
                    },
                },
                ["device_id", "input"],
            ),
            _tool(
                "tv_set_volume",
                "Set a TV's volume to a level out of 100, mute it, or unmute it. "
                "Only some TVs accept a level; for the rest use tv_press with "
                "volume up or volume down. The current level is in tv_get_status.",
                {
                    "device_id": device_property,
                    "level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Loudness as a percent.",
                    },
                    "mute": {"type": "boolean", "description": "True to mute, false to unmute."},
                },
                ["device_id"],
            ),
            _tool(
                "tv_type_text",
                "Type text into the TV's active text field. This cannot focus a field by itself.",
                {"device_id": device_property, "text": {"type": "string", "maxLength": 256}},
                ["device_id", "text"],
            ),
            _tool(
                "tv_launch_app",
                "Launch an app by its name or ID.",
                {
                    "device_id": device_property,
                    "app": {"type": "string", "description": "App name or ID from tv_list_apps."},
                },
                ["device_id", "app"],
            ),
            _tool(
                "tv_deep_link",
                "Launch content when an exact provider-specific content ID is already "
                "known. Never invent a content ID.",
                {
                    "device_id": device_property,
                    "app": {"type": "string", "description": "App name or ID."},
                    "content_id": {"type": "string"},
                    "media_type": {
                        "type": "string",
                        "enum": [
                            "movie",
                            "episode",
                            "season",
                            "series",
                            "shortFormVideo",
                            "special",
                            "live",
                        ],
                    },
                },
                ["device_id", "app", "content_id", "media_type"],
            ),
            _tool(
                "tv_list_users",
                "List the on-screen user accounts a TV itself keeps, for TVs that "
                "have them. These are the TV's own users, not household profiles.",
                {"device_id": device_property},
                ["device_id"],
            ),
            _tool(
                "tv_switch_user",
                "Switch which on-screen user a TV is signed in as. This is the TV's "
                "own user account, not a household profile, and only some TVs have "
                "them.",
                {
                    "device_id": device_property,
                    "user": {"type": "string", "description": "User name or ID from tv_list_users."},
                },
                ["device_id", "user"],
            ),
            _tool(
                "tv_discover",
                "Scan the local network for televisions of any supported brand. Use "
                "this when someone wants to add a TV and does not know its address.",
                {},
            ),
            _tool(
                "tv_pair",
                "Pair with a TV that needs permission before it accepts commands: LG, "
                "Samsung, Vizio, and Android or Google TV. Call with step 'begin' "
                "first and read the returned instruction to the user. LG and Samsung "
                "show an accept prompt on the TV; Vizio and Android TV show a code to "
                "read back. Then call step 'complete', passing that code when there "
                "was one. Roku needs no pairing.",
                {
                    "device_id": device_property,
                    "step": {"type": "string", "enum": ["begin", "complete"]},
                    "code": {
                        "type": "string",
                        "description": "The code shown on the TV screen. Only for step complete, and only when the TV showed one.",
                    },
                },
                ["device_id", "step"],
            ),
            _tool(
                "tv_list_routines",
                "List calibrated TV navigation routines. Routines are the only "
                "supported way to claim a profile or app menu was selected.",
                {},
            ),
            _tool(
                "tv_run_routine",
                "Run a named, user-calibrated TV navigation routine.",
                {"name": {"type": "string"}},
                ["name"],
            ),
            _tool(
                "device_list",
                "List smart home devices (lights, plugs, switches on Matter or Zigbee) "
                "with their name, room, online state, and current power, brightness, "
                "and color temperature.",
                {},
            ),
            _tool(
                "device_set",
                "Control a smart light, plug, or switch. Match the device by its name "
                "or room ('kitchen light'). Brightness is a percent. Color temperature "
                "is in kelvin: warm/cozy is about 2700, neutral 4000, cool/daylight 6500.",
                {
                    "device": {
                        "type": "string",
                        "description": "Device name, room, or id from device_list.",
                    },
                    "power": {"type": "string", "enum": ["on", "off", "toggle"]},
                    "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                    "color_temp": {
                        "type": "integer",
                        "minimum": 1500,
                        "maximum": 6500,
                        "description": "Kelvin. 2700 warm, 4000 neutral, 6500 cool daylight.",
                    },
                    "color": {
                        "type": "string",
                        "description": "A named color (red, blue, pink, ...) or hex "
                        "#rrggbb, for lights with the color capability.",
                    },
                },
                ["device"],
            ),
            _tool(
                "device_flash",
                "Blink a light off and on a few times so a person can see which "
                "physical device it is. Use it right after a new device pairs — "
                "before asking what to name it — so the user knows which light "
                "joined, and whenever someone asks which light is which.",
                {
                    "device": {
                        "type": "string",
                        "description": "Device name, room, or id from device_list. "
                        "For a light that has no name yet, pass the id from the "
                        "setup note, or the words 'last flashed' to flash the "
                        "same light again, or 'next' for the first unnamed one.",
                    },
                    "times": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                ["device"],
            ),
            _tool(
                "device_pair",
                "Add a new smart device. Matter devices (most WiFi bulbs and plugs) "
                "need the pairing code printed on the device, its box, or QR label — "
                "ask the user to read it out."
                + (
                    " This controller has Bluetooth, so a brand-new Matter device "
                    "pairs straight from the box with its code: pairing tries "
                    "Bluetooth first and, if that fails, looks for the device on "
                    "the home WiFi by itself — never ask for a vendor app unless "
                    "both have failed."
                    if self.config.matter.bluetooth
                    else " IMPORTANT: this home's controller has no Bluetooth, so a "
                    "brand-new Matter device must FIRST be set up in its vendor app "
                    "(for Linkind bulbs: the AiDot app) to get it onto the home WiFi. "
                    "Say that up front the moment someone wants to add a Matter "
                    "device, and confirm it is done before asking for the code."
                )
                + " The Matter pairing window only opens for 15 minutes after "
                "power-on: have the user cut power completely at the switch or plug "
                "— not the app — wait 10 seconds, power it back on, then pair right "
                "away. Zigbee opens a three-minute join window instead. Pairing can "
                "take a minute; tell the user you are working on it.",
                {
                    "network": {"type": "string", "enum": ["matter", "zigbee"]},
                    "code": {
                        "type": "string",
                        "maxLength": 120,
                        "description": "Matter pairing code: 11 or 21 digits, or the MT: QR "
                        "string, exactly as the user gave it. Only pass a code the user "
                        "actually said or typed in this conversation; never guess one.",
                    },
                },
                ["network"],
            ),
            _tool(
                "device_rename",
                "Give a smart device a friendly name or assign it to a room so people "
                "can refer to it naturally. If the name is already taken the tool "
                "says so: ask the user whether to reassign it, and only after they "
                "agree call again with replace true.",
                {
                    "device": {
                        "type": "string",
                        "description": "The device's id (from device_list or a pairing "
                        "result) or its CURRENT name — never the new name being given. "
                        "When naming a light that was just flashed, pass the words "
                        "'last flashed'.",
                    },
                    "name": {"type": "string", "maxLength": 60},
                    "room": {"type": "string", "maxLength": 60},
                    "replace": {
                        "type": "boolean",
                        "description": "Take the name from the device currently holding "
                        "it. Only after the user explicitly agreed.",
                    },
                },
                ["device"],
            ),
            _tool(
                "device_remove",
                "Unpair (remove, forget, delete) a smart device from this home so it "
                "can be paired again or given away. Matter devices are told to forget "
                "this controller and drop back into pairing mode. Only call this when "
                "the user clearly asks to remove or re-pair a specific device.",
                {"device": {"type": "string", "description": "Device name, room, or id."}},
                ["device"],
            ),
            _tool(
                "watch_history_find",
                "Read locally recorded watch history. An empty result means unknown, not definitely unwatched.",
                {
                    "profile": {"type": "string"},
                    "provider": {"type": "string"},
                    "title": {"type": "string"},
                },
                ["profile"],
            ),
            _tool(
                "watch_history_record",
                "Record viewing state only when the user explicitly states it or trusted playback telemetry confirms it.",
                {
                    "profile": {"type": "string"},
                    "provider": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["planned", "started", "watched", "abandoned"],
                    },
                    "season": {"type": "integer", "minimum": 1},
                    "episode": {"type": "integer", "minimum": 1},
                },
                ["profile", "provider", "title", "status"],
            ),
            _tool(
                "household_list_subscriptions",
                "List the household's enabled streaming subscriptions. Prefer these and avoid rentals unless asked.",
                {},
            ),
            _tool(
                "catalog_search",
                "Search movies and shows, filtered to household subscriptions and region. This does not play anything.",
                {"query": {"type": "string", "maxLength": 300}},
                ["query"],
            ),
            _tool(
                "taste_memory_get",
                "Read locally learned likes, dislikes, temporary deferrals, recent recommendations, and incomplete watch history for a household profile.",
                {"profile": {"type": "string"}},
            ),
            _tool(
                "taste_feedback",
                "Record the user's reaction to a title. liked = accepted, played, or praised. "
                "disliked = a firm rejection of the title itself (I don't like that, hate it, never) — long ban. "
                "soft_no = an unenthusiastic pass (ehh, idk, nah, something else) — retry in a few weeks. "
                "deferred = timing only (not right now, maybe later, not tonight) — retry in about a week. "
                "asked = the user asked about a suggested title but gave no verdict yet.",
                {
                    "title": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["liked", "disliked", "soft_no", "deferred", "asked"],
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "series", "unknown"],
                    },
                    "profile": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["title", "status"],
            ),
            _tool(
                "content_recommend",
                "Find well-rated movies or series available on household subscriptions while excluding learned dislikes, already selected titles, recent deferrals, and local watch history. “Show” may mean either movie or series.",
                {
                    "profile": {"type": "string"},
                    "media_type": {
                        "type": "string",
                        "enum": ["any", "movie", "series"],
                    },
                    "genres": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 5,
                    },
                    "year_min": {"type": "integer", "minimum": 1900, "maximum": 2100},
                    "year_max": {"type": "integer", "minimum": 1900, "maximum": 2100},
                    "user_rating_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "critic_score_min": {"type": "integer", "minimum": 0, "maximum": 100},
                    "sort_by": {
                        "type": "string",
                        "enum": [
                            "popularity_desc",
                            "release_date_desc",
                            "relevance_desc",
                        ],
                    },
                    "similar_to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": "Titles the user named as examples of what they want ('like Knives Out'). They steer results toward similar titles and are never returned as suggestions.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            ),
            _tool(
                "profile_note_add",
                "Remember one fact a person told you about themselves or their taste "
                "('loves slow-burn sci-fi', 'hates horror', 'works nights, watches late'), "
                "in their words. Profile is the person's name, or household for things "
                "everyone shares. Omit profile for the person speaking.",
                {
                    "profile": {"type": "string"},
                    "note": {"type": "string", "minLength": 3, "maxLength": 300},
                },
                ["note"],
            ),
            _tool(
                "style_preference_add",
                "Remember how someone wants you to reply, whenever they say so — "
                "'just the titles, skip the years', 'add the rating', 'be a bit "
                "warmer', 'keep it short'. Save it in their words, one preference "
                "per call, the moment they ask; it changes every later reply. "
                "Profile is the person's name, or household for how you speak to "
                "everyone. Omit profile for the person speaking. This is for how "
                "you talk, not what they like — tastes go to profile_note_add.",
                {
                    "profile": {"type": "string"},
                    "preference": {"type": "string", "minLength": 3, "maxLength": 200},
                },
                ["preference"],
            ),
            _tool(
                "style_preference_remove",
                "Drop one saved reply preference when the person changes their mind "
                "('actually, put the years back'), by the id shown in taste_memory_get.",
                {"id": {"type": "integer"}},
                ["id"],
            ),
            _tool(
                "profile_note_remove",
                "Forget one profile note that no longer holds, by the id shown in "
                "taste_memory_get.",
                {"note_id": {"type": "integer", "minimum": 1}},
                ["note_id"],
            ),
            _tool(
                "household_setup",
                "Record where first-time preferences setup stands: in_progress when they "
                "agree to do it now, deferred when they want to do it later, done when "
                "the flow is finished.",
                {"status": {"type": "string", "enum": ["in_progress", "deferred", "done"]}},
                ["status"],
            ),
            _tool(
                "voice_enroll",
                "Save the current speaker's voice under their name so they are recognized "
                "automatically from now on. Call only when they answer the name question "
                "or explicitly ask to be remembered. Pass favorite as well whenever they "
                "named a film or show they love in the same breath — it is saved to their "
                "taste profile here, because this call ends the turn and no later one "
                "will capture it.",
                {
                    "name": {"type": "string", "minLength": 1, "maxLength": 40},
                    "favorite": {"type": "string", "maxLength": 200},
                },
                ["name"],
            ),
            _tool(
                "voice_stay_anonymous",
                "The current speaker declined to give a name. Keeps this chat anonymous "
                "and stops any further asking.",
                {},
            ),
            _tool(
                "content_details",
                "Verified facts about one movie or series from the catalog: plot overview, "
                "cast, creators, genres, runtime, seasons, rating. Always use this — never "
                "memory — when asked what a title is about or who is in it.",
                {
                    "title": {"type": "string"},
                    "media_type": {
                        "type": "string",
                        "enum": ["any", "movie", "series"],
                    },
                },
                ["title"],
            ),
            _tool(
                "web_search",
                "Narrow web search returning result titles, URLs, and snippets. Use ONLY "
                "when catalog tools cannot answer: background a title's catalog entry "
                "lacks, or ideas when content_recommend returns nothing suitable. Snippets "
                "are unverified — check availability with catalog tools before offering "
                "to play anything found here.",
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                ["query"],
            ),
            _tool(
                "web_fetch",
                "Read one web page found by web_search and return its text. Search "
                "snippets are only 300 characters, so when the answer lives inside a "
                "page — the titles a list article names, the details a review gives — "
                "fetch that page instead of answering from the snippet. Never state a "
                "title, year, author, or narrator that is not in a result you fetched.",
                {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000},
                },
                ["url"],
            ),
            _tool(
                "content_episodes",
                "Which episodes a series has and which one is newest. Call this whenever someone asks for the newest, latest, or last episode, asks what the most recent one is, or names an episode by number — it gives the season, the episode number, its name and the day it aired, plus what is coming next. Episodes cannot be deep-linked on this catalog plan, so say which episode it is, play the series, and tell them to pick it in the app.",
                {
                    "title": {"type": "string", "description": "The series, in the user's words."},
                    "season": {"type": "integer", "minimum": 1, "description": "List this season's episodes. Omit for just the newest."},
                },
                ["title"],
            ),
            _tool(
                "content_plan_playback",
                "Resolve a title to a subscribed provider and grounded Roku content link without controlling the TV. Omit season and episode to resume a series."
                + episode_guidance,
                {
                    "title": {"type": "string"},
                    "provider": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "episode", "season", "series", "special", "live"]},
                    "season": {"type": "integer", "minimum": 1},
                    "episode": {"type": "integer", "minimum": 1},
                },
                ["title"],
            ),
            _tool(
                "content_play",
                "Play a resolved title on a Roku. Omit season and episode to resume a "
                "series. A provider's first play is supervised automatically: the "
                "result says first_play_on_provider — tell the user, and ask them to "
                "confirm the right title started; a confirmed start verifies the "
                "provider for the future."
                + episode_guidance,
                {
                    "device_id": device_property,
                    "title": {"type": "string"},
                    "provider": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["movie", "episode", "season", "series", "special", "live"]},
                    "season": {"type": "integer", "minimum": 1},
                    "episode": {"type": "integer", "minimum": 1},
                    "profile": {"type": "string"},
                    "allow_unverified": {
                        "type": "boolean",
                        "description": "True to run a first-time supervised play on a provider whose link pattern is not yet verified.",
                    },
                },
                ["device_id", "title"],
            ),
            _tool(
                "weather_now",
                "Current weather plus today's high, low, and any active severe "
                "weather alerts. Use this for how it is outside right now, "
                "whether to take a jacket, or whether it is raining.",
                {
                    "location": {
                        "type": "string",
                        "description": "Only for somewhere other than home, and only when the user named it in this request. Omit it for the house itself.",
                    }
                },
            ),
            _tool(
                "directions_travel_time",
                "How long it takes to get somewhere and how far it is. Use this "
                "for how long to reach a place, whether somewhere is far, or when "
                "to leave. Answer from the result, never from memory.",
                {
                    "destination": {
                        "type": "string",
                        "description": "Where they are going: an address, a business name, or a town.",
                    },
                    "origin": {
                        "type": "string",
                        "description": "Where they are starting from. Omit to start from the house.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["driving", "walking", "cycling"],
                        "default": "driving",
                    },
                },
                ["destination"],
            ),
            _tool(
                "weather_forecast",
                "Day-by-day forecast starting today: condition, high, low, and "
                "chance of rain. Use this for tomorrow, the weekend, or the week "
                "ahead. For right now, use weather_now instead.",
                {
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "default": 5,
                        "description": "How many days to return, counting today.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Only for somewhere other than home, and only when the user named it in this request. Omit it for the house itself.",
                    },
                },
            ),
            _tool(
                "flight_search",
                "Live flight fares between two airports on a date: the cheapest "
                "offers with airline, departure and arrival times, stops, duration, "
                "price per person, and a booking link. Use this for any question "
                "about flight prices or options; never quote a fare from memory. "
                "Takes up to a couple of minutes.",
                {
                    "origin": {
                        "type": "string",
                        "description": "Three-letter airport code, e.g. DEN. Convert a city name to its main airport yourself.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Three-letter airport code, e.g. LIS.",
                    },
                    "depart": {
                        "type": "string",
                        "description": "Departure date, YYYY-MM-DD, worked out from the Now line.",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Return date, YYYY-MM-DD. Omit for one way.",
                    },
                    "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
                    "cabin": {
                        "type": "string",
                        "enum": ["economy", "premium_economy", "business", "first"],
                    },
                    "max_stops": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "0 for nonstop only. Omit to allow any.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
                },
                ["origin", "destination", "depart"],
            ),
            _tool(
                "hotel_search",
                "Live hotel rates in a city for a stay: the best-rated bookable rooms "
                "within the budget, with hotel name, stars, guest rating, price for "
                "the whole stay and per night, free-cancellation deadline, and how "
                "far from the area asked for. Use this for any question about "
                "where to stay or what hotels cost; never quote a rate from memory. "
                "Takes about a minute.",
                {
                    "place": {
                        "type": "string",
                        "description": "City or town, e.g. Lisbon or Las Vegas.",
                    },
                    "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                    "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                    "adults": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
                    "max_price_per_night": {
                        "type": "number",
                        "description": "Only rooms at or under this per night. Omit for no ceiling.",
                    },
                    "near": {
                        "type": "string",
                        "description": "An area or landmark inside the city to stay within a walk of — the Strip, downtown, the convention center, Times Square. Only when the user names one.",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["best", "cheapest"],
                        "default": "best",
                        "description": "best ranks by guest rating and stars within the budget; cheapest only when the user asks for the cheapest.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
                },
                ["place", "check_in", "check_out"],
            ),
            _tool(
                "weather_set_home",
                "Save where this house is, so weather questions never need a "
                "location again. Call it when someone says they have moved or asks "
                "to change the weather location, and after asking because a weather "
                "tool reported needs_home_location. Not for a city mentioned while "
                "ordering food, planning a trip, or asking about somewhere else.",
                {
                    "location": {
                        "type": "string",
                        "description": "The town or city the household lives in, with state or country if they gave one.",
                    }
                },
                ["location"],
            ),
            _tool(
                "reminder_add",
                "Set a reminder ('remind me to…') or a scheduled check. Work the exact time out from the Now line: 'tomorrow morning' is 08:00, 'tonight' 20:00, 'this afternoon' 15:00. kind say reads the text aloud then; kind ask makes the text a question you answer with tools at that time (e.g. rain before an event). Confirm the time back in plain words.",
                {
                    "text": {
                        "type": "string",
                        "maxLength": 500,
                        "description": "For say: what to announce, as a sentence addressed to the person ('Call your grandma'). For ask: the question to answer at that time.",
                    },
                    "when": {
                        "type": "string",
                        "description": "'YYYY-MM-DD HH:MM' in 24-hour local time. For repeating reminders just 'HH:MM'.",
                    },
                    "repeat": {
                        "type": "string",
                        "enum": ["once", "daily", "weekdays", "weekends", "weekly"],
                        "default": "once",
                    },
                    "days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                        "description": "Only for repeat weekly.",
                    },
                    "kind": {"type": "string", "enum": ["say", "ask"], "default": "say"},
                    "profile": {"type": "string", "description": "Who the reminder is for. Filled in automatically for the speaker."},
                },
                ["text", "when"],
            ),
            _tool(
                "light_schedule_add",
                "A light that changes by itself on a schedule: a sunrise ('brighten the bedroom from 7:30' = brightness_from 0 to 100 over ~20 minutes), a wind-down ('fade from 10 till lights out at 11' = brightness_to 0 over 60 minutes at 22:00), or a plain on/off (minutes 0). Ending at 0 turns it off. Confirm the schedule in plain words.",
                {
                    "name": {"type": "string", "maxLength": 60, "description": "Short label, e.g. 'bedroom sunrise'."},
                    "device": {"type": "string", "description": "Device name or room, as for device_set."},
                    "when": {"type": "string", "description": "The time exactly as the person said it — '6:20pm', '7 am', 'noon' — or HH:MM 24-hour. Never convert am/pm to 24-hour yourself. "},
                    "repeat": {
                        "type": "string",
                        "enum": ["daily", "weekdays", "weekends", "weekly", "once"],
                        "default": "daily",
                    },
                    "days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                        "description": "Only for repeat weekly.",
                    },
                    "brightness_to": {"type": "integer", "minimum": 0, "maximum": 100},
                    "brightness_from": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Omit to start from wherever the light is at that moment.",
                    },
                    "minutes": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 720,
                        "default": 0,
                        "description": "How long the change takes. 0 changes it at once.",
                    },
                    "color_temp_from": {"type": "integer", "minimum": 1500, "maximum": 6500},
                    "color_temp_to": {
                        "type": "integer",
                        "minimum": 1500,
                        "maximum": 6500,
                        "description": "Kelvin. A sunrise can warm-to-cool (2200 to 4000); a wind-down cool-to-warm.",
                    },
                },
                ["name", "device", "when", "brightness_to"],
            ),
            _tool(
                "light_fade",
                "Change a light gradually starting now ('dim the kitchen over ten minutes'). Runs in the background; ending at 0 turns it off.",
                {
                    "device": {"type": "string"},
                    "brightness_to": {"type": "integer", "minimum": 0, "maximum": 100},
                    "minutes": {"type": "number", "minimum": 0, "maximum": 720},
                    "brightness_from": {"type": "integer", "minimum": 0, "maximum": 100},
                    "color_temp_to": {"type": "integer", "minimum": 1500, "maximum": 6500},
                },
                ["device", "brightness_to", "minutes"],
            ),
            _tool(
                "automation_list",
                "Everything scheduled — reminders, light schedules, checks — and when each next runs.",
                {},
            ),
            _tool(
                "automation_delete",
                "Cancel a reminder or light schedule by id from automation_list.",
                {"id": {"type": "integer"}},
                ["id"],
            ),
            _tool(
                "list_add",
                "Put items on a household list. 'I need eggs', 'we're out of milk', 'add coffee' mean the shopping list; to-dos go on todo. Reply with a short confirmation.",
                {
                    "items": {"type": "array", "items": {"type": "string", "maxLength": 120}, "minItems": 1, "maxItems": 40},
                    "list": {"type": "string", "default": "shopping", "description": "shopping, todo, or any other list name."},
                },
                ["items"],
            ),
            _tool(
                "list_show",
                "Read a list. Omit the name to see every list.",
                {"list": {"type": "string"}},
            ),
            _tool(
                "list_remove",
                "Take items off a list (bought, done, or a mistake); matching is forgiving ('eggs' clears 'a dozen eggs').",
                {
                    "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "list": {"type": "string", "default": "shopping"},
                },
                ["items"],
            ),
            _tool(
                "list_clear",
                "Empty a list, only when asked to clear it.",
                {"list": {"type": "string", "default": "shopping"}},
            ),
            _tool(
                "recipe_search",
                "Find recipes on the web as structured data: ingredients, minutes, servings, rating, calories, protein. Put the dish and every constraint in the query ('fluffy chocolate cake light not-too-sweet frosting', 'high protein dinner'). best true when they named a specific dish: returns the single best match with full steps to read out at once. best false for open-ended asks (dinner ideas, something light and easy): returns two or three summaries to offer. Never invent ingredients or steps.",
                {
                    "query": {"type": "string", "maxLength": 200},
                    "best": {
                        "type": "boolean",
                        "description": "true for a specific dish (one recipe, with steps); false for open-ended requests (a few choices).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 4, "default": 3},
                },
                ["query"],
            ),
            _tool(
                "recipe_get",
                "Full ingredients and steps of one recipe by url from recipe_search or recipe_list.",
                {"url": {"type": "string"}},
                ["url"],
            ),
            _tool(
                "recipe_save",
                "Keep a recipe ('save that one').",
                {"url": {"type": "string"}},
                ["url"],
            ),
            _tool(
                "recipe_list",
                "Saved recipes, optionally filtered by a word in the title or ingredients.",
                {"query": {"type": "string"}},
            ),
            _tool(
                "recipe_forget",
                "Delete a saved recipe by id from recipe_list.",
                {"id": {"type": "integer"}},
                ["id"],
            ),
            _tool(
                "calendar_add",
                "Remember a plan someone is going to at a day and time (meeting, party, concert, appointment, flight, game) or asks to put on the calendar. Not for 'remind me to…' (reminder_add). Date from the Now line. Outdoor plans and travel get an automatic weather check an hour before; calls and indoor tasks do not — mention a check only if the result has weather_heads_up_at. Confirm with day and time.",
                {
                    "title": {"type": "string", "maxLength": 200, "description": "Short, e.g. 'Ana's birthday party'."},
                    "when": {"type": "string", "description": "The day and time exactly as the person said them — 'next Monday 9am', 'tomorrow at noon', 'Friday 6:20pm', 'tonight'. Pass their words through; do not work the date out yourself and do not convert am/pm to 24-hour. 'YYYY-MM-DD HH:MM' also works when they gave an actual date."},
                    "ends": {"type": "string", "description": "'YYYY-MM-DD HH:MM' or 'HH:MM' if they gave an end time."},
                    "location": {"type": "string", "maxLength": 200},
                    "notes": {"type": "string", "maxLength": 1000},
                    "weather_heads_up": {
                        "type": "boolean",
                        "description": "Omit to decide from the plan itself. Pass true or false only when the user asked for or declined a weather check.",
                    },
                    "profile": {"type": "string", "description": "Whose plan it is. Filled in automatically for the speaker."},
                },
                ["title", "when"],
            ),
            _tool(
                "calendar_events",
                "What is on the calendar today, the next few days, or one date; also the next meeting and what day something falls on. Never answer plans from memory.",
                {
                    "days": {"type": "integer", "minimum": 1, "maximum": 60, "default": 2, "description": "How many days from today, counting today."},
                    "date": {"type": "string", "description": "'YYYY-MM-DD' to see one day only."},
                },
            ),
            _tool(
                "calendar_delete",
                "Remove a plan: pass title to match it by name, or id from calendar_events. Use this when someone says to cancel, drop, or remove something, and to clear up a plan you got wrong. Plans whose calendar is not friday come from a subscribed calendar and can only be removed there.",
                {
                    "title": {"type": "string", "description": "Enough of the plan's name to find it, e.g. 'mexico ticket'."},
                    "id": {"type": ["integer", "string"], "description": "From calendar_events; only needed when a title matches more than one."},
                },
            ),
            _tool(
                "calendar_update",
                "Change a plan already on the calendar: move it to another day or time, or rename it. This is how a wrong day is fixed — never delete and re-add, and never leave a wrong plan and add a second one. Identify it by title or by id from calendar_events.",
                {
                    "title": {"type": "string", "description": "Enough of the plan's current name to find it."},
                    "id": {"type": ["integer", "string"], "description": "From calendar_events; only needed when a title matches more than one."},
                    "when": {"type": "string", "description": "The new day and time in the person's own words — 'next Monday', 'tomorrow at noon', 'Friday 7pm' — or 'YYYY-MM-DD HH:MM'."},
                    "ends": {"type": "string"},
                    "new_title": {"type": "string", "maxLength": 200, "description": "Only to rename it."},
                    "location": {"type": "string", "maxLength": 200},
                    "notes": {"type": "string", "maxLength": 1000},
                },
            ),
            _tool(
                "calendar_setup_help",
                "Step-by-step instructions for connecting calendars: getting Friday's plans and alerts onto a phone or computer, or letting Friday read a Google or iCloud calendar. Call it whenever someone asks how to set up, sync, push, or get alerts for the calendar, or why plans are not showing on their phone. Without app it tells you what to ask first.",
                {
                    "app": {
                        "type": "string",
                        "enum": ["apple", "google", "mac", "outlook"],
                        "description": "Which calendar they use: apple (iPhone/iPad), google (Android or Google Calendar), mac, or outlook. Omit until they have said.",
                    },
                    "goal": {
                        "type": "string",
                        "enum": ["see_friday_on_my_device", "let_friday_read_my_calendar"],
                        "default": "see_friday_on_my_device",
                    },
                },
            ),
            _tool(
                "weather_at",
                "Expected conditions at a given hour up to a week ahead, with concerns picked out (rain chance, storms, snow, heat, cold, wind). Use for a plan's time or before advising what to wear or bring.",
                {
                    "when": {"type": "string", "description": "'YYYY-MM-DD HH:MM', or 'HH:MM' for the next time that hour comes."},
                    "location": {
                        "type": "string",
                        "description": "Only for somewhere other than home, and only when the user named it.",
                    },
                },
                ["when"],
            ),
            _tool(
                "inbox_send",
                "Put something on the user's screen, in the messages panel behind the envelope icon, instead of saying it: a link (booking, checkout, sign-in, a calendar address), step-by-step instructions, a code, an address — anything useless read aloud. One-way; they cannot reply there. Then say in a few words that it is in their messages.",
                {
                    "title": {"type": "string", "maxLength": 120, "description": "Short heading, e.g. 'Hotel booking link' or 'Calendar setup steps'."},
                    "text": {"type": "string", "maxLength": 4000, "description": "The instructions or note, plain text; number steps on separate lines."},
                    "url": {"type": "string", "description": "A full http(s) link, exactly as a tool returned it."},
                    "kind": {"type": "string", "enum": ["link", "instructions", "note"], "description": "Defaults to link when a url is given."},
                },
                ["title"],
            ),
            _tool(
                "house_config_show",
                "What this house has saved about itself: each TV's saved address, port, and name, and the Wi-Fi network name handed to new smart devices (never the password). Use it when someone asks what address or network is saved, or before changing one.",
                {},
            ),
            _tool(
                "house_config_set",
                "Change a saved setting of this house, only when the user explicitly asks for that change: a TV's saved address after the network changed (tv_address), a TV's name (tv_name), or the home Wi-Fi name and password given to new smart devices (wifi). Writes the configuration files and takes effect at once. For tv_address with no host, the network is searched for the TV and the one match is applied. Never call it to work around an error on your own.",
                {
                    "setting": {"type": "string", "enum": ["tv_address", "tv_name", "wifi"]},
                    "device": {"type": "string", "description": "The TV, by its name or id, for tv_address and tv_name. Omit when there is only one."},
                    "host": {"type": "string", "description": "The new IP address or hostname, as the user said it. Omit to search the network."},
                    "port": {"type": "integer", "description": "Only if the user gave one."},
                    "name": {"type": "string", "description": "The new name, for tv_name."},
                    "ssid": {"type": "string", "description": "The Wi-Fi network name, for wifi."},
                    "password": {"type": "string", "description": "The Wi-Fi password, for wifi. Never repeat it back."},
                },
                ["setting"],
            ),
            *self._doordash_schemas(),
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        # Models emit narrow/no-break spaces inside titles ("Glass\u202fOnion");
        # catalogs expect ordinary spaces.
        arguments = {
            key: (_plain_spaces(value) if isinstance(value, str) else value)
            for key, value in arguments.items()
        }
        speaker = CURRENT_SPEAKER.get()
        if speaker and name in self._profile_tools and not arguments.get("profile"):
            if speaker.get("profile"):
                # Recognized by voice: their profile, unless the model named one.
                arguments["profile"] = speaker["profile"]
        if (
            speaker
            and speaker.get("status") == "anonymous"
            and (
                name in {"taste_feedback", "watch_history_record"}
                or (
                    name in {"profile_note_add", "style_preference_add"}
                    and not arguments.get("profile")
                )
            )
        ):
            return {
                "ok": True,
                "skipped": "Speaker is an anonymous guest; nothing was recorded.",
            }
        try:
            self._note_domain(name)
            return await handler(arguments)
        except MissingKey as exc:
            return self._missing_key(name, exc)
        except ToolError:
            raise
        except (CatalogError, ContentError) as exc:
            # The house has the key; the service itself timed out, rate
            # limited us, or went down. A bare error here is what turned
            # "how is this film like Gravity?" into "I couldn't fetch a
            # summary" — so it comes back with the web as the way on.
            return self._web_instead(
                name,
                str(exc) or exc.__class__.__name__,
                group="movies",
                reason="the catalog was unavailable",
            )
        except (TypeError, ValueError, KeyError, TVError) as exc:
            raise ToolError(str(exc)) from exc

    def keyed_features(self) -> list[dict[str, Any]]:
        """Each feature that runs on an API key: whether this house has the
        key, and whether Friday may search the web when it doesn't."""
        from .travel import bearer_token

        catalog = self.config.catalog
        has = {"movies": bool(catalog.tmdb_token or catalog.watchmode_api_key)}
        try:
            has["flights"] = bool(bearer_token(self.config.travel))
        except MissingKey:
            has["flights"] = False
        return [
            {"id": group, **info, "has_key": has.get(group, False), "web_search": web_fallback_enabled(self.database, group)}
            for group, info in KEYED_FEATURES.items()
        ]

    def _web_instead(
        self, tool: str, error: str, *, group: str = "", reason: str
    ) -> dict[str, Any]:
        """A tool that could not answer, and the web as the way on.

        Every dead end a tool can hit — an outage, a rate limit, a title the
        catalog has never heard of — is still answerable from a search, and
        the model needs telling so in the result itself: guidance in the
        system prompt loses to a fresh `ok: false` in front of it. Silent
        unless the household switched the web fallback off.
        """
        result: dict[str, Any] = {"ok": False, "error": error, "reason": reason}
        if not web_fallback_enabled(self.database, group):
            return result
        result["fallback"] = "web_search"
        result["next_step"] = (
            f"{tool} could not answer because {reason}. This is not a dead end: answer the same "
            "request from the web in this same turn — call web_search now, then web_fetch on the "
            "most promising result, and when a page refuses (403, 429, paywall) fetch the next "
            "result instead of stopping. Never end the turn saying you could not find or fetch "
            "something while a search result is still unread. In your reply say in a few words "
            "that this came from a web search, so the user knows it may be less exact."
        )
        return result

    def _missing_key(self, tool: str, exc: MissingKey) -> dict[str, Any]:
        """A feature whose API key the house doesn't have. Unless the
        household switched it off, Friday answers from a web search instead
        and says that it did, since pages can be stale or wrong."""
        feature = exc.feature or "that"
        result: dict[str, Any] = {"ok": False, "no_key": True, "feature": feature, "error": str(exc)}
        if not web_fallback_enabled(self.database, exc.group):
            result["next_step"] = (
                f"Tell the user {feature} isn't set up on this house: it needs {exc.key} "
                "(the README's 'Getting API keys' section explains how to get one, free). "
                "Don't search the web for it instead; the household turned that off for this feature."
            )
            return result
        if tool.startswith("content_play") or tool == "content_plan_playback":
            how = (
                "Use web_search to find which streaming service has the title, then open that app "
                "with tv_launch_app and tell the user to pick the title there. You cannot open the "
                "exact title or episode without the key."
            )
        else:
            how = "Use web_search (and web_fetch on a good result) to answer the same request."
        result["fallback"] = "web_search"
        result["next_step"] = (
            f"{how} In your reply say, in a few words, that this came from a web search because "
            f"{feature} has no API key set up, so it may be less accurate or up to date. "
            "Don't mention key names unless asked."
        )
        return result

    def _device(self, arguments: dict[str, Any]) -> TVDeviceConfig:
        device_id = str(arguments.get("device_id", ""))
        device = self.devices.get(device_id)
        if device is None:
            known = ", ".join(self.devices) or "none configured"
            raise ToolError(f"Unknown TV '{device_id}'. Configured devices: {known}")
        return device

    def _tv_options(self) -> dict[str, Any]:
        return {
            "input_aliases": dict(self.config.roku_input_aliases),
            "cert_dir": self.config.storage.database_path.parent / "tv-certs",
        }

    def client_for(self, device_id: str) -> TVClient:
        device = self._device({"device_id": device_id})
        return tv_client_for(
            device, self.database.tv_credentials(device.id), self._tv_options()
        )

    def roku_client_for(self, device_id: str) -> RokuClient:
        """Calibrated routines walk a Roku's own menus pixel by pixel, so they
        are the one thing that stays brand-specific."""
        device = self._device({"device_id": device_id})
        if device.platform != "roku":
            raise ToolError(
                f"{device.name} is a {device.platform} TV; calibrated routines only "
                "exist for Roku."
            )
        return RokuClient(device)

    async def _ensure_input(self, device: TVDeviceConfig) -> str | None:
        """A box plugged into a TV is invisible until that TV is on its input.
        Done for the verbs that start something playing, never for bare key
        presses — turning the volume down should not change the source."""
        if not device.connected_to or not device.input:
            return None
        parent = self.devices.get(device.connected_to)
        if parent is None:
            return None
        try:
            await self.client_for(parent.id).switch_input(device.input)
        except (TVError, ValueError) as exc:
            # The launch itself may still work if the TV is already there.
            return f"Could not switch {parent.name} to {device.input}: {exc}"
        return f"Switched {parent.name} to {device.input} first."

    async def _list_devices(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "devices": [
                {**asdict(device), "capabilities": sorted(TV_PLATFORMS[device.platform].capabilities)}
                for device in self.devices.values()
            ]
        }

    async def _get_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call answers is it on, what is it running, what is playing and
        how loud — a house assistant is nearly always asked several at once."""
        client = self.client_for(str(arguments["device_id"]))
        status = await client.status()
        for field, capability, call in (
            ("now_playing", MEDIA_STATUS, client.now_playing),
            ("volume", VOLUME, client.volume),
        ):
            if capability in client.capabilities and field not in status:
                try:
                    status[field] = await call()
                except (TVError, ValueError):
                    # An extra that failed must not lose the whole status.
                    status[field] = None
        return status

    async def _list_apps(self, arguments: dict[str, Any]) -> dict[str, Any]:
        apps = await self.client_for(str(arguments["device_id"])).apps()
        return {"apps": [asdict(app) for app in apps]}

    async def _press(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self._device(arguments)
        client = self.client_for(device.id)
        key = str(arguments["key"])
        count = max(1, min(int(arguments.get("count", 1)), 10))
        woke = None
        if key.strip().lower() in {"power_on", "on"} and WAKE_ON_LAN in client.capabilities:
            # An LG or Samsung that is fully off has no socket to command, so
            # the magic packet has to go first. Harmless if it is only asleep.
            if device.mac:
                await wake_on_lan(device.mac)
                woke = f"Sent a wake-up packet to {device.name}."
            else:
                woke = (
                    f"{device.name} cannot be woken over the network without its MAC "
                    "address in the configuration; it only responds when asleep."
                )
        for _ in range(count):
            await client.press(key)
        return {"ok": True, "key": key, "count": count, **({"wake": woke} if woke else {})}

    async def _set_volume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        level = arguments.get("level")
        mute = arguments.get("mute")
        if level is None and mute is None:
            raise ToolError("Give a level, a mute setting, or both.")
        result = await self.client_for(str(arguments["device_id"])).set_volume(
            int(level) if level is not None else None,
            bool(mute) if mute is not None else None,
        )
        return {"ok": True, **result}

    async def _switch_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = str(arguments["input"])
        await self.client_for(str(arguments["device_id"])).switch_input(target)
        return {"ok": True, "input": target}

    async def _type_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments["text"])
        await self.client_for(str(arguments["device_id"])).type_text(text)
        return {"ok": True, "typed": text}

    async def _launch_app(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self._device(arguments)
        switched = await self._ensure_input(device)
        app = await self.client_for(device.id).launch_app(str(arguments["app"]))
        return {"ok": True, "app": asdict(app), **({"input": switched} if switched else {})}

    async def _deep_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self._device(arguments)
        switched = await self._ensure_input(device)
        await self.client_for(device.id).deep_link(
            str(arguments["app"]),
            str(arguments["content_id"]),
            str(arguments["media_type"]),
        )
        return {"ok": True, **({"input": switched} if switched else {})}

    async def _list_users(self, arguments: dict[str, Any]) -> dict[str, Any]:
        users = await self.client_for(str(arguments["device_id"])).accounts()
        return {"ok": True, "users": users}

    async def _switch_user(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user = await self.client_for(str(arguments["device_id"])).switch_account(
            str(arguments["user"])
        )
        return {"ok": True, "user": user}

    async def _tv_discover(self, _: dict[str, Any]) -> dict[str, Any]:
        found = await tv_discover()
        configured = {device.host for device in self.devices.values()}
        return {
            "ok": True,
            "found": [{**entry, "already_configured": entry["host"] in configured} for entry in found],
            "note": (
                "Nothing was added. Ask which one to keep, what to call it, and "
                "then add it to the configuration."
            ),
        }

    async def _tv_pair(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self._device(arguments)
        step = str(arguments.get("step", "begin")).strip().lower()
        if step == "begin":
            client = self.client_for(device.id)
            try:
                guidance = await client.begin_pairing()
            except TVUnsupported as exc:
                return {"ok": False, "error": str(exc)}
            # Android TV holds its pairing socket open across the exchange, so
            # the same client object has to finish what it started.
            self._pairing[device.id] = client
            challenge = guidance.pop("challenge", None)
            if challenge:
                client.credentials["challenge"] = challenge
            return {"ok": True, "step": "begin", **guidance}
        client = self._pairing.get(device.id)
        if client is None:
            return {
                "ok": False,
                "error": "Pairing was not started for that TV. Begin pairing first.",
            }
        try:
            credentials = await client.complete_pairing(
                str(arguments["code"]) if arguments.get("code") else None
            )
        finally:
            self._pairing.pop(device.id, None)
        self.database.save_tv_credentials(device.id, device.platform, credentials)
        return {"ok": True, "step": "complete", "paired": device.name}

    async def _list_routines(self, _: dict[str, Any]) -> dict[str, Any]:
        routines = self.database.list_routines()
        return {
            "routines": [
                {key: value for key, value in routine.items() if key != "actions"}
                for routine in routines
            ]
        }

    async def _run_routine(self, arguments: dict[str, Any]) -> dict[str, Any]:
        routine = self.database.get_routine(str(arguments["name"]))
        if routine is None:
            raise ToolError(f"Unknown routine: {arguments['name']}")
        client = self.roku_client_for(routine["device_id"])
        trace = await execute_routine(client, routine["actions"])
        return {
            "ok": True,
            "name": routine["name"],
            "device_id": routine["device_id"],
            "actions_executed": trace,
            "verification": "Commands were accepted by Roku; the streaming app screen is not observable.",
        }

    def _unnamed_zigbee(self) -> list[Any]:
        """Freshly joined zigbee lights nobody has named yet. Matter lights
        are excluded: each pairs with its own code and gets named right
        after."""
        aliases = self.database.smart_device_aliases()
        return [
            device
            for device in self.smarthome.devices()
            if device.backend == "zigbee"
            and not (aliases.get(device.key) or {}).get("name")
        ]

    def _announce(
        self,
        conversation_id: str | None,
        text: str,
        *,
        source: str = "pairing",
        household: bool = False,
    ) -> None:
        """Say something unprompted: push it to every connected client and
        record it in the conversation, so the model's next turn knows it
        already happened and never re-announces it. household=True speaks
        to every screen (food arriving concerns whoever is around), while
        the default addresses only the conversation that started it."""
        try:
            if self.announcer is not None:
                # Addressed to this conversation's screens only: someone else
                # talking to the house must not hear about this light.
                self.announcer.publish(
                    text,
                    source=source,
                    conversation_id=None if household else conversation_id,
                )
        except Exception:  # noqa: BLE001 - an announcement must never kill the watcher
            log.exception("pairing announcement failed")
        if conversation_id:
            try:
                self.database.add_message(
                    conversation_id, "assistant", text, metadata={"announcement": True}
                )
            except Exception:  # noqa: BLE001
                log.exception("could not record pairing announcement")

    def _resolve_setup_device(self, query: str, *, prefer_last: bool = False) -> str:
        """What the model passed as a device, resolved against the light
        actually being set up. Placeholders ('next', 'setup_pending', 'it')
        and queries that match nothing while a just-flashed light awaits a
        name both mean that light — the model saying 'bathroom' for a light
        it is about to NAME bathroom must not error back and forth.

        prefer_last (renaming): every placeholder means the light most
        recently flashed — that is the one the user just looked at. Without
        it (flashing): 'next' means a light OTHER than the one just flashed,
        so "flash the next one" moves on instead of blinking the same bulb."""
        pending = self._unnamed_zigbee()
        speaker = CURRENT_SPEAKER.get() or {}
        last = self._last_flashed.get(str(speaker.get("conversation_id") or ""))
        if last not in {device.key for device in self.smarthome.devices()}:
            last = None  # gone from the network
        wanted = query.casefold()
        if wanted in LAST_WORDS or (prefer_last and wanted in NEXT_WORDS):
            return last or (pending[0].key if pending else query)
        if wanted in NEXT_WORDS:
            others = [device.key for device in pending if device.key != last]
            return others[0] if others else (last or query)
        if last:
            try:
                self.smarthome.find(query)
            except SmartHomeError:
                return last
        return query

    def _system_note(self, conversation_id: str | None, text: str) -> None:
        """A line for the model's next turn only — never spoken or shown."""
        if not conversation_id:
            return
        try:
            self.database.add_message(conversation_id, "system", text)
        except Exception:  # noqa: BLE001
            log.exception("could not record system note")

    async def _flash_quietly(self, key: str) -> None:
        try:
            await self.smarthome.flash(key, times=3)
        except Exception:  # noqa: BLE001 - keep watching either way
            log.exception("could not flash joined device %s", key)

    async def _watch_zigbee_joins(self, conversation_id: str | None) -> None:
        """Background watcher for an open zigbee join window. The user was
        told 'I'll let you know when it appears' — this is what makes that
        true: the moment a device joins, announce it and flash it, without
        the user having to ask or the model having to poll.

        Joins come in two shapes. A brand-new device shows up unnamed in the
        device list. But a factory-reset light REJOINS under its old address
        and keeps its old name — the device list shows nothing new, and the
        only tell is the network's join event. Both mean the same thing: the
        light in the user's hand just connected, so say so and flash it."""
        started = time.monotonic()
        known = {
            device.key
            for device in self.smarthome.devices()
            if device.backend == "zigbee"
        }
        deadline = started + self.join_watch_seconds
        renames_at_start = self._renames
        handled: set[str] = set()
        joined = 0
        nagged = False
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(self.join_watch_poll)
                unnamed = {device.key for device in self._unnamed_zigbee()}
                fresh = [key for key in unnamed if key not in known and key not in handled]
                rejoined = [
                    key
                    for key in self.smarthome.zigbee_joins_since(started)
                    if key in known and key not in handled
                ]
                for key in fresh + rejoined:
                    handled.add(key)
                    joined += 1
                    if conversation_id:
                        self._last_flashed[conversation_id] = key
                    device = next(
                        (d for d in self.smarthome.devices() if d.key == key), None
                    )
                    if joined > 1:
                        self._announce(
                            conversation_id,
                            f"Another light joined — that's {joined} now. "
                            "We'll set them up one at a time.",
                        )
                        continue
                    if device is not None and key in known and key not in unnamed:
                        # A named device rejoining: the user factory-reset a
                        # light this house already knows.
                        where = f" in the {device.room}" if device.room else ""
                        self._announce(
                            conversation_id,
                            f"Found it — that light was already paired here as "
                            f"'{device.name}'{where}. I'm flashing it now so you "
                            "can confirm it's the right one. Tell me its room "
                            "and what to call it, and I'll update it.",
                        )
                        self._system_note(
                            conversation_id,
                            f"The light that just reconnected is device id "
                            f"'{key}', currently named '{device.name}'. When "
                            "the user gives a room or name, call device_rename "
                            f"with device '{key}'.",
                        )
                    else:
                        self._announce(
                            conversation_id,
                            "A new light just joined — I'm blinking it now. "
                            "Tell me which room it's in and what to call it.",
                        )
                        self._system_note(
                            conversation_id,
                            f"The light that just joined is device id '{key}' "
                            "and was already flashed. When the user gives a "
                            "room or name, call device_rename with device "
                            f"'{key}'.",
                        )
                    await self._flash_quietly(key)
                if (
                    not joined
                    and not nagged
                    and self._renames == renames_at_start
                    and time.monotonic() - started > self.join_watch_seconds / 2
                ):
                    nagged = True
                    self._announce(
                        conversation_id,
                        "No light has joined yet — if it isn't blinking, switch "
                        "it off and on six times, about a second apart. I'm "
                        "still watching.",
                    )
            if not joined and self._renames == renames_at_start:
                # Say so only if the user is plausibly still waiting: a light
                # named during the window means setup went fine another way,
                # and "nothing joined — try again" would just be confusing.
                self._announce(
                    conversation_id,
                    "The pairing window closed and no light joined. Switch the "
                    "light off and on six times, about a second apart, until it "
                    "blinks, then ask me to try again.",
                )
            self._system_note(
                conversation_id,
                "The zigbee join window has closed. If the user asks to pair "
                "or try again, call device_pair with network zigbee — the "
                "do-not-call-again rule applied only while it was open.",
            )
        finally:
            if conversation_id:
                # The window is spent; a fresh device_pair may truly reopen it.
                self._zigbee_windows.discard(conversation_id)
            self._join_watchers.pop(conversation_id or "", None)

    async def close(self) -> None:
        """Stop background watchers (app shutdown)."""
        with contextlib.suppress(Exception):
            await self.doordash.close()
        tasks = list(self._join_watchers.values()) + list(self._order_watchers.values())
        self._join_watchers.clear()
        self._order_watchers.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _device_list(self, _: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "devices": [device.summary() for device in self.smarthome.devices()],
            "backends": self.smarthome.status(),
        }
        pending = self._unnamed_zigbee()
        if pending:
            result["setup_pending"] = [device.summary() for device in pending]
            result["setup_note"] = (
                f"{len(pending)} zigbee light(s) are connected but not set up "
                "yet. Tell the user how many you found, then start with the "
                f"first: call device_flash with device '{pending[0].key}' "
                "(that exact id). It will blink so the user can spot it."
            )
        missing = self.smarthome.missing()
        if missing:
            result["missing"] = missing
            result["note"] = (
                "Devices listed under missing were paired before and are still "
                "known by name, but the controller has lost their pairing record. "
                "Tell the user that plainly -- never that no devices exist."
            )
        elif not result["devices"] and not pending:
            result.setdefault(
                "note",
                "No lights or plugs are paired to this house yet. Say so and offer to add "
                "one: ask whether the device has a QR code or numeric pairing code printed "
                "on it (a code means Matter, no code means Zigbee), then device_pair.",
            )
        return result

    def _whole_house(self, query: str) -> str:
        """'all lights' when the user plainly asked for every light."""
        request = CURRENT_REQUEST.get() or ""
        if not WHOLE_HOUSE.search(request):
            return query
        rooms = {str(d.room).casefold() for d in self.smarthome.devices() if d.room}
        if any(re.search(rf"\b{re.escape(room)}\b", request, re.IGNORECASE) for room in rooms):
            return query
        return "all lights"

    async def _device_set(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._whole_house(str(arguments["device"]))
        try:
            devices, failures = await self.smarthome.control(
                query,
                power=str(arguments["power"]) if arguments.get("power") else None,
                brightness=(
                    int(arguments["brightness"])
                    if arguments.get("brightness") is not None
                    else None
                ),
                color_temp_kelvin=(
                    int(arguments["color_temp"])
                    if arguments.get("color_temp") is not None
                    else None
                ),
                color=str(arguments["color"]) if arguments.get("color") else None,
            )
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        # One plain sentence first: a small model reading a list of states
        # has claimed "the other light is still on" about a light this very
        # call had just switched off.
        names = _join_names([device.name for device in devices])
        power = arguments.get("power")
        if power in ("on", "off"):
            summary = f"Turned {power}: {names}."
        else:
            summary = f"Changed: {names}."
        if len(devices) > 1 and not failures:
            summary += f" That is all {len(devices)} of them; nothing else needs doing."
        result: dict[str, Any] = {"ok": True, "summary": summary}
        if len(devices) == 1 and not failures:
            result["device"] = devices[0].summary()
        else:
            result["devices"] = [device.summary() for device in devices]
        if failures:
            result["failed"] = failures
            result["note"] = (
                "Some devices could not be changed — tell the user exactly "
                "which ones worked and which failed, and why."
            )
        return result

    async def _device_flash(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments["device"]).strip()
        # Small models pass placeholders instead of ids ("setup_pending",
        # "new light"). Resolve those deterministically to the light being
        # set up rather than erroring back and forth.
        resolved = self._resolve_setup_device(query)
        if resolved == query and query.casefold() in SETUP_PLACEHOLDERS:
            speaker = CURRENT_SPEAKER.get() or {}
            if str(speaker.get("conversation_id") or "") in self._join_watchers:
                return {
                    "ok": False,
                    "error": "No light has joined yet. The join window is "
                    "still open and watched: the moment a light joins, it is "
                    "announced and flashed automatically — do not call "
                    "device_flash again before that announcement. Tell the "
                    "user you are still watching, and that a light that never "
                    "blinks needs a factory reset: off and on six times, "
                    "about a second apart.",
                }
            return {
                "ok": False,
                "error": "No unnamed zigbee lights are waiting. Call "
                "device_list to see the devices that exist, and flash one "
                "by its id or name.",
            }
        query = resolved
        try:
            device = await self.smarthome.flash(
                query,
                times=int(arguments.get("times") or 3),
            )
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        speaker = CURRENT_SPEAKER.get() or {}
        if speaker.get("conversation_id"):
            self._last_flashed[str(speaker["conversation_id"])] = device.key
        remaining = [d for d in self._unnamed_zigbee() if d.key != device.key]
        note = (
            'The light blinked off and on. Say: "I\'m flashing a light now — '
            'please tell me where it is and what you\'d like to name it." '
            "When the user answers, save it with device_rename passing "
            f"'{device.key}' as the device. If the user says they did not see "
            f"any light flash, call device_flash again with device "
            f"'{device.key}' and times 6"
        )
        if remaining:
            note += (
                f", or move on and call device_flash with device "
                f"'{remaining[0].key}' — the flashed one may be in another room"
            )
        note += "."
        if remaining:
            note += f" {len(remaining)} more light(s) still need names after this one."
        return {"ok": True, "device": device.summary(), "note": note}

    async def _device_pair(self, arguments: dict[str, Any]) -> dict[str, Any]:
        network = str(arguments.get("network"))
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = str(speaker.get("conversation_id") or "") or None
        if network == "zigbee":
            if conversation_id and conversation_id in self._zigbee_windows:
                # The model is looping on device_pair. The window is open;
                # answer with ground truth so it cannot invent a join.
                pending = self._unnamed_zigbee()
                if pending:
                    note = (
                        "The join window is ALREADY open — do not open it "
                        f"again. {len(pending)} light(s) have joined and are "
                        f"waiting: call device_flash with device "
                        f"'{pending[0].key}' (that exact id) right now."
                    )
                else:
                    note = (
                        "The join window is ALREADY open and NO new light has "
                        "joined yet — do not open it again, and do not tell "
                        "the user a light was found. Tell them to put the "
                        "light in pairing mode (switch it off and on six "
                        "times, about a second apart, until it blinks); you "
                        "will announce it the moment it joins."
                    )
                return {"ok": True, "note": note}
            if conversation_id:
                self._zigbee_windows.add(conversation_id)
        code = str(arguments["code"]) if arguments.get("code") else None
        if code and network == "matter":
            rejection = self._code_not_from_user(code)
            if rejection is not None:
                return rejection
        try:
            result = await self.smarthome.pair(network, code)
        except SmartHomeError as exc:
            if network == "zigbee" and conversation_id:
                self._zigbee_windows.discard(conversation_id)
            return {"ok": False, "error": str(exc)}
        if network == "zigbee" and result.get("ok"):
            watcher_key = conversation_id or ""
            # A watcher is only worth running when someone can hear it: a
            # connected client via the announcer, or the conversation itself.
            can_be_heard = self.announcer is not None or bool(conversation_id)
            if can_be_heard and watcher_key not in self._join_watchers:
                self._join_watchers[watcher_key] = asyncio.create_task(
                    self._watch_zigbee_joins(conversation_id)
                )
            if watcher_key in self._join_watchers:
                result["note"] = (
                    "Zigbee join window is open for 3 minutes and is being "
                    "watched in the background: the moment a light joins — "
                    "even one this house already knows, rejoining after a "
                    "factory reset — you will automatically announce it and "
                    "flash it. Never claim a light was found before that "
                    "happens, and do not call device_pair again while this "
                    "window is open (you will be told when it closes). Tell "
                    "the user to put the light in pairing mode now and that "
                    "you'll speak up when it appears; if it never blinks, "
                    "switching it off and on six times, about a second apart, "
                    "resets it into pairing mode."
                )
            already = self._unnamed_zigbee()
            if already:
                result["note"] += (
                    f" Separately, {len(already)} earlier light(s) are already "
                    f"joined but unnamed — you can start now: call device_flash "
                    f"with device '{already[0].key}'."
                )
        return result

    def _code_not_from_user(self, code: str) -> dict[str, Any] | None:
        """Models sometimes hallucinate a pairing code before the user has
        given one. A real code must appear verbatim in something the user
        said or typed this conversation; anything else is invented."""
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = speaker.get("conversation_id")
        if not conversation_id:
            return None  # direct API call, not a model turn: trust it
        code = code.strip()
        digits = "".join(ch for ch in code if ch.isdigit())
        for message in self.database.get_messages(conversation_id, limit=40):
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "")
            if code.upper().startswith("MT:"):
                if code.casefold() in content.casefold():
                    return None
            elif digits and digits in "".join(ch for ch in content if ch.isdigit()):
                return None
        return {
            "ok": False,
            "error": (
                "The user has NOT given this code — it does not appear in "
                "anything they said or typed, so it must not be used. Never "
                "guess or invent a pairing code. Ask the user to read out or "
                "type the code printed on the device, its box, or QR label, "
                "and call this tool only with the code exactly as they give it."
            ),
        }

    async def _device_rename(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            device = self.smarthome.rename(
                self._resolve_setup_device(
                    str(arguments["device"]).strip(), prefer_last=True
                ),
                str(arguments["name"]).strip() if arguments.get("name") else None,
                str(arguments["room"]).strip() if arguments.get("room") else None,
                replace=bool(arguments.get("replace")),
            )
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        self._renames += 1
        result: dict[str, Any] = {"ok": True, "device": device.summary()}
        if not device.room:
            result["room_note"] = (
                "This device has NO room saved. If the user mentioned a room "
                "(in the kitchen, the bathroom light), call device_rename "
                f"again with device '{device.key}' and that room."
            )
        pending = self._unnamed_zigbee()
        if pending:
            result["note"] = (
                f"Saved. {len(pending)} zigbee light(s) still have no name. "
                f"Continue: flash the next one with device_flash using device "
                f"'{pending[0].key}', ask the user if they saw it and what to "
                "call it, and rename it the same way."
            )
        return result

    async def _device_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            device = await self.smarthome.remove(str(arguments["device"]))
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "removed": device.summary(),
            "note": "Unpaired. To add it back, pair it again with its code "
            "(device_pair); it is no longer in device_list.",
        }

    async def _watch_history_find(self, arguments: dict[str, Any]) -> dict[str, Any]:
        history = self.database.find_watch_history(
            profile=str(arguments["profile"]),
            provider=str(arguments["provider"]) if arguments.get("provider") else None,
            title=str(arguments["title"]) if arguments.get("title") else None,
        )
        return {"history": history, "complete": False, "source": "local user-maintained ledger"}

    async def _watch_history_record(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.database.record_watch(
            profile=str(arguments["profile"]),
            provider=str(arguments["provider"]),
            title=str(arguments["title"]),
            status=str(arguments["status"]),
            season=int(arguments["season"]) if arguments.get("season") is not None else None,
            episode=int(arguments["episode"]) if arguments.get("episode") is not None else None,
        )
        return {"ok": True}

    async def _list_subscriptions(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"subscriptions": self.database.list_subscriptions(enabled_only=True)}

    async def _catalog_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.playback.search(str(arguments["query"]))
        if isinstance(result, dict) and result.get("did_you_mean"):
            guess = result["did_you_mean"]
            result.setdefault(
                "next_step",
                f"The user said '{guess['asked_for']}'; the web says they meant "
                f"'{guess['title']}'"
                + (f" ({guess['year']})" if guess.get("year") else "")
                + ". Say that title back briefly and continue with it.",
            )
        elif isinstance(result, dict) and not result.get("results"):
            result.setdefault(
                "next_step",
                "No catalog match, and the web did not agree on a title either. "
                "Use web_search yourself with more of what the user said (plot, "
                "actor, year), then search the catalog again with the real title.",
            )
        return result

    def _resolve_profile(self, raw: Any) -> str | None:
        profile = " ".join(str(raw or "").split())
        if profile.casefold() in {"", "household", "everyone", "all", "home", "family"}:
            return self.config.household_profile
        return profile if len(profile) <= 40 else None

    async def _style_preference_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = self._resolve_profile(arguments.get("profile"))
        if profile is None:
            return {"ok": False, "error": "Profile names are at most 40 characters."}
        preference = str(arguments["preference"]).strip()
        if len(preference) < 3:
            return {"ok": False, "error": "The preference is empty."}
        saved = self.database.add_profile_note(profile, preference, kind="style")
        return {
            "ok": True,
            **saved,
            "note": (
                "Saved. Apply it from this reply onward, including to anything else "
                "they asked for in the same breath, and acknowledge it in a few words "
                "rather than a sentence about what you have recorded."
            ),
        }

    async def _style_preference_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.database.delete_profile_note(int(arguments["id"])):
            return {"ok": False, "error": "No saved preference with that id."}
        return {"ok": True, "removed": int(arguments["id"])}

    async def _profile_note_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = " ".join(str(arguments.get("profile") or "").split())
        if profile.casefold() in {"", "household", "everyone", "all", "home", "family"}:
            profile = self.config.household_profile
        if len(profile) > 40:
            return {"ok": False, "error": "Profile names are at most 40 characters."}
        note = str(arguments["note"]).strip()
        if len(note) < 3:
            return {"ok": False, "error": "The note is empty."}
        saved = self.database.add_profile_note(profile, note)
        return {"ok": True, **saved}

    async def _profile_note_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.database.delete_profile_note(int(arguments["note_id"])):
            return {"ok": True}
        return {"ok": False, "error": "No note with that id."}

    async def _household_setup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .onboarding import DEFERRED, DONE, IN_PROGRESS, SETUP_KEY

        status = str(arguments["status"])
        if status not in {IN_PROGRESS, DEFERRED, DONE}:
            return {"ok": False, "error": "Status must be in_progress, deferred, or done."}
        self.database.set_setting(SETUP_KEY, status)
        return {"ok": True, "status": status}

    async def _voice_enroll(self, arguments: dict[str, Any]) -> dict[str, Any]:
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = speaker.get("conversation_id")
        if not conversation_id:
            return {"ok": False, "error": "No voice conversation is active."}
        try:
            result = self.voices.enroll(conversation_id, str(arguments["name"]))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        # Saved here rather than left to a follow-up call: enrolling ends the
        # turn, so a second tool round never comes. Asking someone for their
        # favourite and then forgetting it would be worse than not asking.
        favorite = " ".join(str(arguments.get("favorite") or "").split())
        if favorite:
            self.database.add_profile_note(result["profile"], f"likes {favorite}"[:300])
            result = {**result, "favorite_saved": favorite}
        CURRENT_SPEAKER.set({**speaker, "status": "identified", "profile": result["profile"]})
        return result

    async def _voice_stay_anonymous(self, arguments: dict[str, Any]) -> dict[str, Any]:
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = speaker.get("conversation_id")
        if not conversation_id:
            return {"ok": False, "error": "No voice conversation is active."}
        if not speaker.get("asked"):
            # The snapshot predates this turn's ask, so this can only be an
            # eager guess — nobody has declined anything yet.
            return {
                "ok": False,
                "error": (
                    "You have not asked for their name yet. Ask now; call this only "
                    "if they then decline."
                ),
            }
        result = self.voices.stay_anonymous(conversation_id)
        CURRENT_SPEAKER.set({**speaker, "status": "anonymous", "profile": None})
        return result

    async def _taste_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.recommendations.memory(
            str(arguments["profile"]) if arguments.get("profile") else None
        )

    async def _content_details(self, arguments: dict[str, Any]) -> dict[str, Any]:
        details = await self.recommendations.catalog.details(
            str(arguments["title"]),
            str(arguments.get("media_type", "any")),
        )
        if details is None:
            return self._web_instead(
                "content_details",
                "No catalog entry found for that title.",
                group="movies",
                reason="the catalog has no entry for that title",
            )
        return {"ok": True, "details": details}

    async def _web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            results = await web_search(
                str(arguments["query"]),
                int(arguments.get("limit", 5)),
            )
        except (WebSearchError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "results": results,
            "note": (
                "Unverified web snippets, 300 characters each. These are page "
                "descriptions, not answers: if the titles or facts you need are "
                "inside a page, call web_fetch on its url."
            ),
        }

    async def _web_fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            page = await fetch_page(
                str(arguments["url"]), int(arguments.get("max_chars", 8000))
            )
        except (WebSearchError, ValueError) as exc:
            # One page refusing is normal — paywalls, bot walls, rate limits.
            # The search returned five of them for a reason.
            return {
                "ok": False,
                "error": str(exc),
                "next_step": (
                    "That one page would not open. Call web_fetch on the next url from the "
                    "search results, and keep going until one opens or they are all spent. "
                    "The snippets you already have may answer it on their own. Do not tell "
                    "the user you could not find anything while a result is still unread."
                ),
            }
        return {"ok": True, **page, "note": "Unverified page text."}

    async def _weather_now(self, arguments: dict[str, Any]) -> dict[str, Any]:
        location = str(arguments["location"]) if arguments.get("location") else None
        try:
            report = await self.weather.now(location)
        except HomeLocationUnknown as exc:
            return {"ok": False, "needs_home_location": True, "error": str(exc)}
        except WeatherError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **report}

    async def _weather_forecast(self, arguments: dict[str, Any]) -> dict[str, Any]:
        location = str(arguments["location"]) if arguments.get("location") else None
        try:
            report = await self.weather.forecast(int(arguments.get("days", 5)), location)
        except HomeLocationUnknown as exc:
            return {"ok": False, "needs_home_location": True, "error": str(exc)}
        except WeatherError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **report}

    async def _weather_set_home(self, arguments: dict[str, Any]) -> dict[str, Any]:
        location = str(arguments.get("location") or "").strip()
        if not location:
            return {"ok": False, "error": "A town or city is required."}
        try:
            home = await self.weather.set_home(location)
        except WeatherError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "place": home["place"]}

    async def _travel_time(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.directions.travel_time(
                str(arguments["destination"]),
                str(arguments["origin"]) if arguments.get("origin") else None,
                str(arguments.get("mode", "driving")),
            )
        except HomeLocationUnknown as exc:
            return {"ok": False, "needs_home_location": True, "error": str(exc)}
        except DirectionsError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}

    async def _flight_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.travel.search_flights(
                str(arguments["origin"]),
                str(arguments["destination"]),
                str(arguments["depart"]),
                str(arguments["return_date"]) if arguments.get("return_date") else None,
                int(arguments.get("adults", 1)),
                str(arguments["cabin"]) if arguments.get("cabin") else None,
                int(arguments["max_stops"]) if arguments.get("max_stops") is not None else None,
                int(arguments.get("limit", 5)),
            )
        except TravelNotConfigured:
            raise  # execute() offers a web search instead
        except (TravelError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}

    async def _hotel_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.travel.search_hotels(
                str(arguments["place"]),
                str(arguments["check_in"]),
                str(arguments["check_out"]),
                int(arguments.get("adults", 2)),
                (
                    float(arguments["max_price_per_night"])
                    if arguments.get("max_price_per_night") is not None
                    else None
                ),
                str(arguments["near"]) if arguments.get("near") else None,
                str(arguments.get("sort", "best")),
                int(arguments.get("limit", 5)),
            )
        except (TravelError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}

    async def _taste_feedback(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.recommendations.record_feedback(
            title=str(arguments["title"]),
            status=str(arguments["status"]),
            media_type=str(arguments.get("media_type", "unknown")),
            profile=str(arguments["profile"]) if arguments.get("profile") else None,
            reason=str(arguments.get("reason", "")),
        )

    async def _recommend(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._recommend_inner(arguments)
        if isinstance(result, dict) and not result.get("candidates"):
            result.setdefault(
                "next_step",
                "Nothing suitable on the household subscriptions with these "
                "filters. Loosen one filter (year, rating, or genre) and try "
                "once more; if still empty, use web_search for ideas and then "
                "verify availability with catalog tools before suggesting.",
            )
        return result

    async def _recommend_inner(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.recommendations.recommend(
            profile=str(arguments["profile"]) if arguments.get("profile") else None,
            media_type=str(arguments.get("media_type", "any")),
            genres=[str(value) for value in arguments.get("genres", [])],
            year_min=int(arguments["year_min"]) if arguments.get("year_min") else None,
            year_max=int(arguments["year_max"]) if arguments.get("year_max") else None,
            user_rating_min=float(arguments.get("user_rating_min", 7.0)),
            critic_score_min=(
                int(arguments["critic_score_min"])
                if arguments.get("critic_score_min") is not None
                else None
            ),
            sort_by=str(arguments.get("sort_by", "popularity_desc")),
            similar_to=[str(value) for value in arguments.get("similar_to", []) if value],
            limit=int(arguments.get("limit", 5)),
        )

    async def _content_episodes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.playback.episodes(
                str(arguments["title"]),
                int(arguments["season"]) if arguments.get("season") is not None else None,
            )
        except MissingKey:
            raise
        except (CatalogError, ContentError) as exc:
            return {"status": "error", "error": str(exc)}

    async def _content_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._content_plan_inner(arguments)
        if isinstance(result, dict) and result.get("status") == "ready":
            # Planning finds the title; it does not touch the TV. A model that
            # took "ready" for "playing" once told the user a film was on
            # while the TV sat untouched.
            link = result.get("link") or {}
            play_with = {
                key: value
                for key, value in {
                    "device_id": next(iter(self.devices), None) if len(self.devices) == 1 else None,
                    "title": (result.get("title") or {}).get("title") or arguments.get("title"),
                    "provider": link.get("provider"),
                    "media_type": link.get("media_type") or arguments.get("media_type"),
                }.items()
                if value
            }
            result["playing"] = False
            result["play_with"] = play_with
            result.setdefault(
                "next_step",
                "NOTHING IS PLAYING YET — this only found the title. If the user asked to "
                "put it on, play or watch it, call content_play now with play_with. Say it "
                "is playing only after content_play succeeds.",
            )
        if isinstance(result, dict) and result.get("status") == "needs_verification":
            result.setdefault(
                "next_step",
                "The link is ready. Call content_play now — a provider's first play "
                "is supervised automatically; just ask the user to confirm the right "
                "title started.",
            )
        if isinstance(result, dict) and result.get("status") == "not_found":
            if result.get("did_you_mean"):
                guess = result["did_you_mean"]
                result.setdefault(
                    "next_step",
                    f"The catalog has nothing under that name, but the web says they meant "
                    f"{guess.get('title')!r}. Plan that title instead, in this same turn.",
                )
            else:
                result.setdefault(
                    "next_step",
                    "The catalog has nothing under that name and the web did not settle on "
                    "one either. Do not stop here: web_search with more of what the user "
                    "said — plot, actor, year, the kind of thing it is — then plan again "
                    "with the real title. Only if that fails too, say you could not work "
                    "out which title they meant and ask for one more detail.",
                )
        if isinstance(result, dict) and result.get("status") == "not_on_subscription":
            result.setdefault(
                "next_step",
                "Found, but not on a service this house pays for. Say that plainly, name "
                "where it is if a tool said, and offer something on their services instead "
                "— content_recommend with similar_to set to this title finds one.",
            )
        return result

    async def _content_plan_inner(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.playback.plan(
            str(arguments["title"]),
            str(arguments["provider"]) if arguments.get("provider") else None,
            str(arguments["media_type"]) if arguments.get("media_type") else None,
            int(arguments["season"]) if arguments.get("season") is not None else None,
            int(arguments["episode"]) if arguments.get("episode") is not None else None,
        )

    async def _tv_playback(self, device_id: str) -> dict[str, Any] | None:
        """What a TV is doing this second, or None: {app, paused, position}."""
        try:
            client = self.client_for(device_id)
        except (ToolError, TVError, KeyError):
            return None
        if MEDIA_STATUS not in client.capabilities:
            return None
        try:
            playing = await asyncio.wait_for(client.now_playing(), 1.5)
        except Exception:  # noqa: BLE001 - a slow or asleep TV just means "unknown"
            return None
        if not playing:
            return None
        state = str(playing.get("state") or "").lower()
        return {
            "app": playing.get("app"),
            "paused": state.startswith("paus"),
            "playing": state.startswith("play"),
            "position": playing.get("position_seconds"),
            "duration": playing.get("total_seconds"),
        }

    async def tv_live_line(self) -> str | None:
        """A line for the prompt when the talk is about the TV: what is on
        right now. Without it, "put my show back on" over a paused Netflix
        became a catalog search for a title called "my show"."""
        lines = []
        for device in list(self.devices.values())[:3]:
            playback = await self._tv_playback(device.id)
            if not playback or not (playback["paused"] or playback["playing"]):
                continue
            what = playback["app"] or "something"
            where = ""
            if playback["position"]:
                minutes, seconds = divmod(int(playback["position"]), 60)
                where = f" at {minutes}:{seconds:02d}"
            lines.append(
                f"{device.name} (id {device.id}): {what} is "
                f"{'PAUSED' if playback['paused'] else 'playing'}{where}."
            )
        if not lines:
            return None
        return (
            "TV right now: " + " ".join(lines) + " 'Resume', 'unpause', 'keep "
            "watching', 'put it / my show back on' mean tv_press play on that TV; "
            "'pause' or 'hold on' mean tv_press pause. Never search the catalog "
            "for words like 'my show' or 'it'."
        )

    async def _content_play(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # "Put my show back on" is a resume, not a title. If the model still
        # reaches for the catalog with a vague name while the TV is paused,
        # press play instead.
        title = " ".join(str(arguments.get("title", "")).casefold().split())
        if VAGUE_TITLE.fullmatch(title):
            device_id = str(arguments.get("device_id") or "")
            playback = await self._tv_playback(device_id)
            if playback and playback["paused"]:
                await self.execute("tv_press", {"device_id": device_id, "key": "play"})
                return {
                    "ok": True,
                    "resumed": playback["app"] or True,
                    "summary": f"Pressed play: {playback['app'] or 'the show'} was paused and is playing again.",
                }
        return await self.playback.play(
            device_id=str(arguments["device_id"]),
            title=str(arguments["title"]),
            provider=str(arguments["provider"]) if arguments.get("provider") else None,
            media_type=str(arguments["media_type"]) if arguments.get("media_type") else None,
            season=int(arguments["season"]) if arguments.get("season") is not None else None,
            episode=int(arguments["episode"]) if arguments.get("episode") is not None else None,
            profile=str(arguments["profile"]) if arguments.get("profile") else None,
            allow_unverified=bool(arguments.get("allow_unverified", False)),
        )

    # -- automations, lists, recipes -------------------------------------------

    def _speaker_profile(self) -> str | None:
        speaker = CURRENT_SPEAKER.get() or {}
        return str(speaker["profile"]) if speaker.get("profile") else None

    def _schedule_automation(
        self,
        name: str,
        repeat: str,
        when: str,
        days: list[str] | None,
        action: dict[str, Any],
        profile: str | None,
    ) -> dict[str, Any]:
        now = local_now()
        try:
            schedule = normalize_schedule(repeat, when, days, now)
            action = normalize_action(action)
        except ScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        rejection = self._time_not_from_user(schedule)
        if rejection is not None:
            return rejection
        first = next_run(schedule, now)
        # Setting a schedule by a name already in use replaces it: that is how
        # "no, 6:20" after a wrong time becomes one call instead of a delete
        # and an add (or, worse, a promise with no call at all).
        replaced = [
            existing["id"]
            for existing in self.database.list_automations(include_disabled=True)
            if existing["name"].casefold() == name.casefold()
        ]
        for automation_id in replaced:
            self.database.delete_automation(automation_id)
        automation_id = self.database.add_automation(
            name, schedule, action, first.isoformat(timespec="minutes") if first else None, profile
        )
        result = {
            "ok": True,
            "id": automation_id,
            "name": name,
            "schedule": describe_schedule(schedule),
            "action": describe_action(action),
            "next_run": first.strftime("%A %B %-d at %-I:%M %p") if first else None,
            "now": now.strftime("%A %Y-%m-%d %H:%M"),
        }
        if replaced:
            result["replaced"] = replaced
            result["note"] = "Replaced the earlier schedule of the same name."
        assumed = assumed_time(when)
        if assumed:
            result["assumed"] = assumed + " — tell the user the time you set, so they can change it"
        return result

    _SPOKEN_TIME = re.compile(
        r"\b(?P<noon>noon|midday|midnight)\b"
        r"|\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>[ap])\.?\s*m\b\.?"
        r"|\b(?P<hour24>[01]?\d|2[0-3]):(?P<minute24>[0-5]\d)\b(?!\s*[ap]\.?m)",
        re.IGNORECASE,
    )

    @classmethod
    def _spoken_times(cls, text: str) -> dict[str, str]:
        """Clock times in what someone said, as HH:MM -> their words."""
        found: dict[str, str] = {}
        for match in cls._SPOKEN_TIME.finditer(text):
            if match.group("noon"):
                word = match.group("noon").lower()
                clock = "00:00" if word == "midnight" else "12:00"
            elif match.group("ampm"):
                hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
                if hour > 12:
                    continue
                if match.group("ampm").lower() == "p" and hour < 12:
                    hour += 12
                if match.group("ampm").lower() == "a" and hour == 12:
                    hour = 0
                clock = f"{hour:02d}:{minute:02d}"
            else:
                clock = f"{int(match.group('hour24')):02d}:{match.group('minute24')}"
            found.setdefault(clock, match.group(0).strip())
        return found

    def _time_not_from_user(self, schedule: dict[str, Any]) -> dict[str, Any] | None:
        """Models turn "6:20pm" into 22:20 more often than they should. When
        the person named a clock time in this conversation and the schedule
        matches none of them, refuse and hand back the times they did say."""
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = speaker.get("conversation_id")
        if not conversation_id:
            return None  # direct API call, not a model turn: trust it
        clock = schedule.get("time")
        if not clock and schedule.get("at"):
            clock = str(schedule["at"])[11:16]
        if not clock:
            return None
        # Only the request itself counts (or the message before it, when the
        # request just answered a question like "which light?"): a meeting
        # time mentioned earlier must not veto "at bedtime", which names no
        # clock time and leaves the choice to the model.
        users = [
            str(message.get("content") or "")
            for message in self.database.get_messages(conversation_id, limit=12)
            if message.get("role") == "user"
        ]
        if not users:
            return None
        request = users[-1]
        if any(word in request.casefold() for word in VAGUE_TIMES):
            return None  # "at bedtime": the choice of clock time was left to the model
        said = self._spoken_times(request)
        if not said and len(request.split()) <= 4 and len(users) > 1:
            said = self._spoken_times(users[-2])  # "the bedroom one" answering "which light?"
        if not said or clock in said:
            return None
        options = "; ".join(f"'{words}' is {hhmm}" for hhmm, words in said.items())
        return {
            "ok": False,
            "error": (
                f"You passed {clock}, but the user did not say that time. Times the "
                f"user did say: {options}. Nothing was scheduled. Call again with "
                "the time exactly as the user said it (for example when='6:20pm')."
            ),
        }

    async def _reminder_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments.get("text", "")).strip()
        kind = str(arguments.get("kind") or "say")
        name = ("check: " if kind == "ask" else "reminder: ") + text[:50]
        return self._schedule_automation(
            name,
            str(arguments.get("repeat") or "once"),
            str(arguments.get("when", "")),
            list(arguments.get("days") or []),
            {"type": kind, "text": text},
            str(arguments["profile"]) if arguments.get("profile") else self._speaker_profile(),
        )

    async def _light_schedule_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # A room ("the bedroom lights") or "all the lights" means every light
        # in it, the same way device_set reads it -- not a question back.
        try:
            targets = self.smarthome.find_targets(self._whole_house(str(arguments.get("device", ""))))
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        device = targets[0]
        label = _join_names([target.name for target in targets])
        action = {
            "type": "fade",
            "device": device.key,
            "devices": [target.key for target in targets],
            "label": label,
            "brightness_to": arguments.get("brightness_to"),
            "brightness_from": arguments.get("brightness_from"),
            "minutes": arguments.get("minutes", 0),
            "color_temp_from": arguments.get("color_temp_from"),
            "color_temp_to": arguments.get("color_temp_to"),
        }
        result = self._schedule_automation(
            str(arguments.get("name") or f"{label} schedule")[:60],
            str(arguments.get("repeat") or "daily"),
            str(arguments.get("when", "")),
            list(arguments.get("days") or []),
            action,
            self._speaker_profile(),
        )
        if result.get("ok"):
            result["device"] = label
        return result

    async def _light_fade(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = await self.fades.fade(
                str(arguments["device"]),
                max(0, min(100, int(arguments["brightness_to"]))),
                max(0.0, min(720.0, float(arguments.get("minutes", 0) or 0))),
                brightness_from=(
                    int(arguments["brightness_from"])
                    if arguments.get("brightness_from") is not None
                    else None
                ),
                color_temp_to=(
                    int(arguments["color_temp_to"])
                    if arguments.get("color_temp_to") is not None
                    else None
                ),
            )
        except SmartHomeError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            **plan,
            "note": "The fade is running in the background; confirm it has started.",
        }

    async def _automation_list(self, _: dict[str, Any]) -> dict[str, Any]:
        items = []
        for automation in self.database.list_automations():
            when = automation["next_run_at"]
            items.append(
                {
                    "id": automation["id"],
                    "name": automation["name"],
                    "for": automation["profile"],
                    "schedule": describe_schedule(automation["schedule"]),
                    "action": describe_action(automation["action"]),
                    "next_run": when,
                    "last_result": automation["last_result"],
                }
            )
        return {
            "automations": items,
            "fades_in_progress": self.fades.running(),
            "now": local_now().strftime("%A %Y-%m-%d %H:%M"),
        }

    async def _automation_delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        automation_id = int(arguments["id"])
        automation = self.database.get_automation(automation_id)
        if automation is None:
            return {"ok": False, "error": f"No automation with id {automation_id}"}
        self.database.delete_automation(automation_id)
        return {"ok": True, "deleted": automation["name"]}

    @staticmethod
    def _list_name(arguments: dict[str, Any]) -> str:
        name = " ".join(str(arguments.get("list") or "shopping").split()).casefold()
        for suffix in (" list",):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name or "shopping"

    async def _list_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._list_name(arguments)
        items = [str(item) for item in arguments.get("items") or []]
        added = self.database.add_list_items(name, items, self._speaker_profile())
        return {
            "ok": True,
            "list": name,
            "added": added,
            "already_there": [item for item in items if item not in added and item.strip()],
            "count": len(self.database.list_items(name)),
        }

    async def _list_show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not arguments.get("list"):
            names = self.database.list_names()
            lists = {
                name: [row["item"] for row in self.database.list_items(name)] for name in names
            }
            result: dict[str, Any] = {"lists": lists or {"shopping": []}}
            if not any(lists.values()):
                result["note"] = "Every list is empty. Say that, not that nothing could be found."
            return result
        name = self._list_name(arguments)
        items = [row["item"] for row in self.database.list_items(name)]
        result = {"list": name, "items": items}
        if not items:
            # An empty list and a list nobody has ever made look identical from
            # here, and "no items" said of the wrong list is a wrong answer.
            others = [other for other in self.database.list_names() if other != name]
            result["other_lists"] = others
            result["note"] = (
                f"The {name} list is empty" + (f"; these have items: {', '.join(others)}. " if others else ". ")
                + "If the user probably meant one of those, show that one instead of "
                "reporting nothing."
            )
        return result

    async def _list_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._list_name(arguments)
        items = [str(item) for item in arguments.get("items") or []]
        removed = self.database.remove_list_items(name, items)
        return {
            "ok": True,
            "list": name,
            "removed": removed,
            "not_found": [
                item for item in items
                if not any(item.casefold() in text.casefold() for text in removed)
            ],
            "remaining": [row["item"] for row in self.database.list_items(name)],
        }

    async def _list_clear(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._list_name(arguments)
        return {"ok": True, "list": name, "cleared": self.database.clear_list(name)}

    async def _recipe_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        best = bool(arguments.get("best", False))
        try:
            found = await search_recipes(
                str(arguments["query"]), int(arguments.get("limit", 3) or 3), best=best
            )
        except (WebSearchError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if not (found.get("recipe") if best else found.get("recipes")):
            return {
                "ok": False,
                "error": (
                    "No structured recipes came back for that search. Try different "
                    "words, or fall back to web_search plus web_fetch and read the page."
                ),
                "searched": found["searched"],
            }
        if best:
            note = (
                "This is the best match; read it out now — title, total time and "
                "servings, then the ingredients as a plain list, then the steps one "
                "per sentence. Do not offer alternatives unless asked. Ingredients "
                "can go on the shopping list with list_add if asked."
            )
        else:
            note = (
                "Times are minutes. Offer these by title with one detail each and "
                "ask which they want; then call recipe_get for its steps. Ingredients "
                "can go on the shopping list with list_add if asked."
            )
        return {"ok": True, **found, "note": note}

    async def _recipe_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments["url"])
        for saved in self.database.list_recipes():
            if saved.get("url") == url:
                return {"ok": True, **saved, "saved": True}
        try:
            recipe = await get_recipe(url)
        except (WebSearchError, RecipeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **recipe}

    async def _recipe_save(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments["url"])
        try:
            recipe = await get_recipe(url)
        except (WebSearchError, RecipeError) as exc:
            return {"ok": False, "error": str(exc)}
        recipe_id = self.database.save_recipe(
            recipe["title"], recipe.get("url") or url, recipe, self._speaker_profile()
        )
        return {"ok": True, "id": recipe_id, "title": recipe["title"]}

    async def _recipe_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments["query"]) if arguments.get("query") else None
        recipes = [
            {key: value for key, value in recipe.items() if key not in ("steps", "ingredients")}
            | {"ingredient_count": len(recipe.get("ingredients", []))}
            for recipe in self.database.list_recipes(query)
        ]
        return {"recipes": recipes}

    async def _recipe_forget(self, arguments: dict[str, Any]) -> dict[str, Any]:
        recipe_id = int(arguments["id"])
        if not self.database.delete_recipe(recipe_id):
            return {"ok": False, "error": f"No saved recipe with id {recipe_id}"}
        return {"ok": True, "deleted": recipe_id}

    # -- calendar and hour-level weather --------------------------------------

    HEADS_UP_LEAD = timedelta(minutes=60)

    async def _calendar_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        now = local_now()
        title = " ".join(str(arguments.get("title", "")).split())[:200]
        if not title:
            return {"ok": False, "error": "A plan needs a title"}
        try:
            start = parse_when(str(arguments.get("when", "")), now)
            end = None
            if arguments.get("ends"):
                end = parse_when(str(arguments["ends"]), start)
                if end <= start:
                    end = end + timedelta(days=1) if end.date() == start.date() else None
        except ScheduleError as exc:
            return {"ok": False, "error": str(exc)}
        profile = str(arguments["profile"]) if arguments.get("profile") else self._speaker_profile()
        location = str(arguments["location"]).strip() if arguments.get("location") else None
        event_id = self.database.add_event(
            title,
            start.isoformat(timespec="minutes"),
            end.isoformat(timespec="minutes") if end else None,
            location,
            str(arguments["notes"]).strip() if arguments.get("notes") else None,
            profile,
        )
        result: dict[str, Any] = {
            "ok": True,
            "id": event_id,
            "title": title,
            "when": start.strftime("%A %B %-d at %-I:%M %p"),
            "location": location,
        }
        explicit = arguments.get("weather_heads_up")
        wants_heads_up = (
            explicit if isinstance(explicit, bool) else weather_relevant(title, location)
        )
        check_at = start - self.HEADS_UP_LEAD
        if wants_heads_up and check_at > now + timedelta(minutes=2):
            where = f" at {location}" if location else ""
            question = (
                f"Heads-up for {title}{where} at {start.strftime('%-I:%M %p')} today: "
                "call weather_at for that hour and say whether the weather will be "
                "a problem — rain, storms, snow, heat, cold, wind, or an alert — and "
                "how to prepare, in one or two sentences. If it looks fine, reply "
                "exactly: Nothing to report."
            )
            schedule = {"repeat": "once", "at": check_at.isoformat(timespec="minutes")}
            self.database.add_automation(
                f"weather heads-up: {title}"[:60],
                schedule,
                {"type": "ask", "text": question, "quiet": True, "event_id": event_id},
                check_at.isoformat(timespec="minutes"),
                profile,
            )
            result["weather_heads_up_at"] = check_at.strftime("%-I:%M %p")
        elif wants_heads_up:
            result["note"] = "Too soon for a weather heads-up; check weather_at now if it matters."
        else:
            result["note"] = "No weather check for this kind of plan."
        return result

    async def _calendar_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        now = local_now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if arguments.get("date"):
            try:
                start = parse_when(f"{arguments['date']} 00:00", now)
            except ScheduleError as exc:
                return {"ok": False, "error": str(exc)}
            end = start + timedelta(days=1)
        else:
            days = max(1, min(int(arguments.get("days", 2) or 2), 60))
            start, end = today, today + timedelta(days=days)
        events = self.database.list_events(
            start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")
        )
        result = {
            "now": now.strftime("%A %Y-%m-%d %H:%M"),
            "from": start.strftime("%Y-%m-%d"),
            "to": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            "events": [self._spoken_event(event, now) for event in events],
        }
        sources = self.database.external_sources()
        if sources:
            result["subscribed_calendars"] = sources
        elif not self.config.calendar.subscriptions:
            result["note"] = "No outside calendar is connected; calendar_setup_help explains how."
        if not result["events"]:
            result["next_step"] = (
                f"Nothing is on between {result['from']} and {result['to']} — that window and "
                "no other. If the user asked about something further out, call again with a "
                "larger days, and say plainly that the days you looked at are clear rather "
                "than that they have no plans."
            )
        return result

    @staticmethod
    def _spoken_event(event: dict[str, Any], now: datetime) -> dict[str, Any]:
        start = datetime.fromisoformat(event["starts_at"])
        day = start.date()
        if day == now.date():
            when = "today"
        elif day == now.date() + timedelta(days=1):
            when = "tomorrow"
        else:
            when = start.strftime("%A %B %-d")
        clock = "all day" if event.get("all_day") else f"at {start.strftime('%-I:%M %p')}"
        return {
            "id": event["id"],
            "title": event["title"],
            "when": f"{when} {clock}",
            "calendar": event.get("source", "friday"),
            "starts_at": event["starts_at"],
            "ends_at": event["ends_at"],
            "location": event["location"],
            "notes": event["notes"],
            "for": event["profile"],
            "past": start < now,
        }

    # Plans someone might still mean when they say "that thing": a window
    # wide enough to cover a mistake made minutes ago and anything coming up.
    _NEARBY = (timedelta(days=1), timedelta(days=120))

    def _find_event(self, title: str, now: datetime) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Friday's own plans whose title contains the words given. One match
        is the answer; several come back for the caller to choose between."""
        wanted = " ".join(str(title).split()).lower()
        if not wanted:
            return None
        back, ahead = self._NEARBY
        events = [
            event
            for event in self.database.list_events(
                (now - back).isoformat(timespec="minutes"),
                (now + ahead).isoformat(timespec="minutes"),
            )
            if event.get("source", "friday") == "friday"
        ]
        matches = [event for event in events if wanted in str(event["title"]).lower()]
        if not matches:
            # Nothing contains the whole phrase: try the words on their own,
            # so "the mexico ticket" still finds "Buy ticket to Mexico".
            words = [word for word in re.findall(r"[\w']+", wanted) if len(word) > 2]
            matches = [
                event
                for event in events
                if words and all(word in str(event["title"]).lower() for word in words)
            ]
        if not matches:
            return None
        return matches[0] if len(matches) == 1 else matches

    def _resolve_event(
        self, arguments: dict[str, Any], now: datetime
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The one plan an argument set points at, or the error to return."""
        raw = arguments.get("id")
        if raw not in (None, ""):
            if isinstance(raw, str) and not raw.strip().isdigit():
                return None, {
                    "ok": False,
                    "error": (
                        "That plan comes from a subscribed calendar, which Friday only "
                        "reads. It has to be changed in that calendar app."
                    ),
                }
            event = self.database.get_event(int(raw))
            if event is None:
                return None, {"ok": False, "error": f"No plan with id {raw}"}
            return event, None

        title = str(arguments.get("title") or "").strip()
        if not title:
            return None, {"ok": False, "error": "Give either the id from calendar_events or the title."}
        found = self._find_event(title, now)
        if found is None:
            return None, {"ok": False, "error": f"No plan matching {title!r}."}
        if isinstance(found, list):
            return None, {
                "ok": False,
                "several": [self._spoken_event(event, now) for event in found],
                "error": "More than one plan matches. Ask which one, then pass its id.",
            }
        return found, None

    async def _calendar_delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        now = local_now()
        event, problem = self._resolve_event(arguments, now)
        if problem is not None:
            return problem
        self.database.delete_event(int(event["id"]))
        return {"ok": True, "deleted": event["title"], "was": self._spoken_event(event, now)["when"]}

    async def _calendar_update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Move or rename a plan already on the calendar. This is how a wrong
        day gets fixed: "no, next Monday" changes the plan rather than leaving
        the mistake and adding a second one."""
        now = local_now()
        event, problem = self._resolve_event(arguments, now)
        if problem is not None:
            return problem
        changes: dict[str, Any] = {}
        start = datetime.fromisoformat(event["starts_at"])
        if arguments.get("when"):
            try:
                start = parse_when(str(arguments["when"]), now)
            except ScheduleError as exc:
                return {"ok": False, "error": str(exc)}
            changes["starts_at"] = start.isoformat(timespec="minutes")
        if arguments.get("ends"):
            try:
                end = parse_when(str(arguments["ends"]), start)
            except ScheduleError as exc:
                return {"ok": False, "error": str(exc)}
            changes["ends_at"] = end.isoformat(timespec="minutes")
        for field, key in (("title", "new_title"), ("location", "location"), ("notes", "notes")):
            if arguments.get(key):
                changes[field] = " ".join(str(arguments[key]).split())[:200 if field == "title" else 1000]
        if not changes:
            return {"ok": False, "error": "Nothing to change; give when, new_title, location or notes."}
        self.database.update_event(int(event["id"]), **changes)
        # The heads-up was scheduled against the old time, so it goes with it.
        if "starts_at" in changes:
            self.database.delete_automations_for_event(int(event["id"]))
        moved = self.database.get_event(int(event["id"]))
        return {
            "ok": True,
            "id": event["id"],
            "title": moved["title"],
            "when": datetime.fromisoformat(moved["starts_at"]).strftime("%A %B %-d at %-I:%M %p"),
            "was": self._spoken_event(event, now)["when"],
        }

    async def _weather_at(self, arguments: dict[str, Any]) -> dict[str, Any]:
        location = str(arguments["location"]) if arguments.get("location") else None
        try:
            return {"ok": True, **(await self.weather.at(str(arguments["when"]), location))}
        except HomeLocationUnknown as exc:
            return {"ok": False, "needs_home_location": True, "error": str(exc)}
        except WeatherError as exc:
            return {"ok": False, "error": str(exc)}

    def feed_url(self) -> str:
        server = self.config.server
        scheme = "https" if server.ssl_certfile and server.ssl_keyfile else "http"
        return f"{scheme}://{server.public_host}:{server.port}/calendar.ics"

    async def _calendar_setup_help(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Spoken, ordered steps. The address is the awkward part out loud,
        so it is given both as text and spelled the way a person would say it."""
        app = str(arguments.get("app") or "").strip().lower()
        goal = str(arguments.get("goal") or "see_friday_on_my_device")
        if app not in ("apple", "google", "mac", "outlook"):
            return {
                "ask_first": (
                    "Ask exactly one question and nothing else: 'Are you using Apple "
                    "Calendar or Google Calendar?' Then call calendar_setup_help again "
                    "with app set to their answer."
                )
            }
        url = self.feed_url()
        spoken = (
            url.replace("https://", "h t t p s colon slash slash ")
            .replace("http://", "h t t p colon slash slash ")
            .replace(".", " dot ")
            .replace(":", " colon ")
            .replace("/", " slash ")
        )
        spoken = " ".join(spoken.split())
        if goal == "let_friday_read_my_calendar":
            if app in ("apple", "mac"):
                steps = [
                    "On the iPhone or Mac, open the Calendar app and find the calendar you want Friday to read.",
                    "Tap the share or info button beside it and turn on Public Calendar, then copy the link it shows.",
                    "Paste that link into the .env file on the Friday computer as CALENDAR_ICS_URLS, and restart Friday.",
                    "From then on Friday reads that calendar every fifteen minutes, including repeating events, and gives a weather heads-up before outdoor plans.",
                ]
            elif app == "outlook":
                steps = [
                    "In Outlook on the web, open Settings, then Calendar, then Shared calendars.",
                    "Under Publish a calendar, pick the calendar, choose Can view all details, and click Publish; copy the ICS link.",
                    "Paste that link into the .env file on the Friday computer as CALENDAR_ICS_URLS, and restart Friday.",
                ]
            else:
                steps = [
                    "Open Google Calendar in a web browser and click the gear, then Settings.",
                    "On the left, click the calendar you want Friday to read, then scroll to Integrate calendar.",
                    "Copy the address labelled Secret address in iCal format.",
                    "Paste it into the .env file on the Friday computer as CALENDAR_ICS_URLS, and restart Friday.",
                    "From then on Friday reads that calendar every fifteen minutes, including repeating events, and gives a weather heads-up before outdoor plans.",
                ]
            return {"ok": True, "app": app, "goal": goal, "steps": steps}
        if app == "apple":
            steps = [
                "On the iPhone open Settings, scroll down and tap Apps, then Calendar, then Calendar Accounts. On older iPhones it is Settings, Calendar, Accounts.",
                "Tap Add Account, choose Other, then Add Subscribed Calendar.",
                f"Type this address exactly: {url}",
                "Tap Next. If it says the certificate cannot be verified, tap Continue.",
                "Make sure Remove Alerts is off so reminders ring, then tap Save.",
                "Friday's plans now show in the Calendar app and refresh on their own; each has an alert thirty minutes before. Make sure the Calendar app is allowed to send notifications.",
            ]
        elif app == "mac":
            steps = [
                "On the Mac open Calendar, then in the File menu choose New Calendar Subscription.",
                f"Enter this address: {url}",
                "Click Subscribe, then untick Remove Alerts and set Auto-refresh to every fifteen minutes, and click OK.",
            ]
        elif app == "outlook":
            steps = [
                "Outlook can only subscribe to calendars on the public internet, and this house is not on it, so use the phone's own calendar instead.",
                "On an iPhone follow the Apple Calendar steps; on Android use the free ICSx5 app and it appears in the calendar app there.",
            ]
        else:
            steps = [
                "Google Calendar itself can only subscribe to calendars on the public internet, and this house is not, so a small free helper app on the phone does it instead.",
                "On the Android phone install ICSx5 — spelled I C S X five — from the Play Store or F-Droid.",
                "Open it, tap the plus button, and choose Subscribe from URL.",
                f"Type this address exactly: {url}",
                "Accept the certificate warning, name it Friday, and finish.",
                "Friday's plans now show in the Google Calendar app on that phone and refresh on their own. Turn on notifications for the Friday calendar in the Google Calendar app so its thirty-minute alerts ring.",
            ]
        return {
            "ok": True,
            "app": app,
            "goal": goal,
            "address": url,
            "address_spoken": spoken,
            "steps": steps,
            "note": (
                "Give every step in order in this one reply, numbered, with the "
                "address exactly as given above. Do not hold steps back for a later "
                "turn. Offer to spell the address slowly if asked."
            ),
        }

    # ------------------------------------------------------------- DoorDash

    def _doordash_schemas(self) -> list[dict[str, Any]]:
        if not self.doordash.available:
            return []
        items_schema = {
            "type": "array",
            "description": "Each with item_id (from doordash_menu), name, quantity, and options only when doordash_cart_add asked for required_choices: [{id, name}] using the choice ids it listed.",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "name": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1, "default": 1},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1, "default": 1},
                            },
                            "required": ["id"],
                        },
                    },
                },
                "required": ["item_id"],
            },
        }
        return [
            _tool(
                "doordash_login",
                "Sign this house in to DoorDash. With no arguments it starts the sign-in and puts the link in the user's messages; call it when a DoorDash tool returned needs_setup and the user wants to sign in. When the user pastes back the address their browser landed on after signing in (it starts with http://localhost:4180 and contains code=), call it again with that as callback_url to finish.",
                {
                    "callback_url": {"type": "string", "description": "The address pasted by the user after signing in, containing code= and state=."},
                },
            ),
            _tool(
                "doordash_search",
                "Browse restaurants and stores on DoorDash near home for a cuisine, dish, or store name — for 'what looks good', 'any thai places', 'is there a McDonald's'. Returns store_id, name, delivery time, and any open cart there. When the user has already named the place and the dish, use doordash_order instead. Use kind grocery/convenience/alcohol/pets/retail for non-restaurant shopping. Never guess what is available from memory.",
                {
                    "query": {"type": "string", "description": "What they want: 'thai', 'pizza', 'Zeeks', 'sushi'. For groceries the store name, or empty to list nearby stores."},
                    "kind": {"type": "string", "enum": ["restaurants", "grocery", "convenience", "alcohol", "pets", "retail", "any"], "default": "restaurants"},
                    "address": {"type": "string", "description": "A saved DoorDash address label only when the user named it this turn. Omit for home — home is where they are."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                ["query"],
            ),
            _tool(
                "doordash_order",
                "Order named dishes from a named place in one call: finds the store near home, finds each dish on its menu, adds them to the cart there, and prices it — returning the store, what was added with prices, the total, delivery time, card, and tip suggestion, ready to read back. Use it the moment the user has said a place and a dish ('a cheeseburger from McDonald's', 'two pad thai from Wanida', 'add fries'). Nearest open branch of a chain is picked automatically. If it returns needs_choices, ask that one question and call again for just that item with options. Never place the order with this; it only builds and prices the cart.",
                {
                    "store": {"type": "string", "description": "The place as the user said it: 'McDonald's', 'the thai place on Meridian', 'Zeeks'."},
                    "store_id": {"type": "string", "description": "Instead of store when the DoorDash line or an earlier result already gave the store's id (adding a dish to the store in play)."},
                    "items": {
                        "type": "array",
                        "description": "Each dish in the user's words with a quantity: [{name: 'cheeseburger', quantity: 1}]. options only on the second try, from required_choices.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1, "default": 1},
                                "options": {
                                    "type": "array",
                                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}, "required": ["id"]},
                                },
                            },
                            "required": ["name"],
                        },
                    },
                    "address": {"type": "string", "description": "A saved DoorDash address label only when the user named it this turn. Omit for home."},
                },
                ["items"],
            ),
            _tool(
                "doordash_store_info",
                "A store's street address, hours, and phone — for 'which location?' or 'is it open?'.",
                {"store_id": {"type": "string"}},
                ["store_id"],
            ),
            _tool(
                "doordash_menu",
                "A store's menu, with item_id, name, and price for each dish, plus menu_id for the cart. Pass query with the dish words to get only matching items; without it you get categories and the first items. Set grocery true for grocery or retail stores.",
                {
                    "store_id": {"type": "string"},
                    "query": {"type": "string", "description": "Dish or item words to match, e.g. 'pad thai' or 'milk, eggs'."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 15},
                    "grocery": {"type": "boolean", "default": False, "description": "true for grocery, convenience, and retail stores."},
                },
                ["store_id"],
            ),
            _tool(
                "doordash_item_details",
                "One item's full price, description, and customization groups (size, sauce, toppings) with the option ids doordash_cart_add takes. Call it when the user asks about an item or wants to customize one. Pass menu_id for restaurant items.",
                {
                    "store_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "menu_id": {"type": "string", "description": "From doordash_menu; omit for grocery or retail items."},
                },
                ["store_id", "item_id"],
            ),
            _tool(
                "doordash_cart_add",
                "Add items to the DoorDash cart at a store (an open cart there is added to, never duplicated). If the result has item_errors with required_choices, ask the user those choices and add those items again with options. Follow with doordash_order_preview before any talk of placing.",
                {
                    "store_id": {"type": "string"},
                    "menu_id": {"type": "string", "description": "From doordash_menu. Omit only for grocery items; it is looked up."},
                    "items": items_schema,
                    "cart_uuid": {"type": "string", "description": "Only to add to a specific cart already known."},
                    "fulfillment": {"type": "string", "enum": ["delivery", "pickup"], "description": "Only when the user said pickup."},
                },
                ["store_id", "items"],
            ),
            _tool(
                "doordash_cart_show",
                "What is in this chat's cart, or with cart_uuid 'all' every open cart. Use for 'what's in my cart' and before removing an item.",
                {"cart_uuid": {"type": "string", "description": "Leave out for this chat's cart; 'all' lists every open cart."}},
            ),
            _tool(
                "doordash_cart_remove",
                "Take one item out of a cart by name. Say the item as the user did; it is matched against the cart.",
                {
                    "cart_uuid": {"type": "string", "description": "Leave out: the house knows this chat's cart."},
                    "item_name": {"type": "string"},
                    "cart_item_id": {"type": "string", "description": "Instead of item_name when known from doordash_cart_show."},
                },
                ["item_name"],
            ),
            _tool(
                "doordash_cart_delete",
                "Empty and abandon this chat's cart — 'start over', 'forget that order', 'clear my cart'.",
                {"cart_uuid": {"type": "string", "description": "Leave out: the house knows this chat's cart."}},
            ),
            _tool(
                "doordash_grocery_list",
                "Turn a list of groceries or household items into a priced cart at a nearby grocery store (or a named one) in one call — 'order milk, eggs and bread', 'get my shopping list delivered' (read it with list_show first). Returns the store, each item found with its price, anything it could not find, and the cart when add_to_cart is true. Not for restaurant food.",
                {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "number", "default": 1, "description": "Count, or pounds for produce and meat. Eggs are by the dozen."},
                            },
                            "required": ["name"],
                        },
                    },
                    "store_name": {"type": "string", "description": "Only when the user named the store, e.g. 'Fred Meyer'."},
                    "store_id": {"type": "string", "description": "To switch to one of the other_stores a previous call listed."},
                    "add_to_cart": {"type": "boolean", "default": True},
                },
                ["items"],
            ),
            _tool(
                "doordash_order_history",
                "Past DoorDash orders: store, when, items, total, whether it can be reordered — plus usual_places, the stores ordered from most. Use for 'what did I get last time', 'my usual', 'how much did I spend', and to find an order_uuid for a receipt or reorder.",
                {
                    "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 90},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
            ),
            _tool(
                "doordash_reorder",
                "Order the same thing again from a store: builds a cart from the most recent past order there and prices it, returning the preview (items, total, delivery time, card, tip suggestion). Then read that back and ask before doordash_order_place. Mention any dropped_items first.",
                {
                    "store_name": {"type": "string", "description": "The store as the user said it, e.g. 'Zeeks' or 'the thai place'."},
                    "order_uuid": {"type": "string", "description": "A specific past order from doordash_order_history instead of store_name."},
                },
            ),
            _tool(
                "doordash_order_preview",
                "Price this chat's cart again without charging: items, total with fees, delivery or pickup time, the card that will be charged, the suggested Dasher tip, and any PIN handoff. doordash_order and doordash_reorder already price the cart; call this only after a cart change, to switch to pickup, or to schedule.",
                {
                    "cart_uuid": {"type": "string", "description": "Leave out: the house knows this chat's cart."},
                    "fulfillment": {"type": "string", "enum": ["delivery", "pickup"], "description": "Only when the user asked to switch."},
                    "scheduled_time": {"type": "string", "description": "A scheduled_slots value from an earlier preview, when ASAP was not available or they asked for a time."},
                    "priority": {"type": "boolean", "default": False, "description": "Paid faster delivery, only if asked."},
                    "work_benefits": {"type": "boolean", "default": False, "description": "true when they mention work, the office, a company or team budget, or expensing it."},
                    "budget_id": {"type": "string", "description": "A work_budgets entry they chose."},
                    "apply_credits": {"type": "boolean", "default": True, "description": "false only if they said not to use their DoorDash credits."},
                },
            ),
            _tool(
                "doordash_order_place",
                "Place this chat's priced cart and charge the card on file. Call it the moment the user clearly says yes after hearing the total and the card — no other call is needed first, the house already has the cart and its price. tip is what they agreed to: 'suggested' (also for a plain yes), 'none', or an amount like '$3'. A result of accepted means sent, not yet confirmed.",
                {
                    "cart_uuid": {"type": "string", "description": "Leave out: the house knows this chat's cart."},
                    "confirmed": {"type": "boolean", "description": "true only when the user explicitly said yes to placing it after hearing the total and card."},
                    "tip": {"type": "string", "description": "'suggested', 'none', or an amount in dollars like '$3' or '2.50'. Leave out for the suggested tip."},
                    "expense_code": {"type": "string", "description": "Only for a work budget that asks for one."},
                    "expense_note": {"type": "string"},
                },
                ["confirmed"],
            ),
            _tool(
                "doordash_order_status",
                "Where an order is now: processing, placed, store confirmed, Dasher on the way, nearby, delivered. Without order_uuid, the order placed most recently through this house.",
                {"order_uuid": {"type": "string"}},
            ),
            _tool(
                "doordash_checkout_link",
                "A browser checkout link for this chat's cart, only when the user wants to change the payment method or address, or the cart has age-restricted items. Not a substitute for placing the order.",
                {"cart_uuid": {"type": "string", "description": "Leave out: the house knows this chat's cart."}},
            ),
            _tool(
                "doordash_receipt",
                "The receipt for one past order: items, subtotal, fees, tip, total, and card last four. order_uuid comes from doordash_order_history.",
                {"order_uuid": {"type": "string"}},
                ["order_uuid"],
            ),
            _tool(
                "doordash_account",
                "Saved DoorDash addresses (labels like home or work) or cards on file; set_default_address switches where every order goes, account-wide, so confirm before that.",
                {
                    "action": {"type": "string", "enum": ["addresses", "cards", "set_default_address"]},
                    "label": {"type": "string", "description": "For set_default_address: the saved address label or a street to match."},
                },
                ["action"],
            ),
            _tool(
                "doordash_promo",
                "Deals at a store (list, needs store_id) or apply/remove a promo code on a cart (needs cart_uuid and code). Preview again after applying.",
                {
                    "action": {"type": "string", "enum": ["list", "apply", "remove"]},
                    "store_id": {"type": "string"},
                    "cart_uuid": {"type": "string"},
                    "code": {"type": "string"},
                },
                ["action"],
            ),
            _tool(
                "doordash_favorite",
                "Remember, list, or forget a favorite place to order from, in the household's words: 'my favorite pizza place is Zeeks', 'my usual there is a large pepperoni'. Only the name and the usual order are kept, never DoorDash data.",
                {
                    "action": {"type": "string", "enum": ["add", "list", "remove"]},
                    "name": {"type": "string", "description": "The place, as they call it."},
                    "usual_order": {"type": "string", "description": "What they normally get there, in their words."},
                    "note": {"type": "string", "description": "Anything else worth remembering: 'ask for extra ranch', 'pickup only'."},
                },
                ["action"],
            ),
        ]

    @staticmethod
    def _purpose() -> str | None:
        return CURRENT_REQUEST.get()

    def _food_session(self) -> FoodSession | None:
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = speaker.get("conversation_id")
        if not conversation_id:
            return None
        session = self._food.get(conversation_id)
        if session is None or time.monotonic() - session.updated > FoodSession.TTL:
            session = FoodSession()
            self._food[conversation_id] = session
        session.updated = time.monotonic()
        return session

    def recent_domains(self, conversation_id: str, within: float = 1800.0) -> list[str]:
        used = self._domains_used.get(conversation_id) or {}
        now = time.monotonic()
        return sorted((d for d, at in used.items() if now - at <= within), key=lambda d: -used[d])

    def active_domains(self, conversation_id: str) -> list[str]:
        """Domains with something in progress in this chat."""
        active: list[str] = []
        if self.food_context(conversation_id):
            active.append("doordash")
        if self._join_watchers:
            active.append("devices")
        return active

    def _note_domain(self, tool: str) -> None:
        domain = domain_of_tool(tool)
        conversation_id = (CURRENT_SPEAKER.get() or {}).get("conversation_id")
        if domain and conversation_id:
            self._domains_used.setdefault(conversation_id, {})[domain] = time.monotonic()

    def food_context(self, conversation_id: str) -> str | None:
        """The DoorDash working memory for a chat, as one line for the model."""
        session = self._food.get(conversation_id)
        return session.line() if session else None

    def _dd_cart(self, arguments: dict[str, Any], result_note: dict[str, Any] | None = None) -> str:
        """The cart a doordash tool means. The house keeps this chat's cart
        itself, because a small model copying a 36-character id from a line
        of prose drops it or garbles it — both were seen the same evening."""
        given = self._optional(arguments, "cart_uuid")
        session = self._food.get((CURRENT_SPEAKER.get() or {}).get("conversation_id") or "")
        remembered = session.cart_uuid if session else None
        if given and (self.doordash.known_cart(given) or not remembered):
            return given
        # Only this chat's cart, never another chat's: a yes said here must
        # not place something someone else built elsewhere.
        chosen = remembered
        if not chosen:
            if given:
                return given
            raise DoorDashError(
                "No cart in this chat yet. Build one with doordash_order (place and dishes) "
                "or doordash_reorder, which price it too."
            )
        if result_note is not None:
            result_note["cart_source"] = "this chat's cart" + (" (the id passed was not it)" if given else "")
        return chosen

    def _dd_address(self, arguments: dict[str, Any]) -> str | None:
        """A saved-address label, but only one the user actually said this
        turn. The model has been seen sending an order to a hotel label it
        remembered from a list; anything not in the request means home."""
        label = self._optional(arguments, "address")
        if not label:
            return None
        if mentioned(label, self._purpose() or ""):
            return label
        arguments["address_ignored"] = label
        return None

    async def _doordash_call(
        self,
        coroutine: Awaitable[Any] | Callable[[], Awaitable[Any]],
        remember: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        try:
            if callable(coroutine):
                coroutine = coroutine()
            result = await coroutine
        except DoorDashNotSignedIn as exc:
            # Not signed in is fixed by signing in: run `dd-cli login` and put
            # DoorDash's link on their screen now, rather than have the model
            # relay a command.
            try:
                await self._offer_login()
            except (DoorDashError, ValueError) as problem:
                return {"ok": False, "needs_setup": True, "error": f"{exc} {problem}"}
            return {
                "ok": False,
                "needs_setup": True,
                "sign_in_link_sent": True,
                "error": str(exc),
                "note": (
                    "The DoorDash sign-in link is in their messages (envelope icon): say so, "
                    "ask them to open it and sign in, and say you will announce when it is "
                    "done — then they can ask again. Never tell them to run a command."
                ),
            }
        except DoorDashNotConfigured as exc:
            return {"ok": False, "needs_setup": True, "error": str(exc)}
        except (DoorDashError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if isinstance(result, list):
            return {"ok": True, "items": result}
        if remember is not None:
            try:
                remember(result)
            except Exception:  # noqa: BLE001 - memory must never break a call
                logger.debug("food session update failed", exc_info=True)
        return {"ok": True, **result}

    # Session memory updates, one per tool that learns something worth
    # carrying into the next turn.

    def _remember_search(self, result: dict[str, Any]) -> None:
        session = self._food_session()
        if session is None:
            return
        session.query = result.get("query") or session.query
        session.near = result.get("near") or session.near
        session.stores = [
            (str(s.get("store_id")), str(s.get("name")))
            for s in result.get("stores") or [] if s.get("store_id") and s.get("name")
        ]

    def _remember_store(self, result: dict[str, Any]) -> None:
        session = self._food_session()
        if session is None:
            return
        store_id = result.get("store_id")
        name = result.get("store_name")
        if store_id and name:
            session.store = (str(store_id), str(name))

    def _remember_cart(self, result: dict[str, Any]) -> None:
        session = self._food_session()
        if session is None:
            return
        cart = result.get("cart") if isinstance(result.get("cart"), dict) else result
        cart_uuid = cart.get("cart_uuid") or result.get("cart_uuid")
        if cart_uuid:
            session.cart_uuid = str(cart_uuid)
            session.cart_store = cart.get("store_name") or result.get("store_name") or session.cart_store
            items = cart.get("items") if isinstance(cart.get("items"), list) else result.get("items")
            if isinstance(items, list) and items:
                session.cart_items = [
                    f"{i.get('quantity', 1)}× {i.get('name')}" for i in items if isinstance(i, dict) and i.get("name")
                ]
            if result.get("total"):
                session.total = str(result["total"])
                session.card = result.get("default_card_spoken") or session.card
                tip = result.get("tip") if isinstance(result.get("tip"), dict) else {}
                session.tip = tip.get("suggested") or session.tip
        store = result.get("store") if isinstance(result.get("store"), dict) else None
        if store and store.get("store_id") and store.get("name"):
            session.store = (str(store["store_id"]), str(store["name"]))
        if result.get("stores") or result.get("choose_store"):
            session.stores = [
                (str(s.get("store_id")), str(s.get("name")))
                for s in (result.get("stores") or result.get("choose_store") or [])
                if s.get("store_id") and s.get("name")
            ]

    def _forget_cart(self, result: dict[str, Any]) -> None:
        session = self._food_session()
        if session is None:
            return
        if not session.cart_uuid or str(result.get("cart_uuid")) == session.cart_uuid:
            session.cart_uuid = None
            session.cart_items = []
            session.total = session.card = session.tip = None

    def _remember_order(self, result: dict[str, Any]) -> None:
        session = self._food_session()
        if session is None:
            return
        if result.get("accepted") or result.get("placed"):
            store = result.get("store_name") or session.cart_store or "the store"
            session.order = f"{store}, {result.get('status') or 'accepted'}"
            self._forget_cart({})

    @staticmethod
    def _optional(arguments: dict[str, Any], key: str) -> str | None:
        value = arguments.get(key)
        return str(value).strip() if value not in (None, "") else None

    async def _dd_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        address = self._dd_address(arguments)
        result = await self._doordash_call(
            self.doordash.search(
                str(arguments.get("query", "") or ""),
                str(arguments.get("kind") or "restaurants"),
                address,
                int(arguments.get("limit", 5) or 5),
                purpose=self._purpose(),
            ),
            remember=self._remember_search,
        )
        return self._with_address_note(result, arguments)

    @staticmethod
    def _with_address_note(result: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        ignored = arguments.get("address_ignored")
        if ignored and result.get("ok"):
            result["address_ignored"] = (
                f"{ignored!r} was not in the user's request, so home was used. Only pass an "
                "address the user named this turn."
            )
        return result

    async def _dd_order(self, arguments: dict[str, Any]) -> dict[str, Any]:
        address = self._dd_address(arguments)
        raw_items = arguments.get("items")
        if isinstance(raw_items, str):
            raw_items = [{"name": part.strip()} for part in re.split(r",| and ", raw_items) if part.strip()]
        result = await self._doordash_call(
            self.doordash.order_items(
                self._optional(arguments, "store"),
                raw_items if isinstance(raw_items, list) else [],
                address,
                self._optional(arguments, "store_id"),
                purpose=self._purpose(),
            ),
            remember=self._remember_cart,
        )
        return self._with_address_note(result, arguments)

    async def _dd_store_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        invented = self._dd_known_store(arguments)
        if invented:
            return invented
        return await self._doordash_call(
            self.doordash.store_info(str(arguments["store_id"]), purpose=self._purpose())
        )

    def _dd_known_store(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """The refusal for a store id nobody handed the model.

        Every id the model may use came from a search, the carts, or the order
        history, and all three are remembered. One invented id ("7002352")
        landed on a real store with no menu, and the answer to "what flavors
        do they have?" became "this store has no menu available".
        """
        store_id = str(arguments.get("store_id") or "")
        if self.doordash.known_store(store_id):
            return None
        return {
            "ok": False,
            "error": f"No store with id {store_id} has come up in this chat.",
            "next_step": (
                "That id was not in any result you have been given, so it is not the store the "
                "user means. Call doordash_search for the place by name — or for the plainest "
                "words in what they asked for — and use the store_id it returns."
            ),
        }

    async def _dd_menu(self, arguments: dict[str, Any]) -> dict[str, Any]:
        invented = self._dd_known_store(arguments)
        if invented:
            return invented
        store_id = str(arguments["store_id"])
        result = await self._doordash_call(
            self.doordash.menu(
                store_id,
                self._optional(arguments, "query"),
                int(arguments.get("limit", 15) or 15),
                self._dd_address(arguments),
                bool(arguments.get("grocery", False)),
                purpose=self._purpose(),
            ),
            remember=self._remember_store,
        )
        if result.get("no_match"):
            # The nearest store to a house is not the one that sells a named
            # drink. One menu without it is not an answer while the rest of
            # the search is sitting there unopened.
            others = self._dd_unseen_stores(store_id)
            if others:
                result["other_stores"] = others
                result["next_step"] = (
                    f"Nothing on this menu is called {result['no_match']!r}, and these stores "
                    f"from the same search have not been looked at: {', '.join(others)}. Read "
                    "the next one's menu before telling the user nobody nearby has it, then "
                    "say which store does — or offer this store's closest items by name."
                )
        return result

    def _dd_unseen_stores(self, store_id: str) -> list[str]:
        """Stores this chat's last search found that no menu call has opened."""
        session = self._food.get((CURRENT_SPEAKER.get() or {}).get("conversation_id") or "")
        if not session:
            return []
        return [name for sid, name in session.stores if sid != store_id][:4]

    async def _dd_item_details(self, arguments: dict[str, Any]) -> dict[str, Any]:
        invented = self._dd_known_store(arguments)
        if invented:
            return invented
        return await self._doordash_call(
            self.doordash.item_details(
                str(arguments["store_id"]),
                str(arguments["item_id"]),
                self._optional(arguments, "menu_id"),
                purpose=self._purpose(),
            )
        )

    @staticmethod
    def _items_argument(value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                # "milk, eggs" — a plain list of names.
                value = [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, dict):
            value = [value]
        return list(value or [])

    async def _dd_cart_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            self.doordash.cart_add(
                str(arguments["store_id"]),
                self._items_argument(arguments.get("items")),
                self._optional(arguments, "menu_id"),
                self._optional(arguments, "cart_uuid"),
                self._optional(arguments, "fulfillment"),
                purpose=self._purpose(),
            ),
            remember=self._remember_cart,
        )

    async def _dd_cart_show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        cart_uuid = self._optional(arguments, "cart_uuid")
        if cart_uuid != "all":
            try:
                cart_uuid = self._dd_cart(arguments)
            except DoorDashError:
                cart_uuid = None  # no cart in this chat: list whatever is open
            if cart_uuid:
                return await self._doordash_call(self.doordash.cart_show(cart_uuid, purpose=self._purpose()))
        result = await self._doordash_call(self.doordash.carts(purpose=self._purpose()))
        if result.get("ok"):
            result["carts"] = result.pop("items")
            if not result["carts"]:
                result["note"] = "No open carts."
        return result

    async def _dd_cart_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            lambda: self.doordash.cart_remove(
                self._dd_cart(arguments),
                self._optional(arguments, "cart_item_id"),
                self._optional(arguments, "item_name"),
                purpose=self._purpose(),
            )
        )

    async def _dd_cart_delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            lambda: self.doordash.cart_delete(self._dd_cart(arguments), purpose=self._purpose()),
            remember=self._forget_cart,
        )

    async def _dd_grocery_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._doordash_call(
            self.doordash.grocery_list(
                self._items_argument(arguments.get("items")),
                self._optional(arguments, "store_name"),
                self._optional(arguments, "store_id"),
                purpose=self._purpose(),
            )
        )
        if not result.get("ok") or not arguments.get("add_to_cart", True):
            return result
        items = [
            {"item_id": item["item_id"], "name": item.get("name"), "quantity": item.get("quantity", 1)}
            for item in result.get("items") or []
            if item.get("item_id")
        ]
        if not result.get("store_id") or not items:
            result["note"] = "Nothing could be added to a cart; tell the user what was not found."
            return result
        cart = await self._doordash_call(
            self.doordash.cart_add(
                str(result["store_id"]), items, result.get("menu_id"), purpose=self._purpose()
            )
        )
        if cart.get("ok"):
            result["cart"] = cart.get("cart")
            result["added"] = cart.get("added")
            if cart.get("item_errors"):
                result["item_errors"] = cart["item_errors"]
            result["next"] = "Call doordash_order_preview with this cart_uuid, then read back the total."
        else:
            result["cart_error"] = cart.get("error")
        return result

    async def _dd_order_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            self.doordash.order_history(
                int(arguments.get("days", 90) or 90),
                int(arguments.get("limit", 10) or 10),
                purpose=self._purpose(),
            )
        )

    async def _dd_reorder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            self.doordash.reorder(
                self._optional(arguments, "order_uuid"),
                self._optional(arguments, "store_name"),
                purpose=self._purpose(),
            ),
            remember=self._remember_cart,
        )

    async def _dd_order_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note: dict[str, Any] = {}
        result = await self._doordash_call(
            lambda: self.doordash.preview(
                self._dd_cart(arguments, note),
                self._optional(arguments, "fulfillment"),
                self._optional(arguments, "scheduled_time"),
                bool(arguments.get("priority", False)),
                bool(arguments.get("work_benefits", False)),
                self._optional(arguments, "budget_id"),
                bool(arguments.get("apply_credits", True)),
                purpose=self._purpose(),
            ),
            remember=self._remember_cart,
        )
        return {**result, **note}

    async def _dd_order_place(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tip = arguments.get("tip_cents")
        if tip in (None, ""):
            tip = arguments.get("tip")
        note: dict[str, Any] = {}
        result = await self._doordash_call(
            lambda: self.doordash.place(
                self._dd_cart(arguments, note),
                bool(arguments.get("confirmed", False)),
                tip if tip not in (None, "") else None,
                self._optional(arguments, "expense_code"),
                self._optional(arguments, "expense_note"),
                purpose=self._purpose(),
            ),
            remember=self._remember_order,
        )
        result.update(note)
        if result.get("ok") and result.get("accepted") and result.get("order_uuid"):
            if self._start_order_watch(str(result["order_uuid"]), result.get("store_name")):
                result["watching"] = True
                result["note"] = (
                    result.get("note", "")
                    + " The order is being watched in the background: you will announce when "
                    "the store confirms it, when the Dasher is nearby, and when it arrives, so "
                    "do not offer to check on it."
                ).strip()
        return result

    async def _dd_order_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            self.doordash.order_status(self._optional(arguments, "order_uuid"), purpose=self._purpose())
        )

    async def _dd_checkout_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            lambda: self.doordash.checkout_url(self._dd_cart(arguments), purpose=self._purpose())
        )

    async def _dd_receipt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._doordash_call(
            self.doordash.receipt(str(arguments["order_uuid"]), purpose=self._purpose())
        )

    async def _dd_account(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "addresses").casefold()
        if action == "cards":
            return await self._doordash_call(self.doordash.payment_methods(purpose=self._purpose()))
        if action == "set_default_address":
            label = self._optional(arguments, "label")
            if not label:
                return {"ok": False, "error": "Say which saved address (its label or street) to make the default."}
            return await self._doordash_call(self.doordash.set_default_address(label, purpose=self._purpose()))
        return await self._doordash_call(self.doordash.addresses(purpose=self._purpose()))

    async def _dd_promo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").casefold()
        if action == "list":
            store_id = self._optional(arguments, "store_id")
            if not store_id:
                return {"ok": False, "error": "Deals are per store: pass the store_id."}
            return await self._doordash_call(self.doordash.promos(store_id, purpose=self._purpose()))
        cart_uuid = self._optional(arguments, "cart_uuid")
        code = self._optional(arguments, "code")
        if not cart_uuid or not code:
            return {"ok": False, "error": "Applying or removing a promo needs the cart_uuid and the code."}
        return await self._doordash_call(
            self.doordash.promo_apply(cart_uuid, code, remove=action == "remove", purpose=self._purpose())
        )

    async def _dd_favorite(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list").casefold()
        if action == "list":
            favorites = self.database.list_food_favorites()
            return {"ok": True, "favorites": favorites, "note": None if favorites else "No favorite places saved yet."}
        name = self._optional(arguments, "name")
        if not name:
            return {"ok": False, "error": "Which place? Give its name."}
        if action == "remove":
            if self.database.remove_food_favorite(name):
                return {"ok": True, "removed": name}
            return {"ok": False, "error": f"No favorite matches {name!r}; doordash_favorite list shows them."}
        speaker = CURRENT_SPEAKER.get() or {}
        saved = self.database.add_food_favorite(
            name,
            self._optional(arguments, "usual_order"),
            self._optional(arguments, "note"),
            added_by=str(speaker.get("profile") or "") or None,
        )
        return {"ok": True, "favorite": saved}

    def _start_order_watch(self, order_uuid: str, store_name: str | None) -> bool:
        if not self.config.doordash.announce_updates or not order_uuid:
            return False
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = str(speaker.get("conversation_id") or "") or None
        if self.announcer is None and not conversation_id:
            return False
        if order_uuid not in self._order_watchers:
            fulfillment = ((self.doordash.last_order or {}).get("fulfillment")) or "delivery"
            self._order_watchers[order_uuid] = asyncio.create_task(
                self._watch_order(conversation_id, order_uuid, store_name, fulfillment)
            )
        return True

    async def _watch_order(
        self,
        conversation_id: str | None,
        order_uuid: str,
        store_name: str | None,
        fulfillment: str,
    ) -> None:
        """Follow a placed order until it lands, saying only what matters:
        the store confirmed it, the Dasher is close, it arrived — or it fell
        through. Polls sparingly; DoorDash asks that of agents."""
        store = f"{store_name} order" if store_name else "DoorDash order"
        announced: set[str] = set()
        started = time.monotonic()
        interval = self.order_watch_poll
        try:
            while time.monotonic() - started < self.order_watch_seconds:
                await asyncio.sleep(interval)
                try:
                    status = await self.doordash.order_status(
                        order_uuid, purpose="tell the household how their order is going"
                    )
                except DoorDashError:
                    continue
                state = str(status.get("status") or "")
                text: str | None = None
                if state == "order_declined":
                    text = f"Your {store} did not go through — DoorDash declined it. Check the DoorDash app to finish it."
                elif state == "action_required":
                    text = f"Your {store} needs a verification step in the DoorDash app before it goes ahead."
                elif state == "cancelled":
                    text = f"Your {store} was cancelled."
                elif state == "completed":
                    text = (
                        f"Your {store} has been picked up."
                        if fulfillment == "pickup"
                        else f"Your {store} has been delivered."
                    )
                elif state == "dasher_nearby" and "nearby" not in announced:
                    announced.add("nearby")
                    text = f"Your {store} is almost here — the Dasher is nearby."
                elif state == "ready_for_pickup" and "ready" not in announced:
                    announced.add("ready")
                    text = f"Your {store} is ready for pickup."
                elif status.get("placed") and "placed" not in announced:
                    announced.add("placed")
                    eta = status.get("eta")
                    text = f"Your {store} is confirmed" + (f"; it should arrive around {eta}." if eta else ".")
                if text:
                    self._announce(conversation_id, text, source="doordash", household=True)
                if status.get("terminal"):
                    break
                if "placed" in announced:
                    interval = self.order_watch_slow
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watcher must never take the app down
            log.exception("order watch failed")
        finally:
            self._order_watchers.pop(order_uuid, None)

    # ---------------------------------------------------------------- inbox

    def _push_inbox(
        self, title: str, body: str | None, url: str | None, kind: str = "note"
    ) -> dict[str, Any]:
        """Put a message on every screen's messages panel: stored, so a
        phone opened later still sees it, and pushed live so the badge
        lights up now."""
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = str(speaker.get("conversation_id") or "") or None
        message = self.database.add_inbox_message(title, body, url, kind, conversation_id)
        unread = self.database.unread_inbox_count(conversation_id)
        if self.announcer is not None:
            try:
                # Addressed to this chat's screens: another person talking to
                # the house does not see this conversation's board.
                self.announcer.publish(
                    title,
                    type="inbox",
                    source="inbox",
                    message=message,
                    unread=unread,
                    conversation_id=conversation_id,
                )
            except Exception:  # noqa: BLE001 - the message is saved either way
                log.exception("inbox push failed")
        return {**message, "unread": unread}

    async def _inbox_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = " ".join(str(arguments.get("title") or "").split())
        text = str(arguments.get("text") or "").strip()
        url = self._optional(arguments, "url")
        if not title:
            return {"ok": False, "error": "Give the message a short title."}
        if not text and not url:
            return {"ok": False, "error": "Nothing to send: give text, a url, or both."}
        if url and not re.match(r"^https?://", url, re.IGNORECASE):
            return {"ok": False, "error": "url must be a full link starting with http:// or https://."}
        kind = str(arguments.get("kind") or ("link" if url else "note")).casefold()
        message = self._push_inbox(title, text or None, url, kind)
        return {
            "ok": True,
            "sent": {"id": message["id"], "title": title, "kind": kind},
            "unread": message["unread"],
            "note": (
                "Say in a few words that it is in their messages (the envelope icon); "
                "do not read the link or the steps aloud."
            ),
        }

    _LOGIN_STEPS = (
        "1. Open the link and sign in to DoorDash.\n"
        "2. On the house computer that is all: Friday will say when it is done.\n"
        "3. On a phone or another computer the last page cannot load — that is normal. "
        "Its address starts with http://localhost:4180/ (if you were already signed in "
        "to DoorDash it may jump there straight away). Copy that whole address from "
        "the address bar, paste it into Friday's chat box, and send it; Friday finishes "
        "the sign-in from it.\n"
        "DoorDash gives each link five minutes; Friday swaps in a fresh one here for the "
        "next half hour, so if a page says the sign-in expired, come back and open it again."
    )
    # How long an unused sign-in link is kept fresh on the board before the
    # household has to ask again.
    _LOGIN_REFRESH_SECONDS = 30 * 60.0

    async def _offer_login(self) -> bool:
        """Run `dd-cli login` and put DoorDash's sign-in link on this chat's
        board — once: a second DoorDash call while still signed out reuses
        the attempt that is waiting instead of piling up duplicates. Returns
        whether a new card was added."""
        speaker = CURRENT_SPEAKER.get() or {}
        conversation_id = str(speaker.get("conversation_id") or "") or None
        state = getattr(self, "_login_offer", None)
        if (
            state
            and state["conversation_id"] == conversation_id
            and self.doordash.login_in_progress()
        ):
            return False

        async def on_done(ok: bool, message: str) -> None:
            await self._login_finished(conversation_id, ok, message)

        async def on_expired() -> None:
            await self._login_expired()

        result = await self.doordash.start_login(on_done, on_expired)
        message = self._push_inbox("Sign in to DoorDash", self._LOGIN_STEPS, str(result["url"]), "link")
        self._login_offer = {
            "message_id": message["id"],
            "conversation_id": conversation_id,
            "since": time.monotonic(),
            "on_done": on_done,
            "on_expired": on_expired,
        }
        return True

    async def finish_doordash_login(self, pasted: str, conversation_id: str | None) -> dict[str, Any]:
        """The address a phone landed on after signing in, pasted into the chat
        box: the client hands it straight here, so no model has to notice it.
        The chat's history still records what happened."""
        if conversation_id:
            with contextlib.suppress(Exception):
                self.database.add_message(conversation_id, "user", "(pasted the DoorDash sign-in address)")
        try:
            result = await self.doordash.finish_login(pasted)
        except DoorDashNotConfigured as exc:
            outcome: dict[str, Any] = {"ok": False, "needs_setup": True, "error": str(exc)}
        except (DoorDashError, ValueError) as exc:
            outcome = {"ok": False, "error": str(exc)}
        else:
            outcome = {"ok": True, **result}
        if outcome.get("ok") and outcome.get("signed_in"):
            self._login_offer = None
            text = "DoorDash is signed in now — ordering works. What would you like?"
        elif outcome.get("ok"):
            text = str(outcome.get("message") or "The code was delivered; DoorDash is still finishing the sign-in.")
        else:
            text = f"That did not finish the DoorDash sign-in: {outcome.get('error')}"
        outcome["text"] = text
        if conversation_id:
            with contextlib.suppress(Exception):
                self.database.add_message(
                    conversation_id, "assistant", text, metadata={"announcement": True}
                )
            if outcome.get("signed_in"):
                self._system_note(conversation_id, "DoorDash sign-in completed; the doordash tools work now.")
        return outcome

    async def _login_expired(self) -> None:
        """dd-cli's five minutes ran out with the link unopened: mint a fresh
        attempt and swap its link into the same card, quietly, for a while.
        After that the card goes stale and asking again makes a new one."""
        state = getattr(self, "_login_offer", None)
        if not state:
            return
        if time.monotonic() - state["since"] > self._LOGIN_REFRESH_SECONDS:
            self._login_offer = None
            self._system_note(
                state["conversation_id"],
                "The DoorDash sign-in link on the messages board has expired. If they ask "
                "to sign in again, call doordash_login for a fresh one.",
            )
            return
        try:
            result = await self.doordash.start_login(state["on_done"], state["on_expired"])
        except DoorDashError:
            log.exception("could not renew the DoorDash sign-in link")
            self._login_offer = None
            return
        message = self.database.update_inbox_message(state["message_id"], url=str(result["url"]))
        if message and self.announcer is not None:
            try:
                self.announcer.publish(
                    message["title"],
                    type="inbox",
                    source="inbox",
                    message=message,
                    updated=True,
                    unread=self.database.unread_inbox_count(state["conversation_id"]),
                    conversation_id=state["conversation_id"],
                )
            except Exception:  # noqa: BLE001
                log.exception("inbox refresh push failed")

    async def _login_finished(self, conversation_id: str | None, ok: bool, message: str) -> None:
        self._login_offer = None
        text = (
            "DoorDash is signed in now — ordering works."
            if ok
            else f"DoorDash sign-in did not finish: {message}"
        )
        self._announce(conversation_id, text, source="doordash", household=True)
        if conversation_id:
            with contextlib.suppress(Exception):
                self._system_note(
                    conversation_id,
                    "DoorDash sign-in completed; the doordash tools work now."
                    if ok
                    else "DoorDash sign-in failed; offer doordash_login again if asked.",
                )

    async def _dd_login(self, arguments: dict[str, Any]) -> dict[str, Any]:
        callback = self._optional(arguments, "callback_url")
        if callback:
            return await self._doordash_call(self.doordash.finish_login(callback))
        try:
            sent = await self._offer_login()
        except DoorDashNotConfigured as exc:
            return {"ok": False, "needs_setup": True, "error": str(exc)}
        except (DoorDashError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "sign_in_link_sent": True,
            "note": (
                "DoorDash's sign-in link and the steps are in their messages (envelope icon)"
                + ("" if sent else " already")
                + ". Tell them to open it and sign in; if they are on a phone, the final page "
                "will not load and they should paste its address (starting http://localhost:4180) "
                "here, after which you call doordash_login with callback_url. You will announce "
                "when the sign-in completes. Never read a link aloud."
            ),
        }

    # -------------------------------------------------------- house settings

    def _tv_by_words(self, query: Any) -> TVDeviceConfig:
        """A TV by id or by the words someone used for it; the only TV when
        there is just one."""
        wanted = " ".join(str(query or "").casefold().split())
        devices = list(self.devices.values())
        if not devices:
            raise ToolError("No televisions are configured.")
        if not wanted or wanted in {"tv", "the tv", "television", "the television"}:
            if len(devices) == 1:
                return devices[0]
            raise ToolError("Which TV? Configured: " + ", ".join(d.name for d in devices))
        if wanted in self.devices:
            return self.devices[wanted]
        words = [w for w in re.split(r"[^\w]+", wanted) if w and w not in {"tv", "the", "television"}]
        matches = [
            d for d in devices
            if wanted in {d.id.casefold(), d.name.casefold()}
            or (words and all(w in f"{d.name} {d.id}".casefold() for w in words))
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches and len(devices) == 1:
            return devices[0]
        raise ToolError(
            f"Which TV is {query!r}? Configured: " + ", ".join(d.name for d in devices)
        )

    def _apply_tv(self, device: TVDeviceConfig) -> None:
        """A changed TV entry, in effect everywhere at once: clients are made
        fresh per call from this table, and playback keeps its own copy."""
        self.devices[device.id] = device
        playback_devices = getattr(self.playback, "devices", None)
        if isinstance(playback_devices, dict):
            playback_devices[device.id] = device
        self.config = dataclasses.replace(self.config, tv_devices=tuple(self.devices.values()))

    async def _tv_reachable(self, device_id: str) -> bool:
        try:
            await asyncio.wait_for(self.client_for(device_id).status(), 6.0)
        except Exception:  # noqa: BLE001 - off, wrong address, or no such platform check
            return False
        return True

    async def _house_config_show(self, _: dict[str, Any]) -> dict[str, Any]:
        televisions = []
        for device in self.devices.values():
            televisions.append(
                {
                    "id": device.id,
                    "name": device.name,
                    "host": device.host,
                    "port": device.port,
                    "platform": device.platform,
                    "reachable": await self._tv_reachable(device.id),
                }
            )
        return {
            "ok": True,
            "televisions": televisions,
            "wifi": {
                "ssid": self.config.matter.wifi_ssid,
                "password_saved": bool(self.config.matter.wifi_password),
            },
            "files": [self.settings.config_path.name, self.settings.env_path.name],
            "note": "Never say the Wi-Fi password; say only the network name.",
        }

    async def _house_config_set(self, arguments: dict[str, Any]) -> dict[str, Any]:
        setting = str(arguments.get("setting") or "").casefold().strip()
        if setting == "tv_address":
            return await self._set_tv_address(arguments)
        if setting == "tv_name":
            return await self._set_tv_name(arguments)
        if setting == "wifi":
            return await self._set_wifi(arguments)
        return {"ok": False, "error": "setting must be tv_address, tv_name, or wifi."}

    async def _set_tv_address(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            device = self._tv_by_words(arguments.get("device"))
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        host = self._optional(arguments, "host")
        port: Any = arguments.get("port")
        discovered = False
        if not host:
            found = await tv_discover()
            known = {d.host for d in self.devices.values() if d.id != device.id}
            candidates = [
                f for f in found
                if f.get("host") and f.get("platform", device.platform) == device.platform and f["host"] not in known
            ]
            device_words = [w for w in re.split(r"[^\w]+", device.name.casefold()) if len(w) > 2 and w not in {"the", "roku"}]
            named = [
                f for f in candidates
                if device_words and any(w in str(f.get("name") or "").casefold() for w in device_words)
            ]
            pick = named[0] if len(named) == 1 else (candidates[0] if len(candidates) == 1 else None)
            if pick is None:
                return {
                    "ok": False,
                    "found": [
                        {"name": f.get("name"), "host": f.get("host"), "platform": f.get("platform")}
                        for f in found
                    ],
                    "error": (
                        "Several TVs answered on the network; ask which address belongs to "
                        f"{device.name} and call again with host."
                        if len(candidates) > 1
                        else f"No {device.platform} TV answered on the network. Ask for the new "
                        "address — it is under the TV's network settings — and call again with host."
                    ),
                }
            host, port, discovered = str(pick["host"]), pick.get("port") or port, True
        try:
            host = clean_host(host)
            port_number = clean_port(port)
        except SettingsError as exc:
            return {"ok": False, "error": str(exc)}
        if host == device.host and (port_number in (None, device.port)):
            return {"ok": True, "unchanged": True, "device": device.id, "name": device.name, "host": host,
                    "reachable": await self._tv_reachable(device.id),
                    "note": "That is already the saved address."}
        try:
            saved = self.settings.save_tv_address(device.id, host, port_number)
        except (SettingsError, OSError) as exc:
            return {"ok": False, "error": f"Could not save it: {exc}"}
        updated = dataclasses.replace(device, host=host, port=port_number or device.port)
        self._apply_tv(updated)
        reachable = await self._tv_reachable(updated.id)
        return {
            "ok": True,
            "device": updated.id,
            "name": updated.name,
            "host": updated.host,
            "port": updated.port,
            "previous": saved[0].previous,
            "saved_in": sorted({s.file for s in saved}),
            "discovered": discovered,
            "reachable": reachable,
            "note": (
                "Saved and in effect now; it survives restarts. Confirm the new address back."
                + ("" if reachable else " The TV did not answer at it yet — it may be off, or the address may be wrong.")
            ),
        }

    async def _set_tv_name(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            device = self._tv_by_words(arguments.get("device"))
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        name = " ".join(str(arguments.get("name") or "").split())
        if not name:
            return {"ok": False, "error": "What should the TV be called?"}
        try:
            saved = self.settings.save_tv_name(device.id, name)
        except (SettingsError, OSError) as exc:
            return {"ok": False, "error": f"Could not save it: {exc}"}
        updated = dataclasses.replace(device, name=name)
        self._apply_tv(updated)
        return {
            "ok": True, "device": updated.id, "name": name, "previous": saved[0].previous,
            "saved_in": sorted({s.file for s in saved}),
            "note": "Saved and in effect now. Confirm the new name back.",
        }

    async def _set_wifi(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ssid = " ".join(str(arguments.get("ssid") or "").split())
        password = str(arguments.get("password") or "")
        if not ssid or not password:
            return {"ok": False, "error": "Both the Wi-Fi network name and its password are needed."}
        current = self.config.matter
        if ssid == current.wifi_ssid and password == current.wifi_password:
            return {"ok": True, "unchanged": True, "ssid": ssid, "note": "That is already the saved network."}
        try:
            self.settings.save_wifi(ssid, password)
        except (SettingsError, OSError) as exc:
            return {"ok": False, "error": f"Could not save it: {exc}"}
        self.config = dataclasses.replace(
            self.config, matter=dataclasses.replace(current, wifi_ssid=ssid, wifi_password=password)
        )
        taken = False
        try:
            taken = await self.smarthome.set_wifi(ssid, password)
        except Exception:  # noqa: BLE001 - saved regardless; the controller retries on connect
            log.exception("could not hand the new WiFi to the smart-home hub")
        return {
            "ok": True,
            "ssid": ssid,
            "saved_in": [self.settings.env_path.name],
            "controller_updated": taken,
            "note": (
                "Saved. Say which network new smart devices will be put on from now on, and never "
                "say the password. Devices already paired keep working as they are."
                + ("" if taken else " The Matter controller is not connected right now; it gets the new "
                   "network the moment it is.")
            ),
        }

    # ----------------------------------------------------------- the app page

    async def tv_overview(self) -> list[dict[str, Any]]:
        """Every saved television with whether it answers right now, checked
        all at once so a dark TV does not hold the page for the others."""
        devices = list(self.devices.values())
        reachable = await asyncio.gather(*(self._tv_reachable(d.id) for d in devices))
        override = self.settings.roku_env_override()
        return [
            {
                "id": device.id,
                "name": device.name,
                "host": device.host,
                "port": device.port,
                "platform": device.platform,
                "connected_to": device.connected_to,
                "input": device.input,
                "mac": device.mac,
                "reachable": bool(ok),
                "capabilities": sorted(TV_PLATFORMS[device.platform].capabilities),
                "from_env": bool(override and override["id"] == device.id),
            }
            for device, ok in zip(devices, reachable)
        ]

    async def add_tv(
        self,
        device_id: str,
        name: str,
        host: str,
        port: int | None = None,
        platform: str = "roku",
        mac: str | None = None,
    ) -> dict[str, Any]:
        from .config import _default_tv_port, _tv_platform

        device_id = re.sub(r"[^a-z0-9-]+", "-", str(device_id).strip().casefold()).strip("-")
        name = " ".join(str(name).split())
        if not device_id:
            raise ToolError("The TV needs an id (letters, digits and dashes).")
        if not name:
            raise ToolError("The TV needs a name.")
        if device_id in self.devices:
            raise ToolError(f"A TV with the id {device_id!r} already exists.")
        try:
            host = clean_host(host)
            port_number = clean_port(port)
            platform = _tv_platform(platform)
        except (SettingsError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        if any(d.host == host and d.port == (port_number or _default_tv_port(platform)) for d in self.devices.values()):
            raise ToolError(f"{host} is already saved as another TV.")
        device = TVDeviceConfig(
            id=device_id,
            name=name,
            host=host,
            port=port_number or _default_tv_port(platform),
            platform=platform,
            mac=mac or None,
        )
        try:
            saved = self.settings.add_tv(
                {
                    "id": device.id,
                    "name": device.name,
                    "host": device.host,
                    "port": device.port,
                    "platform": device.platform,
                    "mac": device.mac,
                }
            )
        except (SettingsError, OSError) as exc:
            raise ToolError(f"Could not save it: {exc}") from exc
        self._apply_tv(device)
        return {
            "ok": True,
            "device": {**asdict(device), "reachable": await self._tv_reachable(device.id)},
            "saved_in": sorted({item.file for item in saved}),
        }

    async def remove_tv(self, device_id: str) -> dict[str, Any]:
        device = self.devices.get(device_id)
        if device is None:
            raise ToolError(f"No TV with the id {device_id!r}.")
        try:
            saved = self.settings.remove_tv(device.id)
        except (SettingsError, OSError) as exc:
            raise ToolError(f"Could not remove it: {exc}") from exc
        self.devices.pop(device.id, None)
        playback_devices = getattr(self.playback, "devices", None)
        if isinstance(playback_devices, dict):
            playback_devices.pop(device.id, None)
        self.config = dataclasses.replace(self.config, tv_devices=tuple(self.devices.values()))
        return {"ok": True, "removed": device.id, "saved_in": sorted({item.file for item in saved})}

    async def doordash_status(self, probe: bool = False) -> dict[str, Any]:
        """Whether ordering works from here: installed, signed in, whose
        account (its default address) — probing DoorDash only when asked,
        since that runs the CLI."""
        status: dict[str, Any] = {
            **self.doordash.status(),
            "login_in_progress": self.doordash.login_in_progress(),
            "login_url": self.doordash.login_url(),
            "signed_in": None,
            "account": None,
            "error": None,
        }
        if not probe or not self.doordash.available:
            return status
        try:
            addresses = await asyncio.wait_for(
                self.doordash.addresses(purpose="checking the DoorDash account from the app"), 30
            )
        except DoorDashNotSignedIn:
            status["signed_in"] = False
        except (DoorDashError, asyncio.TimeoutError) as exc:
            status["error"] = str(exc) or "DoorDash did not answer."
        else:
            status["signed_in"] = True
            status["account"] = {
                "default_address": addresses.get("default"),
                "addresses": addresses.get("count", 0),
                "labeled": addresses.get("labeled") or [],
            }
        return status

    async def start_doordash_login(self, conversation_id: str | None) -> dict[str, Any]:
        """Begin the sign-in from the settings page: the link goes on the
        chat's board as usual, and comes straight back too."""
        token = CURRENT_SPEAKER.set({"conversation_id": conversation_id or ""})
        try:
            try:
                await self._offer_login()
            except DoorDashNotConfigured as exc:
                return {"ok": False, "needs_setup": True, "error": str(exc)}
            except (DoorDashError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        finally:
            CURRENT_SPEAKER.reset(token)
        return {"ok": True, "url": self.doordash.login_url()}


def _plain_spaces(text: str) -> str:
    return text.replace("\u202f", " ").replace("\u00a0", " ").replace("\u2009", " ")


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }
