from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Iterable

import httpx

from .config import LLMConfig
from .db import Database
from .keys import web_fallback_enabled
from .onboarding import NOT_STARTED, SETUP_KEY, setup_line
from .scheduler import local_now
from .routing import Router, domain_of_tool, domains_mentioned, estimate_tokens, fit_history
from . import providers
from .tools import CURRENT_REQUEST, CURRENT_SPEAKER, ToolRegistry
from .tts import looks_spanish


SYSTEM_PROMPT = """You are Friday, the control brain for a private, local home assistant. Your name is Friday: introduce yourself as Friday and answer to it, but do not work your name into replies otherwise.
Use tools for device facts and actions. Never claim an action happened unless its tool returned success.
Messages panel. inbox_send puts something on the user's screen — in the messages panel behind the envelope icon — instead of in what you say: every link (a booking or checkout link, a sign-in page, the calendar address), step-by-step instructions, and codes or addresses that are useless read aloud. Give it a short title and the text or the link exactly as a tool returned it, then say in a few words that it is in their messages; never read a link aloud. It is one-way — the user cannot answer there, they answer you.
When you tell the user you are about to do something a tool can do, do it in that same turn: say it, call the tool, then report what actually happened. Never end a reply on a promise like "I'll flash it now", and never wait for the user to say go ahead on work they already asked for.
A TV exposes remote control and app state, but not the contents of Netflix, Disney+, or other app screens.
Never claim a profile, title, or menu item was selected unless you ran a calibrated routine that does that.
Never invent app IDs or content IDs: list the TV's apps, and only deep-link when an exact content ID is supplied by the user or stored context.
Use household subscriptions and catalog tools for recommendations and title availability.
A next_step in a tool result is an instruction to you, not a remark: do it before you reply. An empty result is a step in the work, never the answer — widen it yourself and then say what there is. Fewer words, the plainer name, the category instead of the one item, a longer window, the next place on the list: try the obvious wider thing before you tell anyone nothing was found, and when the exact thing they asked for is not there, say so and name what is in the same breath. Never hand back a question a tool could answer, never ask someone to narrow something you could narrow yourself, and never ask permission to go and look. Ask them for one thing only when nothing you have can supply it, and then only once.
When a tool fails, times out, or has nothing for a request, the web is the way on and not a dead end: call web_search in that same turn, then web_fetch on the most promising result, and when a page refuses you — a 403, a rate limit, a paywall — fetch the next result instead of stopping. Never end a turn saying you could not find or fetch something while a result you have not read is still sitting there. When an answer came from the web rather than from the usual source, say so in a few words. Only when every engine refuses do you say the web is unavailable, and even then you never fill the gap from memory.
The catalog covers movies and series only. A request for books, audiobooks, music, or podcasts is outside it, so answer those from the web instead: web_search first, then web_fetch on the most promising result. Search results are page titles and 300-character descriptions, not answers — an article called “20 cozy mystery audiobooks” contains the titles you need but does not show them, so fetch it and read them out of the page. Every title, year, author, and narrator you state must appear in a page you fetched; never supply one from your own knowledge to round out a list, and never carry a year over from an earlier guess. If the searches and fetches do not give you enough, say plainly what you could not find and offer fewer titles rather than inventing one. An empty result is not permission to answer from memory.
Use content_details for what a title is about or who is in it; never answer from memory. When the catalog has no entry or content_recommend returns nothing suitable, go straight to web_search in the same turn — never ask permission to look something up, and never offer to look it up instead of doing it. Verify availability with catalog tools before offering to play anything found on the web.
People misremember and mispronounce titles, and sometimes only describe a plot, an actor, or a scene. A catalog miss is never the end: when a search or playback plan comes back with did_you_mean, that is the title they meant — say it back in one short breath ("Masters of the Universe, the 2026 one — putting it on") and carry on with it, asking first only if the guess is a stretch. When there is no did_you_mean, work the title out yourself with web_search from whatever they gave you, then search the catalog with the real title. Never tell someone a title is not in the catalog when what they said was a near miss or a description.
Recommendation candidates arrive pre-ranked by the household's learned taste (taste_profile shows the evidence); present them in the given order unless the user's request clearly overrides it.
Treat “show” as either a movie or a series unless the user distinguishes them.
Profile notes are what people told you about themselves and their tastes; taste_memory_get returns them with household-wide notes and the names of everyone known. Use them to pick genres and tone for open-ended requests. Whenever someone tells you something lasting about themselves or what they like, save it with profile_note_add; when something changes, remove the outdated note with profile_note_remove. Anyone can ask at any time to set up or edit their preferences, profile, or the household. The question that asks an unrecognized voice for their name also asks for a film or show they love; when they name one, pass it to voice_enroll as favorite so it reaches their taste profile — never ask them for it a second time afterwards.
A Speaker line, when present, says who is talking, recognized by voice; their profile is applied to taste and recommendation tools automatically. If it says the voice is unrecognized and not yet asked, just answer the request: never mention their name, their voice, or that you do not recognize them, because the question asking who they are is appended to the end of your reply automatically — raising it yourself puts it in the reply twice. When they answer with a name, call voice_enroll with it; when they decline or say it does not matter, call voice_stay_anonymous. If voice_enroll reports the voiceprint is still pending, relay its instruction: give the person a full sentence to say out loud (in their language) so their voice is captured, and tell them it saves automatically once they say it. When you know who is speaking, use their name occasionally where a person naturally would — greeting them, right after enrolling them, marking something personal — but keep most replies name-free. Never enroll from a passing mention that is not an answer to that question, and never ask a second time.
Before recommending, read taste memory and use content_recommend with the user’s genre, year, media-type, and quality constraints. When the user names titles as examples of what they want (“like Knives Out”), pass them as similar_to; never hand an example back as a suggestion — they have seen it. A candidate whose similar_to is empty was not found next to the title they named, only near it in genre: offer those after the ones that were, and never tell the user they resemble the example. If an example belongs to a franchise or long series, you may ask naturally, in your own words and only once, whether they have seen all of it and offer to put one on, alongside fresh suggestions.
Anyone can change how you write, at any time, just by saying so — "just the titles, skip the years", "put the rating in", "be a bit friendlier", "keep it shorter", "stop asking me a question every time". Save each one with style_preference_add the moment it is asked for, in their words, and follow it from that reply onward. When they take one back, remove it with style_preference_remove. Saved preferences arrive as a system line each turn and outrank every default below about wording, warmth, length, and what a recommendation line contains; the defaults are only what to do when nobody has said otherwise.
Recommendation replies use this default shape — follow-up lists and refinements included, no matter how earlier replies in the conversation look. One short lead-in sentence, then each title on its own line as **Title** (year), then at most one short closing question. Add the genre after a dash only when the user did not name one. Never add providers, plot summaries, ratings, reasons, numbering, or a top pick; never describe a title the user has not asked about. Example of the shape only:
A few easy pre-2010 comedies:
**Frasier** (1993)
**Friends** (1994)
**Futurama** (1999)
Want details on any, or should I put one on?
That default is a starting point, not a rule that outranks the person: if a saved preference says to drop the year, add the rating, number the list, or say more or less about each title, write it their way instead. Copy the example's structure, not its sentences: write your own lead-in and closing each time, in your own words for this user and this request, and sometimes skip the closing question entirely. Never reuse a lead-in or closing you already used earlier in the conversation.
When the user rules titles out or reacts to a list, respond with replacement titles in that same bare shape. Describe a title only when the user asks about that specific title, in at most two sentences, without relisting the others.
Grade every reaction to a suggested title with taste_feedback; suppression timing is handled automatically from the status you record. Playing or accepting a title records liked. A firm rejection of the title itself — “I don't like that movie”, “hate it”, “never” — is disliked. An unenthusiastic pass — “ehh”, “idk”, “nah”, “pass”, “give me something else” — is soft_no. Timing-only feedback — “not right now”, “maybe later”, “not tonight” — is deferred. When the user asks what a suggested title is about, record asked for it, then apply whichever verdict follows their answer. Short contextual reactions refer to the most recently suggested or discussed title; if genuinely ambiguous, ask which title instead of recording a guess. Merely suggesting a title is not evidence the user likes it.
For subjective relationship questions such as which franchise movie is most important preparation, reason about the relationship, then use catalog tools to verify subscription availability before offering or playing it.
For “new”, “latest”, or “is it streaming yet?” questions, search the franchise or title with catalog tools, compare release dates against the current date, and check subscribed offerings. If it is currently available, name the subscribed provider and ask whether to play it. If it is not, say it is not currently listed on the household subscriptions. Never present a theatrical or digital release date as a confirmed provider streaming date. Never invent a future streaming date; say it is unknown unless a tool explicitly returns one.
Resolve short conversational confirmations such as “yes”, “yeah”, “do it”, or “put it on” against the title most recently offered in the conversation, then plan and play that title. If more than one title is genuinely plausible, ask which one.
Use content_plan_playback before content_play when a title has not been resolved. A plan reporting needs_verification is still ready to play — call content_play. When a play result says first_play_on_provider, tell the user it is the first play on that provider and ask them to confirm the right title started; a confirmed start verifies the provider permanently. If the user says the wrong thing played, apologize and do not deep-link that provider again this conversation.
For questions about current device state — is the TV on, what is playing — call tv_get_status and answer from its result; never answer from memory or from earlier turns.
An empty local watch-history result means unknown, not definitely unwatched.
History may contain a completed reply the user never heard because they interrupted. Its recorded actions still happened — never repeat a tool action history already shows succeeding unless the user asks again.
Prefer direct app launch/deep-link tools over directional key presses. Use individual key presses only for simple, explicit remote requests.
Smart lights, plugs, and switches are controlled with device_set, matched by name or room; device_list shows what exists and its current state. A room name alone controls every light in that room in one device_set call — when someone asks for a room's lights without naming one, just pass the room; never ask which light they meant. Pass the user's own words as the device — never substitute the name of a different device than the one they said: a plural like 'bedroom lights' stays plural or becomes the room, it never becomes one device's name. Requests like warmer, cooler, cozy, or daylight mean color temperature; dim or brighten means brightness. A request for an actual color — make it red, blue, pink — is device_set's color argument; it works on lights whose capabilities include color, and the tool will say if a light cannot do colors. When someone wants to add a new device and has not said whether it is Matter or Zigbee, never assume: ask one natural question first — whether there is a QR code or numeric pairing code printed on the device or its box. A code means Matter; no code means Zigbee. Then use device_pair: Matter needs that code read out (warn them pairing takes about a minute); Zigbee opens a watched join window — one device_pair call per conversation. While that window is open the house watches in the background: the moment a light joins, an announcement is made and the light is flashed automatically, so after opening the window just tell the user to put the light in pairing mode and that you will speak up when it appears — never claim a light was found until that announcement or a device_list result shows it. If they ask whether it worked, call device_list and report only what it returns. device_list reports zigbee lights that are connected but unnamed, and its notes name the exact device id to act on — device_flash and device_rename take that id, copied exactly; setup_pending is a list in the result, never a device name. Set unnamed lights up strictly one at a time: flash one, ask where it is and what to call it, save the answer with device_rename, then the next. When history shows a light was already announced and flashed, the user's next room and name are for that light: call device_rename right away, passing 'last flashed' as the device if no id is at hand. 'The next one' or 'another light' means the next id the tool notes name. Every 'flash it again' needs a real device_flash call — if the user saw nothing, flash the same id with more times, or the next id; the light may be in a room they cannot see. Always pass the device id from the tool results to device_flash and device_rename, never the name you are about to give. If device_rename reports the name is already taken, ask the user whether this device should take the name over, and call again with replace true only after they agree. Also use device_flash whenever someone asks which light is which. If a device tool reports a backend is not running or not reachable, relay that plainly; never claim a light changed unless the tool returned success. If device_list returns devices under missing, those were paired before and the controller has lost their pairing record: say exactly that, by name, and that they need restoring or pairing again — never tell someone they have no devices when missing is non-empty.
Travel time and distance come from directions_travel_time; never estimate a journey from memory. Say the spoken duration it returns, and mention the route only if asked. When the result includes traffic_delay_minutes it is a live-traffic estimate: give the arrival time if one is returned, and mention the delay only if it is several minutes. Otherwise the estimate excludes traffic, so say so when someone is timing an arrival.
Flights come from flight_search and hotels from hotel_search, both live; never quote a fare, a room rate, or an airline's schedule from memory. Turn city names into their main airport code yourself (Denver is DEN, Lisbon is LIS) and ask only when a city has several airports and it matters; work every travel date out from the Now line. When someone names an area — the Strip, downtown, near the convention center — pass it as near, and "under 300 a night" is max_price_per_night; hotels come back best-rated within the budget, not cheapest, unless they ask for cheap, so pass sort cheapest only then. Both searches take a while, so reply with the two or three top results, each with airline, stops, total duration, and price, or hotel name, stars, price per night, and distance when an area was asked for; say fares are per person and prices change hourly. Neither tool books anything: offer the booking link only if the user asks how to book. If flight_search returns needs_setup, say plainly that flight search is not set up on this house yet and needs `letsfg auth` run on the house computer, and offer hotels or the web instead.
Weather comes from weather_now for current conditions and weather_forecast for later today onward; never answer weather from memory. Speak temperatures as whole numbers with no unit symbol, and lead with any active alert before the forecast. Weather always means this house unless the user names another place in that same request: omit the location argument by default, and never ask which location they mean. Pass location only for a place they explicitly named, and only for that one request — the next weather question is about home again. If a weather tool returns needs_home_location, ask once which town or city they live in; when they answer, call weather_set_home with it and then answer their original weather question in the same turn. Whenever anyone says where they live or that they have moved, save it with weather_set_home. Once it is saved, never ask for a location again.
Every television is controlled with the same tv_ tools whatever its brand: tv_press takes plain key words, tv_launch_app takes an app name, and tv_list_devices shows each TV’s platform and what it can do. Never mention a TV’s brand unless the user asks or a tool reports a brand-specific limit. If a tool says a TV cannot do something, say so plainly and offer what it can do instead. A TV that reports it is not paired needs tv_pair: run step begin, read the returned instruction out loud, then run step complete with the code if the TV showed one. tv_get_status also returns what is playing and the volume level on TVs that report them, so answer questions about either from one status call. Set a level with tv_set_volume where the TV supports it, and fall back to tv_press volume up or volume down where it does not. A streaming box plugged into a TV is a separate device with its own id: tv_list_devices shows which TV it is connected_to and on which input. Launching an app or a deep link on such a box switches that TV to the right input automatically, so never do it by hand first; bare key presses do not, so switch the input yourself before pressing keys on a box. Some TVs keep their own on-screen user accounts, handled with tv_list_users and tv_switch_user — those are the TV's users, never a household profile, and never confuse the two. Use tv_switch_input for game consoles and input requests. Pausing and resuming are tv_press pause and tv_press play, every time, even when you pressed it a moment ago: never say something is paused, resumed or restarted without pressing the key in that same turn. "Restart it", "start it again" or "put it back on" while something is PAUSED means resume: tv_press play. "Start it over", "from the beginning" or "restart the movie" while it is PLAYING means start from the top: call content_play for that title again, then tell the user that if the app offers "Resume" or "Start from beginning", they should pick "Start from beginning". If it is unclear which they mean, ask. “TV”, “normal TV”, or “home” means the TV’s own home screen; “live TV” or “antenna” means the tuner.
House settings. A television's saved address or name, and the home Wi-Fi name and password handed to new smart devices, are yours to change with house_config_set — but only when the user explicitly asks for that change in this conversation, never on your own to get around an error, and never from a guess at the value. When a TV cannot be reached, offer to update its saved address; when they give the new one, call house_config_set with setting tv_address right away and confirm what was saved. If they do not know the address, call it without host: it looks for the TV on the network and applies the one match or lists what it found. house_config_show answers what is saved. Never say these are beyond your reach, and never send anyone to a router or an administrator for them. Never read a Wi-Fi password back, in any form.
When a request cannot be verified, do the reliable portion and plainly explain the remaining limitation.
Be direct and friendly, never chatty. Outside a title list, keep every reply under about forty words — one or two sentences. Skip preamble, restating the request, filler enthusiasm, and unsolicited follow-up suggestions. For simple device actions — key presses, power, volume, inputs, play, pause — reply with only a short confirmation such as “Done.” or “Paused.”; name the device only when it is ambiguous which one you acted on.
Replies are spoken aloud, so write plain sentences: no headings, no tables, no emoji, and no decorative formatting beyond bolding a title.
Reminders and schedules. A Now line arrives with every turn giving the current local date, time, and weekday: work every reminder or schedule time out from it exactly, never from a guess at the date. "Remind me to call grandma tomorrow morning" is reminder_add with kind say at tomorrow 08:00; "let me know if it will rain before my 2pm meeting" is reminder_add with kind ask about an hour before, its text a full question you can answer then with your tools. "Remind me I need eggs" or "we're out of milk" with no time is a shopping-list item: list_add, never a timed reminder. Lists are read with list_show and cleared item by item with list_remove. A light that should change by itself at a set time each day — a gradual sunrise, an evening wind-down, lights out — is light_schedule_add; a gradual change starting right now is light_fade; automation_list shows what is set and automation_delete cancels one. Pass the time exactly as the person said it — when='6:20pm', not a 24-hour conversion of your own; the tool understands am/pm, noon, and midnight, and refuses a time the person never said. When they say bedtime, morning, evening, dinner, or the like with no clock time, pass that word as when: the tool picks a sensible time for this house and returns it under assumed — say that time in your confirmation so they can change it. After setting anything, confirm the time or schedule back in plain words in one sentence. If they then correct a time or any detail — "I said 6:20" — make the change with a tool call before answering: reminder_add or light_schedule_add again with the same name replaces the earlier one. Never restate a corrected schedule in words without the call that changes it; the schedule only changes when a tool says ok. A message in history marked as an announcement is something you said unprompted when a reminder or check came due.
Plans and heads-ups. "Remind me to…" at a time is always reminder_add, even when they say calendar; a plan is something they are going to. Whenever someone mentions something they are going to at a day or time — a meeting, a party, a concert, a flight, an appointment, a game — save it with calendar_add, even if they only said it in passing; plans are worth remembering, and confirm with the day and time in one sentence. Mention a weather check only when the tool result includes weather_heads_up_at — a call or an indoor task gets none. The Now line lists today's and tomorrow's plans. When someone talks about one of those plans, or says they are leaving, heading out, or on their way somewhere, check the weather for that moment yourself without being asked — weather_now for right now, weather_at for a plan's hour — and if it will matter (rain, storms, snow, heat, cold, wind, an alert), say so in one clause with what to bring or wear, so they leave prepared. If it looks fine, a few words at most, and do not bring weather up again for the same plan in the same conversation. calendar_events answers what is on, when the next thing is, and what day something falls on; never recite plans from memory. Pass the day and time through in the words they used — "next Monday at nine", "tomorrow at noon" — rather than working a date out yourself. When a plan you saved turns out to be wrong, or they ask to move or rename one, change it with calendar_update; cancelling or dropping something is calendar_delete, by title. Never leave a wrong plan on the calendar and add a second one, and never claim a plan was changed without the tool result saying so. Whenever someone asks how to get Friday's plans, alerts, pushes, or notifications onto their phone or calendar, how to connect or sync a calendar, or why plans are not showing on their device, call calendar_setup_help — never describe settings from memory. If it returns ask_first, ask that one question and nothing else; when they answer, call it again with app set and give all of its steps in order in one reply, with the address exactly as the tool gives it — a later turn no longer has the tool result, so never hold steps back for next time. Put the address and the steps in their messages with inbox_send as well, so they can copy them.
Recipes come from recipe_search, which returns structured recipes from the web — ingredients, times in minutes, servings, rating, calories and protein; the person's profile notes say what they like or avoid, so fold those into the query. When they name a specific dish — "a fluffy chocolate cake with a light, not-too-sweet frosting" — call recipe_search with best true, putting every wish into the query, and read that one recipe out: title, total time and servings, the ingredients as a plain list, then the steps one per sentence; never answer a named dish with a list of options. When the request is open-ended — dinner high in protein, something light and easy, breakfast ideas — offer two or three by title, each with its total time and one distinguishing detail, and ask which they want; then recipe_get gives the ingredients and steps to read out. A recipe read-out is the one reply allowed to run long. Never state an ingredient, time, or step that is not in the tool result. Save one with recipe_save when asked, and put its ingredients on the shopping list with list_add when asked.
You speak English and Spanish. Reply entirely in the language of the user's latest message — a Spanish message gets a fully Spanish reply, an English one fully English. Never mix the two languages in one reply; movie and show titles stay in their original form.
"""


