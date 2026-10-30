"""Recipes from the web, as structured data rather than blog prose.

Nearly every recipe site embeds a schema.org ``Recipe`` block (JSON-LD) so
that search engines can show ingredients and cook times. Reading that block
gives the model a clean list of ingredients, steps, times, servings, rating
and nutrition instead of eight thousand characters of someone's story about
their grandmother -- which is both cheaper for a local model and far easier
to read aloud.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .websearch import WebSearchError, fetch_html, web_search

_LD_JSON = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
    re.IGNORECASE,
)
MAX_PAGES = 4
# A free, keyless recipe database used when the web search engines are
# unavailable. Smaller than the web, but never rate limited.
_MEALDB_URL = "https://www.themealdb.com/api/json/v1/1/search.php"
_STOPWORDS = {
    "recipe", "recipes", "dinner", "lunch", "breakfast", "meal", "meals", "healthy",
    "quick", "easy", "good", "great", "best", "high", "low", "protein", "family",
    "for", "the", "and", "with", "tonight", "something", "some", "a", "an", "of",
    "that", "will", "keep", "me", "going", "through", "thru", "day", "night",
    "fluffy", "simple", "fast", "cheap", "idea", "ideas", "food", "cook", "make",
}


class RecipeError(RuntimeError):
    pass


def parse_duration_minutes(value: Any) -> int | None:
    """ISO 8601 durations ('PT1H30M') as whole minutes."""
    if not isinstance(value, str) or not value.strip().upper().startswith("P"):
        return None
    match = _DURATION.fullmatch(value.strip())
    if not match or not any(match.groupdict().values()):
        return None
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    minutes = parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"]
    if parts["seconds"] >= 30:
        minutes += 1
    return minutes or None


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text") or value.get("name") or ""
    if isinstance(value, list):
        value = " ".join(_text(item) for item in value)
    text = html.unescape(_TAGS.sub(" ", str(value or "")))
    return " ".join(text.split())


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _instructions(value: Any, out: list[str] | None = None) -> list[str]:
    out = [] if out is None else out
    if isinstance(value, str):
        # Some sites stuff every step into one string separated by newlines,
        # <br> tags, or "1. ... 2. ..." numbering.
        raw = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.IGNORECASE)
        for line in re.split(r"\n+|(?<=\.)\s+(?=\d+[.)]\s)", raw):
            line = _text(line)
            if line:
                out.append(line)
    elif isinstance(value, list):
        for item in value:
            _instructions(item, out)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "HowToSection" or "itemListElement" in value:
            _instructions(value.get("itemListElement"), out)
        else:
            text = _text(value)
            if text:
                out.append(text)
    return out


def _is_recipe(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(item).lower() == "recipe" for item in kinds if item)


def _find_recipe(node: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if _is_recipe(node):
        return node
    if isinstance(node, dict):
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "hasPart", "itemListElement"):
            found = _find_recipe(node.get(key), depth + 1)
            if found:
                return found
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _find_recipe(value, depth + 1)
                if found:
                    return found
    elif isinstance(node, list):
        for item in node:
            found = _find_recipe(item, depth + 1)
            if found:
                return found
    return None


def _number(value: Any) -> float | None:
    try:
        return float(str(_first(value)).strip().split()[0])
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def extract_recipe(markup: str, url: str | None = None) -> dict[str, Any] | None:
    """The schema.org Recipe on a page, reduced to what a cook needs."""
    for block in _LD_JSON.findall(markup):
        block = block.strip()
        # Sites wrap the JSON in CDATA or HTML comments now and then.
        block = re.sub(r"^\s*(<!--|//<!\[CDATA\[)|(-->|//\]\]>)\s*$", "", block).strip()
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            try:
                data = json.loads(block.replace("\n", " ").replace("\t", " "))
            except json.JSONDecodeError:
                continue
        node = _find_recipe(data)
        if node:
            return _shape(node, url)
    return None


def _shape(node: dict[str, Any], url: str | None) -> dict[str, Any]:
    nutrition = node.get("nutrition") if isinstance(node.get("nutrition"), dict) else {}
    rating = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
    author = node.get("author")
    if isinstance(author, list):
        author = author[0] if author else None
    ingredients = [_text(item) for item in (node.get("recipeIngredient") or node.get("ingredients") or [])]
    steps = _instructions(node.get("recipeInstructions"))
    total = parse_duration_minutes(node.get("totalTime"))
    prep = parse_duration_minutes(node.get("prepTime"))
    cook = parse_duration_minutes(node.get("cookTime"))
    if total is None and (prep or cook):
        total = (prep or 0) + (cook or 0)
    recipe: dict[str, Any] = {
        "title": _text(node.get("name")) or "Untitled recipe",
        "url": url or _text(node.get("url")) or None,
        "description": _text(node.get("description"))[:300] or None,
        "author": _text(author.get("name") if isinstance(author, dict) else author) or None,
        "servings": _text(_first(node.get("recipeYield"))) or None,
        "prep_minutes": prep,
        "cook_minutes": cook,
        "total_minutes": total,
        "rating": _number(rating.get("ratingValue")),
        "rating_count": int(_number(rating.get("ratingCount") or rating.get("reviewCount")) or 0) or None,
        "category": _text(_first(node.get("recipeCategory"))) or None,
        "cuisine": _text(_first(node.get("recipeCuisine"))) or None,
        "calories": _text(nutrition.get("calories")) or None,
        "protein": _text(nutrition.get("proteinContent")) or None,
        "ingredients": [item for item in ingredients if item],
        "steps": steps,
    }
    return {key: value for key, value in recipe.items() if value not in (None, "", [])}


def summarize(recipe: dict[str, Any]) -> dict[str, Any]:
    """What is worth reading out when comparing several recipes: everything
    but the step-by-step method, which recipe_get returns for the chosen one."""
    summary = {key: value for key, value in recipe.items() if key != "steps"}
    summary["step_count"] = len(recipe.get("steps", []))
    return summary


_HREF = re.compile(r'href="(https?://[^"#?]+)', re.IGNORECASE)
_HEADING_LINK = re.compile(r"<h[2-4][^>]*>.*?<a[^>]+href=\"(https?://[^\"#?]+)", re.IGNORECASE | re.DOTALL)
_NOT_A_RECIPE = ("/category/", "/tag/", "/page/", "/author/", "/wp-content/", "/feed", "/comments", "/search")
_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".css", ".js", ".xml", ".json")
MAX_HOPS_PER_PAGE = 3
MAX_HOPS = 4
# Recipe pages are fetched several at once; one slow site must not hold the
# reply hostage, so each gets a short leash.
FETCH_TIMEOUT = 8


def _candidate(href: str, host: str, base_path: str) -> bool:
    parsed = urlparse(href)
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if parsed.netloc != host or not path or path == base_path:
        return False
    if any(marker in lowered for marker in _NOT_A_RECIPE) or lowered.endswith(_FILE_SUFFIXES):
        return False
    return not lowered.endswith(("/recipes", "/recipe", "/recipe-index"))


def _item_list_urls(markup: str) -> list[str]:
    """Round-ups often carry a schema.org ItemList naming each recipe's URL."""
    urls: list[str] = []
    for block in _LD_JSON.findall(markup):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if str(node.get("@type", "")).lower() == "itemlist":
                    for item in node.get("itemListElement") or []:
                        url = item.get("url") if isinstance(item, dict) else None
                        if not url and isinstance(item, dict) and isinstance(item.get("item"), dict):
                            url = item["item"].get("url") or item["item"].get("@id")
                        if isinstance(url, str) and url.startswith("http"):
                            urls.append(url.split("#")[0])
                stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
    return urls


