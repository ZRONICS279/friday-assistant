"""Which tools and which guidance a turn gets.

Friday has ninety-odd tools whose schemas alone run to ~14k tokens, and a
system prompt of ~7k more. On a 16k-context model that is already over
budget before the first word of history — and Ollama's answer to an
over-long prompt is to keep the *last* half and drop the rest, which is
the system prompt and most of the tools. Fifty-seven turns did that in the
first ten days of September, and every one of them looked like a model
that had forgotten who it was.

So each turn is routed: a core set is always present, and a domain (TV,
lights, DoorDash, calendar…) rides along only when the request, the last
few turns, or something in progress points at it. Guidance paragraphs are
tagged the same way and come and go with their domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Rough but safe: measured prompts run about five characters per token, so
# four leaves headroom rather than eating it.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if text else 0


@dataclass(frozen=True, slots=True)
class Domain:
    name: str
    tools: tuple[str, ...]  # exact names or prefixes ending in "_"
    keywords: re.Pattern[str]
    guidance: re.Pattern[str]  # what marks a prompt paragraph as this domain's

    def owns_tool(self, tool: str) -> bool:
        return any(tool == t or (t.endswith("_") and tool.startswith(t)) for t in self.tools)


def _kw(*words: str) -> re.Pattern[str]:
    return re.compile("|".join(words), re.IGNORECASE)


# Tools present on every turn: cheap, general, or the kind of thing said out
# of nowhere ("add eggs to the list", "send me that link").
CORE_TOOLS = ("inbox_send", "list_", "web_search", "web_fetch", "style_preference_")

DOMAINS: tuple[Domain, ...] = (
    Domain(
        "tv",
        ("tv_",),
        _kw(r"\btv\b", r"televis", r"\broku\b", r"remote", r"\bvolume\b", r"\bmute\b", r"\bpause\b",
            r"\bplay", r"\bnetflix\b|\bhulu\b|\byoutube\b|\bprime\b|\bdisney\b|\bmax\b|\bparamount\b|\bpeacock\b|\bplex\b|\bspotify\b",
            r"\bapp\b", r"\bchannel\b", r"\binput\b|\bhdmi\b", r"turn (it |the )?(on|off)", r"\bscreen\b",
            r"\bhome screen\b",
            r"\bresume\b|\bun-?pause\b|\bback on\b|\bleft off\b|\bkeep (watching|playing|going)\b|\bcontinue\b",
            r"\brestart\b|\bstart (it |the \w+ )?over\b|\bfrom the (beginning|start)\b|\bstop (it|the \w+)\b",
            r"\brewind\b|\bfast ?forward\b|\bskip\b|\bgo back\b|\bhold on\b",
            r"\bpon\b|\bapaga\b|\bprende\b|\benciende\b|\bsube\b|\bbaja\b|\btele\b"),
        re.compile(r"\bTV\b|television|key press|deep-link|app IDs"),
    ),
    Domain(
        "content",
        ("content_", "catalog_", "watch_history_", "taste_", "household_list_subscriptions"),
        _kw(r"\bwatch", r"\bmovie", r"\bfilm", r"\bshow\b|\bshows\b", r"\bseries\b", r"\bseason\b", r"\bepisode",
            r"\bput on\b", r"\bplay\b", r"\brecommend", r"\bsomething (good|funny|scary|new)\b", r"\bcomedy|\bdrama|\bthriller|\bhorror|\banime|\bdocumentar",
            r"\bstreaming\b", r"\bnetflix\b|\bhulu\b|\bprime\b|\bdisney\b|\bmax\b|\bparamount\b|\bpeacock\b",
            r"\btitle", r"\bactor|\bactress|\bdirector|\bstarring|\bcast\b", r"\brated\b|\brating\b",
            r"\bpelícula|\bpelicula|\bserie\b|\bver\b|\bvemos\b|\btemporada|\bcapítulo|\bcapitulo"),
        re.compile(r"catalog|[Rr]ecommend|taste memory|watch[- ]history|episode|playback|subscriptions|franchise|rules titles out|^That default is|^Resolve short conversational"),
    ),
    Domain(
        "devices",
        ("device_",),
        _kw(r"\blight", r"\blamp", r"\bplug\b", r"\bswitch\b", r"\boutlet\b", r"\bdim", r"\bbright",
            r"\bcolou?r\b", r"\bwarm", r"\bcool", r"\bcozy\b", r"\bpair", r"\bzigbee\b|\bmatter\b",
            r"\bflash", r"\bturn (it |them |the )?(on|off)", r"\bbedroom\b|\bkitchen\b|\bliving room\b|\bbathroom\b|\boffice\b|\bhallway\b|\bgarage\b|\bporch\b",
            r"\bluz\b|\bluces\b|\blámpara|\blampara|\benchufe"),
        re.compile(r"Zigbee|Matter|[Ss]mart lights|pairing code"),
    ),
    Domain(
        "schedule",
        ("reminder_add", "light_schedule_", "light_fade", "automation_"),
        _kw(r"\bremind", r"\breminder", r"\bschedule", r"\bevery (day|morning|night|evening)\b", r"\bat \d", r"\bo'?clock\b",
            r"\bsunrise\b|\bsunset\b|\bbedtime\b|\bwake\b", r"\bfade\b", r"\btimer\b", r"\balarm\b", r"\bautomation",
            r"\bcancel\b", r"\bdon'?t (let me )?forget\b", r"\brecu[ée]rda", r"\brecordatorio", r"\bhorario"),
        re.compile(r"[Rr]eminders and schedules|Plans and heads-ups"),
    ),
    Domain(
        "calendar",
        ("calendar_",),
        _kw(r"\bcal[ae]nd[ae]r", r"\bappointment", r"\bmeeting", r"\bevent", r"\btomorrow\b", r"\bnext week\b", r"\bthis weekend\b",
            r"\bwhat('s| is) on\b", r"\bplans?\b", r"\bbirthday", r"\bagenda\b", r"\bcita\b", r"\bcalendario",
            # A day or a date named out loud is almost always a plan.
            r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b", r"\bnext (month|year)\b",
            r"\bon the \d{1,2}(st|nd|rd|th)\b", r"\b(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\b"),
        re.compile(r"calendar_"),
    ),
    Domain(
        "weather",
        ("weather_", "directions_"),
        _kw(r"\bweather", r"\brain", r"\bsnow", r"\bforecast", r"\btemperature", r"\bdegrees", r"\bhot\b|\bcold\b|\bwindy\b|\bsunny\b|\bcloudy\b",
            r"\bumbrella\b", r"\bhow far\b", r"\bhow long (to|does it take)\b", r"\bdrive\b|\bdriving\b", r"\btraffic\b", r"\bdirections\b",
            r"\bclima\b|\btiempo\b|\bllover|\blluvia"),
        re.compile(r"[Ww]eather comes|[Tt]ravel time and distance"),
    ),
    Domain(
        "travel",
        ("flight_search", "hotel_search"),
        _kw(r"\bflight", r"\bfly\b|\bflying\b", r"\bairline", r"\bairport", r"\bhotel", r"\btrip\b", r"\bvacation\b",
            r"\bbook (a|me)\b", r"\bplane\b", r"\bticket", r"\bvuelo", r"\bviaje", r"\bboleto"),
        re.compile(r"[Ff]lights come|hotel_search"),
    ),
    Domain(
        "recipes",
        ("recipe_",),
        _kw(r"\brecipe", r"\bcook", r"\bbake", r"\bmake (dinner|lunch|breakfast|some)\b", r"\bingredient", r"\breceta", r"\bcocinar"),
        re.compile(r"[Rr]ecipes come|recipe_"),
    ),
    Domain(
        "doordash",
        ("doordash_",),
        _kw(r"\bdoordash\b|\bdoor dash\b", r"\border", r"\bdeliver", r"\bfood\b", r"\bhungry\b", r"\bdinner\b|\blunch\b|\bbreakfast\b|\bsnack",
            r"\beat\b|\beating\b", r"\brestaurant", r"\bmenu\b", r"\bcart\b", r"\btip\b", r"\bgroceries\b|\bgrocery\b",
            r"\bpizza\b|\bburger|\btaco|\bsushi\b|\bthai\b|\bchinese\b|\bmexican\b|\bindian\b|\bramen\b|\bpho\b|\bwings\b|\bfries\b|\bcoffee\b|\bboba\b",
            r"\bmcdonald|\bstarbucks\b|\bchipotle\b|\btaco bell\b|\bwendy|\bsubway\b|\bdomino|\bpanda express\b",
            r"\bplace (it|the order|that)\b", r"\bfavorite (place|spot|restaurant)", r"\breorder\b", r"\bthe usual\b",
            # What people say mid-order, when the restaurant's name has already
            # been said and the words left are about the food itself.
            r"\bmeal\b|\bmeals\b|\bcombo\b|\bvalue meal\b", r"\bcheeseburger|\bsandwich|\bnugget|\bshake\b|\bmilkshake\b",
            r"\bsalad\b|\bsoda\b|\bdrinks?\b|\bdessert|\bappetizer|\bentree\b|\bside\b|\bsides\b|\bcombo meal\b",
            # Mid-order follow-ups that name no food at all: "what flavors do
            # they have?", "is there a limited edition one?"
            r"\bflavou?rs?\b|\bvariet(?:y|ies)\b|\b(?:limited|special) edition\b",
            r"\bwhat (do|does) (they|it|you) (have|serve|offer)\b", r"\bwhat'?s? on the menu\b",
            r"\bcomida\b|\bpedir\b|\bpedido\b|\bhambre\b|\bcena\b|\balmuerzo\b"),
        re.compile(r"DoorDash|doordash_"),
    ),
    Domain(
        "profile",
        ("voice_", "profile_", "household_setup"),
        _kw(r"\bmy name\b", r"\bi'?m [A-Z]", r"\bcall me\b", r"\bremember (that )?i\b", r"\bi (like|love|hate|prefer|can't stand)\b",
            r"\banonymous\b", r"\bwho am i\b", r"\bme llamo\b|\bsoy\b"),
        re.compile(r"Speaker line|[Pp]rofile notes|voice_enroll|household_setup"),
    ),
    Domain(
        "house",
        ("house_config_",),
        _kw(r"\bip\b|\bip address\b", r"\bwi-?fi\b", r"\bpassword\b", r"\brouter\b", r"\bnetwork\b", r"\bsetting", r"\bconfig",
            r"\brename the tv\b", r"\bnew (tv|television|router|internet)\b", r"\bcontraseña"),
        re.compile(r"House settings|house_config"),
    ),
)

DOMAIN_BY_NAME = {d.name: d for d in DOMAINS}

# When nothing in the request points anywhere, the domains a house is most
# often spoken to about.
DEFAULT_DOMAINS = ("tv", "devices", "weather")

# Someone asking for something to be kept for later. The tools that can
# actually keep it are calendar_add and reminder_add, and when neither is
# routed the nearest thing left is inbox_send — so the turn writes a note
# and reports it as a plan. Keywords miss on a typo ("calander") or a
# phrasing nobody listed, so a save verb next to a time is enough to bring
# those two domains along even when no keyword matched.
_SAVING = re.compile(
    r"\b(add|put|save|note|jot|pencil|book|schedule|remember|remind|don'?t forget)\b",
    re.IGNORECASE,
)
_WHEN = re.compile(
    r"\b(today|tonight|tomorrow|next|this)\b"
    r"|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b\d{1,2}(st|nd|rd|th)\b"
    r"|\b\d{1,2}(:\d{2})?\s*([ap]\.?m\.?)\b"
    r"|\bo'?clock\b"
    r"|\bin (a|an|\d+) (minute|hour|day|week|month)",
    re.IGNORECASE,
)


def wants_saving(text: str) -> bool:
    """Whether a request asks for something to be kept until a time."""
    return bool(text) and bool(_SAVING.search(text)) and bool(_WHEN.search(text))

_TOOL_NAME = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
# Guidance that is about how to talk, not what to do: on every turn.
_ALWAYS = re.compile(r"^(Be direct|Anyone can change how you write|Replies are spoken|You speak)")


def domain_of_tool(tool: str) -> str | None:
    for domain in DOMAINS:
        if domain.owns_tool(tool):
            return domain.name
    return None


def is_core_tool(tool: str) -> bool:
    return any(tool == t or (t.endswith("_") and tool.startswith(t)) for t in CORE_TOOLS)


def domains_mentioned(text: str) -> set[str]:
    """Domains a piece of text points at, by its words."""
    if not text:
        return set()
    return {d.name for d in DOMAINS if d.keywords.search(text)}


def _paragraphs(prompt: str) -> list[str]:
    """Prompt lines, with an example block ("A few easy comedies: / **Frasier**
    (1993) / …") folded into the paragraph that introduces it."""
    out: list[str] = []
    for line in prompt.split("\n"):
        if not line.strip():
            continue
        continuation = out and (len(line) < 60 and not line.rstrip().endswith("."))
        if continuation:
            out[-1] = out[-1] + "\n" + line
        else:
            out.append(line)
    return out


def paragraph_domains(paragraph: str) -> set[str]:
    """Which domains a guidance paragraph belongs to. Paragraphs are written
    topic-first, so the first domain tool they name is their subject; a
    subject marker ("Weather comes from…") counts too. Every other tool
    mentioned in passing does not drag the paragraph into that domain.
    Empty means general guidance, present on every turn."""
    found: set[str] = set()
    if _ALWAYS.search(paragraph):
        return found
    for name in _TOOL_NAME.findall(paragraph):
        owner = domain_of_tool(name)
        if owner:
            found.add(owner)
            break
    for domain in DOMAINS:
        if domain.guidance.search(paragraph):
            found.add(domain.name)
    return found


@dataclass(slots=True)
class Routed:
    domains: list[str]
    schemas: list[dict[str, Any]]
    prompt: str
    schema_tokens: int
    prompt_tokens: int
    dropped: list[str] = field(default_factory=list)


class Router:
    def __init__(self, prompt: str, schemas: list[dict[str, Any]], budget_tokens: int) -> None:
        self.budget_tokens = budget_tokens
        self._paragraphs: list[tuple[str, set[str]]] = [(p, paragraph_domains(p)) for p in _paragraphs(prompt)]
        self._core_schemas: list[dict[str, Any]] = []
        self._domain_schemas: dict[str, list[dict[str, Any]]] = {d.name: [] for d in DOMAINS}
        self._unowned: list[dict[str, Any]] = []
        for schema in schemas:
            name = schema["function"]["name"]
            if is_core_tool(name):
                self._core_schemas.append(schema)
                continue
            owner = domain_of_tool(name)
            if owner:
                self._domain_schemas[owner].append(schema)
            else:
                self._unowned.append(schema)  # a tool no domain claims stays always-on
        self._cost = {
            name: sum(estimate_tokens(_dumps(s)) for s in group) for name, group in self._domain_schemas.items()
        }
        self.core_cost = sum(estimate_tokens(_dumps(s)) for s in self._core_schemas + self._unowned)

    def domains_for(
        self,
        message: str,
        history: Iterable[dict[str, Any]],
        active: Iterable[str] = (),
        recent: Iterable[str] = (),
    ) -> list[str]:
        """Domains in priority order: what the message names, then what is
        in progress, then what the last turns were about, then what this
        chat has used lately."""
        scores: dict[str, float] = {}
        for name in domains_mentioned(message):
            scores[name] = scores.get(name, 0) + 4
        # A request to keep something until a time, with nothing that can
        # keep it already routed: ride the two domains that can along, below
        # anything the message named outright.
        if wants_saving(message) and not {"calendar", "schedule"} & scores.keys():
            scores["calendar"] = 2
            scores["schedule"] = 2
        for name in active:
            if name in DOMAIN_BY_NAME:
                scores[name] = scores.get(name, 0) + 3
        tail = [h for h in history if h.get("role") in {"user", "assistant"}][-4:]
        for item in tail:
            for name in domains_mentioned(str(item.get("content") or "")):
                scores[name] = scores.get(name, 0) + 1
        for name in recent:
            if name in DOMAIN_BY_NAME:
                scores[name] = scores.get(name, 0) + 1
        if not scores:
            return list(DEFAULT_DOMAINS)
        return sorted(scores, key=lambda n: (-scores[n], n))

    def route(
        self,
        message: str,
        history: Iterable[dict[str, Any]],
        active: Iterable[str] = (),
        recent: Iterable[str] = (),
    ) -> Routed:
        wanted = self.domains_for(message, list(history), active, recent)
        chosen: list[str] = []
        dropped: list[str] = []
        spent = self.core_cost
        for name in wanted:
            cost = self._cost.get(name, 0)
            if chosen and spent + cost > self.budget_tokens:
                dropped.append(name)
                continue
            chosen.append(name)
            spent += cost
        picked = set(chosen)
        schemas = list(self._core_schemas) + list(self._unowned)
        for name in chosen:
            schemas.extend(self._domain_schemas[name])
        prompt = "\n".join(p for p, tags in self._paragraphs if not tags or tags & picked) + "\n"
        return Routed(
            domains=chosen,
            schemas=schemas,
            prompt=prompt,
            schema_tokens=spent,
            prompt_tokens=estimate_tokens(prompt),
            dropped=dropped,
        )

    def everything(self) -> Routed:
        """All tools and all guidance — for a model with room for it."""
        schemas = list(self._core_schemas) + list(self._unowned)
        for name in DOMAIN_BY_NAME:
            schemas.extend(self._domain_schemas[name])
        prompt = "\n".join(p for p, _ in self._paragraphs) + "\n"
        return Routed(
            domains=list(DOMAIN_BY_NAME),
            schemas=schemas,
            prompt=prompt,
            schema_tokens=self.core_cost + sum(self._cost.values()),
            prompt_tokens=estimate_tokens(prompt),
        )


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def fit_history(
    history: list[dict[str, Any]], available_tokens: int, keep_last: int = 2
) -> tuple[list[dict[str, Any]], int]:
    """Drop the oldest turns until the rest fits; never the last few."""
    kept = list(history)
    dropped = 0
    while len(kept) > keep_last and sum(estimate_tokens(str(h.get("content") or "")) for h in kept) > available_tokens:
        kept.pop(0)
        dropped += 1
    return kept, dropped