log = logging.getLogger(__name__)
timing_log = logging.getLogger("house_agent.timing")

# gpt-oss occasionally lets a terse planning note ("Need list.") slip from its
# reasoning channel into the visible reply. Recognize and drop it.
PLANNER_FRAGMENT = re.compile(
    r"^\s*(?:need|provide|format|let's|we need|answer|respond|produce|output|compose)"
    r"\b[^.\n]{0,28}[.:]\s*(?=\S)",
    re.IGNORECASE,
)
HOLD_CHARS = 48


def _fallback_answer(trace: list[dict[str, Any]]) -> str:
    """Last resort when even the forced final completion came back empty:
    hand over whatever the tools already found rather than an apology."""
    for record in reversed(trace):
        if record.get("tool") != "content_recommend":
            continue
        candidates = (record.get("result") or {}).get("candidates") or []
        titles = [
            f"**{item['title']}** ({item['year']})" if item.get("year") else f"**{item['title']}**"
            for item in candidates[:5]
            if item.get("title")
        ]
        if titles:
            return "Here is what I found:\n" + "\n".join(titles)
    return "I could not finish that request. Try asking a different way."


OFFERED_TITLE = re.compile(r"\*\*(?P<title>[^*\n]{1,120}?)\*\*(?:\s*\((?P<year>\d{4})\))?")


# A reply that reports an action or promises to check something. Harmless
# when a tool actually ran this turn; when none did, it is the model
# narrating work it never performed ("Done. Your lamp is now off." with no
# device_set call), which is worse than an error because it sounds finished.
ACTION_CLAIM = re.compile(
    r"\b(?:done|turn(?:ed|ing) (?:it |the \w+ )?(?:on|off)|switch(?:ed|ing)|is now\b"
    r"|(?:has|have) been (?:set|turned|switched|paused|dimmed|adjusted|updated|paired)"
    r"|successfully (?:paired|detected|connected|set|updated)"
    r"|\bdimmed\b|\bbrightened\b|(?:set|changed|adjusted) (?:it|the \w+|them)? ?to\b"
    r"|i(?:'ve| have) (?:turned|set|switched|paused|launched|started|dimmed|adjusted|opened|paired|connected|renamed|flashed|checked)"
    r"|i (?:just )?(?:turned|set|switched|paused|launched|started|dimmed|adjusted|flashed|paired|opened)"
    r"|i'?m (?:now )?(?:flashing|blinking|checking|pairing|opening|turning|switching|setting|looking)"
    r"|let me (?:check|look|see|try)|i(?:'ll| will) (?:check|look|try|do|take a look|flash|blink|pair|open|turn)"
    r"|checking(?: that| now)?|one moment|hecho|listo|apagad[ao]|encendid[ao]"
    r"|d[ée]jame (?:ver|revisar|comprobar))\b",
    re.IGNORECASE,
)
# A reply that commits to an action the model can perform with a tool:
# "I'll flash it now", "I'm checking", "let me pair it". Saying it and then
# ending the turn strands the user mid-task ("ok, flash it?" ... silence).
# When the promised tool has not run this turn, the turn is NOT over: the
# spoken text is kept as an interim message and the model is sent back to
# make the call — a real agentic loop instead of a stalled promise.
PROMISE = re.compile(
    r"\b(?:i(?:'ll| will| am going to|'m going to| am about to|'m about to)"
    r"|let me|voy a|d[ée]jame|i'?m(?: now)?|estoy)\s+"
    r"(?:go(?:ing)? ahead and\s+|need to\s+|have to\s+|just\s+|now\s+|quickly\s+)*"
    r"(?P<verb>flash|blink|pair|rename|check|look|see|turn|switch|dim|brighten"
    r"|play|put|flashing|blinking|pairing|renaming|checking|looking|turning"
    r"|switching|dimming|brightening|putting|playing"
    r"|do (?:that|it|this)"
    r"|encender|apagar|revisar|comprobar|ver"
    r"|open(?:ing)? the (?:zigbee )?(?:join|pairing) window)\b",
    re.IGNORECASE,
)