def recipe_links(markup: str, page_url: str) -> list[str]:
    """Links from a round-up article ("15 high-protein dinners") to the
    recipes it lists. Its own ItemList first, then links wrapped in item
    headings, then any same-site link whose path says recipe -- never
    categories, tags, images, or the page itself."""
    host = urlparse(page_url).netloc
    base_path = urlparse(page_url).path.rstrip("/")
    ranked = (
        _item_list_urls(markup)
        + _HEADING_LINK.findall(markup)
        + [href for href in _HREF.findall(markup) if "recipe" in urlparse(href).path.lower()]
    )
    links: list[str] = []
    for href in ranked:
        if href in links or not _candidate(href, host, base_path):
            continue
        links.append(href)
        if len(links) >= MAX_HOPS_PER_PAGE:
            break
    return links


async def _fetch_recipe(url: str, follow: bool = True) -> dict[str, Any] | list[str] | None:
    """A page's recipe, or -- for a round-up with none -- the recipe links
    it points at, so the caller can take a second hop."""
    try:
        final_url, markup = await fetch_html(url, timeout=FETCH_TIMEOUT)
    except WebSearchError:
        return None
    recipe = extract_recipe(markup, final_url)
    if recipe is not None or not follow:
        return recipe
    return recipe_links(markup, final_url) or None


