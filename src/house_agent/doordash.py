"""Food, grocery, and retail delivery through DoorDash's own command-line
tool, dd-cli (github.com/doordash-oss/doordash-cli).

Everything here is a thin, careful wrapper around that binary: each request
becomes one `dd-cli --json-output <command> --intent ...` subprocess, its JSON
is trimmed to what a spoken answer needs, and the pieces DoorDash's own agent
guidance insists on are enforced in code rather than left to the model —
search from a saved address, append to an open cart instead of orphaning it,
preview before placing, and never submit without an explicit yes, a confirmed
Dasher tip, and the card named.

Sign-in is the user's job: `dd-cli login` once on the house computer (it
opens a browser and keeps the token in the OS keychain), or `dd-cli
export-token` on any machine with DD_CLI_ACCESS_TOKEN pasted into .env.

DoorDash's CLI terms forbid keeping what the CLI returns — menus, prices,
order history, addresses — in any local store beyond finishing the order at
hand. So nothing from DoorDash is written to the database: the only thing
this house remembers is what the household says in its own words (a
favorite place, the usual order), and tool results are redacted from the
saved conversation trace."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import DoorDashConfig

log = logging.getLogger(__name__)


class DoorDashError(RuntimeError):
    pass


class DoorDashNotConfigured(DoorDashError):
    """dd-cli is missing, turned off, or not signed in."""


class DoorDashNotSignedIn(DoorDashNotConfigured):
    """dd-cli is there but has no credentials: a sign-in fixes it."""


# argv, environment, timeout -> (exit code, stdout, stderr)
Runner = Callable[[list[str], dict[str, str], float], Awaitable[tuple[int, bytes, bytes]]]
HomeLookup = Callable[[], tuple[float, float, str] | None]

INTENT_SUMMARY = (
    "Help a household member order food, groceries, or household items "
    "for their own home by voice through Friday, a local home assistant"
)
_CREDENTIAL_PROBLEM = re.compile(
    r"missing credentials|dd-cli login|not signed in|sign in|token .*expired|"
    r"expired token|unauthori[sz]ed|\b401\b|invalid token|authentication",
    re.IGNORECASE,
)
_ITEM_PREFIX = re.compile(r"^[ioe]_")
_ADDRESS_CACHE_SECONDS = 300.0
# A quote is only good for a little while: prices, ETAs, and availability
# move, so an old preview must be refreshed before money changes hands.
_PREVIEW_VALID_SECONDS = 30 * 60
_VERTICALS = {
    "restaurants": None,
    "restaurant": None,
    "food": None,
    "grocery": "grocery",
    "groceries": "grocery",
    "convenience": "convenience",
    "alcohol": "alcohol",
    "pets": "pets",
    "pet": "pets",
    "retail": "retail",
    "shops": "nv",
    "any": "nv",
    "nv": "nv",
}
_METERS_PER_MILE = 1609.344
# `dd-cli login` is an OAuth flow: it prints an identity.doordash.com link,
# listens on localhost:4180 for the redirect, then keeps the token in the OS
# keychain. From a phone the redirect cannot reach this machine, so the user
# pastes the address the browser landed on and it is delivered here instead.
_LOGIN_URL = re.compile(r"https://identity\.doordash\.com/authorize\?[^\s'\"<>]+")
_LOGIN_CALLBACK = "http://localhost:4180/oauth2/callback"
_LOGIN_URL_WAIT = 25.0
# dd-cli gives up on its own after five minutes ("Authentication/Authorization
# failed"); the window here is only a backstop, and an attempt that ran that
# long simply went unused.
_DD_CLI_LOGIN_WINDOW = 300.0
_LOGIN_WINDOW_SECONDS = 15 * 60.0
_LOGIN_FINISH_WAIT = 30.0
_TERMINAL_STATUSES = {"completed", "cancelled", "order_declined", "action_required"}
_PLACED_STATUSES = {
    "placed", "scheduled", "store_confirmed", "ready_for_pickup", "dasher_assigned",
    "dasher_at_store", "picked_up", "dasher_nearby", "completed",
}


def find_binary(configured: Path | None = None) -> Path | None:
    """dd-cli: the configured path, then PATH, then where its installer puts it.
    The systemd service's PATH rarely includes ~/.local/bin, so the last one
    matters."""
    if configured:
        # Told where it is: believe that, and say so if it is not there,
        # rather than quietly running some other copy.
        explicit = Path(configured).expanduser()
        if explicit.is_file() and os.access(explicit, os.X_OK):
            return explicit
        log.warning("dd-cli not found at configured path %s", explicit)
        return None
    candidates: list[Path] = []
    found = shutil.which("dd-cli")
    if found:
        candidates.append(Path(found))
    candidates.append(Path.home() / ".local" / "bin" / "dd-cli")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def intent_for(purpose: str | None) -> str:
    """The --intent every dd-cli command requires: what the workflow is for,
    with the user's own words, in the two-line shape DoorDash asks for."""
    words = " ".join(str(purpose or "").split())
    return f"Summary: {INTENT_SUMMARY}\nuser prompt/purpose: \"{words}\""