# Commitments recognizable without a leading "I'll": the reply scripted by
# the prompt itself ("— putting it on") and naming the join window at all.
PLAYING_CLAIM = re.compile(
    r"\b(?:now playing|(?:is|it'?s|should be) (?:now )?(?:playing|on)\b|playing now"
    r"|started (?:it|playing)|put (?:it|that) on|enjoy)",
    re.IGNORECASE,
)

SCHEDULING_TOOLS = ("light_schedule_add", "light_fade", "reminder_add", "calendar_add")

PROMISE_PHRASES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\bputting (?:it|that|this|one) on\b", re.IGNORECASE),
        ("content_play", "content_plan_playback"),
    ),
    (
        re.compile(
            r"\bopen(?:ing)? the (?:zigbee )?(?:join|pairing) window\b", re.IGNORECASE
        ),
        ("device_pair",),
    ),
    # "Paused **Thor**", "I've resumed it", "Restarted the movie": what the
    # TV was told to do. A status read is not a key press, and saying it
    # without one left the film playing through four "Paused" replies.
    (
        re.compile(
            r"(?:^|[.!?]\s+|\n)\W*(?:paused|resumed|unpaused|restarted|rewound|fast-forwarded|stopped)\b"
            r"|\bi(?:'ve| have)? (?:just )?(?:paused|resumed|unpaused|restarted|rewound|stopped)\b"
            r"|\b(?:has|have) been (?:paused|resumed|unpaused|restarted|stopped)\b"
            r"|\bis (?:now )?(?:paused|resumed|unpaused|restarted|playing again)\b",
            re.IGNORECASE,
        ),
        ("tv_press", "content_play", "tv_launch_app"),
    ),
    # "Let me check their menu to see if they have it" after a store search.
    # A search has not been near a menu, so this promise is still open — and
    # it is the whole answer to "is there a special Baja Blast?".
    (
        re.compile(r"\b(?:check|checking|look at|see|pull up)\b[^.?!]{0,40}\bmenu\b", re.IGNORECASE),
        ("doordash_menu", "doordash_order"),
    ),
    # "The automation is all set" after a light_schedule_add that failed.
    (
        re.compile(
            r"\b(?:automation|schedule|reminder|fade|routine)s?\b[^.?!]{0,40}"
            r"\b(?:set up|all set|scheduled|created|added|in place|ready)\b"
            r"|\b(?:set(?:ting)? up|schedul(?:e|ed|ing)|creat(?:e|ed|ing)|add(?:ed|ing)?)\b[^.?!]{0,30}"
            r"\b(?:automation|schedule|reminder|fade)s?\b",
            re.IGNORECASE,
        ),
        SCHEDULING_TOOLS,
    ),
)

# What running each promised verb actually looks like. An empty tuple means
# any tool call at all satisfies it ("let me check").
PROMISED_TOOLS: dict[str, tuple[str, ...]] = {
    "flash": ("device_flash",),
    "blink": ("device_flash",),
    "pair": ("device_pair",),
    "window": ("device_pair",),
    "rename": ("device_rename",),
    "turn": ("device_set",),
    "switch": ("device_set",),
    "dim": ("device_set",),
    "brighten": ("device_set",),
    "encender": ("device_set",),
    "apagar": ("device_set",),
    "play": ("content_play", "content_plan_playback"),
    "put": ("content_play", "content_plan_playback"),
    "check": (),
    "look": (),
    "see": (),
    "ver": (),
    "revisar": (),
    "comprobar": (),
}


# A reply that hands the floor to the user — asking them for the room, the
# name, a confirmation. A promise conditioned on their answer ("tell me the
# room and I'll rename it") is legitimately waiting, not stalling; forcing
# the turn onward makes a small model invent the user's answer instead.
ASKING_USER = re.compile(
    r"\b(?:let me know|tell me|what would you like|what should i call"
    r"|which (?:room|one|light)|where (?:is it|it is|is the)|are you ready"
    r"|d[ií]me|av[ií]same)\b",
    re.IGNORECASE,
)