async def get_recipe(url: str) -> dict[str, Any]:
    final_url, markup = await fetch_html(url)
    recipe = extract_recipe(markup, final_url)
    if recipe is None:
        raise RecipeError(
            "That page has no structured recipe data. Use web_fetch to read it as text instead."
        )
    return recipe


def _mealdb_shape(meal: dict[str, Any]) -> dict[str, Any]:
    ingredients = []
    for index in range(1, 21):
        name = _text(meal.get(f"strIngredient{index}"))
        measure = _text(meal.get(f"strMeasure{index}"))
        if name:
            ingredients.append(f"{measure} {name}".strip())
    steps = [
        line.strip()
        for line in re.split(r"\r?\n+", str(meal.get("strInstructions") or ""))
        if line.strip() and not re.fullmatch(r"(step\s*)?\d+[.)]?", line.strip(), re.IGNORECASE)
    ]
    recipe = {
        "title": _text(meal.get("strMeal")) or "Untitled recipe",
        "url": _text(meal.get("strSource")) or f"https://www.themealdb.com/meal/{meal.get('idMeal')}",
        "category": _text(meal.get("strCategory")),
        "cuisine": _text(meal.get("strArea")),
        "ingredients": ingredients,
        "steps": steps,
        "source": "TheMealDB",
    }
    return {key: value for key, value in recipe.items() if value not in (None, "", [])}