async def run_dd_cli(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise DoorDashError(f"DoorDash did not answer within {timeout:.0f} seconds") from None
    return int(process.returncode or 0), stdout, stderr


async def _http_get(url: str) -> None:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        await client.get(url)


def _callback_params(text: str) -> tuple[str | None, str | None]:
    """The code and state out of whatever the user pasted: a full callback
    address, or just its query."""
    text = str(text or "").strip()
    try:
        query = parse_qs(urlsplit(text).query) if "?" in text else parse_qs(text)
    except ValueError:
        query = {}
    code = (query.get("code") or [None])[0]
    state = (query.get("state") or [None])[0]
    if not code:
        match = re.search(r"code=([A-Za-z0-9._~-]+)", text)
        code = match.group(1) if match else None
    if not state:
        match = re.search(r"state=([A-Za-z0-9._~-]+)", text)
        state = match.group(1) if match else None
    return code, state


def _login_failure(output: list[str]) -> str:
    """Why a sign-in ended without credentials, fit to be said out loud: the
    last thing dd-cli complained about, never the link itself."""
    lines = [
        re.sub(r"https?://\S+", "", line).strip()
        for line in output
        if line.strip() and not line.startswith("[Identity]")
    ]
    lines = [line for line in lines if line and not re.match(r"(open in browser|if the browser|listening for|opening browser)", line, re.IGNORECASE)]
    tail = _clean(" ".join(lines[-2:])) or "the sign-in window closed before it was completed"
    if re.search(r"keyring|keychain|secret ?service|dbus", " ".join(lines), re.IGNORECASE):
        tail += (
            " The house computer's keychain would not store it: run `dd-cli export-token` "
            "on a machine with a browser and put the token in .env as DD_CLI_ACCESS_TOKEN."
        )
    return tail


def _parse_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    # A stray log line before the JSON: decode from the first bracket.
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(text[start:])
                return value
            except ValueError:
                continue
    return {"text": text}


def _unwrap(payload: Any) -> dict[str, Any]:
    """The command's result, out of the envelope the CLI wraps it in: an
    MCP-style {"content": [{"type": "text", "text": ...}], "structuredContent":
    {...}, "isError": bool}, with the structured part the one to read."""
    if isinstance(payload, dict) and ("structuredContent" in payload or "content" in payload):
        inner = payload.get("structuredContent")
        if not isinstance(inner, dict):
            text = "".join(
                str(block.get("text") or "")
                for block in payload.get("content") or []
                if isinstance(block, dict) and block.get("type", "text") == "text"
            ).strip()
            try:
                parsed = json.loads(text) if text[:1] in "{[" else None
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                inner = {"items": parsed}
            elif isinstance(parsed, dict):
                inner = parsed
            else:
                inner = {"message": text} if text else {}
        if payload.get("isError"):
            inner = {**inner, "is_error": True}
        payload = inner
    if isinstance(payload, list):
        return {"items": payload}
    if not isinstance(payload, dict):
        return {"value": payload}
    for key in ("result", "data", "response", "output"):
        inner = payload.get(key)
        if isinstance(inner, dict) and len(payload) <= 4:
            outer = {k: v for k, v in payload.items() if k != key}
            return {**outer, **inner}
    return payload


def _clean(message: Any) -> str:
    text = " ".join(str(message or "").split())
    return text[:300]


def _first(source: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(source, dict):
        return default
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _money(value: Any) -> Any:
    """A price as a spoken string when the CLI gave one, else as it came."""
    if isinstance(value, dict):
        display = _first(value, "display_string", "display", "formatted")
        if display:
            return str(display)
        unit = value.get("unit_amount")
        if unit is not None:
            try:
                return f"${int(unit) / 100:.2f}"
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"${float(value):.2f}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _cents_to_text(cents: Any) -> str | None:
    try:
        return f"${int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return None


def _epoch_ms(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e11:  # milliseconds
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return None


def _spoken_time(value: Any) -> Any:
    """An ISO timestamp (UTC or offset) as local clock time, for an ETA."""
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%-I:%M %p")
    return _when(value)


def _when(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return _epoch_ms(value)
    if isinstance(value, str) and value.strip().isdigit():
        return _epoch_ms(value.strip())
    if isinstance(value, str) and "T" in value:
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %-I:%M %p")
    return value


def _item_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _ITEM_PREFIX.sub("", str(value))


def _short(text: Any, limit: int = 110) -> str | None:
    if not text:
        return None
    words = " ".join(str(text).split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


_FILLER = {"the", "a", "an", "my", "of", "some", "that", "this", "please", "one"}
# What a menu never has an item called. Asked "what flavors do they have?", a
# model queries 'flavors', gets no match, and reads the miss back as an answer
# ("there are no items named flavors"). The question is really "show me the
# sections", so that is what it gets.
_NOT_AN_ITEM = {
    "flavor", "flavors", "flavour", "flavours", "option", "options", "choice", "choices",
    "variety", "varieties", "kind", "kinds", "type", "types", "selection", "menu",
    "everything", "all", "anything", "something", "item", "items", "food", "stuff",
}
# Words that describe an item rather than name a store or a dish. A store
# search carrying them ("special Baja Blast Olivia Rodrigo") matches nothing,
# while the two words underneath it find the place that sells the thing.
_QUALIFIER = {
    "special", "limited", "edition", "new", "latest", "exclusive", "collab",
    "collaboration", "flavor", "flavors", "flavour", "flavours", "any", "right", "now",
}


def _plain(text: Any) -> str:
    """Lower-case with apostrophes and such dropped, so 'mcdonalds' finds
    McDonald's and 'menchies' finds Menchie's."""
    return re.sub(r"[^\w\s]", "", str(text or "").casefold().replace("’", ""))


def _words(text: Any) -> list[str]:
    """Search words from what someone said, without the articles: 'the coke'
    must find Coke."""
    words = [w for w in re.split(r"\s+", _plain(text)) if w]
    kept = [w for w in words if w not in _FILLER]
    return kept or words


def _matches(words: list[str], *fields: Any) -> bool:
    haystack = _plain(" ".join(str(field or "") for field in fields))
    return all(word in haystack for word in words)


def _near_matches(words: list[str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Menu items sharing some of the words asked for, closest first.

    An exact match wants every word, which is right for "big mac" and useless
    for how people actually order: "cheese burger meal small" has four words
    and McDonald's has no item containing all of them. Rather than answering
    that nothing matches — which reads as "I cannot see the menu" — the words
    are scored separately, a hit in the name counting for more than one in the
    description, so a cheeseburger still comes back."""
    wanted = {_singular(word) for word in words}
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        name = _plain(item.get("name"))
        rest = _plain(" ".join(str(item.get(f) or "") for f in ("description", "category")))
        score = sum(3 for word in wanted if word in name) + sum(1 for word in wanted if word in rest)
        if score:
            scored.append((-score, index, item))
    return [item for _, _, item in sorted(scored, key=lambda row: row[:2])]


def _singular(word: str) -> str:
    """'fries' stays, 'cheeseburgers' -> 'cheeseburger': enough plural handling
    for menu words without a stemmer."""
    if len(word) > 4 and word.endswith("es") and not word.endswith("ies"):
        return word[:-2] if word[:-2].endswith(("sh", "ch", "x", "s")) else word[:-1]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def mentioned(label: Any, text: Any) -> bool:
    """Whether any real word of a saved-address label appears in what the
    user just said — the check that stops a model from sending an order to
    a label it merely remembers seeing."""
    words = [_singular(w) for w in _words(label)]
    said = {_singular(w) for w in _words(text)}
    return any(w in said for w in words)


def _eta_minutes(value: Any) -> float:
    match = re.search(r"\d+", str(value or ""))
    return float(match.group()) if match else 999.0


def _resolve_tip(asked: Any, suggested: Any) -> tuple[int | None, str]:
    """The Dasher tip in cents from whatever the model passed: a number of
    cents, 'suggested', 'none', or an amount in words ('$3', '3.50', '2
    dollars'). Nothing at all means the suggested tip, because the read-back
    always names it and a plain yes is a yes to that."""
    suggested_cents = None
    try:
        suggested_cents = int(suggested) if suggested not in (None, "") else None
    except (TypeError, ValueError):
        suggested_cents = None
    if asked in (None, ""):
        return (suggested_cents, "suggested") if suggested_cents is not None else (None, "missing")
    if isinstance(asked, bool):
        raise ValueError("tip must be an amount, 'suggested', or 'none'")
    if isinstance(asked, (int, float)):
        return max(0, int(asked)), "cents"
    text = str(asked).strip().casefold()
    if text in {"suggested", "default", "recommended", "yes", "the suggested tip"}:
        if suggested_cents is None:
            return None, "missing"
        return suggested_cents, "suggested"
    if text in {"none", "no", "no tip", "zero", "0", "skip"}:
        return 0, "none"
    match = re.fullmatch(r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(dollars?|bucks|usd|cents?|c)?", text)
    if not match:
        raise ValueError(f"tip {asked!r} not understood: pass 'suggested', 'none', or an amount like '$3'")
    amount = float(match.group(1))
    unit = match.group(2) or ""
    if unit.startswith("c"):
        return max(0, int(round(amount))), "cents"
    if "." in match.group(1) or unit or text.startswith("$") or amount < 20:
        return max(0, int(round(amount * 100))), "amount"
    return max(0, int(amount)), "cents"


def _best_branch(stores: list[dict[str, Any]]) -> dict[str, Any]:
    """Of several branches of the same chain, the one to order from: open
    beats closed, then the fastest, then the nearest."""
    def rank(store: dict[str, Any]) -> tuple[int, float, float]:
        closed = 1 if store.get("open") is False else 0
        try:
            miles = float(store.get("miles", 99))
        except (TypeError, ValueError):
            miles = 99.0
        return (closed, _eta_minutes(store.get("eta")), miles)
    return sorted(stores, key=rank)[0]


# Words that say how many or what sort of place, not what: "two
# cheeseburgers", "the burger place".
_COUNT_WORDS = {"one", "two", "three", "four", "five", "six", "a", "an", "1", "2", "3", "4", "5", "6"}
_PLACE_WORDS = {"place", "restaurant", "spot", "joint", "shop", "store", "location", "nearest", "closest"}


def _store_words(store: Any) -> list[str]:
    words = [w for w in _words(store) if w not in _PLACE_WORDS]
    return words or _words(store)


def _pick_item(items: list[dict[str, Any]], asked: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The menu item someone means by a few words, and the runners-up.

    'cheeseburger' on a McDonald's menu matches Cheeseburger, Double
    Cheeseburger and Bacon Cheeseburger; the plain one wins because its name
    adds nothing to what was asked. Sold-out items are never picked."""
    words = [_singular(w) for w in _words(asked) if w not in _COUNT_WORDS]
    if not words:
        return None, []
    def hit(item: dict[str, Any], *fields: str) -> bool:
        haystack = " ".join(_singular(w) for w in _words(" ".join(str(item.get(f) or "") for f in fields)))
        return all(w in haystack.split() for w in words)
    candidates = [i for i in items if hit(i, "name")]
    if not candidates:
        candidates = [i for i in items if hit(i, "name", "description", "category")]
    if not candidates:
        return None, []
    available = [i for i in candidates if not i.get("unavailable")] or candidates
    def rank(item: dict[str, Any]) -> tuple[int, int, int]:
        name_words = [_singular(w) for w in _words(item.get("name"))]
        exact = 0 if name_words == words else 1
        return (exact, len(name_words) - len(words), len(str(item.get("name") or "")))
    ordered = sorted(available, key=rank)
    return ordered[0], ordered[1:5]


def _normalize_status(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    for prefix in ("order_status_", "status_"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _fulfillment(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).upper()
    if "PICKUP" in text:
        return "pickup"
    if "DELIVERY" in text:
        return "delivery"
    return text.lower()


def _store_summary(store: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "store_id": str(_first(store, "store_id", "id") or ""),
        "name": _first(store, "name", "store_name", "display_name"),
    }
    meters = _first(store, "distance_meters")
    if meters is not None:
        try:
            summary["miles"] = round(float(meters) / _METERS_PER_MILE, 1)
        except (TypeError, ValueError):
            pass
    for key, sources in (
        ("eta", ("delivery_time", "eta", "delivery_eta", "estimated_delivery_time", "asap_minutes_range")),
        ("rating", ("rating", "average_rating", "star_rating")),
        ("reviews", ("num_ratings", "number_of_ratings", "review_count")),
        ("price_range", ("price_range", "price_range_display")),
        ("cuisine", ("cuisine", "description", "tags", "category")),
        ("open", ("is_open", "is_currently_open", "open")),
        ("address", ("printable_address", "address", "display_address")),
        ("delivery_fee", ("delivery_fee", "delivery_fee_display")),
        ("dashpass", ("is_dashpass_partner", "dashpass")),
        ("kind", ("business_vertical", "vertical", "store_type")),
    ):
        value = _first(store, *sources)
        if value is not None:
            if key == "cuisine" and isinstance(value, list):
                value = ", ".join(str(item) for item in value[:4])
            if key in {"delivery_fee"}:
                value = _money(value)
            summary[key] = value
    status = store.get("availability_status")
    if status:
        summary["open"] = str(status).lower() == "available"
        if not summary["open"]:
            opens = _epoch_ms(_first(store, "next_open_time_asap_delivery_ms", "next_open_time_scheduled_delivery_ms"))
            if opens:
                summary["opens_at"] = opens
    if store.get("is_link_out"):
        summary["orders_elsewhere"] = True
    return summary


_ITEM_LIST_KEYS = ("items", "menu_items", "products", "results", "item_list")


def _looks_like_item(node: dict[str, Any]) -> bool:
    if "name" not in node and "item_name" not in node:
        return False
    if not any(key in node for key in ("item_id", "id", "menu_item_id")):
        return False
    return not any(isinstance(node.get(key), list) for key in _ITEM_LIST_KEYS)


def _collect_items(node: Any, category: str | None, out: list[tuple[str | None, dict[str, Any]]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for entry in node:
            _collect_items(entry, category, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if _looks_like_item(node):
        out.append((category, node))
        return
    own_name = _first(node, "name", "title", "category_name", "category")
    for key, value in node.items():
        if key in {"promotions", "store", "delivery_address", "available_stores"}:
            continue
        if isinstance(value, (list, dict)):
            next_category = str(own_name) if own_name and key in _ITEM_LIST_KEYS else category
            _collect_items(value, next_category, out, depth + 1)


def _item_summary(category: str | None, item: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "item_id": _item_id(_first(item, "item_id", "menu_item_id", "id")),
        "name": _first(item, "name", "item_name"),
        "price": _money(
            _first(
                item, "main_display_price", "display_price", "price_display", "price",
                "main_price", "unit_price", "price_monetary_fields", "pricing_fields",
            )
        ),
    }
    if item.get("price_varies"):
        summary["price"] = f"from {summary['price']}" if summary["price"] else "varies"
    description = _first(item, "description", "item_description")
    if description == "default_description":
        description = None
    description = description or _first(item, "details")
    if description:
        summary["description"] = _short(description)
    category = category or _first(item, "category_name", "category", "taxonomy")
    if category:
        summary["category"] = category
    variation = item.get("variation") if isinstance(item.get("variation"), dict) else {}
    for key, sources in (
        ("size", ("quantity_display", "size", "measurement_unit")),
        ("original_price", ("non_discounted_display_price", "non_discounted_price", "original_price")),
        ("discount", ("discount_text", "discount", "callout_text")),
        ("promo_codes", ("applicable_promotion_ids",)),
        ("store_id", ("store_id",)),
    ):
        value = _first(item, *sources)
        if value is None and key == "size":
            value = variation.get("size")
        if value is not None:
            summary[key] = _money(value) if key == "original_price" else value
    if item.get("is_orderable") is False or _first(item, "is_out_of_stock", "out_of_stock", "sold_out"):
        summary["unavailable"] = True
        reason = _first(item, "unavailability_reason")
        if reason:
            summary["reason"] = _short(reason, 80)
    if _first(item, "has_required_modifiers", "has_required_options", "requires_customization", "has_required_extras"):
        summary["needs_choices"] = True
    elif _first(item, "has_modifiers", "has_options"):
        summary["has_options"] = True
    return summary


def _option_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Customization groups (size, sauce, toppings) with the option ids that
    cart add-items takes, from either item-details shape."""
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    groups = _first(item, "extras", "option_groups", "options", "customizations", "required_options", default=[])
    result: list[dict[str, Any]] = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        minimum = _first(group, "min_num_options", "min_selections", "min", default=0)
        maximum = _first(group, "max_num_options", "max_selections", "max")
        choices = []
        for option in _first(group, "options", "choices", default=[]) or []:
            if not isinstance(option, dict):
                continue
            choice: dict[str, Any] = {
                "id": _item_id(_first(option, "option_id", "id")) or "",
                "name": _first(option, "name", "option_name"),
            }
            price = _money(_first(option, "price", "display_price", "price_monetary_fields"))
            if price not in (None, 0, 0.0, "$0.00"):
                choice["price"] = price
            nested = _first(option, "extras", "options", "option_groups")
            if isinstance(nested, list) and nested and isinstance(nested[0], dict) and _first(nested[0], "options", "choices"):
                choice["has_sub_choices"] = True
            choices.append(choice)
        try:
            required = int(minimum or 0) > 0
        except (TypeError, ValueError):
            required = False
        entry: dict[str, Any] = {
            "group": _first(group, "title", "name", "extra_name"),
            "required": required,
            "choose_min": minimum,
            "choose_max": maximum,
            "choices": choices[:25],
        }
        free = group.get("num_free_options")
        if free:
            entry["free_choices"] = free
        result.append(entry)
    return result


def _cart_summary(cart: dict[str, Any] | None) -> dict[str, Any]:
    cart = cart or {}
    items = []
    menu_id = None
    for entry in cart.get("items") or []:
        if isinstance(entry, dict):
            quantity = _first(entry, "quantity", default=1)
            if isinstance(quantity, float) and quantity.is_integer():
                quantity = int(quantity)
            menu_id = menu_id or _first(entry, "menu_id")
            line: dict[str, Any] = {
                "name": _first(entry, "name", "item_name"),
                "quantity": quantity,
            }
            chosen = [
                str(option.get("name"))
                for option in entry.get("nested_options") or []
                if isinstance(option, dict) and option.get("name")
            ]
            if chosen:
                line["options"] = chosen[:8]
            price = _money(_first(entry, "price", "unit_price", "display_price"))
            if price is not None:
                line["price"] = price
            line_id = _first(entry, "id", "cart_item_id")
            if line_id is not None:
                line["cart_item_id"] = str(line_id)
            items.append(line)
        elif isinstance(entry, str):
            items.append({"name": entry, "quantity": 1})
    summary: dict[str, Any] = {
        "cart_uuid": str(_first(cart, "cart_uuid", "id", "uuid") or ""),
        "store_id": str(_first(cart, "store_id") or "") or None,
        "store_name": _first(cart, "store_name", "name"),
        "items": items,
        "item_count": _first(cart, "items_count", default=len(items)),
    }
    if menu_id:
        summary["menu_id"] = str(menu_id)
    updated = _epoch_ms(_first(cart, "updated_at", "created_at"))
    if updated:
        summary["updated"] = updated
    group_url = cart.get("group_cart_url")
    if group_url:
        summary["group_cart_url"] = group_url
    return summary


def _item_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    for entry in payload.get("item_errors") or []:
        if not isinstance(entry, dict):
            continue
        error: dict[str, Any] = {
            "item": _first(entry, "item_name", "name"),
            "item_id": _item_id(_first(entry, "item_id", "id")),
            "error": _clean(_first(entry, "error_message", "message", "error", default="could not be added")),
        }
        required = []
        for group in entry.get("required_options") or []:
            if not isinstance(group, dict):
                continue
            required.append(
                {
                    "group": _first(group, "name", "title"),
                    "choose_min": _first(group, "min_num_options", default=1),
                    "choose_max": _first(group, "max_num_options"),
                    "choices": [
                        {"id": str(_first(option, "id", "option_id") or ""), "name": _first(option, "name")}
                        for option in group.get("options") or []
                        if isinstance(option, dict)
                    ][:25],
                }
            )
        if required:
            error["required_choices"] = required
        errors.append(error)
    return errors


def _cart_line(item: dict[str, Any]) -> dict[str, Any]:
    """One item as cart add-items wants it, from the model's looser shape."""
    item_id = _item_id(_first(item, "item_id", "id"))
    if not item_id:
        raise ValueError("every item needs an item_id from doordash_menu")
    try:
        quantity = max(1, int(item.get("quantity", 1) or 1))
    except (TypeError, ValueError):
        raise ValueError(f"quantity for {item.get('name') or item_id} must be a whole number") from None
    line: dict[str, Any] = {
        "item_id": item_id,
        "item_name": str(_first(item, "name", "item_name", default=item_id)),
        "quantity": quantity,
    }
    options = _first(item, "options", "nested_options", "choices", default=[]) or []
    nested = _nested_options(options)
    if nested:
        line["nested_options"] = nested
    return line


def _nested_options(options: Any) -> list[dict[str, Any]]:
    result = []
    for option in options if isinstance(options, list) else []:
        if isinstance(option, str):
            result.append({"id": option, "name": option, "quantity": 1})
            continue
        if not isinstance(option, dict):
            continue
        option_id = _first(option, "id", "option_id")
        if not option_id:
            raise ValueError("every option needs the id from doordash_item_details")
        entry: dict[str, Any] = {
            "id": str(option_id),
            "name": str(_first(option, "name", default=option_id)),
            "quantity": int(option.get("quantity", 1) or 1),
        }
        inner = _nested_options(_first(option, "options", "nested_options", "choices", default=[]))
        if inner:
            entry["options"] = inner
        result.append(entry)
    return result


def _order_summary(order: dict[str, Any]) -> dict[str, Any]:
    items = []
    for entry in order.get("items") or []:
        if isinstance(entry, dict):
            name = _first(entry, "name", "item_name")
            quantity = _first(entry, "quantity", default=1)
            items.append(f"{quantity}× {name}" if quantity not in (1, "1", None) else str(name))
        elif isinstance(entry, str):
            items.append(entry)
    summary: dict[str, Any] = {
        "order_uuid": str(_first(order, "order_uuid", "uuid", "id") or ""),
        "store_id": str(_first(order, "store_id") or "") or None,
        "store_name": _first(order, "store_name", "store", "name"),
        "when": _when(
            _first(order, "created_at", "submitted_at", "order_date", "date", "placed_at", "delivered_at", "created_time")
        ),
        "items": items[:10],
        "reorderable": bool(order.get("is_reorderable", True)),
    }
    total = _money(_first(order, "total", "order_total", "grand_total", "total_charged", "amount", "total_monetary_fields"))
    if total is not None:
        summary["total"] = total
    fulfillment = _fulfillment(_first(order, "fulfillment_type", "fulfillment"))
    if fulfillment:
        summary["fulfillment"] = fulfillment
    target = _first(order, "order_target")
    if target:
        summary["kind"] = str(target).replace("ORDER_TARGET_", "").lower()
    status = _normalize_status(_first(order, "status", "order_status"))
    if status:
        summary["status"] = status
    return summary


def _preview_summary(payload: dict[str, Any]) -> dict[str, Any]:
    quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
    cart = quote.get("store_order_cart") if isinstance(quote.get("store_order_cart"), dict) else {}
    items = []
    for order in cart.get("orders") or []:
        for entry in (order.get("order_items") if isinstance(order, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
            line = {
                "name": _first(item, "name") or _first(entry, "name", "item_name"),
                "quantity": _first(entry, "quantity", default=1),
            }
            price = _money(_first(entry, "unit_price_monetary_fields", "unit_price", "price"))
            if price is not None:
                line["unit_price"] = price
            items.append(line)
    breakdown = []
    for line in quote.get("line_items") or []:
        if isinstance(line, dict):
            amount = _money(_first(line, "final_money", "amount", "money"))
            label = _first(line, "label", "charge_id", "name")
            if label and amount is not None:
                breakdown.append({"label": str(label), "amount": amount})
    availability = quote.get("delivery_availability") if isinstance(quote.get("delivery_availability"), dict) else {}
    store = cart.get("store") if isinstance(cart.get("store"), dict) else {}
    if cart.get("is_consumer_pickup") is not None:
        fulfillment = "pickup" if cart.get("is_consumer_pickup") else "delivery"
    else:
        fulfillment = _fulfillment(_first(cart, "fulfillment_type"))
    summary: dict[str, Any] = {
        "store_name": _first(store, "name") or _first(cart, "store_name") or _first(payload, "store_name"),
        "fulfillment": fulfillment,
        "items": items,
        "total": _money(_first(quote, "net_total_before_tip", "total", "net_total")),
        "breakdown": breakdown,
        "deliver_to": _first(quote.get("delivery_address") or {}, "printable_address") if isinstance(quote.get("delivery_address"), dict) else _first(quote, "delivery_address"),
        "asap_available": availability.get("asap_available"),
        "eta": _first(
            availability,
            "asap_delivery_minutes_range_string", "asap_minutes_range_string", "delivery_minutes_range_string",
            "asap_delivery_range_string", "eta_minutes_range_string",
        ),
        "pickup_eta": _first(availability, "asap_pickup_minutes_range_string", "pickup_minutes_range_string"),
        "dashpass": quote.get("is_dashpass_applied"),
    }
    if availability.get("scheduled_delivery_available") and not availability.get("asap_available", True):
        slots = []
        for day in availability.get("available_days") or []:
            stamp = day.get("day_timestamp") if isinstance(day, dict) else None
            for window in (day.get("time_windows") if isinstance(day, dict) else None) or []:
                if isinstance(window, dict) and len(slots) < 4:
                    slot = {
                        "label": window.get("display_string"),
                        "scheduled_time": _first(window, "midpoint_timestamp", "range_min"),
                    }
                    if isinstance(stamp, dict):
                        slot["day"] = f"{stamp.get('year')}-{int(stamp.get('month', 0)):02d}-{int(stamp.get('day', 0)):02d}"
                    slots.append(slot)
        summary["asap_available"] = False
        summary["scheduled_slots"] = slots
    priority = [
        option for option in (availability.get("delivery_options") or [])
        if isinstance(option, dict) and str(option.get("delivery_option_type", "")).upper() == "PRIORITY"
    ]
    if priority:
        summary["priority_available"] = True
    credits = quote.get("credit_details") if isinstance(quote.get("credit_details"), dict) else {}
    credit_amount = _money(credits.get("total_credits_applied"))
    if credit_amount not in (None, "$0.00", 0, 0.0):
        summary["credits_applied"] = credit_amount
    savings = [
        banner.get("discount_details_message")
        for banner in quote.get("discount_banner_details") or []
        if isinstance(banner, dict) and banner.get("discount_details_message")
    ]
    promos = [
        _first(promo, "title", "description")
        for promo in quote.get("promotions") or []
        if isinstance(promo, dict) and _first(promo, "title", "description")
    ]
    if savings or promos:
        summary["savings"] = savings or promos
    pin = any(
        str(option.get("proof_of_delivery_type", "")).upper() == "PIN_CODE"
        for option in quote.get("dropoff_options") or []
        if isinstance(option, dict)
    )
    if pin:
        summary["pin_required_at_handoff"] = True
    tip: dict[str, Any] = {"pre_tippable": quote.get("is_pre_tippable")}
    suggestions = quote.get("tips_suggestion_details") or []
    if suggestions and isinstance(suggestions[0], dict):
        group = suggestions[0]
        try:
            index = int(group.get("default_index"))
            # Suggested as amounts, or as percentages mapped to amounts.
            amounts = group.get("monetary_values") or group.get("percentage_to_amount_monetary_values") or []
            cents = amounts[index].get("unit_amount") if isinstance(amounts[index], dict) else None
            if cents is not None:
                tip["suggested_cents"] = int(cents)
                tip["suggested"] = _cents_to_text(cents)
            tip["choices"] = [
                _money(amount) for amount in amounts if isinstance(amount, dict) and _money(amount)
            ][:5]
            percents = group.get("percentage_values") or []
            if index < len(percents):
                tip["suggested_percent"] = percents[index]
        except (TypeError, ValueError, IndexError, AttributeError):
            pass
    summary["tip"] = tip
    savings_total = _money(quote.get("total_savings"))
    if savings_total not in (None, "$0.00"):
        summary["total_savings"] = savings_total
    if quote.get("contains_alcohol_item") or (quote.get("min_age_requirement") or 0) > 0:
        summary["age_restricted"] = True
    expense = quote.get("expense_order_options") if isinstance(quote.get("expense_order_options"), dict) else {}
    budgets = []
    for budget in expense.get("all_eligible_expense_order_budgets") or []:
        if not isinstance(budget, dict):
            continue
        remaining = budget.get("remaining_amount") if isinstance(budget.get("remaining_amount"), dict) else {}
        try:
            if int(remaining.get("unit_amount", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        budgets.append(
            {
                "budget_id": budget.get("id"),
                "name": budget.get("name"),
                "remaining": _money(remaining),
                "expense_code": budget.get("expense_code_mode"),
                "note_required": budget.get("is_expense_note_required"),
            }
        )
    if budgets:
        summary["work_budgets"] = budgets
        team = (quote.get("company_payment_info") or {}).get("team_order_info") or {}
        if isinstance(team, dict) and team.get("team_id"):
            summary["_team_id"] = team.get("team_id")
    return summary


def _card_summary(payload: dict[str, Any]) -> dict[str, Any]:
    cards = []
    default_id = _first(payload, "default_payment_method_id")
    default_card = None
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        entry = {
            "brand": _first(card, "brand", "card_brand", default="card"),
            "last4": _first(card, "last4", "last_four"),
        }
        if default_id and str(card.get("payment_method_id")) == str(default_id):
            entry["default"] = True
            default_card = entry
        cards.append(entry)
    result: dict[str, Any] = {"cards": cards}
    if default_card:
        result["default_card"] = default_card
        result["default_card_spoken"] = f"{default_card['brand']} ending in {default_card['last4']}"
    else:
        result["card_note"] = (
            "No default card is visible to the CLI; the account default may be a wallet "
            "(Apple Pay, Google Pay, PayPal). Say 'whatever default payment you have on "
            "file', and offer the checkout link if they want to check or change it."
        )
    return result


class DoorDashService:
    def __init__(
        self,
        config: DoorDashConfig,
        home: HomeLookup | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.binary = find_binary(config.binary) if config.enabled else None
        self._home = home
        self._runner = runner or run_dd_cli
        self._addresses: tuple[float, list[dict[str, Any]]] | None = None
        # cart_uuid -> the quote the user was read, so placing can insist on
        # a fresh preview and submit with exactly the options it was priced with.
        self._previews: dict[str, dict[str, Any]] = {}
        self.last_order: dict[str, Any] | None = None
        # A sign-in under way: the dd-cli process waiting on localhost:4180.
        self._login: dict[str, Any] | None = None
        self._last_login: dict[str, Any] | None = None
        # Store names seen this session, by id: menus come back without one.
        self._store_names: dict[str, str] = {}
        self._spawn = asyncio.create_subprocess_exec
        self._http_get = _http_get

    @property
    def available(self) -> bool:
        return self.config.enabled and self.binary is not None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "installed": self.binary is not None,
            "binary": str(self.binary) if self.binary else None,
            "token_configured": bool(self.config.access_token),
        }

    # --------------------------------------------------------------- sign-in

    def login_in_progress(self) -> bool:
        login = self._login
        return bool(login) and login["process"].returncode is None

    def login_url(self) -> str | None:
        """The sign-in link of the attempt still waiting, if any."""
        return self._login["url"] if self.login_in_progress() else None

    async def start_login(
        self,
        on_done: Callable[[bool, str], Awaitable[None]] | None = None,
        on_expired: Callable[[], Awaitable[None]] | None = None,
        reuse_seconds: float = 200.0,
    ) -> dict[str, Any]:
        """Run `dd-cli login` and hand back its sign-in link. The browser is
        never opened here — the link goes to the user's screen — and on_done
        is called once the flow ends, either way. dd-cli allows five minutes
        per attempt: one still young is handed out again (a phone that signed
        in against it can still paste its address back), one older than
        reuse_seconds is quietly replaced rather than handed out nearly spent."""
        if not self.config.enabled:
            raise DoorDashNotConfigured(
                "DoorDash ordering is switched off in config.toml ([doordash] enabled = false)."
            )
        if self.binary is None:
            raise DoorDashNotConfigured(
                "DoorDash ordering is not installed on this house yet: dd-cli is missing."
            )
        if self.login_in_progress():
            login = self._login or {}
            if time.time() - login["started"] < reuse_seconds:
                return {"url": login["url"], "already_running": True}
            login["quiet"] = True  # replaced, not failed: nothing to announce
            waiter = login.get("waiter")
            if waiter:
                waiter.cancel()
            await self._abandon_login(login)
        env = dict(os.environ)
        env["BROWSER"] = "/bin/true"
        # Its own session: a Ctrl+C that restarts house-agent in a terminal
        # must not take the waiting sign-in down with it. close() ends it.
        process = await self._spawn(
            str(self.binary), "login",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        login: dict[str, Any] = {
            "process": process, "url": None, "state": None, "output": [],
            "started": time.time(), "on_done": on_done, "on_expired": on_expired,
            "done": None, "quiet": False,
        }
        self._login = login
        found = asyncio.Event()

        async def read(stream: Any) -> None:
            try:
                while stream is not None:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    login["output"].append(text)
                    if login["url"] is None:
                        match = _LOGIN_URL.search(text)
                        if match:
                            login["url"] = match.group(0)
                            login["state"] = (parse_qs(urlsplit(login["url"]).query).get("state") or [None])[0]
                            found.set()
            finally:
                found.set()

        login["readers"] = [asyncio.create_task(read(process.stderr)), asyncio.create_task(read(process.stdout))]
        try:
            await asyncio.wait_for(found.wait(), _LOGIN_URL_WAIT)
        except asyncio.TimeoutError:
            pass
        if not login["url"]:
            await self._abandon_login(login)
            raise DoorDashError("dd-cli login gave no sign-in link: " + _login_failure(login["output"]))
        login["waiter"] = asyncio.create_task(self._await_login(login))
        return {"url": login["url"], "callback": _LOGIN_CALLBACK}

    async def _abandon_login(self, login: dict[str, Any]) -> None:
        process = login["process"]
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5)
        for task in login.get("readers", []):
            task.cancel()
        if self._login is login:
            self._login = None

    async def _await_login(self, login: dict[str, Any]) -> None:
        process = login["process"]
        try:
            expired = False
            try:
                await asyncio.wait_for(process.wait(), _LOGIN_WINDOW_SECONDS)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
                ok, message, expired = False, "the sign-in link went unused and expired", True
            else:
                await asyncio.gather(*login.get("readers", []), return_exceptions=True)
                ok = process.returncode == 0
                message = "DoorDash is signed in." if ok else _login_failure(login["output"])
                if not ok and (process.returncode or 0) < 0:
                    # Killed by a signal — a shutdown, not a sign-in problem.
                    login["quiet"] = True
                elif not ok and time.time() - login["started"] >= _DD_CLI_LOGIN_WINDOW - 20:
                    # dd-cli's own five minutes ran out: the link went unused.
                    message, expired = "the sign-in link went unused and expired", True
            login["done"] = (ok, message)
            if ok:
                self.forget_addresses()
            if login.get("quiet"):
                return
            try:
                if expired:
                    if self._login is login:
                        self._login = None  # so the refresh below can start anew
                    on_expired = login.get("on_expired")
                    if on_expired:
                        await on_expired()
                elif login.get("on_done"):
                    await login["on_done"](ok, message)
            except Exception:  # noqa: BLE001 - reporting must not kill the flow
                log.exception("login completion callback failed")
        finally:
            self._last_login = login
            if self._login is login:
                self._login = None

    async def finish_login(self, pasted: str) -> dict[str, Any]:
        """Deliver the code from the address the user's browser landed on to
        the waiting dd-cli, which exchanges it for the token."""
        login = self._login
        if not login or login["process"].returncode is not None:
            last = self._last_login or {}
            done = last.get("done")
            if done and done[0] and time.time() - last.get("started", 0) < _LOGIN_WINDOW_SECONDS:
                return {"signed_in": True, "message": "DoorDash is already signed in."}
            raise DoorDashError("No DoorDash sign-in is waiting; start one with doordash_login first.")
        code, state = _callback_params(pasted)
        if not code:
            raise DoorDashError(
                "That is not the address the sign-in landed on: it should start with "
                "http://localhost:4180/oauth2/callback and contain code= and state=."
            )
        if login.get("state") and state and state != login["state"]:
            raise DoorDashError(
                "That address is from an earlier sign-in attempt. Open the link in the "
                "messages again, sign in, and paste the new address."
            )
        login["quiet"] = True  # the reply covers it; no separate announcement
        try:
            await self._http_get(f"{_LOGIN_CALLBACK}?{urlencode({'code': code, 'state': state or login['state'] or ''})}")
        except Exception as exc:  # noqa: BLE001 - any transport failure reads the same to the user
            login["quiet"] = False
            raise DoorDashError(f"Could not hand the sign-in code to dd-cli: {_clean(exc)}") from exc
        try:
            await asyncio.wait_for(asyncio.shield(login["waiter"]), _LOGIN_FINISH_WAIT)
        except asyncio.TimeoutError:
            login["quiet"] = False
            return {
                "signed_in": False,
                "pending": True,
                "message": "The code was delivered; DoorDash is still finishing the sign-in and it will be announced.",
            }
        ok, message = login["done"]
        if not ok:
            raise DoorDashError(message)
        return {"signed_in": True, "message": message}

    async def close(self) -> None:
        login = self._login
        if login:
            waiter = login.get("waiter")
            if waiter:
                waiter.cancel()
            await self._abandon_login(login)

    # ------------------------------------------------------------------ core

    async def _call(self, *args: str, purpose: str | None, timeout: float | None = None) -> dict[str, Any]:
        if not self.config.enabled:
            raise DoorDashNotConfigured(
                "DoorDash ordering is switched off in config.toml ([doordash] enabled = false)."
            )
        if self.binary is None:
            raise DoorDashNotConfigured(
                "DoorDash ordering is not installed on this house yet: dd-cli is missing. "
                "Install it from github.com/doordash-oss/doordash-cli and run `dd-cli login` "
                "on the house computer."
            )
        argv = [str(self.binary), "--json-output", *args, "--intent", intent_for(purpose)]
        env = dict(os.environ)
        if self.config.access_token:
            env["DD_CLI_ACCESS_TOKEN"] = self.config.access_token
        log.info("dd-cli %s", " ".join(args[:6]))
        code, out, err = await self._runner(argv, env, timeout or self.config.timeout_seconds)
        text = out.decode("utf-8", errors="replace")
        error_text = err.decode("utf-8", errors="replace").strip()
        if code != 0 and _CREDENTIAL_PROBLEM.search(f"{error_text}\n{text}"):
            raise DoorDashNotSignedIn("DoorDash is not signed in yet (or the sign-in expired).")
        payload = _unwrap(_parse_json(text))
        if code != 0 or payload.get("is_error"):
            message = _first(payload, "message", "error", "error_message", "detail") or error_text or text.strip()
            raise DoorDashError(_clean(message) or f"dd-cli exited with status {code}")
        log.debug("dd-cli %s -> %s", args[0] if args else "?", json.dumps(payload)[:2000])
        return payload

    async def _addresses_cached(self, purpose: str | None) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._addresses and now - self._addresses[0] < _ADDRESS_CACHE_SECONDS:
            return self._addresses[1]
        payload = await self._call("address", "list", purpose=purpose)
        addresses = [entry for entry in payload.get("addresses") or [] if isinstance(entry, dict)]
        self._addresses = (now, addresses)
        return addresses

    def forget_addresses(self) -> None:
        self._addresses = None

    @staticmethod
    def _address_entry(address: dict[str, Any]) -> dict[str, Any]:
        return {
            "address_id": str(_first(address, "address_id", "id") or ""),
            "label": _first(address, "label"),
            "address": _first(address, "printable_address", "address"),
            "default": bool(address.get("is_default")),
        }

    async def _resolve_address(self, label: str | None, purpose: str | None) -> dict[str, Any] | None:
        addresses = await self._addresses_cached(purpose)
        if label:
            wanted = label.casefold().strip()
            labeled = [a for a in addresses if str(a.get("label") or "").casefold() == wanted]
            if not labeled:
                labeled = [a for a in addresses if wanted in str(a.get("label") or "").casefold()]
            if not labeled:
                labeled = [a for a in addresses if wanted in str(a.get("printable_address") or "").casefold()]
            if len(labeled) == 1:
                return labeled[0]
            if not labeled:
                known = sorted({str(a.get("label")) for a in addresses if a.get("label")})
                raise DoorDashError(
                    f"No saved DoorDash address matches {label!r}. "
                    + (f"Saved labels: {', '.join(known)}." if known else "No saved address has a label.")
                )
            raise DoorDashError(
                f"Several saved addresses match {label!r}: "
                + "; ".join(str(a.get("printable_address")) for a in labeled[:4])
                + ". Ask which one."
            )
        defaults = [a for a in addresses if a.get("is_default")]
        if defaults:
            return defaults[0]
        home = [a for a in addresses if str(a.get("label") or "").casefold() == "home"]
        if home:
            return home[0]
        return None

    async def _location_flags(self, label: str | None, purpose: str | None) -> tuple[list[str], str | None]:
        """--address-id for a saved address (the default, or the label asked
        for), else the house's own coordinates. Never the CLI's own fallback,
        which is a fixed point in California."""
        try:
            address = await self._resolve_address(label, purpose)
        except DoorDashNotConfigured:
            raise
        except DoorDashError:
            if label:
                raise
            address = None
        if address and _first(address, "address_id", "id"):
            spoken = _first(address, "label")
            if not spoken and not label and address.get("is_default"):
                spoken = "home"
            spoken = spoken or _first(address, "printable_address")
            return ["--address-id", str(_first(address, "address_id", "id"))], str(spoken) if spoken else None
        home = self._home() if self._home else None
        if home:
            return ["--lat", f"{home[0]:.5f}", "--lng", f"{home[1]:.5f}"], home[2] or "home"
        return [], None

    # --------------------------------------------------------------- reading

    async def search(
        self,
        query: str,
        kind: str = "restaurants",
        address: str | None = None,
        limit: int = 5,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        query = " ".join(str(query or "").split())
        kind_key = str(kind or "restaurants").casefold().strip()
        if kind_key not in _VERTICALS:
            raise ValueError(f"kind must be one of {', '.join(sorted(set(_VERTICALS)))}, not {kind!r}")
        vertical = _VERTICALS[kind_key]
        limit = max(1, min(int(limit or 5), 15))
        location, near = await self._location_flags(address, purpose)
        if vertical is None:
            if not query:
                raise ValueError("say what to search for, e.g. 'thai' or 'pizza'")
            # DoorDash answers with "top picks", which can bury the very
            # store that was named; ask for more than needed and put name
            # matches first.
            payload = await self._call(
                "search", "--query", query, "--limit", str(max(limit * 2, 10)), *location, purpose=purpose
            )
        else:
            payload = await self._call(
                "find-nearby-stores", "--vertical", vertical, "--max", str(max(limit, 8)), *location, purpose=purpose
            )
        stores = [_store_summary(s) for s in payload.get("stores") or [] if isinstance(s, dict)]
        self._remember_stores(stores)
        words = _words(query) if query else []
        if vertical is not None and words:
            named = [s for s in stores if _matches(words, s.get("name"), s.get("cuisine"))]
            # A named store that is not in the list is worth saying; an
            # unnamed browse just lists what is close.
            stores = named or stores
        elif words:
            named = [s for s in stores if _matches(words, s.get("name"))]
            stores = named + [s for s in stores if s not in named]
        stores = stores[:limit]
        widened_to: str | None = None
        if vertical is None and not stores and len(words) > 1:
            stores, widened_to = await self._widen_search(words, limit, location, purpose)
        open_carts: list[dict[str, Any]] = []
        try:
            carts = await self.carts(purpose=purpose)
            by_store = {c["store_id"]: c for c in carts if c.get("store_id")}
            for store in stores:
                cart = by_store.get(store["store_id"])
                if cart:
                    store["open_cart"] = {"cart_uuid": cart["cart_uuid"], "items": cart["items"]}
                    open_carts.append(cart)
        except DoorDashError:
            pass
        result: dict[str, Any] = {"query": query, "kind": kind_key, "near": near, "stores": stores}
        if not stores:
            result["note"] = (
                f"No store on DoorDash matched {query!r}, with fewer words tried too. Search "
                "once more for the plainest thing in the request — the brand, the cuisine, or "
                "the dish on its own — and only then say nothing nearby carries it."
            )
        elif widened_to:
            result["widened_to"] = widened_to
            result["note"] = (
                f"Nothing matched {query!r}, so these are stores for {widened_to!r} instead. "
                "Tell the user you did not find that exact thing, name the store you did find, "
                "and read its menu for what they asked about before saying it does not exist."
            )
        elif query:
            # Searching for "Baja Blast" returns Taco Bell, and a model that
            # reads that as "I found a Baja Blast" tells the user an item
            # exists on the strength of a store's name. Nothing here has been
            # near a menu.
            result["stores_not_items"] = (
                f"These are stores, not menu items — nothing here says whether {query!r} is on "
                "any menu, or which versions of it a store sells. Before telling the user an "
                "item exists, is special, or is limited-edition, call doordash_menu on the "
                "store with those words."
            )
        message = _first(payload, "message")
        if message and not stores:
            result["message"] = _clean(message)
        return result

    async def _widen_search(
        self,
        words: list[str],
        limit: int,
        location: list[str],
        purpose: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """A store search that found nothing, asked again with fewer words.

        "special Baja Blast Olivia Rodrigo" matches no store on DoorDash, and
        a model told only "nothing matched" stops there — while "Baja Blast"
        finds the Taco Bell that sells it. Two shorter tries, then it really
        is nothing nearby.
        """
        plain = [word for word in words if word not in _QUALIFIER] or words
        asked = " ".join(words)
        tries: list[str] = []
        for candidate in (" ".join(plain[:2]), plain[0]):
            if candidate and candidate != asked and candidate not in tries:
                tries.append(candidate)
        for candidate in tries[:2]:
            payload = await self._call(
                "search", "--query", candidate, "--limit", str(max(limit * 2, 10)),
                *location, purpose=purpose,
            )
            found = [_store_summary(s) for s in payload.get("stores") or [] if isinstance(s, dict)]
            if found:
                self._remember_stores(found)
                return found[:limit], candidate
        return [], None

    def known_store(self, store_id: str | None) -> bool:
        """Whether a store id came from a result this house actually saw.

        Every path that hands the model an id — a search, the carts, the order
        history — remembers it here. An id that is not here was invented, and
        a made-up id reaches a real store: one such call answered a question
        about Taco Bell's drinks with "this store has no menu".
        """
        return bool(store_id) and str(store_id) in self._store_names

    def _remember_stores(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            store_id = str(entry.get("store_id") or "")
            name = entry.get("name") or entry.get("store_name")
            if store_id and name:
                self._store_names[store_id] = str(name)

    async def store_info(self, store_id: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("store-details", "--store-id", str(store_id), purpose=purpose)
        store = payload.get("store") if isinstance(payload.get("store"), dict) else payload
        summary = _store_summary(store)
        for key, sources in (
            ("phone", ("phone_number", "phone")),
            ("hours", ("hours", "open_hours", "business_hours")),
            ("description", ("description",)),
        ):
            value = _first(store, *sources)
            if value is not None:
                summary[key] = value if key != "description" else _short(value, 200)
        return summary

    async def _load_menu(
        self, store_id: str, address: str | None, purpose: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The whole menu of a restaurant, every item summarized."""
        flags: list[str] = []
        if address:
            location, _ = await self._location_flags(address, purpose)
            flags = location if location[:1] == ["--address-id"] else []
        payload = await self._call("menu", "--store-id", str(store_id), *flags, purpose=purpose)
        collected: list[tuple[str | None, dict[str, Any]]] = []
        _collect_items(payload, None, collected)
        items = [_item_summary(category, item) for category, item in collected]
        name = _first(payload, "store_name", "name")
        if name:
            self._store_names[str(store_id)] = str(name)
        return payload, items

    async def menu(
        self,
        store_id: str,
        query: str | None = None,
        limit: int = 20,
        address: str | None = None,
        grocery: bool = False,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        store_id = str(store_id).strip()
        limit = max(1, min(int(limit or 20), 60))
        words = _words(query)
        if grocery and words:
            queries: list[str] = []
            for part in re.split(r",| and ", " ".join(words)):
                part = part.strip()
                if part:
                    queries.extend(["--query", part])
            payload = await self._call("find-items", "--store-id", store_id, *queries, purpose=purpose)
            found: list[dict[str, Any]] = []
            results = payload.get("results")
            if isinstance(results, dict):
                for asked, entries in results.items():
                    for entry in entries if isinstance(entries, list) else []:
                        if isinstance(entry, dict):
                            summary = _item_summary(None, entry)
                            summary["matched"] = asked
                            found.append(summary)
            else:
                collected: list[tuple[str | None, dict[str, Any]]] = []
                _collect_items(results if results is not None else payload, None, collected)
                found = [_item_summary(category, item) for category, item in collected]
            return {
                "store_id": store_id,
                "query": " ".join(words),
                "items": found[:limit],
                "menu_id": _first(payload, "menu_id"),
                "note": "Grocery items: menu_id is looked up automatically when adding to the cart.",
            }
        payload, items = await self._load_menu(store_id, address, purpose)
        asked_for_sections = bool(words) and all(word in _NOT_AN_ITEM for word in words)
        if asked_for_sections:
            words = []
        menu_items = items
        total = len(items)
        categories: list[str] = []
        for item in items:
            category = item.get("category")
            if category and category not in categories:
                categories.append(category)
        relaxed = False
        if words:
            exact = [i for i in items if _matches(words, i.get("name"), i.get("description"), i.get("category"))]
            if exact:
                items = exact
            else:
                items, relaxed = _near_matches(words, items), True
        result: dict[str, Any] = {
            "store_id": store_id,
            "store_name": _first(payload, "store_name", "name") or self._store_names.get(store_id),
            "menu_id": str(_first(payload, "menu_id") or "") or None,
            "items": items[:limit],
            "item_count": total,
            "shown": min(len(items), limit),
        }
        if payload.get("store_is_open") is False:
            result["open"] = False
            result["note"] = "The store is closed right now."
        if categories:
            result["categories"] = categories[:25]
        if asked_for_sections:
            result["note"] = (
                f"A menu has no item called {query!r} — that question is about the store's "
                "sections, which are in categories. Call again with the one they mean ('drinks', "
                "'desserts'), or with the drink or dish they named earlier, and read out what "
                "comes back. Never tell the user the menu has nothing by that name."
            )
        elif words and not items:
            # The menu is in hand either way, so hand back a sample of it: a
            # model told only "no match" starts saying it cannot see menus.
            result["items"] = menu_items[:limit]
            result["shown"] = len(result["items"])
            result["no_match"] = " ".join(words)
            result["note"] = (
                f"Nothing on this menu is called {' '.join(words)!r}. These are items from the menu "
                "instead — offer the closest ones by name, or call again with one word (a category "
                "name works). Never tell the user the menu cannot be seen."
            )
        elif words and relaxed:
            result["note"] = (
                f"No item matches all of {' '.join(words)!r}; these are the closest, best first. "
                "Offer them by name and price rather than saying it was not found."
            )
        elif words and len(items) > 1:
            result["note"] = (
                f"{min(len(items), limit)} items match {' '.join(words)!r} — these are the "
                "versions this store sells. Asked what they have, what flavors, kinds, or "
                "sizes there are, name every one of them, not only the first."
            )
        elif not words and total > limit:
            result["note"] = (
                f"Showing {limit} of {total} items: the first on the menu, which is neither the "
                "whole menu nor the section the user asked about. To read out a whole section — "
                "every drink, every dessert — call again with query set to that category name "
                "from categories. Never offer this sample as the full menu or a full section."
            )
        promos = [
            {k: v for k, v in {
                "title": _first(p, "title"),
                "description": _short(_first(p, "description"), 140),
                "code": _first(p, "code"),
            }.items() if v}
            for p in payload.get("promotions") or []
            if isinstance(p, dict)
        ]
        if promos:
            result["promotions"] = promos[:5]
        return result

    async def item_details(
        self,
        store_id: str,
        item_id: str,
        menu_id: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        item_id = _item_id(item_id) or ""
        if menu_id:
            payload = await self._call(
                "restaurant-item-details", "--store-id", str(store_id), "--menu-id", str(menu_id),
                "--item-id", item_id, purpose=purpose,
            )
        else:
            payload = await self._call("item-details", "--store-id", str(store_id), "--item-id", item_id, purpose=purpose)
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        summary = _item_summary(None, {**item, "item_id": _first(item, "item_id", "id", default=item_id)})
        unit = _first(item, "unit")
        if unit and unit != "qty":
            summary["unit"] = unit
        summary["menu_id"] = str(_first(payload, "menu_id") or _first(item, "menu_id") or menu_id or "") or None
        summary["options"] = _option_groups(payload)
        summary["required_choices"] = [g["group"] for g in summary["options"] if g["required"]]
        return summary

    # ----------------------------------------------------------------- carts

    async def carts(self, store_id: str | None = None, purpose: str | None = None) -> list[dict[str, Any]]:
        flags = ["--store-id", str(store_id)] if store_id else []
        payload = await self._call("cart", "list", *flags, purpose=purpose)
        carts = [_cart_summary(c) for c in payload.get("carts") or [] if isinstance(c, dict)]
        self._remember_stores(carts)
        return carts

    async def cart_show(self, cart_uuid: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("cart", "show", "--cart-uuid", str(cart_uuid), purpose=purpose)
        cart = payload.get("cart") if isinstance(payload.get("cart"), dict) else payload
        summary = _cart_summary(cart)
        summary["cart_uuid"] = summary["cart_uuid"] or str(cart_uuid)
        return summary

    async def cart_add(
        self,
        store_id: str,
        items: list[dict[str, Any]],
        menu_id: str | None = None,
        cart_uuid: str | None = None,
        fulfillment: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        store_id = str(store_id).strip()
        if not items:
            raise ValueError("no items to add")
        lines = [_cart_line(item) for item in items if isinstance(item, dict)]
        if not lines:
            raise ValueError("items must be objects with item_id and quantity")
        if not menu_id:
            # Grocery and retail items carry no menu id in search results;
            # item-details knows it.
            details = await self._call(
                "item-details", "--store-id", store_id, "--item-id", lines[0]["item_id"], purpose=purpose
            )
            menu_id = str(_first(details, "menu_id") or "") or None
            if not menu_id:
                raise DoorDashError(
                    "DoorDash did not say which menu these items belong to; call doordash_menu "
                    "for this store and pass its menu_id."
                )
        previous: dict[str, Any] | None = None
        if not cart_uuid:
            # One open cart per store: adding to it beats creating a rival
            # that silently fails, and the preview shows everything anyway.
            try:
                existing = await self.carts(store_id, purpose=purpose)
            except DoorDashError:
                existing = []
            if existing:
                previous = existing[0]
                cart_uuid = previous["cart_uuid"]
        flags = ["--store-id", store_id, "--menu-id", str(menu_id), "--items-json", json.dumps(lines)]
        if cart_uuid:
            flags += ["--cart-uuid", str(cart_uuid)]
        if fulfillment:
            mode = str(fulfillment).casefold()
            if mode not in {"delivery", "pickup"}:
                raise ValueError("fulfillment must be delivery or pickup")
            flags += ["--fulfillment", mode]
        payload = await self._call("cart", "add-items", *flags, purpose=purpose)
        cart = payload.get("cart") if isinstance(payload.get("cart"), dict) else {}
        summary = _cart_summary(cart)
        summary["cart_uuid"] = str(_first(payload, "cart_uuid") or summary["cart_uuid"] or cart_uuid or "")
        summary["store_id"] = summary.get("store_id") or store_id
        errors = _item_errors(payload)
        result: dict[str, Any] = {
            "success": bool(payload.get("success", not errors)),
            "cart": summary,
            "added": [line["item_name"] for line in lines if not any(e.get("item_id") == line["item_id"] for e in errors)],
        }
        if errors:
            result["item_errors"] = errors
            if any(e.get("required_choices") for e in errors):
                result["note"] = (
                    "Some items need a choice made first: ask the user the required_choices "
                    "questions, then call doordash_cart_add again for those items with options set."
                )
        if previous and previous.get("items"):
            result["added_to_existing_cart"] = {
                "already_had": [f"{i.get('quantity', 1)}× {i.get('name')}" for i in previous["items"]][:8],
                "updated": previous.get("updated"),
            }
        return result

    async def cart_remove(
        self,
        cart_uuid: str,
        cart_item_id: str | None = None,
        item_name: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        if not cart_item_id:
            if not item_name:
                raise ValueError("say which item to remove")
            cart = await self.cart_show(cart_uuid, purpose=purpose)
            wanted = _words(item_name)
            matches = [i for i in cart["items"] if _matches(wanted, i.get("name"))]
            if not matches:
                raise DoorDashError(
                    f"Nothing called {item_name!r} is in that cart. It has: "
                    + ", ".join(str(i.get("name")) for i in cart["items"])
                )
            if len(matches) > 1 and len({m.get("name") for m in matches}) > 1:
                raise DoorDashError(
                    f"{item_name!r} matches several lines: "
                    + ", ".join(str(m.get("name")) for m in matches)
                    + ". Ask which one."
                )
            cart_item_id = matches[0].get("cart_item_id")
            if not cart_item_id:
                raise DoorDashError("DoorDash did not give that line an id to remove it by.")
        payload = await self._call(
            "cart", "remove-item", "--cart-uuid", str(cart_uuid), "--cart-item-id", str(cart_item_id), purpose=purpose
        )
        cart = payload.get("cart") if isinstance(payload.get("cart"), dict) else {}
        summary = _cart_summary(cart)
        summary["cart_uuid"] = summary["cart_uuid"] or str(cart_uuid)
        self._previews.pop(str(cart_uuid), None)
        return {"success": bool(payload.get("success", True)), "cart": summary}

    async def cart_delete(self, cart_uuid: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("cart", "delete", "--cart-uuid", str(cart_uuid), purpose=purpose)
        self._previews.pop(str(cart_uuid), None)
        return {"success": bool(payload.get("success", True)), "cart_uuid": str(cart_uuid)}

    async def grocery_list(
        self,
        items: list[dict[str, Any] | str],
        store_name: str | None = None,
        store_id: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        wanted = []
        for entry in items:
            if isinstance(entry, str):
                name = " ".join(entry.split())
                quantity: Any = 1
            elif isinstance(entry, dict):
                name = " ".join(str(_first(entry, "name", "item", default="")).split())
                quantity = entry.get("quantity", 1) or 1
            else:
                continue
            if not name:
                continue
            item: dict[str, Any] = {"name": name}
            try:
                number = float(quantity)
                item["quantity"] = int(number) if number.is_integer() else number
            except (TypeError, ValueError):
                item["quantity"] = 1
            wanted.append(item)
        if not wanted:
            raise ValueError("the grocery list is empty")
        flags = ["--items-json", json.dumps(wanted)]
        if store_id:
            flags += ["--store-id", str(store_id)]
        if store_name:
            flags += ["--desired-mx-name", str(store_name)]
        payload = await self._call("build-grocery-list", *flags, purpose=purpose)
        collected: list[tuple[str | None, dict[str, Any]]] = []
        _collect_items(payload.get("items"), None, collected)
        found = []
        for _, item in collected:
            summary = _item_summary(None, item)
            summary["quantity"] = item.get("quantity", 1)
            unit = _first(item, "measurement_unit")
            if unit:
                summary["unit"] = unit
            found.append(summary)
        stores = [_store_summary(s) for s in payload.get("available_stores") or [] if isinstance(s, dict)]
        chosen_store = str(_first(payload, "store_id") or (found[0].get("store_id") if found else "") or store_id or "")
        if not chosen_store and stores:
            name = str(_first(payload, "store_name") or "").casefold()
            for store in stores:
                if str(store.get("name") or "").casefold() == name:
                    chosen_store = store["store_id"]
                    break
        result: dict[str, Any] = {
            "store_id": chosen_store or None,
            "store_name": _first(payload, "store_name"),
            "menu_id": str(_first(payload, "menu_id") or "") or None,
            "items": found[:40],
            "eta": _first(payload, "delivery_time"),
            "deliver_to": _first(payload, "delivery_address"),
            "other_stores": [{"store_id": s["store_id"], "name": s.get("name"), "eta": s.get("eta")} for s in stores[:5]],
        }
        message = _first(payload, "message")
        if message:
            result["message"] = _clean(message)
        missing = [w["name"] for w in wanted if not any(_matches(_words(w["name"]), f.get("name")) for f in found)]
        if missing and found:
            result["possibly_missing"] = missing
        return result

    # ---------------------------------------------------------------- orders

    async def order_history(self, days: int = 90, limit: int = 20, purpose: str | None = None) -> dict[str, Any]:
        days = max(1, min(int(days or 90), 365))
        limit = max(1, min(int(limit or 20), 100))
        payload = await self._call(
            "order", "history", "--max", str(max(limit, 30)), "--days", str(days), purpose=purpose
        )
        orders = [_order_summary(o) for o in payload.get("orders") or [] if isinstance(o, dict)]
        self._remember_stores(orders)
        counts: Counter[str] = Counter()
        latest: dict[str, dict[str, Any]] = {}
        for order in orders:
            name = str(order.get("store_name") or "").strip()
            if not name:
                continue
            counts[name] += 1
            latest.setdefault(name, order)
        usual = [
            {
                "store_name": name,
                "store_id": latest[name].get("store_id"),
                "times": times,
                "last_order_uuid": latest[name].get("order_uuid"),
                "last_items": latest[name].get("items", [])[:6],
            }
            for name, times in counts.most_common(5)
        ]
        return {
            "days": days,
            "orders": orders[:limit],
            "order_count": len(orders),
            "usual_places": usual,
            "more_available": bool(payload.get("page_full")),
        }

    async def receipt(self, order_uuid: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("order", "receipt", "--order-uuid", str(order_uuid), purpose=purpose)
        inner = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
        receipt = {**inner, **{k: v for k, v in payload.items() if k != "receipt"}}
        items: list[str] = []
        for order in receipt.get("orders") or []:
            for entry in (order.get("order_items") if isinstance(order, dict) else None) or []:
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
                name = _first(item, "name") or _first(entry, "name")
                if not name:
                    continue
                quantity = _first(entry, "quantity", default=1)
                chosen = [
                    str((option.get("item_extra_option") or {}).get("name") or option.get("name"))
                    for option in entry.get("options") or []
                    if isinstance(option, dict) and ((option.get("item_extra_option") or {}).get("name") or option.get("name"))
                ]
                label = f"{quantity}× {name}" if quantity not in (1, "1", None) else str(name)
                if chosen:
                    label += " (" + ", ".join(chosen[:4]) + ")"
                items.append(label)
        summary = _order_summary({**receipt, "order_uuid": order_uuid, "items": []})
        if items:
            summary["items"] = items[:12]
        lines = []
        for line in _first(receipt, "line_items", "charges", default=[]) or []:
            if isinstance(line, dict):
                amount = _money(_first(line, "final_money", "amount", "money", "value"))
                label = _first(line, "label", "name", "charge_id")
                if label and amount is not None:
                    lines.append({"label": str(label), "amount": amount})
                    charge = str(line.get("charge_id") or label).upper()
                    if "TOTAL" in charge and "SUB" not in charge:
                        summary["total"] = amount
                    elif "TIP" in charge:
                        summary["tip"] = amount
                    elif "SUBTOTAL" in charge:
                        summary["subtotal"] = amount
        summary["breakdown"] = lines
        for charge in payload.get("payment_charge_details") or []:
            for data in (charge.get("data") if isinstance(charge, dict) else None) or []:
                if not isinstance(data, dict):
                    continue
                charged = _money(_first(data, "net_amount", "original_amount"))
                if charged and "total" not in summary:
                    summary["total"] = charged
                last4 = _first(data, "last4", "card_last4")
                brand = _first(data, "brand", "card_brand")
                method = data.get("payment_method") if isinstance(data.get("payment_method"), dict) else {}
                last4 = last4 or _first(method, "last4")
                brand = brand or _first(method, "brand")
                if last4:
                    summary["card"] = f"{brand} ending in {last4}" if brand else f"card ending in {last4}"
        for key, sources in (
            ("subtotal", ("subtotal",)),
            ("tip", ("tip", "dasher_tip")),
            ("tax", ("tax", "taxes")),
            ("fees", ("fees", "total_fees")),
            ("total", ("total", "grand_total", "total_charged")),
            ("credits", ("credits", "credits_applied")),
        ):
            value = _money(_first(receipt, *sources))
            if value is not None:
                summary[key] = value
        last4 = _first(receipt, "card_last4", "last4", "payment_last4")
        if last4:
            summary["card_last4"] = last4
        return summary

    async def _find_past_order(self, store_name: str, purpose: str | None) -> dict[str, Any]:
        history = await self.order_history(days=365, limit=100, purpose=purpose)
        wanted = _words(store_name)
        matches = [o for o in history["orders"] if _matches(wanted, o.get("store_name"))]
        if not matches:
            names = [u["store_name"] for u in history["usual_places"]]
            raise DoorDashError(
                f"No past DoorDash order from anywhere called {store_name!r} in the last year. "
                + (f"Usual places: {', '.join(names)}." if names else "")
            )
        reorderable = [o for o in matches if o.get("reorderable")]
        if not reorderable:
            raise DoorDashError(
                f"Past orders from {matches[0].get('store_name')} cannot be reordered as they were; "
                "search the store and build the cart fresh instead."
            )
        return reorderable[0]

    async def order_items(
        self,
        store: str | None,
        items: list[dict[str, Any]],
        address: str | None = None,
        store_id: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """'A cheeseburger from McDonald's' in one call: find the place near
        home, find each dish on its menu, put them in the cart there, and
        price it. Four tool rounds become one, which is the difference
        between a small model finishing the order and wandering off."""
        wanted: list[dict[str, Any]] = []
        for entry in items or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            if isinstance(entry, dict) and _first(entry, "name", "item", "dish"):
                wanted.append(entry)
        if not wanted:
            raise ValueError("say what to order, e.g. items [{name: 'cheeseburger'}]")
        stores_seen: list[dict[str, Any]] = []
        near: str | None = None
        if store_id:
            chosen: dict[str, Any] = {
                "store_id": str(store_id),
                "name": self._store_names.get(str(store_id)) or store or str(store_id),
            }
        else:
            if not str(store or "").strip():
                raise ValueError("say where to order from, e.g. store 'McDonald's'")
            found = await self.search(str(store), address=address, limit=8, purpose=purpose)
            near = found.get("near")
            stores_seen = found.get("stores") or []
            words = _store_words(store)
            named = [s for s in stores_seen if _matches(words, s.get("name"))]
            if not named:
                return {
                    "store": None,
                    "near": near,
                    "stores": stores_seen[:5],
                    "note": (
                        f"No place called {store!r} delivers here on DoorDash. These are nearby: "
                        "offer them or ask for another name. Nothing was added to a cart."
                    ),
                }
            if len({_plain(s.get("name")) for s in named}) > 1:
                return {
                    "store": None,
                    "near": near,
                    "choose_store": named[:5],
                    "note": (
                        "Several different places match that name: ask which one, then call again "
                        "with its store_id. Nothing was added to a cart."
                    ),
                }
            chosen = _best_branch(named)
        store_key = str(chosen["store_id"])
        payload, menu_items = await self._load_menu(store_key, address, purpose)
        menu_id = str(_first(payload, "menu_id") or "") or None
        chosen["name"] = _first(payload, "store_name", "name") or chosen.get("name")
        result: dict[str, Any] = {
            "store": {k: chosen[k] for k in ("store_id", "name", "miles", "eta", "open", "opens_at") if k in chosen},
        }
        if near:
            result["near"] = near
        if len([s for s in stores_seen if _plain(s.get("name")) == _plain(chosen.get("name"))]) > 1:
            result["store"]["branch_note"] = "Nearest open branch picked; say if they wanted a different one."
        if payload.get("store_is_open") is False:
            result["store"]["open"] = False
        # What is already waiting in the cart there: adding a second copy
        # because a model repeated itself would cost real money.
        already: list[dict[str, Any]] = []
        try:
            open_carts = await self.carts(store_key, purpose=purpose)
        except DoorDashError:
            open_carts = []
        if open_carts:
            already = list(open_carts[0].get("items") or [])
        lines: list[dict[str, Any]] = []
        not_found: list[dict[str, Any]] = []
        skipped: list[str] = []
        for entry in wanted:
            asked = str(_first(entry, "name", "item", "dish"))
            picked, others = _pick_item(menu_items, asked)
            if not picked:
                suggestion = {"asked": asked}
                categories = []
                for item in menu_items:
                    if item.get("category") and item["category"] not in categories:
                        categories.append(item["category"])
                if categories:
                    suggestion["categories"] = categories[:12]
                not_found.append(suggestion)
                continue
            if picked.get("unavailable"):
                not_found.append({"asked": asked, "found": picked.get("name"), "unavailable": True,
                                  "reason": picked.get("reason")})
                continue
            in_cart = [i for i in already if _plain(i.get("name")) == _plain(picked.get("name"))]
            if in_cart and not entry.get("options"):
                skipped.append(str(picked.get("name")))
                continue
            line: dict[str, Any] = {
                "item_id": picked["item_id"],
                "name": picked.get("name"),
                "quantity": entry.get("quantity", 1) or 1,
            }
            if entry.get("options"):
                line["options"] = entry["options"]
            if others and _plain(others[0].get("name")) != _plain(picked.get("name")):
                line["instead_of"] = [o.get("name") for o in others[:3]]
            lines.append(line)
        if not_found:
            result["not_found"] = not_found
        if skipped:
            result["already_in_cart"] = skipped
            result["note"] = (
                "Already in the open cart there, so not added twice: " + ", ".join(skipped)
                + ". For more of it, say quantity 2 or use doordash_cart_add."
            )
        if not lines:
            if not_found and not skipped:
                result["note"] = (
                    "Nothing on this menu matched. Tell the user, name a category or two, and "
                    "ask what they would like instead. Nothing was added."
                )
                return result
            if not already:
                return result
            # Everything asked for is already waiting: price it as it stands.
            cart_uuid = str(open_carts[0].get("cart_uuid") or "")
            if cart_uuid:
                preview = await self.preview(cart_uuid, purpose=purpose)
                result.update(preview)
            return result
        added = await self.cart_add(store_key, lines, menu_id=menu_id, purpose=purpose)
        result["added"] = [
            {k: v for k, v in {
                "name": line.get("name"), "quantity": line.get("quantity"),
                "price": next((i.get("price") for i in menu_items if i.get("item_id") == line["item_id"]), None),
                "instead_of": line.get("instead_of"),
            }.items() if v is not None}
            for line in lines if line.get("name") in added.get("added", [])
        ]
        if added.get("item_errors"):
            result["item_errors"] = added["item_errors"]
            if any(e.get("required_choices") for e in added["item_errors"]):
                result["needs_choices"] = True
                result["note"] = (
                    "Some items need a choice before they go in: ask the user that one question "
                    "and call doordash_order again for just those items with options [{id, name}] "
                    "from required_choices. Anything listed under added is in the cart already."
                )
        cart_uuid = str(added.get("cart", {}).get("cart_uuid") or "")
        if cart_uuid and (result.get("added") or already):
            preview = await self.preview(cart_uuid, purpose=purpose)
            result.update(preview)
        elif not result.get("added") and not result.get("note"):
            result["note"] = "Nothing could be added; tell the user what DoorDash said."
        return result

    async def reorder(
        self,
        order_uuid: str | None = None,
        store_name: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        original: dict[str, Any] | None = None
        if not order_uuid:
            if not store_name:
                raise ValueError("give the past order's order_uuid or the store's name")
            original = await self._find_past_order(store_name, purpose)
            order_uuid = original["order_uuid"]
        elif store_name is None:
            original = None
        replaced: list[str] = []
        store_id = original.get("store_id") if original else None
        if store_id:
            try:
                for cart in await self.carts(store_id, purpose=purpose):
                    replaced += [f"{i.get('quantity', 1)}× {i.get('name')}" for i in cart.get("items", [])]
                    await self.cart_delete(cart["cart_uuid"], purpose=purpose)
            except DoorDashError:
                pass
        payload = await self._call("order", "reorder", "--order-uuid", str(order_uuid), purpose=purpose)
        if not payload.get("success", True) or not _first(payload, "cart_uuid"):
            reason = _first(payload, "fail_reason", "message", "error", default="DoorDash could not reorder it")
            raise DoorDashError(f"{_clean(reason)}. Pick a different past order or build the cart fresh.")
        cart_uuid = str(payload["cart_uuid"])
        preview = await self.preview(cart_uuid, purpose=purpose)
        result: dict[str, Any] = {"cart_uuid": cart_uuid, "from_order": order_uuid, **preview}
        if original:
            in_cart = " ".join(str(i.get("name") or "") for i in preview.get("items", [])).casefold()
            dropped = [
                item for item in original.get("items", [])
                if str(item).split("× ")[-1].casefold() not in in_cart
            ]
            if dropped:
                result["dropped_items"] = dropped
                result["note"] = (
                    "Some items from the original order are missing from the new cart (out of "
                    "stock or changed): tell the user which before asking whether to place it."
                )
        if replaced:
            result["replaced_cart_items"] = replaced
        return result

    async def payment_methods(self, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("payment-method", "list", purpose=purpose)
        return _card_summary(payload)

    async def preview(
        self,
        cart_uuid: str,
        fulfillment: str | None = None,
        scheduled_time: str | None = None,
        priority: bool = False,
        work_benefits: bool = False,
        budget_id: str | None = None,
        apply_credits: bool = True,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        cart_uuid = str(cart_uuid).strip()
        flags = ["--cart-uuid", cart_uuid]
        mode = str(fulfillment).casefold() if fulfillment else None
        if mode:
            if mode not in {"delivery", "pickup"}:
                raise ValueError("fulfillment must be delivery or pickup")
            flags += ["--fulfillment", mode]
        if scheduled_time:
            flags += ["--scheduled-time", str(scheduled_time)]
        if priority:
            flags.append("--priority")
        if work_benefits or budget_id:
            flags.append("--include-work-benefits")
        if budget_id:
            flags += ["--selected-budget-id", str(budget_id)]
        if not apply_credits:
            flags.append("--no-apply-credits")
        quote_task = self._call("order", "preview", *flags, purpose=purpose)
        cards_task = self.payment_methods(purpose=purpose)
        payload, cards = await asyncio.gather(quote_task, cards_task, return_exceptions=True)
        if isinstance(payload, BaseException):
            raise payload
        if not payload.get("success", True):
            raise DoorDashError(_clean(_first(payload, "message", "error", default="DoorDash could not price this cart")))
        summary = _preview_summary(payload)
        summary["cart_uuid"] = cart_uuid
        if isinstance(cards, BaseException):
            summary["card_note"] = (
                "The card on file could not be read; say DoorDash will charge whatever default "
                "payment they have on file, and offer the checkout link to check it."
            )
        else:
            summary.update({k: v for k, v in cards.items() if k in {"default_card", "default_card_spoken", "card_note"}})
        team_id = summary.pop("_team_id", None)
        self._previews[cart_uuid] = {
            "at": time.monotonic(),
            "fulfillment": summary.get("fulfillment") or mode,
            "scheduled_time": scheduled_time,
            "priority": bool(priority),
            "apply_credits": bool(apply_credits),
            "budget_id": budget_id,
            "team_id": team_id,
            "total": summary.get("total"),
            "store_name": summary.get("store_name"),
            "card": summary.get("default_card_spoken"),
            "suggested_tip_cents": (summary.get("tip") or {}).get("suggested_cents"),
        }
        summary["next"] = (
            "Read back the items, the total, the delivery or pickup time, and the card, and "
            "ask whether to place it — on a delivery, with the Dasher tip named (the suggested "
            "one, another amount, or none). Only a clear yes leads to doordash_order_place."
        )
        return summary

    def known_cart(self, cart_uuid: str | None) -> bool:
        return bool(cart_uuid) and str(cart_uuid) in self._previews

    def latest_cart(self) -> str | None:
        """The cart previewed most recently and still within its quote window."""
        now = time.monotonic()
        live = {k: v for k, v in self._previews.items() if now - v["at"] <= _PREVIEW_VALID_SECONDS}
        if not live:
            return None
        return max(live, key=lambda k: live[k]["at"])

    async def place(
        self,
        cart_uuid: str,
        confirmed: bool = False,
        tip_cents: int | str | None = None,
        expense_code: str | None = None,
        expense_note: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        cart_uuid = str(cart_uuid).strip()
        if not confirmed:
            raise DoorDashError(
                "Not placed: the user has to say yes first. Read back the total, the card, "
                "and the tip, ask, and call again with confirmed true only on a clear yes."
            )
        quoted = self._previews.get(cart_uuid)
        if quoted is None:
            raise DoorDashError(
                "Not placed: this cart has not been previewed. Call doordash_order_preview, "
                "read the total and card back to the user, and ask before placing."
            )
        if time.monotonic() - quoted["at"] > _PREVIEW_VALID_SECONDS:
            self._previews.pop(cart_uuid, None)
            raise DoorDashError(
                "Not placed: the price quote is more than half an hour old. Preview again, "
                "read the fresh total back, and ask again."
            )
        delivery = (quoted.get("fulfillment") or "delivery") != "pickup"
        tip_source = "none"
        if delivery:
            tip, tip_source = _resolve_tip(tip_cents, quoted.get("suggested_tip_cents"))
            if tip is None:
                raise DoorDashError(
                    "Not placed: the Dasher tip has not been agreed. Ask — the suggested tip, "
                    "a different amount, or none — and pass tip 'suggested', 'none', or the amount."
                )
        else:
            tip = 0
        flags = ["--cart-uuid", cart_uuid, "--tip-cents", str(tip), "--yes"]
        if quoted.get("fulfillment") in {"delivery", "pickup"}:
            flags += ["--fulfillment", quoted["fulfillment"]]
        if quoted.get("scheduled_time"):
            flags += ["--scheduled-time", str(quoted["scheduled_time"])]
        if quoted.get("priority"):
            flags.append("--priority")
        if not quoted.get("apply_credits", True):
            flags.append("--no-apply-credits")
        if quoted.get("budget_id") and quoted.get("team_id"):
            flags += ["--team-id", str(quoted["team_id"]), "--budget-id", str(quoted["budget_id"])]
            if expense_code:
                flags += ["--expense-code", str(expense_code)]
            if expense_note:
                flags += ["--expense-notes", str(expense_note)]
        try:
            payload = await self._call("order", "submit", *flags, purpose=purpose, timeout=max(self.config.timeout_seconds, 60))
        except DoorDashError as exc:
            if "restricted" in str(exc).casefold() or "age" in str(exc).casefold():
                return await self._restricted(cart_uuid, str(exc), purpose)
            raise
        reason = str(_first(payload, "error_reason", default="")).upper()
        if "RESTRICTED" in reason:
            return await self._restricted(cart_uuid, _clean(_first(payload, "message", default=reason)), purpose)
        if not payload.get("success", True) and not _first(payload, "order_uuid"):
            raise DoorDashError(_clean(_first(payload, "message", "error", default="DoorDash did not accept the order")))
        order_uuid = str(_first(payload, "order_uuid", "uuid", "id") or "")
        self._previews.pop(cart_uuid, None)
        result: dict[str, Any] = {
            "accepted": True,
            "order_uuid": order_uuid or None,
            "store_name": quoted.get("store_name"),
            "total": quoted.get("total"),
            "tip": _cents_to_text(tip),
            "tip_source": tip_source,
            "card": quoted.get("card"),
            "status": _normalize_status(_first(payload, "status", "order_status")) or "pending",
        }
        if order_uuid:
            try:
                await asyncio.sleep(2)
                status = await self.order_status(order_uuid, purpose=purpose)
                result["status"] = status.get("status") or result["status"]
                for key in ("eta", "detail"):
                    if status.get(key):
                        result[key] = status[key]
            except DoorDashError:
                pass
        self.last_order = {"order_uuid": order_uuid, "store_name": quoted.get("store_name"), "at": time.time()}
        result["placed"] = result["status"] in _PLACED_STATUSES
        result["note"] = (
            "Say the order is placed."
            if result["placed"]
            else "DoorDash accepted the order and is still processing it: say it is sent and "
            "that you will announce when it is confirmed; do not call it placed yet."
        )
        return result

    async def _restricted(self, cart_uuid: str, message: str, purpose: str | None) -> dict[str, Any]:
        link = None
        try:
            link = (await self.checkout_url(cart_uuid, purpose=purpose)).get("checkout_url")
        except DoorDashError:
            pass
        return {
            "accepted": False,
            "restricted_items": True,
            "error": message or "This cart has age-restricted items, which cannot be placed by an assistant.",
            "checkout_url": link,
            "note": "Tell the user it has to be finished in the DoorDash app or at the checkout link.",
        }

    async def order_status(self, order_uuid: str | None = None, purpose: str | None = None) -> dict[str, Any]:
        if not order_uuid:
            if not self.last_order or not self.last_order.get("order_uuid"):
                raise DoorDashError("No order has been placed through this house today; give the order_uuid from doordash_order_history.")
            order_uuid = str(self.last_order["order_uuid"])
        payload = await self._call("order", "status", "--order-uuid", str(order_uuid), purpose=purpose)
        order = next(
            (payload[key] for key in ("result", "order") if isinstance(payload.get(key), dict)), payload
        )
        status = _normalize_status(_first(order, "status", "order_status", "state"))
        result: dict[str, Any] = {
            "order_uuid": str(order_uuid),
            "status": status or ("not_found" if not order else "unknown"),
            "store_name": _first(order, "merchant_name", "store_name") or (self.last_order or {}).get("store_name"),
        }
        if order.get("is_pickup") is not None:
            result["fulfillment"] = "pickup" if order.get("is_pickup") else "delivery"
        eta = _first(
            order, "quoted_delivery_time", "estimated_pickup_time", "eta", "estimated_delivery_time",
            "delivery_eta", "eta_display", "estimated_arrival",
        )
        if eta and status not in _TERMINAL_STATUSES:
            result["eta"] = _spoken_time(eta)
        for key in ("late_reason", "cancellation_reason", "eta_trend"):
            if order.get(key):
                result[key] = _clean(order[key])
        detail = _first(order, "status_message", "message", "description")
        if detail:
            result["detail"] = _clean(detail)
        if order.get("action_required"):
            result["status"] = "action_required"
        result["terminal"] = result["status"] in _TERMINAL_STATUSES or result["status"] == "not_found"
        result["placed"] = result["status"] in _PLACED_STATUSES
        return result

    async def checkout_url(self, cart_uuid: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("order", "checkout-url", "--cart-uuid", str(cart_uuid), purpose=purpose)
        url = _first(payload, "checkout_url", "url", "link")
        if not url:
            raise DoorDashError("DoorDash did not return a checkout link for that cart.")
        return {"cart_uuid": str(cart_uuid), "checkout_url": str(url)}

    # ----------------------------------------------------------- the account

    async def addresses(self, purpose: str | None = None) -> dict[str, Any]:
        self.forget_addresses()
        entries = [self._address_entry(a) for a in await self._addresses_cached(purpose)]
        return {
            "default": next((a for a in entries if a["default"]), None),
            "labeled": [a for a in entries if a.get("label")],
            "count": len(entries),
        }

    async def set_default_address(self, label: str, purpose: str | None = None) -> dict[str, Any]:
        address = await self._resolve_address(label, purpose)
        if not address:
            raise DoorDashError(f"No saved address matches {label!r}.")
        address_id = str(_first(address, "address_id", "id") or "")
        await self._call("address", "set", "--address-id", address_id, "--yes", purpose=purpose)
        self.forget_addresses()
        return {"success": True, "default": self._address_entry({**address, "is_default": True})}

    # ------------------------------------------------------------- promotions

    async def promos(self, store_id: str, purpose: str | None = None) -> dict[str, Any]:
        payload = await self._call("promo", "list", "--store-id", str(store_id), purpose=purpose)
        promos = []
        for promo in payload.get("promotions") or []:
            if isinstance(promo, dict):
                promos.append(
                    {k: v for k, v in {
                        "code": _first(promo, "code"),
                        "title": _first(promo, "title"),
                        "description": _short(_first(promo, "description"), 140),
                    }.items() if v}
                )
        return {"store_id": str(store_id), "promotions": promos[:8]}

    async def promo_apply(self, cart_uuid: str, code: str, remove: bool = False, purpose: str | None = None) -> dict[str, Any]:
        code = str(code).strip()
        campaign: list[str] = []
        try:
            cart = await self.cart_show(cart_uuid, purpose=purpose)
            if cart.get("store_id"):
                listing = await self._call("promo", "list", "--store-id", str(cart["store_id"]), purpose=purpose)
                for promo in listing.get("promotions") or []:
                    if isinstance(promo, dict) and str(promo.get("code") or "").casefold() == code.casefold():
                        for flag, key in (("--campaign-id", "campaign_id"), ("--ad-group-id", "ad_group_id"), ("--ad-id", "ad_id")):
                            if promo.get(key):
                                campaign += [flag, str(promo[key])]
                        break
        except DoorDashError:
            pass
        action = "remove" if remove else "apply"
        payload = await self._call("promo", action, "--cart-uuid", str(cart_uuid), "--promo-code", code, *campaign, purpose=purpose)
        self._previews.pop(str(cart_uuid), None)
        result: dict[str, Any] = {"success": bool(payload.get("success", True)), "code": code, "action": action}
        discount = payload.get("discount_cents")
        if discount is not None:
            result["discount"] = _cents_to_text(discount)
        subtotal = payload.get("subtotal_cents")
        if subtotal is not None:
            result["subtotal"] = _cents_to_text(subtotal)
        message = _first(payload, "message")
        if message and not result["success"]:
            result["error"] = _clean(message)
            result["note"] = "If the subtotal is under the promo's minimum, adding an item may qualify it."
        return result