def promised_tool_missing(
    reply: str, trace: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """The tool a reply commits to that its tool calls do not back.

    Returns (tool, status) — status "missing" when the tool never ran this
    turn, "failed" when it ran and errored (so the reply is describing work
    that did not actually happen) — or None when the promise is covered.
    """

    def succeeded(record: dict[str, Any]) -> bool:
        result = record.get("result")
        return not (isinstance(result, dict) and result.get("ok") is False)

    ran = {record.get("tool") for record in trace}
    ran_ok = {record.get("tool") for record in trace if succeeded(record)}
    asking = bool(ASKING_USER.search(reply)) or "?" in reply

    def unmet(wanted: tuple[str, ...]) -> tuple[str, str] | None:
        if not wanted:
            # Generic "let me check": hollow unless something actually ran
            # and worked — a failed flash is not the promised check.
            if not ran_ok and not asking:
                return ("device_list", "missing")
            return None
        if any(tool in ran_ok for tool in wanted):
            return None
        if any(tool in ran for tool in wanted):
            return (next(t for t in wanted if t in ran), "failed")
        if wanted[0] == "device_rename" and asking:
            # Renaming legitimately waits on the user's answer.
            return None
        return (wanted[0], "missing")

    for match in PROMISE.finditer(reply):
        verb = match["verb"].casefold()
        if verb.endswith("window"):
            verb = "window"
        elif verb.startswith("do "):
            # "Let me do that now" backed by nothing at all.
            if not ran and not asking:
                return ("the tool that does it", "missing")
            continue
        elif verb.endswith("ing"):
            base = verb[: -len("ing")]
            verb = base if base in PROMISED_TOOLS else base[:-1]
        if verb not in PROMISED_TOOLS:
            continue
        missing = unmet(PROMISED_TOOLS[verb])
        if missing:
            return missing
    for pattern, wanted in PROMISE_PHRASES:
        if pattern.search(reply):
            missing = unmet(wanted)
            if missing:
                return missing
    # A title was looked up but never started, and the reply says it is on.
    if "content_plan_playback" in ran and not ({"content_play", "tv_launch_app"} & ran_ok):
        if PLAYING_CLAIM.search(reply) and not asking:
            return ("content_play", "failed" if "content_play" in ran else "missing")
    return None


def promise_nudge(tool: str, status: str = "missing") -> str:
    if status == "failed":
        return (
            f"The {tool} call you made FAILED — nothing happened, so never "
            "describe it as done or seen. Read its error: either call the "
            "tool again corrected (use the exact device id from the latest "
            "tool results, or call device_list first to get one), or tell "
            "the user plainly what failed. What you already said has been "
            "spoken; do not repeat it. If you need an answer from the user "
            "first, ask your question and stop — never invent their answer."
        )
    return (
        f"You just told the user you are doing that, but {tool} has not run "
        f"this turn, so nothing has happened yet. The turn is still yours: "
        f"call {tool} now — with the exact device id from the latest tool "
        "notes when one is named. What you already said has been spoken; do "
        "not repeat or re-announce it. After the call, reply with only what "
        "is new. If you need an answer from the user first, ask your "
        "question and stop — never invent their answer."
    )


NO_ACTION_NUDGE = (
    "You called no tool this turn, so nothing happened and nothing was checked. "
    "Never describe an action or promise to look into something: call the tool "
    "that does it now — device_set for lights and plugs, the tv_ tools for the "
    "TV, device_list or tv_get_status to check state — or say plainly that you "
    "cannot do it. Reply again."
)


def no_action_nudge(message: str, acted_on: str | None = None) -> str:
    """The nudge, with the request quoted back and the antecedent supplied.

    "Turn them off" answered with "Done." and no call, twice: the generic
    scolding did not tell the model what "them" was, because nothing in the
    prompt did.
    """
    nudge = NO_ACTION_NUDGE
    if message:
        nudge += f' The user asked: "{message}". Do that now.'
    if acted_on:
        nudge += " " + acted_on
    return nudge
NOTHING_DONE = "I wasn't able to do that — nothing was changed."

# Small local models sometimes replay their previous reply verbatim instead
# of reading the new request ("You're welcome!" answered to "make the light
# red"). A repeat of the last assistant turn, in a turn that ran no tools,
# gets one nudge to answer what was actually asked.
REPEAT_NUDGE = (
    "That reply is an exact repeat of your previous message and does not "
    "answer what the user just said. Read their latest message and respond "
    "to it: call the tool that does what they asked, or answer it in words. "
    "Reply again."
)


# A reply that hands the request back unanswered. Narrow on purpose: it is
# about not finding something out, not about an action that failed ("couldn't
# turn the lamp on" is a real report and must stand).
GIVING_UP = re.compile(
    r"\b(?:could(?:n'?t| not)|(?:was|were)n'?t able to|unable to|failed to)\s+"
    r"(?:\w+\s+){0,3}?(?:find|fetch|get|pull|look up|retrieve|confirm|verify)\b"
    r"|\bi (?:do|don'?t) (?:not )?have (?:\w+ ){0,3}?"
    r"(?:information|details|summary|data|context)\b"
    r"|\bno (?:information|details|summary|results|sources?) (?:on|about|for|available)\b"
    r"|\bnothing (?:came back|turned up|to go on)\b"
    r"|\bno (?:pude|puedo) (?:encontrar|obtener)\b|\bno encontr[ée]\b",
    re.IGNORECASE,
)
# Offering to look something up instead of looking it up. The prompt already
# forbids it; a model that has just watched a tool fail asks anyway.
OFFERED_TO_LOOK = re.compile(
    r"\b(?:would you like|do you want|want) (?:me )?to\s+(?:\w+\s+){0,3}?"
    r"(?:search|look (?:it |that |them )?up|look for|find out|google)\b"
    r"|\b(?:should|shall|can|want me to) i?\s*(?:\w+\s+){0,3}?"
    r"(?:search (?:the web|online|for)|look (?:it|that|them) up|google)\b"
    r"|\b[¿]?(?:quieres|te gustar[íi]a) que (?:lo )?busque\b",
    re.IGNORECASE,
)
# A claim to have looked something up. DoorDash results are deliberately never
# kept (its terms forbid it), so in a food turn that called no tool at all,
# "I found the following flavors: Lime, Orange, Grapefruit" is invention: the
# model cannot be reading anything, because there is nothing to read.
STORE_CLAIM = re.compile(
    r"\bi found\b|\bhere (?:are|is)\b|\bthe following\b|\bpriced at\b|\$\d"
    r"|\bflavou?rs? (?:are|include)\b"
    r"|\b(?:they|it) (?:has|have|carr(?:y|ies)|sell|sells|offer|offers|stock|stocks"
    r"|serve|serves|comes? in)\b"
    r"|\btienen\b|\bcuesta\b",
    re.IGNORECASE,
)
# "Haggen Grocery has the Baja Blast cans" — the same claim with the store
# named. Case matters here: it is the capitals that mark a store's name, and
# they keep "what would you like to have?" out of it.
STORE_NAME_CLAIM = re.compile(
    r"\b[A-Z][\w'’&.-]*(?: [A-Z][\w'’&.-]*)* "
    r"(?:has|have|carries|sells|offers|stocks|serves)\b"
)
# The same failure in every other domain: a fact that can only have come from
# a tool, in a turn where no tool ran. Each pattern is that domain's own
# vocabulary — a temperature, a fare, a cooking time, a light's state.
#
# What is NOT here matters as much. The house line in every prompt carries
# device names, rooms, TVs and subscriptions, and the Now line carries today's
# and tomorrow's plans, so saying those back is grounded without a call. These
# patterns are about live state, live prices and catalog facts, which are in no
# prompt and cannot be remembered between turns.
DOMAIN_CLAIMS: dict[str, re.Pattern[str]] = {
    "weather": re.compile(
        r"\b\d{1,3}\s*(?:degrees|°)"
        r"|\bit(?:'s| is| will be|'ll be| gets) (?:going to be )?(?:sunny|clear|cloudy|overcast"
        r"|raining|rainy|snowing|windy|cold|hot|warm|mild|chilly|freezing)\b"
        r"|\b(?:high|low) of\b|\b(?:no|some|light|heavy) (?:rain|snow)\b"
        r"|\b(?:rain|snow|storms?|wind) (?:is|are|starts?|arrives?|expected)\b"
        r"|\b\d+\s*(?:minutes?|mins?|hours?|miles?|km)\b",  # directions rides along
        re.IGNORECASE,
    ),
    "content": re.compile(
        r"\(\d{4}\)"
        r"|\b(?:is|it'?s|that'?s|isn'?t|is not) (?:currently )?"
        r"(?:on|streaming on|available on|available)\b"
        r"|\bstars\b|\bstarring\b|\bdirected by\b|\bcreated by\b"
        r"|\b\d+ (?:seasons?|episodes?)\b|\b\d+ minutes long\b",
        re.IGNORECASE,
    ),
    "travel": re.compile(
        r"\$\d|\bnon-?stop\b|\bper night\b|\bdeparts?\b|\blayover\b"
        r"|\bround trip\b|\b\d+[- ]stars?\b",
        re.IGNORECASE,
    ),
    "recipes": re.compile(
        r"\bingredients?\b|\bpreheat\b|\bserves \d+\b|\bstep \d\b"
        r"|\b\d+\s*(?:cups?|tablespoons?|teaspoons?|tbsp|tsp|grams?|ounces?|oz|lbs?|pounds?)\b"
        r"|\b\d+\s*minutes?\b",
        re.IGNORECASE,
    ),
    # State, never existence: the house line already names every light.
    "devices": re.compile(
        r"\b(?:is|are|it'?s|they'?re) (?:currently )?(?:on|off|dimmed|set to|at \d+)\b"
        r"|\b\d+%\s*(?:brightness|bright)?\b|\bnothing is on\b",
        re.IGNORECASE,
    ),
    "tv": re.compile(
        r"\b(?:tv|television) is (?:on|off)\b|\bnothing (?:is )?(?:playing|on)\b"
        r"|\bvolume is\b|\b(?:is|it'?s) (?:currently )?(?:playing|paused|stopped)\b"
        r"|\bis open\b|\bon the (?:home screen|tuner)\b",
        re.IGNORECASE,
    ),
}
# The cart and the order are held by the house and arrive in the prompt each
# turn, so talking about those is grounded even with no call.
# Narrow on purpose: what the cart holds, not an offer to add to it. "Would
# you like to add these to your cart?" sat on the end of an invented flavor
# list and excused the whole reply.
CART_TALK = re.compile(
    r"\bin (?:your|the) cart\b|\b(?:your|the) cart (?:has|holds|contains|is)\b"
    r"|\byou ordered\b|\border (?:is|was|has been) (?:sent|placed)\b"
    r"|\btotal (?:is|comes|was)\b|\breceipt\b|\bon (?:its|the) way\b",
    re.IGNORECASE,
)
# A reply that gives up but hands over what there *is* ("no Olivia Rodrigo
# one, but they have Baja Midnight") is a good answer, not a gap. A bolded
# name is the tell: every list of real things this house reads out has one.
OFFERS_SOMETHING = re.compile(
    r"\*\*|\bthey (?:do )?(?:have|offer|carry|sell)\b|\binstead\b|\bhere (?:are|is)\b"
    r"|\bclosest\b|\bs[íi] tienen\b|\bs[íi] hay\b",
    re.IGNORECASE,
)
# What answers "what do they have?" in each domain. A model that says it does
# not know is sent here before it is sent to the web: the store's own menu
# beats a search result about the store.
LOOKUP_TOOLS: dict[str, str] = {
    "doordash": (
        "doordash_menu on the store in play, with query set to the thing being asked about — "
        "the drink or dish named anywhere in this chat ('baja blast'), or a category name from "
        "the menu ('drinks', 'desserts') to read out a whole section. Never query the question "
        "itself: 'flavors', 'options' and 'what do they have' are not on any menu"
    ),
    "content": "content_details for what a title is, and content_recommend for what to watch",
    "recipes": "recipe_search",
    "weather": "weather_now for right now, weather_forecast for later",
    "devices": "device_list",
    "tv": "tv_get_status",
    "calendar": "calendar_events",
    "travel": "flight_search for fares, hotel_search for rooms",
    "schedule": "automation_list",
}


def lookup_nudge(tool: str) -> str:
    return (
        f"You told the user you do not know that, and nothing was looked up this turn — "
        f"{tool} answers it. The turn is still yours: call it now with the words the user "
        "used, and answer from what comes back. Never say you do not have the information, "
        "that you cannot see it, or that they should check themselves, and never offer to "
        "look it up — look it up. What you already said has been spoken; do not repeat it."
    )


def widen_nudge(tool: str) -> str:
    return (
        f"{tool} answered and you still told the user you do not know. Do not hand the "
        "question back: call it again wider — drop the specific words, or pass a category "
        "from what it returned — and then tell them what is actually there, by name. If the "
        "exact thing they asked for is not among it, say so plainly and name the nearest "
        "few. What you already said has been spoken; do not repeat it."
    )


WEB_SEARCH_NUDGE = (
    "You have not looked that up, and this house has web search. The turn is still "
    "yours: call web_search now with what the user asked about, then web_fetch on the "
    "most promising result if the snippets are not enough, and answer from what you "
    "read. Never offer to look something up, and never hand the request back or answer "
    "from your own memory, while a search is available. What you already said has been "
    "spoken; do not repeat it — reply with only the answer. If the search engines "
    "themselves refuse, say that plainly instead."
)


def web_fetch_nudge(url: str) -> str:
    return (
        "You gave up while a search result was still unread. Call web_fetch on "
        f"{url} — and on the ones after it if that page refuses too — then answer from "
        "the page. The snippets you already have may be enough on their own. What you "
        "already said has been spoken; do not repeat it, reply with only the answer."
    )


def unfounded_claim(domain: str, reply: str) -> bool:
    """Whether a reply states something this domain can only learn from a tool.

    Only ever asked of a turn that called no tool at all. "It's 59 and clear",
    "Haggen Grocery has the Baja Blast cans", "**Interstellar** (2014)" — none
    of those can be in front of the model, because nothing was looked up and
    tool results do not survive the turn.
    """
    if domain == "doordash":
        if CART_TALK.search(reply):
            return False  # the cart and the order are in the prompt
        return bool(STORE_CLAIM.search(reply) or STORE_NAME_CLAIM.search(reply))
    pattern = DOMAIN_CLAIMS.get(domain)
    return bool(pattern and pattern.search(reply))


def gap_nudge(
    reply: str,
    trace: list[dict[str, Any]],
    domains: Iterable[str] = (),
    *,
    web: bool = True,
) -> str | None:
    """The nudge for a reply that hands the request back unanswered.

    Three shapes of the same failure. Nothing was looked up and a tool would
    have said ("what flavors do they have?" answered with "I don't have
    specific information" while doordash_menu sat unused). The tool answered
    and the reply still gave up, so it has to be asked again wider — they do
    not have this, here is what they do have. Or the domain's own source is
    spent and the web has not been tried. Returns None when the reply already
    offers what there is, or when the web was genuinely exhausted.
    """
    if not reply:
        return None
    if not trace and not claims_action(reply):
        # Nothing was read, so nothing can have been found. Checked before the
        # reply is credited with offering something: the something is made up.
        # A reply that reports an *action* nobody took ("Done, it's off now")
        # belongs to the action-claim guard, which says so in plainer words.
        for name in domains:
            if unfounded_claim(name, reply):
                return lookup_nudge(LOOKUP_TOOLS[name])
    if OFFERS_SOMETHING.search(reply):
        return None
    searched = [r for r in trace if r.get("tool") == "web_search"]

    def ok(record: dict[str, Any]) -> bool:
        result = record.get("result")
        return isinstance(result, dict) and bool(result.get("ok"))

    if not GIVING_UP.search(reply):
        # Asking to be allowed to look it up, having looked nowhere.
        if not OFFERED_TO_LOOK.search(reply) or searched:
            return None
        if not trace:
            for name in domains:
                if name in LOOKUP_TOOLS:
                    return lookup_nudge(LOOKUP_TOOLS[name])
        return WEB_SEARCH_NUDGE if web else None
    # The tool that knows, before the web that only reads about it.
    ran = {record.get("tool") for record in trace}
    for name in domains:
        tool = LOOKUP_TOOLS.get(name)
        if not tool:
            continue
        mine = {record for record in ran if record and domain_of_tool(str(record)) == name}
        if not mine:
            return lookup_nudge(tool)
        if any(ok(record) for record in trace if record.get("tool") in mine):
            return widen_nudge(tool)
        break  # its tool ran and failed: the web is the way on
    if not web:
        return None
    fetches = [r for r in trace if r.get("tool") == "web_fetch"]
    if any(ok(record) for record in fetches):
        return None  # a page was read; whatever it said, the web was tried
    found: list[str] = []
    for record in searched:
        if not ok(record):
            continue
        for item in (record["result"].get("results") or []):
            url = str(item.get("url") or "")
            if url and url not in found:
                found.append(url)
    if not searched:
        return WEB_SEARCH_NUDGE
    if not found:
        # Every engine refused, or the search came back empty. Saying so is
        # the honest answer, not a failure to try.
        return None
    tried = {
        str((record.get("arguments") or {}).get("url") or "") for record in fetches
    }
    unread = [url for url in found if url not in tried]
    return web_fetch_nudge(unread[0]) if unread else None


def repeats_last_reply(content: str, history: list[dict[str, Any]]) -> bool:
    if not content.strip():
        return False
    for item in reversed(history):
        if item.get("role") == "assistant":
            prev = str(item.get("content") or "")
            return " ".join(prev.casefold().split()) == " ".join(content.casefold().split())
    return False


def claims_action(reply: str) -> bool:
    return bool(ACTION_CLAIM.search(reply))


# A web address is the one thing a model reliably fabricates when it has
# run out of facts: the real one was in a tool result two turns ago, gone
# from the prompt, and the shape of a URL is easy to improvise. Every URL
# spoken must have come from a tool result, the user, or this conversation.
URL_PATTERN = re.compile(r"https?://[^\s<>\"'()\[\]]+")
URL_NUDGE = (
    "The web address in that reply does not appear in any tool result, in the "
    "user's words, or earlier in this conversation: it is invented. Never state "
    "a URL from memory. Call the tool that provides it (calendar_setup_help gives "
    "the calendar address; recipe and search tools give page addresses) and reply "
    "again with the address exactly as the tool returns it."
)
URL_WITHHELD = "(address withheld — ask me for it again)"


def _urls(text: str) -> set[str]:
    return {match.rstrip(".,;:!?*`") for match in URL_PATTERN.findall(text)}


def invented_urls(reply: str, known_text: str) -> list[str]:
    """URLs in the reply that appear nowhere the model could have read them."""
    known = _urls(known_text)
    return [url for url in _urls(reply) if url not in known]


def devices_in_play(traces: list[list[dict[str, Any]]]) -> str | None:
    """The lights and plugs this chat has already acted on, named.

    A device turn leaves nothing behind to refer to. Titles survive in the
    reply's own prose ("**Interstellar** (2014)"), but the prompt tells the
    model to answer a light with "Done." and nothing else, and the saved trace
    that holds the names never reaches the next prompt. So "turn them off" one
    message later has no antecedent, and the model that cannot find one
    answered "Done." again without calling anything — twice, on a house with
    the lights still on.
    """
    seen: dict[str, None] = {}
    for trace in traces:
        for record in trace:
            tool = str(record.get("tool") or "")
            if not tool.startswith("device_"):
                continue
            # A saved trace holds its results as JSON text (_compact_trace
            # clips them on the way to the database); a live one holds dicts.
            result = record.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    continue
            if not isinstance(result, dict) or result.get("ok") is False:
                continue
            entries = result.get("devices") or ([result["device"]] if result.get("device") else [])
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                room = entry.get("room")
                seen[f"{entry['name']}" + (f" ({room})" if room else "")] = None
    if not seen:
        return None
    names = list(seen)[-6:]
    return (
        "Devices this chat has already acted on: " + "; ".join(names) + ". A follow-up that "
        "says them, those, it, both, all of them, or again means exactly these: pass these "
        "names to device_set (or their room, which covers every light in it) and make the "
        "call. Never ask which ones, and never answer a device request without the call."
    )


def _titles_offered(history: list[dict[str, Any]]) -> str | None:
    """The titles named in the last reply, handed back as fact.

    Without this the model resolves "the 2014 one" or "what were the others"
    against its own remembered prose, and a small model re-reads that prose
    loosely -- renaming titles, re-dating them, and dropping half the list.
    Only the latest reply counts; an older list is stale by definition.
    """
    for item in reversed(history):
        if item["role"] != "assistant":
            continue
        seen: dict[str, None] = {}
        for match in OFFERED_TITLE.finditer(item["content"] or ""):
            year = match["year"]
            seen[f"{match['title'].strip()}" + (f" ({year})" if year else "")] = None
        if not seen:
            return None
        return (
            "Titles you offered in your last reply, in order: "
            + "; ".join(seen)
            + ". Resolve every follow-up reference — \u201cthe 2014 one\u201d, \u201cthe second one\u201d, "
            "\u201cthe others\u201d — against exactly this list, and repeat each title and year "
            "exactly as written here. \u201cThe others\u201d means the entries in this list you "
            "have not just been asked about. If nothing in the list matches what "
            "they said, say so instead of guessing."
        )
    return None


# What of a DoorDash tool call may be kept in the saved trace. DoorDash's CLI
# terms forbid storing what it returns (menus, prices, orders, addresses), so
# only the outcome survives — enough to see that a call failed, not what it
# saw.
_DOORDASH_KEPT_ARGUMENTS = frozenset(
    {"action", "kind", "query", "days", "limit", "confirmed", "tip_cents", "fulfillment", "grocery", "priority"}
)
_DOORDASH_KEPT_RESULT = ("ok", "error", "needs_setup", "accepted", "placed", "status", "cart_source", "tip_source")
_SECRET_KEY = re.compile(r"password|passphrase|secret|token", re.IGNORECASE)


def _compact_trace(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn's tool calls, results clipped, so a failure can be
    reconstructed from the database later instead of guessed at."""
    compact: list[dict[str, Any]] = []
    for record in trace:
        tool = record.get("tool")
        arguments = record.get("arguments")
        raw = record.get("result")
        if isinstance(arguments, dict) and any(_SECRET_KEY.search(key) for key in arguments):
            arguments = {
                key: ("•••" if _SECRET_KEY.search(key) else value) for key, value in arguments.items()
            }
        if str(tool or "").startswith("doordash_"):
            arguments = {
                key: value
                for key, value in (arguments or {}).items()
                if key in _DOORDASH_KEPT_ARGUMENTS
            } if isinstance(arguments, dict) else None
            raw = {
                **{key: raw[key] for key in _DOORDASH_KEPT_RESULT if isinstance(raw, dict) and key in raw},
                "redacted": "DoorDash data is not kept",
            }
        result = json.dumps(raw, ensure_ascii=False, default=str)
        compact.append(
            {
                "tool": tool,
                "arguments": arguments,
                "result": result if len(result) <= 1500 else result[:1500] + "…",
            }
        )
    return compact


DOORDASH_GUIDANCE = (
    "Food, groceries, and household items are ordered from DoorDash with the doordash_ tools; "
    "never quote a menu, a price, a delivery time, or a past order from memory. Someone who is "
    "hungry, wants dinner, or asks for something to eat wants food delivered unless they said "
    "cook, make, or recipe: stay on DoorDash and do not offer recipes. Ordering is one call: "
    "doordash_order with the place and the dishes in the user's words ('a cheeseburger from "
    "McDonald's' is store 'McDonald's', items [{name: 'cheeseburger', quantity: 1}]) — it finds "
    "the store, picks the nearest open branch, finds the dish, builds the cart, and prices it; "
    "never list branches of a chain or search first when the place and dish are known, and "
    "when the user names a place after a dish or a dish after a place, put both together from "
    "the conversation. When a DoorDash line is present in this turn, it is the truth about what "
    "was already found and what is in the cart: continue from it with its ids, never search "
    "again for the same thing, and never say a store, item, cart, or menu id aloud — call a "
    "store by name. Searching is only for browsing: an open-ended 'what looks good' is "
    "doordash_search with query 'food' near home, or doordash_order_history for their usual "
    "places — never invent a cuisine they did not mention. A 'under $20' kind of limit is "
    "answered from the menu prices doordash_menu returns for one store, not from a search. "
    "Anything about what a place sells — what they have, which meals or sizes, what a dish "
    "costs — is doordash_menu on that store. You can always see the menu: never say you "
    "cannot look one up, cannot see real-time information, or that they should check the "
    "restaurant themselves. When a menu result carries no_match, or says its items are the "
    "closest ones, the menu came back fine and the words simply are not an item name: name "
    "two or three real items from what it returned and ask which they want, rather than "
    "saying it was not found. "
    "A search finds stores, never items: 'is there a special Baja Blast', 'do they have "
    "churros', 'what flavors are there' are all doordash_menu on the store with the user's "
    "own words as query — finding the store is not finding the item, and a limited edition "
    "is real only when it is on the menu you read. One call answers more than one question: "
    "when the thing they asked for is not on the menu, say so and in the same breath name "
    "what is, and when they ask what kinds or flavors there are, name every matching item "
    "the menu returned. To read out a whole section, call again with the category name from "
    "the result's categories ('drinks', 'desserts'); the first items of a menu are a sample, "
    "never that section. Never answer a question about a store from nothing. "
    "Delivery goes to home; pass address only for a saved label the user said in this very "
    "request. A city the user mentions while ordering is where home already is, not a weather "
    "or address change. A saved favorite (doordash_favorite, also listed in the house line) is "
    "a place and the usual order in the household's own words. 'The usual', 'my regular', or "
    "'reorder' means doordash_order_history for usual_places, then doordash_reorder with "
    "store_name, which builds the cart and prices it in one call. When a result lists "
    "required_choices, ask that one question and call doordash_order again for just that item "
    "with options. A grocery run is doordash_grocery_list, which can take the items from "
    "list_show. Once a result carries a total, read back in one breath the items, the total, "
    "the delivery or pickup time, and the card: 'Want me to place this on your Visa ending 1234 "
    "with the suggested $4 tip?' — on a delivery always name the tip. Never ask 'ready to "
    "order?' without the total and the card. Only a clear yes ('yes', 'do it', 'place it', 'go "
    "ahead', 'the suggested tip is fine') gets doordash_order_place with confirmed true and tip "
    "'suggested', 'none', or the amount they named — no cart_uuid and no preview first, the house "
    "holds the cart and its price; silence, 'maybe', or a new question is not a yes, and never place an "
    "order nobody asked for in this conversation. When the result says accepted, say the order "
    "is sent and that you will announce when the store confirms it; call it placed only when a "
    "tool result or a later announcement says so. A needs_setup result with sign_in_link_sent "
    "means DoorDash is not signed in and the sign-in link is already in their messages: say "
    "that, ask them to open it and sign in, and that you will announce when it is done — never "
    "tell them to run a command. On a phone the last page will not load and they should paste "
    "its address here; when they paste an address containing code=, call doordash_login with "
    "it as callback_url. Menus and prices come from the merchants and can be off; anyone with "
    "an allergy should check with the restaurant.\n"
)


# Tools whose repeat within one turn is the point: a second flash, another
# key press, one more message. Everything else repeated verbatim is a stall.
_REPEATABLE = ("device_flash", "device_pair", "tv_press", "tv_set_volume", "tv_type_text", "inbox_send", "list_add")


def _repeatable(tool: str) -> bool:
    return tool in _REPEATABLE


# A tool result the model gets to read is capped: a full menu or a long web
# page would push the conversation out of the context window.
_RESULT_CHARS = 8000


# One turn's tool traffic, held back from history so that the results arriving
# later in the turn have somewhere to go. Three capped results plus their tool
# calls: past that, shrink_to_fit takes over.
TOOL_WORK_RESERVE = 6200
SHRUNK_RESULT = (
    '{"note": "This earlier result was dropped to make room in the context. '
    'Call the tool again if you still need what it said."}'
)


def shrink_to_fit(
    messages: list[dict[str, Any]], budget_tokens: int
) -> tuple[int, int]:
    """Empty the oldest tool results until the prompt fits. Returns (dropped, tokens).

    History is fitted before the turn starts, but the turn then appends a tool
    call and a result per round — up to ten of them, 2000 tokens each. Nothing
    was holding that against the context, and Ollama's answer to an over-long
    prompt is to keep the *last* half: the system prompt and every tool schema
    go, which is the exact failure routing exists to prevent.

    A result that has already been acted on is the cheapest thing in the
    prompt to give up. Emptying it rather than removing it keeps every
    tool_call paired with a result, which the API requires.
    """
    def size(message: dict[str, Any]) -> int:
        return estimate_tokens(str(message.get("content") or "")) + (
            estimate_tokens(_dumps_calls(message)) if message.get("tool_calls") else 0
        )

    total = sum(size(message) for message in messages)
    dropped = 0
    for message in messages:
        if total <= budget_tokens:
            break
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if content == SHRUNK_RESULT:
            continue
        total -= estimate_tokens(content) - estimate_tokens(SHRUNK_RESULT)
        message["content"] = SHRUNK_RESULT
        dropped += 1
    return dropped, total


def _dumps_calls(message: dict[str, Any]) -> str:
    try:
        return json.dumps(message.get("tool_calls"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _bounded(text: str) -> str:
    if len(text) <= _RESULT_CHARS:
        return text
    return text[:_RESULT_CHARS] + '… (result cut here; ask for less, e.g. pass a query or a smaller limit)"}'


def strip_planner_fragment(text: str) -> str:
    return PLANNER_FRAGMENT.sub("", text, count=1)


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class AgentResult:
    conversation_id: str
    message: str
    trace: list[dict[str, Any]]


class LocalAgent:
    COMPACT_AFTER_MESSAGES = 24
    COMPACT_KEEP_MESSAGES = 10

    def __init__(self, config: LLMConfig, database: Database, tools: ToolRegistry) -> None:
        self.config = config
        # The configured local model, kept so a cloud choice can fall back
        # to it (and Settings can switch back) without re-reading config.
        self.local_config = config
        self.provider = providers.LOCAL
        self.fell_back: str | None = None  # why the last cloud call went local
        self.database = database
        self.tools = tools
        self.system_prompt = SYSTEM_PROMPT
        if tools.config.matter.enabled and not tools.config.matter.bluetooth:
            self.system_prompt += (
                "When someone wants to add a new Matter device, tell them up front, "
                "before asking for any code, that it must first be set up in its "
                "vendor app so it joins the home WiFi — this controller has no "
                "Bluetooth to do that step itself. Once they confirm that is done, "
                "continue with the power-cycle and pairing code.\n"
            )
        if tools.doordash.available:
            self.system_prompt += DOORDASH_GUIDANCE
        else:
            self.system_prompt += (
                "Food delivery is not set up on this house: if someone asks to order food or "
                "groceries, say DoorDash ordering needs dd-cli installed and signed in on the "
                "house computer, and offer the shopping list or a recipe instead.\n"
            )
        if not tools.config.catalog.exact_episode_playback_enabled:
            self.system_prompt += (
                "Exact season-and-episode playback is currently disabled because the catalog "
                "plan does not provide episode deep links. Do not call content tools with season "
                "or episode. When someone asks for a particular episode — the newest, the last "
                "one, or one by number — call content_episodes first and tell them which episode "
                "it is by season, number and name, then play the series and say to pick that one "
                "in the app. Never start a series as though the episode had been honored, and "
                "never say you cannot tell which episode is newest.\n"
            )
        self.router = Router(self.system_prompt, tools.schemas, config.tool_budget_tokens)
        self.context_tokens = config.context_tokens
        self._local_context = config.context_tokens
        self._turn_schemas: list[dict[str, Any]] | None = None  # the current turn's routed tools

    def use_model(self, provider: str, model: str, api_key: str = "", base_url: str = "") -> None:
        """Switch where replies come from. Local models keep the configured
        Ollama address; cloud ones use the provider's, and a custom endpoint
        the address the household gave."""
        spec = providers.PROVIDERS[provider]
        if provider == providers.LOCAL:
            self.config = replace(self.local_config, model=model or self.local_config.model)
            self.context_tokens = self._local_context
        else:
            self.config = replace(
                self.local_config, base_url=base_url or spec.base_url, model=model, api_key=api_key or "none"
            )
            self.context_tokens = spec.context_tokens
        self.provider = provider
        self.fell_back = None
        log.info("LLM: now using %s model %s", spec.label, self.config.model)

    async def probe_context(self) -> int | None:
        """Ask the server how much context the model really has, and never
        plan for more than that. Ollama answers on /api/show; anything else
        keeps the configured value."""
        if self.provider != providers.LOCAL:
            return None
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{base}/api/show", json={"name": self.config.model})
                response.raise_for_status()
                parameters = str(response.json().get("parameters") or "")
        except Exception:  # noqa: BLE001 - not Ollama, or not up yet; fine
            return None
        match = re.search(r"num_ctx\s+(\d+)", parameters)
        if not match:
            log.warning(
                "LLM context: %s has no window of its own, so Ollama gives it its default and "
                "long turns lose their start. Pick it again in Settings to build its house- version.",
                self.config.model,
            )
            return None
        actual = int(match.group(1))
        self.context_tokens = min(self._local_context, max(actual, 2048))
        if actual < self._local_context:
            log.warning(
                "LLM context: %s serves %d tokens but config says %d; using %d",
                self.config.model, actual, self.context_tokens, actual,
            )
            self.context_tokens = actual
        return actual

    def _routed(self, message: str, history: list[dict[str, Any]], conversation_id: str):
        if not self.config.route_tools:
            return self.router.everything()
        return self.router.route(
            message,
            history,
            active=self.tools.active_domains(conversation_id),
            recent=self.tools.recent_domains(conversation_id),
        )

    def _gap_domains(self, message: str, conversation_id: str) -> list[str]:
        """Domains this request or this chat actually points at, nearest first.

        Deliberately not the routing defaults: routing falls back to TV,
        lights and weather when a message names nothing, and a film question
        answered with "I don't know" must not then be sent to tv_get_status.
        """
        ordered: list[str] = []
        for name in [
            *sorted(domains_mentioned(message)),
            *self.tools.active_domains(conversation_id),
            *self.tools.recent_domains(conversation_id),
        ]:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _house_state(self) -> str | None:
        """One compact line of ground truth the model would otherwise spend a
        tool round discovering: device names, rooms, TVs, subscriptions. Only
        stable facts — no live on/off state — so the prompt prefix stays
        cacheable between turns."""
        parts: list[str] = []
        try:
            devices = self.tools.smarthome.devices()
        except Exception:  # noqa: BLE001 - state line must never break a turn
            devices = []
        if devices:
            names = "; ".join(
                device.name
                + (f" ({device.room}, {device.kind})" if device.room else f" ({device.kind})")
                for device in devices[:12]
            )
            parts.append(
                f"Smart devices: {names}. Use these names with device_set directly; "
                "call device_list only when current on/off state matters."
            )
        else:
            offline = [
                name
                for name, s in self.tools.smarthome.status().items()
                if s["enabled"] and not s["connected"]
            ]
            if offline:
                parts.append(
                    "Smart-device backend offline: " + ", ".join(offline) + ". If asked "
                    "to control lights, say the controller is not reachable."
                )
        if self.tools.devices:
            parts.append("Televisions: " + ", ".join(self.tools.devices) + ".")
        if self.tools.doordash.available:
            try:
                favorites = self.database.list_food_favorites()
            except Exception:  # noqa: BLE001
                favorites = []
            if favorites:
                parts.append(
                    "Favorite food places: "
                    + "; ".join(
                        str(f["name"]) + (f" (usual: {f['usual_order']})" if f.get("usual_order") else "")
                        for f in favorites[:8]
                    )
                    + ". Search these by name on DoorDash when asked for them."
                )
        try:
            home = self.tools.weather.home()
        except Exception:  # noqa: BLE001
            home = None
        if home:
            parts.append(
                f"The house is in {home[2]}; a weather question with no place named "
                "means here, so call weather tools with no location argument."
            )
        try:
            subscriptions = self.database.list_subscriptions(enabled_only=True)
        except Exception:  # noqa: BLE001
            subscriptions = []
        if subscriptions:
            parts.append(
                "Subscriptions: "
                + ", ".join(item["provider"] for item in subscriptions)
                + "."
            )
        return " ".join(parts) if parts else None

    def _clock_line(self) -> str:
        """The time, and what is coming up. Plans ride along with the clock
        so the model knows about the four o'clock party when someone says
        they are heading out, without a tool call it would not think to make."""
        now = local_now()
        line = (
            f"Now: {now.strftime('%A %Y-%m-%d %H:%M')} local time. Compute every "
            "reminder, schedule, or 'tomorrow' from this."
        )
        try:
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            events = self.database.list_events(
                today.isoformat(timespec="minutes"),
                (today + timedelta(days=2)).isoformat(timespec="minutes"),
            )
        except Exception:  # noqa: BLE001 - the clock must never break a turn
            events = []
        if events:
            spoken = []
            for event in events[:8]:
                start = datetime.fromisoformat(event["starts_at"])
                day = "today" if start.date() == now.date() else "tomorrow"
                where = f" at {event['location']}" if event.get("location") else ""
                who = f" ({event['profile']})" if event.get("profile") else ""
                clock = "all day" if event.get("all_day") else start.strftime("%-I:%M %p")
                spoken.append(f"{event['title']}{where} {day} {clock}{who}")
            line += " Plans: " + "; ".join(spoken) + "."
        return line

    def _style_line(self, speaker: dict[str, Any]) -> str | None:
        """How this person has asked to be spoken to.

        These are standing instructions someone gave out loud, so they belong
        in every turn rather than behind a tool call the model has to think to
        make -- an unread preference is the same as one never given.
        """
        household = self.tools.config.household_profile
        profile = speaker.get("profile")
        wanted = [household] if not profile or profile == household else [profile, household]
        saved = self.database.list_style_preferences(wanted)
        if not saved:
            return None

        def render(items: list[dict[str, Any]]) -> str:
            return "\n".join(
                f"- (everyone) {item['note']}"
                if item["profile"] == household
                else f"- ({item['profile']}) {item['note']}"
                for item in items
            )

        styles = [item for item in saved if item.get("kind", "style") != "rule"]
        rules = [item for item in saved if item.get("kind") == "rule"]
        parts: list[str] = []
        if styles:
            parts.append(
                "How replies should be written for whoever is speaking, in their own "
                "words. These were asked for out loud, and they override the default "
                "reply and recommendation formatting wherever the two disagree:\n"
                + render(styles)
            )
        if rules:
            parts.append(
                "Standing rules this household wrote down for you. Follow them on "
                "every turn, before anything else in this prompt that disagrees:\n"
                + render(rules)
            )
        return "\n\n".join(parts)

    def _missing_name_question(self, conversation_id: str, reply: str) -> str:
        """The name question to append, when the model was asked to put it and did not.

        Being handed the instruction is not the same as obeying it: a small
        model working through a long prompt will answer the question in front
        of it and drop the rest. So the reply is checked, and the question is
        added if it is not already there — the person gets asked exactly once
        either way, and only then is it recorded as asked.
        """
        voices = self.tools.voices
        if not voices.should_ask(conversation_id):
            return ""
        if voices.sounds_like_a_name_question(reply):
            voices.mark_asked(conversation_id)
            return ""
        log.info("voice: model did not ask for a name; asking on its behalf")
        voices.mark_asked(conversation_id)
        return voices.name_question(spanish=looks_spanish(reply))

    async def chat(self, message: str, conversation_id: str | None = None) -> AgentResult:
        final: AgentResult | None = None
        async for event in self.chat_stream(message, conversation_id):
            if event["type"] == "done":
                final = AgentResult(event["conversation_id"], event["message"], event["trace"])
        if final is None:
            raise LLMError("Model stream ended without a final message")
        return final

    async def chat_stream(
        self,
        message: str,
        conversation_id: str | None = None,
        *,
        spoken: bool = True,
        typed_by: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one agent turn, yielding events as it goes.

        Events: {"type": "token", "text"} for each piece of the final reply,
        {"type": "tool", "name"} when a tool starts, {"type": "thinking"} before
        a planning request's reasoning step, {"type": "reset"} if
        streamed text turned out to belong to a tool round and must be
        discarded, and one final {"type": "done", "conversation_id",
        "message", "trace"}.
        """
        message = message.strip()
        if not message:
            raise ValueError("Message cannot be empty")
        conversation_id = conversation_id or self.database.new_conversation_id()
        history_state = self.database.get_history(conversation_id)
        summary = history_state["summary"]
        history = history_state["messages"]
        if len(history) > self.COMPACT_AFTER_MESSAGES:
            # Fold older turns into the running summary. Besides bounding
            # context, this keeps the prompt PREFIX stable between turns, so
            # the model server's cache hits instead of re-reading everything.
            kept = history[-self.COMPACT_KEEP_MESSAGES :]
            folded = history[: -self.COMPACT_KEEP_MESSAGES]
            try:
                summary = await self._summarize(summary, folded)
                self.database.save_conversation_summary(
                    conversation_id, summary, folded[-1]["id"]
                )
                history = kept
            except LLMError:
                history = history[-30:]  # compaction failed; just bound it
        self.database.add_message(conversation_id, "user", message)
        new_conversation = not history and not summary
        speaker_line = self.tools.voices.context(
            conversation_id, new_conversation=new_conversation
        )
        onboarding, next_status = setup_line(
            self.database.get_setting(SETUP_KEY, NOT_STARTED) or NOT_STARTED,
            first_run=self.tools.voices.first_run,
            new_conversation=new_conversation,
            has_notes=self.database.has_profile_notes(),
        )
        if next_status:
            self.database.set_setting(SETUP_KEY, next_status)
        if onboarding:
            speaker_line = f"{speaker_line}\n{onboarding}" if speaker_line else onboarding
        speaker = self.tools.voices.speaker(conversation_id) or {}
        if typed_by and speaker.get("status") != "identified":
            # Typed on a screen that knows its person: the same profile,
            # rules and reply style a recognised voice would get.
            speaker = {"status": "identified", "profile": typed_by, "name": typed_by, "source": "screen"}
            speaker_line = (
                f"Speaker: {typed_by} (typing on their own screen). Use this profile for "
                "taste memory, recommendations, and feedback. Address them by name now and "
                "then where it feels natural — never in most replies."
                + (f"\n{onboarding}" if onboarding else "")
            )
        reported_speaker = speaker if typed_by else None
        CURRENT_SPEAKER.set({**speaker, "conversation_id": conversation_id})
        CURRENT_REQUEST.set(message)
        house_state = self._house_state()
        style_line = self._style_line(speaker)
        clock_line = self._clock_line()
        offered = _titles_offered(history)
        acted_on = devices_in_play(self.database.recent_traces(conversation_id))
        food = self.tools.food_context(conversation_id)
        routed = self._routed(message, history, conversation_id)
        tv_live = None
        if {"tv", "content"} & set(routed.domains):
            try:
                tv_live = await self.tools.tv_live_line()
            except Exception:  # noqa: BLE001 - live TV state is a nicety, never a failure
                tv_live = None
        # Everything but history has to fit, then history gets what is left;
        # an over-long prompt would lose its head, which is the prompt itself.
        fixed_tokens = (
            routed.prompt_tokens + routed.schema_tokens
            + sum(estimate_tokens(t) for t in (house_state, summary, clock_line, tv_live, speaker_line, style_line, offered, acted_on, food, message))
        )
        reply_reserve = 2400 if (self.config.think_complex and self._complex_request(message)) else 1200
        # History gets what is left after the reply AND after room for the
        # tool results this turn will collect; it used to get all of it, and a
        # ten-round turn on a full history overran the context by more than it
        # held.
        prompt_budget = self.context_tokens - reply_reserve - 400
        room = prompt_budget - fixed_tokens - TOOL_WORK_RESERVE
        history, dropped_turns = fit_history(history, max(room, 0))
        timing_log.info(
            "TIMING prompt: domains=%s tools~%d guidance~%d history=%d msgs (%d dropped) fixed~%d ctx=%d%s",
            ",".join(routed.domains), routed.schema_tokens, routed.prompt_tokens, len(history), dropped_turns,
            fixed_tokens, self.context_tokens, f" domains dropped={routed.dropped}" if routed.dropped else "",
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": routed.prompt},
            *([{"role": "system", "content": house_state}] if house_state else []),
            *(
                [{"role": "system", "content": f"Summary of the conversation so far:\n{summary}"}]
                if summary
                else []
            ),
            *({"role": item["role"], "content": item["content"]} for item in history),
            # Last, beside the user's own words: an instruction placed ahead of
            # a dozen history turns is one a small model reads and forgets.
            {"role": "system", "content": clock_line},
            *([{"role": "system", "content": tv_live}] if tv_live else []),
            *([{"role": "system", "content": speaker_line}] if speaker_line else []),
            *([{"role": "system", "content": style_line}] if style_line else []),
            *([{"role": "system", "content": offered}] if offered else []),
            *([{"role": "system", "content": acted_on}] if acted_on else []),
            *([{"role": "system", "content": food}] if food else []),
            {"role": "user", "content": message},
        ]
        trace: list[dict[str, Any]] = []

        turn_started = time.monotonic()
        failed_calls: dict[str, int] = {}
        # Successful calls this turn, by exact signature. A small model that
        # loses its place re-issues the same search or web fetch two or three
        # times in a row; the second copy gets the first answer back at once.
        done_calls: dict[str, dict[str, Any]] = {}
        # Thinking is spent surgically: the first round of a complex request
        # gets it (that is where planning happens); every other round, and
        # all of a simple command, runs with reasoning off for speed.
        # Tools that end a turn. Once someone's name is settled, the only thing
        # left to do is say so: given another tool round the model reaches for
        # the previous request and answers it a second time instead.
        CLOSING_TOOLS = ("voice_enroll", "voice_stay_anonymous")
        closing = False
        nudged = False  # one second chance for a reply that claims work no tool did
        repeat_nudged = False  # likewise for a verbatim repeat of the last reply
        url_nudged = False  # likewise for a reply that states a made-up web address
        gap_rounds = 0  # two chances for a reply that hands the request back
        # Qwen3 runs with thinking off: it is ten times faster and calls tools
        # correctly for anything stated outright. What it loses is the terse
        # follow-up — "turn them off" two messages after the lights went on
        # came back as "Done." with no call at all, twice, measured 0/3 with
        # thinking off and 3/3 with it on for ~2s more. So the first thing a
        # nudge does is give the model back its reasoning step.
        think_harder = False
        # Sending the model to the web is the household's choice; sending it
        # to the tool that already knows never was.
        web_searchable = web_fallback_enabled(self.database, "")
        # Interim replies already spoken this turn ("I'll flash it now") kept
        # while the model goes on to do the promised work. The final reply is
        # everything joined; the client already heard the early segments live.
        segments: list[str] = []
        promise_rounds = 0
        # After an interim segment is kept, later rounds' text is buffered
        # instead of streamed: a "reset" would wipe the kept segment from the
        # client, so deliberation must never reach it in the first place.
        hold_stream = False
        known_text = message + " " + " ".join(str(item.get("content") or "") for item in history)
        # Spoken and typed requests think alike: a short follow-up ("turn them
        # off") for a second or two, a planning request for longer. The client
        # is told, so a spoken turn says "let me think" instead of going quiet.
        think_first = self.config.think_complex and (
            self._leans_on_the_last_turn(message) or self._complex_request(message)
        )
        if think_first:
            timing_log.info("TIMING llm: complex request, thinking on for round 1")
            if self._complex_request(message):
                yield {"type": "thinking"}
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            for round_number in range(1, self.config.max_tool_rounds + 1):
                response_message: dict[str, Any] = {}
                streamed_text = False
                round_started = time.monotonic()
                first_activity: float | None = None
                token_count = 0
                held = ""  # opening text held back until the planner check
                releasing = False
                effort = (
                    "low"
                    if think_harder or (think_first and round_number == 1)
                    else "minimal"
                )
                self._turn_schemas = routed.schemas
                shrunk, prompt_tokens = shrink_to_fit(messages, prompt_budget)
                if shrunk:
                    timing_log.info(
                        "TIMING prompt: round %d over budget; emptied %d earlier tool "
                        "result(s), now ~%d of %d tokens",
                        round_number, shrunk, prompt_tokens, self.context_tokens,
                    )
                async for kind, data in self._stream_completion(
                    client, messages, effort, allow_tools=not closing
                ):
                    if first_activity is None:
                        first_activity = time.monotonic() - round_started
                    if kind == "token":
                        token_count += 1
                        if hold_stream:
                            continue  # emitted later, once its role is known
                        streamed_text = True
                        if releasing:
                            yield {"type": "token", "text": data}
                            continue
                        held += data
                        if len(held) >= HOLD_CHARS or "\n" in held:
                            releasing = True
                            cleaned = strip_planner_fragment(held)
                            if cleaned:
                                yield {"type": "token", "text": cleaned}
                            held = ""
                    elif kind == "tool_start":
                        # Early notice: lets the client speak its "on it"
                        # ack seconds before the tool round completes.
                        yield {"type": "tool", "name": data}
                    else:
                        response_message = data
                if held and not releasing:
                    cleaned = strip_planner_fragment(held)
                    if cleaned:
                        yield {"type": "token", "text": cleaned}
                timing_log.info(
                    "TIMING llm round %d: first activity %.2fs, %d text tokens, "
                    "round total %.2fs",
                    round_number,
                    first_activity or 0.0,
                    token_count,
                    time.monotonic() - round_started,
                )
                tool_calls = response_message.get("tool_calls") or []
                if not tool_calls and not (response_message.get("content") or "").strip():
                    # The model spent its whole budget thinking and produced
                    # nothing. Retry the round with reasoning off.
                    timing_log.info("TIMING llm round %d: empty; retrying without reasoning", round_number)
                    async for kind, data in self._stream_completion(client, messages, "none"):
                        if kind == "token":
                            if hold_stream:
                                continue
                            streamed_text = True
                            yield {"type": "token", "text": data}
                        elif kind == "tool_start":
                            yield {"type": "tool", "name": data}
                        else:
                            response_message = data
                    tool_calls = response_message.get("tool_calls") or []
                if not tool_calls:
                    content = strip_planner_fragment(
                        str(response_message.get("content") or "")
                    ).strip()
                    succeeded = any(
                        isinstance(record.get("result"), dict) and record["result"].get("ok")
                        for record in trace
                    )
                    if not content and succeeded:
                        content = "Done."
                    if hold_stream and content:
                        # Buffered round turned out to be reply text: emit it.
                        yield {"type": "token", "text": "\n" + content}
                        streamed_text = True
                    # A reply that commits to tool work not yet done does not
                    # end the turn: keep it as an interim message and send the
                    # model straight back to make the call it promised.
                    unbacked = (
                        promised_tool_missing(content, trace)
                        if content and not closing and promise_rounds < 3
                        else None
                    )
                    if unbacked:
                        tool_name, status = unbacked
                        promise_rounds += 1
                        think_harder = True
                        timing_log.info(
                            "TIMING llm round %d: promised %s (%s); continuing",
                            round_number,
                            tool_name,
                            status,
                        )
                        segments.append(content)
                        messages.append({"role": "assistant", "content": content})
                        messages.append(
                            {"role": "system", "content": promise_nudge(tool_name, status)}
                        )
                        hold_stream = True
                        continue
                    # Handing the request back unanswered — nothing looked
                    # up, or looked up too narrowly, or the web untouched.
                    # None of those is an answer, and the turn is still open.
                    # A nudge that got nothing looked up is not tried again: the
                    # model has answered from what it knows, and asking twice
                    # more only costs the listener another 10-20 seconds.
                    # Nor once a spoken *answer* is already out loud: the
                    # listener has heard it, and a second, rewritten answer
                    # after it is what sounded like Friday repeating itself.
                    # A spoken give-up ("I don't have that") is still worth
                    # following with the real answer.
                    if (
                        content
                        and not closing
                        and gap_rounds < 2
                        and not (gap_rounds and not trace)
                        and not (spoken and streamed_text and not GIVING_UP.search(content))
                    ):
                        nudge = gap_nudge(
                            content,
                            trace,
                            self._gap_domains(message, conversation_id),
                            web=web_searchable,
                        )
                        if nudge:
                            gap_rounds += 1
                            think_harder = True
                            timing_log.info(
                                "TIMING llm round %d: handed the request back; nudging",
                                round_number,
                            )
                            if streamed_text:
                                yield {"type": "reset"}
                                segments.clear()
                            hold_stream = False
                            messages.append({"role": "assistant", "content": content})
                            messages.append({"role": "system", "content": nudge})
                            continue
                    if not trace and not closing and (not content or claims_action(content)):
                        # Reported or promised work, with no tool behind it.
                        if not nudged:
                            nudged = True
                            think_harder = True
                            timing_log.info(
                                "TIMING llm round %d: action claimed with no tool; nudging",
                                round_number,
                            )
                            if streamed_text:
                                yield {"type": "reset"}
                                segments.clear()
                                hold_stream = False
                            messages.append({"role": "assistant", "content": content or "Done."})
                            messages.append(
                                {"role": "system", "content": no_action_nudge(message, acted_on)}
                            )
                            continue
                        if streamed_text:
                            yield {"type": "reset"}
                            segments.clear()
                        content = NOTHING_DONE
                        yield {"type": "token", "text": content}
                    elif not trace and not closing and repeats_last_reply(content, history):
                        if not repeat_nudged:
                            repeat_nudged = True
                            timing_log.info(
                                "TIMING llm round %d: reply repeats previous turn; nudging",
                                round_number,
                            )
                            if streamed_text:
                                yield {"type": "reset"}
                                segments.clear()
                                hold_stream = False
                            messages.append({"role": "assistant", "content": content})
                            messages.append({"role": "system", "content": REPEAT_NUDGE})
                            continue
                        # Still repeating after the nudge: let it stand — a
                        # repeated pleasantry beats a false "nothing changed".
                    elif not content:
                        content = NOTHING_DONE
                    invented = invented_urls(
                        content, known_text + json.dumps(trace, ensure_ascii=False)
                    )
                    if invented:
                        if not url_nudged:
                            url_nudged = True
                            timing_log.info(
                                "TIMING llm round %d: invented URL %s; nudging",
                                round_number,
                                invented[0],
                            )
                            if streamed_text:
                                yield {"type": "reset"}
                                segments.clear()
                                hold_stream = False
                            messages.append({"role": "assistant", "content": content})
                            messages.append({"role": "system", "content": URL_NUDGE})
                            continue
                        if streamed_text:
                            yield {"type": "reset"}
                            segments.clear()
                        for url in invented:
                            content = content.replace(url, URL_WITHHELD)
                        yield {"type": "token", "text": content}
                    asking = self._missing_name_question(conversation_id, content) if spoken else ""
                    if asking:
                        # Emitted as tokens so it is spoken as well as shown:
                        # the client only synthesizes what it streamed.
                        yield {"type": "token", "text": asking}
                        content += asking
                    if segments:
                        # Interim promises spoken earlier in the turn belong
                        # to the reply the user heard.
                        content = "\n".join([*segments, content])
                    self.database.add_message(
                        conversation_id,
                        "assistant",
                        content,
                        metadata={"trace": _compact_trace(trace)},
                    )
                    timing_log.info(
                        "TIMING turn total %.2fs (%d tool calls)",
                        time.monotonic() - turn_started,
                        len(trace),
                    )
                    yield {
                        "type": "done",
                        "conversation_id": conversation_id,
                        "message": content,
                        "trace": trace,
                        "speaker": reported_speaker or self.tools.voices.speaker(conversation_id),
                    }
                    return

                if streamed_text:
                    # Text emitted alongside tool calls is deliberation, not
                    # the reply; tell the client to throw it away.
                    yield {"type": "reset"}
                messages.append(response_message)
                for call in tool_calls:
                    call_id = str(call.get("id", "tool-call"))
                    function = call.get("function") or {}
                    name = str(function.get("name", ""))
                    yield {"type": "tool", "name": name}
                    tool_started = time.monotonic()
                    signature = None
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be a JSON object")
                        signature = f"{name}:{json.dumps(arguments, sort_keys=True)}"
                        if failed_calls.get(signature, 0) >= 2:
                            raise ValueError(
                                "This exact call already failed twice. Do not retry it; "
                                "answer the user with what you have."
                            )
                        if signature in done_calls and not _repeatable(name):
                            result = {
                                **done_calls[signature],
                                "note": (
                                    "This exact call already ran this turn; this is the same "
                                    "result. Do not call it again — answer the user now."
                                ),
                            }
                        else:
                            result = await self.tools.execute(name, arguments)
                            if not (isinstance(result, dict) and result.get("ok") is False):
                                done_calls[signature] = result if isinstance(result, dict) else {"result": result}
                        if (
                            isinstance(result, dict)
                            and result.get("ok") is False
                            and result.get("status") != "needs_verification"
                        ):
                            # A precondition ("verify first") is not a failure;
                            # only real failures count toward the retry lockout.
                            failed_calls[signature] = failed_calls.get(signature, 0) + 1
                        if (
                            name in CLOSING_TOOLS
                            and isinstance(result, dict)
                            and result.get("ok")
                        ):
                            closing = True
                        record = {"tool": name, "arguments": arguments, "result": result}
                    except Exception as exc:
                        if signature:
                            failed_calls[signature] = failed_calls.get(signature, 0) + 1
                        result = {"ok": False, "error": str(exc)}
                        record = {"tool": name, "result": result}
                    timing_log.info(
                        "TIMING tool %s: %.2fs (result ~%d chars)",
                        name,
                        time.monotonic() - tool_started,
                        len(json.dumps(result, ensure_ascii=False)),
                    )
                    trace.append(record)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _bounded(json.dumps(result, ensure_ascii=False)),
                        }
                    )

            # Round budget spent. The user must still get an answer: one
            # final completion with tools withheld, grounded in whatever the
            # tool results so far contain.
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "You are out of tool steps. Answer the user now in one or two "
                        "plain sentences using only what the tool results above "
                        "established; say plainly what you could not find or do."
                    ),
                }
            )
            response_message = {}
            if segments:
                yield {"type": "token", "text": "\n"}
            # The last round of a long turn holds the most: the answer has to
            # fit beside it.
            shrink_to_fit(messages, prompt_budget)
            async for kind, data in self._stream_completion(
                client, messages, "none", allow_tools=False
            ):
                if kind == "token":
                    yield {"type": "token", "text": data}
                elif kind == "message":
                    response_message = data
            content = strip_planner_fragment(
                str(response_message.get("content") or "")
            ).strip() or _fallback_answer(trace)
        asking = self._missing_name_question(conversation_id, content) if spoken else ""
        if asking:
            yield {"type": "token", "text": asking}
            content += asking
        if segments:
            content = "\n".join([*segments, content])
        self.database.add_message(
            conversation_id, "assistant", content, metadata={"trace": _compact_trace(trace)}
        )
        timing_log.info(
            "TIMING turn total %.2fs (%d tool calls, forced final answer)",
            time.monotonic() - turn_started,
            len(trace),
        )
        yield {
            "type": "done",
            "conversation_id": conversation_id,
            "message": content,
            "trace": trace,
            "speaker": reported_speaker or self.tools.voices.speaker(conversation_id),
        }

    @staticmethod
    def _leans_on_the_last_turn(message: str) -> bool:
        """A short request that only means anything through the turn before it.

        "Turn them off", "do it again", "both of those": five words with a
        pronoun and no subject. With thinking off, the model answers these
        from the shape of the conversation instead of resolving them — "Done."
        with no call — so they get the reasoning step from the first round
        rather than after a wasted one.
        """
        words = message.split()
        if not 1 <= len(words) <= 6:
            return False
        lowered = f" {message.casefold().strip('?.!')} "
        return any(
            f" {word} " in lowered
            for word in (
                "them", "those", "these", "it", "that", "this", "both", "again",
                "all", "ones", "one", "other", "same", "too", "as well",
                "esos", "esas", "eso", "lo", "la", "los", "las", "también", "otra",
            )
        )

    @staticmethod
    def _complex_request(message: str) -> bool:
        """Whether the opening round deserves real thinking. Reserved for
        requests that need planning — multi-constraint recommendations,
        comparisons, references back to other things — while quick device
        commands and short asks stay instant. Length alone is no sign:
        spoken requests are wordy ("hey Friday, so I was wondering...") and
        a 14-word threshold put nearly every one of them into 40s of thought."""
        words = message.split()
        if len(words) >= 45:
            return True
        lowered = f" {message.casefold()} "
        cues = (" something like", " stuff like", " ones like", " movies like", " shows like",
                " similar", " but not", " except", " instead of", " haven't", " hasn't",
                " we watched", " last time", " compare")
        return len(words) >= 8 and any(cue in lowered for cue in cues)

    def _is_gpt_oss(self) -> bool:
        return self.provider == providers.LOCAL and "gpt-oss" in self.config.model.casefold()

    def _effort(self, requested: str) -> str:
        """Map our reasoning levels onto what the model family accepts.

        gpt-oss only knows low/medium/high, and "low" is already brief.
        Qwen3 through Ollama's OpenAI endpoint honors only "none" as the
        think-off switch — "minimal" is ignored and the model thinks at
        length (measured on qwen3:14b: 430 tokens and 7s for a one-call
        turn vs 28 tokens and 0.5s with "none", same correct tool call).
        """
        if self._is_gpt_oss():
            return "low" if requested in {"none", "minimal", "low"} else requested
        return "none" if requested in {"none", "minimal"} else requested

    def _sampling(self) -> dict[str, Any]:
        if self._is_gpt_oss():
            return {"temperature": 1.0}  # the model card's recommended default
        # Qwen's published sampling: very low temperature with no presence
        # penalty is a documented trigger for endless repetition loops (one
        # was observed at 27k tokens).
        return {"temperature": 0.6, "presence_penalty": 1.0}

    async def _summarize(
        self, previous: str | None, messages: list[dict[str, Any]]
    ) -> str:
        transcript = "\n".join(
            f"{item['role']}: {item['content']}" for item in messages
        )
        prompt = (
            "Update this running summary of a home-assistant conversation in under 200 "
            "words. Preserve: unfinished requests, titles suggested and how the user "
            "reacted to each, device actions taken, and stated user preferences. Output "
            "only the updated summary.\n"
            + (f"Current summary:\n{previous}\n" if previous else "")
            + f"New messages:\n{transcript}"
        )
        if self.provider == providers.ANTHROPIC:
            try:
                content = await providers.anthropic_text(
                    api_key=self.config.api_key, model=self.config.model, prompt=prompt,
                    max_tokens=600, timeout=self.config.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - any SDK failure is a failed compaction
                raise LLMError(f"Conversation compaction failed: {exc}") from exc
            if not content.strip():
                raise LLMError("Conversation compaction returned no summary")
            return content.strip()
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            **self._request_options("none", 400, temperature=0.2),
        }
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                content = str(response.json()["choices"][0]["message"]["content"] or "")
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Conversation compaction failed: {exc}") from exc
        if not content.strip():
            raise LLMError("Conversation compaction returned no summary")
        return content.strip()

    def _request_options(self, reasoning_effort: str, max_tokens: int, *, temperature: float | None = None) -> dict[str, Any]:
        """Length, sampling and reasoning settings in the dialect of the
        model behind self.config. Ollama takes Qwen's and gpt-oss's tuned
        sampling; OpenAI's reasoning models reject temperature and max_tokens;
        OpenRouter normalizes across its models, so it gets the plain set."""
        if self.provider == providers.OPENAI:
            options: dict[str, Any] = {"max_completion_tokens": max(max_tokens, 4000)}
            if re.match(r"(gpt-5|o\d)", self.config.model):
                options["reasoning_effort"] = "low"
            elif temperature is not None:
                options["temperature"] = temperature
            return options
        if self.provider in (providers.OPENROUTER, providers.CUSTOM):
            options = {"max_tokens": max(max_tokens, 4000)}
            if temperature is not None:
                options["temperature"] = temperature
            return options
        options = {"max_tokens": max_tokens, "reasoning_effort": self._effort(reasoning_effort), **self._sampling()}
        if temperature is not None:
            options["temperature"] = temperature
            options.pop("presence_penalty", None)
        return options

    async def _stream_completion(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        reasoning_effort: str = "minimal",
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stream one completion from whichever model is chosen. A cloud
        model that fails before saying anything (bad key, no internet,
        provider down) hands the round to the local model, so the house
        keeps answering."""
        if self.provider == providers.LOCAL:
            async for piece in self._stream_openai(client, messages, reasoning_effort, allow_tools=allow_tools):
                yield piece
            return
        started = False
        try:
            if self.provider == providers.ANTHROPIC:
                stream = providers.anthropic_stream(
                    api_key=self.config.api_key,
                    model=self.config.model,
                    messages=messages,
                    tools=(self._turn_schemas if self._turn_schemas is not None else self.tools.schemas),
                    allow_tools=allow_tools,
                    effort=reasoning_effort,
                    timeout=self.config.timeout_seconds,
                )
            else:
                stream = self._stream_openai(client, messages, reasoning_effort, allow_tools=allow_tools)
            async for piece in stream:
                started = True
                yield piece
            self.fell_back = None
            return
        except Exception as exc:  # noqa: BLE001 - any cloud failure falls back below
            if started:
                raise LLMError(f"{providers.PROVIDERS[self.provider].label} stopped mid-reply: {exc}") from exc
            self.fell_back = str(exc)[:300]
            log.warning(
                "LLM: %s failed (%s); answering with the local model instead",
                providers.PROVIDERS[self.provider].label, exc,
            )
        cloud_config, cloud_provider = self.config, self.provider
        self.config, self.provider = self.local_config, providers.LOCAL
        try:
            async for piece in self._stream_openai(client, providers.strip_private(messages), reasoning_effort, allow_tools=allow_tools):
                yield piece
        finally:
            self.config, self.provider = cloud_config, cloud_provider

    async def _stream_openai(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        reasoning_effort: str = "minimal",
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stream one chat-completions call, yielding ("token", text) pieces
        as they arrive and a final ("message", assembled_message)."""
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        effort = self._effort(reasoning_effort)
        # A round that thinks needs headroom or the reasoning eats the whole
        # budget and truncates the tool call; a non-thinking round is ~100
        # tokens for a reply, so 1200 bounds any runaway to a few seconds.
        thinking_round = effort not in {"none", "minimal"} and not self._is_gpt_oss()
        payload = {
            "model": self.config.model,
            "messages": providers.strip_private(messages),
            "stream": True,
            **self._request_options(reasoning_effort, 2400 if thinking_round else 1200),
        }
        if allow_tools:
            payload["tools"] = self._turn_schemas if self._turn_schemas is not None else self.tools.schemas
            payload["tool_choice"] = "auto"
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for attempt in (1, 2):
            try:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=payload
                ) as response:
                    if response.status_code >= 500 and attempt == 1:
                        # Ollama fails after a complete generation when it
                        # cannot parse the model's tool call. Sampling is
                        # random, so one more try almost always succeeds.
                        body = (await response.aread()).decode("utf-8", "replace")
                        timing_log.info(
                            "TIMING llm: %d from model server, retrying once (%s)",
                            response.status_code,
                            body[:300].replace("\n", " "),
                        )
                        continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data or data == "[DONE]":
                            continue
                        choices = json.loads(data).get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            content_parts.append(piece)
                            yield "token", piece
                        for call in delta.get("tool_calls") or []:
                            slot = calls.setdefault(
                                int(call.get("index") or 0),
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["function"]["name"] = function["name"]
                                yield "tool_start", function["name"]
                            if function.get("arguments"):
                                slot["function"]["arguments"] += function["arguments"]
                    break
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                where = "Local model" if self.provider == providers.LOCAL else providers.PROVIDERS[self.provider].label
                raise LLMError(f"{where} endpoint failed: {exc}") from exc
        assembled: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if calls:
            assembled["tool_calls"] = [calls[index] for index in sorted(calls)]
        yield "message", assembled
