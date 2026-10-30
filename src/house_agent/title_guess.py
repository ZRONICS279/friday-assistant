"""Work out which title someone meant when the catalog has never heard of
what they said.

"Put on Master of the Unicverse" finds nothing in a catalog that only knows
"Masters of the Universe"; and "that movie where the kid hides an alien in
his closet" is not a title at all. A web search answers both at once --
IMDb, Wikipedia and Rotten Tomatoes each head their page with the real
title, so whatever title recurs across the results is the one meant. This
module turns search results into that guess; the callers re-run the catalog
with it and tell the model it was a guess, so it can say "did you mean".
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable
from typing import Any

# Sites whose result titles are "<Title> (<year>) - <Site>" or similar.
_SITE_SUFFIX = re.compile(
    r"\s*[-|–—:]\s*("
    r"IMDb|Wikipedia|Rotten Tomatoes|Moviefone|Metacritic|Letterboxd|JustWatch|"
    r"TV Guide|Fandom|Prime Video|Netflix|Hulu|Max|Disney\+|Apple TV\+?|Paramount\+|"
    r"Peacock|YouTube|Amazon\.com|Movie|TV Series|Full cast.*|Plot.*|Reviews?.*|"
    r"Trailer.*|Official.*|Watch.*|Stream.*|Cast.*|Where to.*|Release.*|Ending.*"
    r")\s*$",
    re.IGNORECASE,
)
_SITE_PREFIX = re.compile(
    r"^\s*(Prime Video|Netflix|Hulu|Amazon\.com|Watch|Stream|Buy|Rent)\s*[:|-]?\s+",
    re.IGNORECASE,
)
# "(2026)", "(2026 film)", "(TV series)", "(film)", "(1987 movie)"
_PARENS = re.compile(r"\s*\((?:\d{4}(?:\s+\w+)*|TV series|TV Mini Series|film|movie)\)\s*", re.I)
_YEAR = re.compile(r"\((\d{4})")

Searcher = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def title_from_result(heading: str) -> str | None:
    """The title a search-result heading is about, or None if it does not
    look like a title page."""
    text = heading.strip()
    for _ in range(3):  # "Title (2026) - Full cast & crew - IMDb"
        stripped = _SITE_SUFFIX.sub("", text)
        stripped = _SITE_PREFIX.sub("", stripped)
        if stripped == text:
            break
        text = stripped.strip()
    text = _PARENS.sub(" ", text).strip(" -–—:|")
    text = " ".join(text.split())
    if not 2 <= len(text) <= 80 or text.casefold() in {"home", "movies", "tv shows"}:
        return None
    return text


def guess_title(query: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The title the results agree on, with its year when they show one.
    None when nothing recurs and nothing resembles the query: the results
    are about something else, and a guess would be an invention."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    years: dict[str, str] = {}
    for item in results:
        heading = str(item.get("title") or "")
        title = title_from_result(heading)
        if not title:
            continue
        key = _fold(title)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, title)
        year = _YEAR.search(heading)
        if year and key not in years:
            years[key] = year.group(1)
    if not counts:
        return None
    folded_query = _fold(query)

    def score(key: str) -> tuple[int, float]:
        return counts[key], difflib.SequenceMatcher(None, folded_query, key).ratio()

    best = max(counts, key=score)
    count, similarity = score(best)
    if count < 2 and similarity < 0.6:
        return None
    if best == folded_query:
        return None  # the catalog simply lacks it; no better name exists
    guess: dict[str, Any] = {"title": display[best], "similarity": round(similarity, 2)}
    if best in years:
        guess["year"] = int(years[best])
    return guess


async def guess_title_online(query: str, search: Searcher) -> dict[str, Any] | None:
    """Ask the web what "<query>" is, as a movie or show."""
    try:
        results = await search(f"{query} movie or show", 6)
    except Exception:  # noqa: BLE001 - a guess is best-effort
        return None
    return guess_title(query, results)