async def mealdb_search(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Recipes from TheMealDB, matched by dish name. Its search is a name
    lookup, so the query is tried whole and then word by word, skipping
    words that describe a meal rather than name one."""
    words = [word for word in re.findall(r"[a-z]+", query.lower()) if word not in _STOPWORDS and len(word) > 2]
    attempts = [query.strip()] + [word for word in words if word != query.strip().lower()]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in attempts:
            try:
                response = await client.get(_MEALDB_URL, params={"s": attempt})
                response.raise_for_status()
                meals = response.json().get("meals") or []
            except (httpx.HTTPError, ValueError):
                continue
            for meal in meals:
                key = str(meal.get("idMeal"))
                if key in seen:
                    continue
                seen.add(key)
                found.append(_mealdb_shape(meal))
                if len(found) >= limit:
                    return found
    return found


# For ranking, only filler is ignored: "fluffy", "light" and "healthy" are
# exactly the words a title should be matched on.
_RANK_STOPWORDS = {
    "recipe", "recipes", "for", "the", "and", "with", "some", "something",
    "good", "best", "great", "nice", "easy", "quick", "simple", "find", "make",
    "want", "like", "please", "tonight", "today", "not", "too", "very",
}
_DISH_SPLIT = re.compile(r"\b(?:with|that|which|topped|filled|without|but)\b|,", re.IGNORECASE)


def _query_words(query: str) -> tuple[set[str], set[str]]:
    """The dish itself and the wishes about it. "Fluffy chocolate cake with
    a light frosting" is a cake first: the words before "with" name the
    dish, the rest describe it, and a frosting recipe is not a cake."""
    head, *tail = _DISH_SPLIT.split(query, maxsplit=1)
    words = lambda text: {  # noqa: E731
        word for word in re.findall(r"[a-z]+", text.lower())
        if word not in _RANK_STOPWORDS and len(word) > 2
    }
    return words(head), words(" ".join(tail))


def _dish_noun(query: str) -> str | None:
    """The thing itself: the last real word before any "with" -- "cake" in
    "fluffy chocolate cake with light frosting"."""
    head = _DISH_SPLIT.split(query, maxsplit=1)[0]
    words = [word for word in re.findall(r"[a-z]+", head.lower()) if len(word) > 2]
    return words[-1] if words else None


def _score(recipe: dict[str, Any], dish: set[str], wishes: set[str], noun: str | None) -> float:
    """Which of several recipes to read out when only one is wanted: the
    one that is the dish asked for, matches the wishes, that people rated
    well and in numbers, and that actually has ingredients and steps."""
    title_words = set(re.findall(r"[a-z]+", str(recipe.get("title", "")).lower()))
    stems = {word.rstrip("s") for word in title_words}
    score = 2.5 * len(dish & title_words) + 1.0 * len(wishes & title_words)
    if noun:
        # A frosting recipe is not a cake, however well it matches the rest.
        score += 4 if noun.rstrip("s") in stems else -4
    rating = recipe.get("rating")
    count = recipe.get("rating_count") or 0
    if isinstance(rating, (int, float)):
        score += float(rating)
        score += min(1.5, math.log10(count + 1) / 2)
    else:
        score += 3.5
    steps = len(recipe.get("steps") or [])
    if not steps:
        score -= 5
    else:
        # A real method has several steps; two lines is a note, not a recipe.
        score += min(steps, 8) * 0.15
    if not recipe.get("ingredients"):
        score -= 5
    if recipe.get("total_minutes"):
        score += 0.3
    return score


def pick_best(recipes: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    if not recipes:
        return None
    dish, wishes = _query_words(query)
    noun = _dish_noun(query)
    return max(recipes, key=lambda recipe: _score(recipe, dish, wishes, noun))


def dish_query(query: str) -> str | None:
    """The bare dish from a wishful query, for a second search that makes
    sure the dish itself is among the candidates; None if there is no
    separate wish part."""
    head, *tail = _DISH_SPLIT.split(query, maxsplit=1)
    head = " ".join(head.split())
    return head if tail and head and head.lower() != query.strip().lower() else None


async def search_recipes(query: str, limit: int = 3, best: bool = False) -> dict[str, Any]:
    """Search the web for recipes and return the structured ones found.

    Pages without a Recipe block (round-ups, videos, forums) are dropped
    rather than returned as prose, so every result has real ingredients.
    With ``best`` the single strongest match comes back whole, steps and
    all, so a named dish can be read out in one go; otherwise a few
    summaries come back to choose from.
    """
    query = " ".join(query.split())
    if not query:
        raise ValueError("Say what kind of recipe to look for")
    limit = max(1, min(int(limit), MAX_PAGES))
    terms = query if "recipe" in query.lower() else f"{query} recipe"
    searched: list[str] = []
    recipes: list[dict[str, Any]] = []
    search_error: WebSearchError | None = None
    try:
        hits = await web_search(terms, 5)
        dish = dish_query(query) if best else None
        if dish:
            # "chocolate cake with light frosting" finds frosting pages; a
            # second search for the dish alone keeps a cake in the running.
            extra = await web_search(f"{dish} recipe", 3)
            known = {hit["url"] for hit in hits}
            hits = hits[:MAX_PAGES] + [hit for hit in extra if hit["url"] not in known]
    except WebSearchError as exc:
        search_error = exc
        hits = []
    if hits:
        candidates = [hit["url"] for hit in hits[: MAX_PAGES + (3 if best else 0)]]
        pages = await asyncio.gather(*(_fetch_recipe(url) for url in candidates))
        recipes = [page for page in pages if isinstance(page, dict)]
        searched = list(candidates)
        # Round-ups link to the actual recipes; follow a few of those links.
        hops: list[str] = []
        for page in pages:
            if isinstance(page, list):
                hops += [link for link in page if link not in hops and link not in searched]
        hops = hops[:MAX_HOPS]
        if hops and len(recipes) < limit:
            second = await asyncio.gather(*(_fetch_recipe(url, follow=False) for url in hops))
            recipes += [page for page in second if isinstance(page, dict)]
            searched += hops
    source = "web"
    if not recipes:
        # The web let us down (throttled, or nothing structured): fall back
        # to a recipe database rather than to the model's imagination.
        fallback = await mealdb_search(query, limit)
        if fallback:
            recipes = fallback
            source = "TheMealDB"
        elif search_error is not None:
            raise search_error
    result: dict[str, Any] = {"query": query, "searched": searched, "source": source}
    if best:
        chosen = pick_best(recipes, query)
        result["recipe"] = chosen
        result["considered"] = [recipe["title"] for recipe in recipes if recipe is not chosen]
    else:
        result["recipes"] = [summarize(recipe) for recipe in recipes[:limit]]
    return result
