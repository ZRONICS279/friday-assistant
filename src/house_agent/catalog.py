from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import httpx

from .config import CatalogConfig
from .keys import MissingKey


PROVIDER_ALIASES = {
    "amazon prime": "prime_video",
    "amazon prime video": "prime_video",
    "apple tv": "apple_tv_plus",
    "apple tv+": "apple_tv_plus",
    "apple tv plus": "apple_tv_plus",
    "appletv+": "apple_tv_plus",
    "prime video": "prime_video",
    "disney plus": "disney_plus",
    "disney+": "disney_plus",
    "hbo": "max",
    "hbo max": "max",
    "max": "max",
    "netflix": "netflix",
    "paramount plus": "paramount_plus",
    "paramount+": "paramount_plus",
    "paramount+ with showtime": "paramount_plus",
    "hulu": "hulu",
}
PROVIDER_DISPLAY_NAMES = {
    "apple_tv_plus": "Apple TV+",
    "disney_plus": "Disney+",
    "max": "Max",
    "netflix": "Netflix",
    "prime_video": "Prime Video",
    "hulu": "Hulu",
    "paramount_plus": "Paramount+",
}
PROVIDER_APP_NAMES = {
    "apple_tv_plus": "Apple TV",
    "disney_plus": "Disney Plus",
    "max": "Max",
    "netflix": "Netflix",
    "prime_video": "Prime Video",
    "hulu": "Hulu",
    "paramount_plus": "Paramount Plus",
}
DEFAULT_SUBSCRIPTIONS = list(PROVIDER_DISPLAY_NAMES.items())


# Everyday phrasings the model may pass as "genres", mapped onto TMDB's
# vocabulary (movie names; the TV variants resolve via the per-type aliases).
GENRE_PHRASES: dict[str, Any] = {
    "murder mystery": "mystery",
    "whodunit": "mystery",
    "whodunnit": "mystery",
    "detective": "mystery",
    "light-hearted": "comedy",
    "lighthearted": "comedy",
    "feel good": "comedy",
    "feel-good": "comedy",
    "funny": "comedy",
    "sitcom": "comedy",
    "rom com": ["romance", "comedy"],
    "rom-com": ["romance", "comedy"],
    "romcom": ["romance", "comedy"],
    "romantic comedy": ["romance", "comedy"],
    "dark comedy": "comedy",
    "scary": "horror",
    "spooky": "horror",
    "superhero": "action",
    "cartoon": "animation",
    "anime": "animation",
    "animated": "animation",
    "space": "science fiction",
    "true crime": "crime",
    "heist": "crime",
    "historical": "history",
    "period drama": "drama",
    "docuseries": "documentary",
    "doc": "documentary",
}


def _playable_url(raw: Any) -> str | None:
    """A link only if it is one. On plans without episode deep links, Watchmode
    fills the url fields with the sentence 'Episode links available for paid
    plans only.' — which would otherwise be handed to a TV as an address."""
    text = str(raw or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _episode_date(raw: Any) -> str | None:
    """Watchmode dates read 'Monday Sep 7 2026'; ISO is what everything else
    here speaks. An unparseable one is dropped rather than guessed at."""
    text = str(raw or "").strip()
    if not text:
        return None
    for shape in ("%A %b %d %Y", "%A %B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape).date().isoformat()
        except ValueError:
            continue
    return None


def _fold_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.casefold())


class CatalogError(RuntimeError):
    pass


class CatalogNotConfigured(CatalogError, MissingKey):
    """No TMDB or Watchmode key for what was asked."""

    def __init__(self, message: str, *, feature: str, key: str) -> None:
        MissingKey.__init__(self, message, feature=feature, key=key, group="movies")


def canonical_provider(name: str) -> str:
    normalized = " ".join(name.strip().lower().replace("_", " ").split())
    return PROVIDER_ALIASES.get(normalized, normalized.replace(" ", "_"))


@dataclass(frozen=True, slots=True)
class Offering:
    provider: str
    display_name: str
    region: str
    kind: str
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogTitle:
    catalog: str
    catalog_id: str
    title: str
    media_type: str
    year: int | None
    imdb_id: str | None
    tmdb_id: int | None
    offerings: tuple[Offering, ...]
    release_date: str | None = None
    overview: str | None = None
    user_rating: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["offerings"] = [asdict(offering) for offering in self.offerings]
        return result


class WatchmodeCatalog:
    def __init__(
        self,
        config: CatalogConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client

    async def search(self, query: str, limit: int = 5) -> list[CatalogTitle]:
        if not self.config.watchmode_api_key:
            raise CatalogNotConfigured(
                "WATCHMODE_API_KEY is not configured", feature="streaming links", key="WATCHMODE_API_KEY"
            )
        body = await self._cached_get(
            "/search/",
            params={"search_field": "name", "search_value": query, "types": "tv,movie"},
        )
        results = body.get("title_results", [])[:limit]
        source_lists = await asyncio.gather(
            *(self._sources(str(item["id"])) for item in results)
        )
        titles: list[CatalogTitle] = []
        for item, sources in zip(results, source_lists, strict=True):
            titles.append(
                CatalogTitle(
                    catalog="watchmode",
                    catalog_id=str(item["id"]),
                    title=str(item["name"]),
                    media_type="movie" if item.get("type") == "movie" else "series",
                    year=int(item["year"]) if item.get("year") else None,
                    imdb_id=item.get("imdb_id"),
                    tmdb_id=int(item["tmdb_id"]) if item.get("tmdb_id") else None,
                    offerings=tuple(sources),
                )
            )
        return titles

    async def resolve(
        self, query: str, media_type: str | None = None
    ) -> CatalogTitle | None:
        if not self.config.watchmode_api_key:
            raise CatalogNotConfigured(
                "WATCHMODE_API_KEY is not configured", feature="streaming links", key="WATCHMODE_API_KEY"
            )
        body = await self._cached_get(
            "/search/",
            params={"search_field": "name", "search_value": query, "types": "tv,movie"},
        )
        results = body.get("title_results", [])
        # "put on What If, the series" must not resolve to the 2013 movie:
        # honor the requested kind before anything else.
        if media_type in {"series", "episode", "season"}:
            typed = [r for r in results if str(r.get("type", "")).startswith("tv")]
        elif media_type == "movie":
            typed = [r for r in results if not str(r.get("type", "")).startswith("tv")]
        else:
            typed = results
        results = typed or results
        if not results:
            return None
        wanted = _fold_title(query)
        exact = [
            item for item in results if _fold_title(str(item.get("name", ""))) == wanted
        ]
        item = (exact or results)[0]
        sources = await self._sources(str(item["id"]))
        return CatalogTitle(
            catalog="watchmode",
            catalog_id=str(item["id"]),
            title=str(item["name"]),
            media_type="movie" if item.get("type") == "movie" else "series",
            year=int(item["year"]) if item.get("year") else None,
            imdb_id=item.get("imdb_id"),
            tmdb_id=int(item["tmdb_id"]) if item.get("tmdb_id") else None,
            offerings=tuple(sources),
        )

    async def discover(
        self,
        subscriptions: set[str],
        *,
        media_type: str = "any",
        genres: list[str] | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        user_rating_min: float = 7.0,
        critic_score_min: int | None = None,
        sort_by: str = "popularity_desc",
        exclude_titles: set[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        sources, genre_values = await asyncio.gather(
            self._cached_get(
                "/sources/",
                params={"regions": self.config.region, "types": "sub"},
            ),
            self._cached_get("/genres/", params={}),
        )
        source_ids = [
            str(source["id"])
            for source in sources
            if canonical_provider(str(source.get("name", ""))) in subscriptions
        ]
        if not source_ids:
            return []
        genre_lookup = {
            str(item["name"]).casefold(): str(item["id"])
            for item in genre_values
        }
        requested_genres = genres or []
        unknown_genres = [name for name in requested_genres if name.casefold() not in genre_lookup]
        if unknown_genres:
            raise CatalogError(f"Unknown Watchmode genres: {', '.join(unknown_genres)}")
        types = {
            "any": "movie,tv_movie,tv_series,tv_miniseries",
            "movie": "movie,tv_movie",
            "series": "tv_series,tv_miniseries",
        }.get(media_type)
        if types is None:
            raise CatalogError(f"Unsupported recommendation media type: {media_type}")
        params: dict[str, Any] = {
            "types": types,
            "regions": self.config.region,
            "source_types": "sub",
            "source_ids": ",".join(source_ids),
            "user_rating_low": user_rating_min,
            "sort_by": sort_by,
            "limit": max(limit * 5, 25),
        }
        if requested_genres:
            params["genres"] = ",".join(
                genre_lookup[name.casefold()] for name in requested_genres
            )
        if year_min is not None:
            params["release_date_start"] = year_min * 10_000 + 101
        if year_max is not None:
            params["release_date_end"] = year_max * 10_000 + 1231
        if critic_score_min is not None:
            params["critic_score_low"] = critic_score_min
        body = await self._cached_get("/list-titles/", params=params)
        excluded = exclude_titles or set()
        titles = [
            item
            for item in body.get("titles", [])
            if str(item.get("title", "")).casefold() not in excluded
        ][:limit]
        return await asyncio.gather(
            *(self._details(str(item["id"]), subscriptions) for item in titles)
        )

    async def _details(
        self, title_id: str, subscriptions: set[str]
    ) -> dict[str, Any]:
        body = await self._cached_get(
            f"/title/{title_id}/details/",
            params={
                "append_to_response": "sources",
                "regions": self.config.region,
            },
        )
        offerings = [
            asdict(self._offering(source))
            for source in body.get("sources", [])
            if source.get("type") == "sub"
            and canonical_provider(str(source.get("name", ""))) in subscriptions
        ]
        return {
            "catalog": "watchmode",
            "catalog_id": str(body["id"]),
            "title": str(body["title"]),
            "media_type": "movie" if body.get("type") in {"movie", "tv_movie"} else "series",
            "year": int(body["year"]) if body.get("year") else None,
            "overview": body.get("plot_overview"),
            "genres": body.get("genre_names") or [],
            "user_rating": body.get("user_rating"),
            "critic_score": body.get("critic_score"),
            "imdb_id": body.get("imdb_id"),
            "tmdb_id": int(body["tmdb_id"]) if body.get("tmdb_id") else None,
            "offerings": offerings,
            "available_on_subscription": bool(offerings),
        }

    async def _sources(self, title_id: str) -> list[Offering]:
        body = await self._cached_get(
            f"/title/{title_id}/sources/", params={"regions": self.config.region}
        )
        return [
            self._offering(source)
            for source in body
            if source.get("type") == "sub"
        ]

    async def episode_list(self, title_id: str) -> list[dict[str, Any]]:
        """Every episode of a series, in order, with the day it aired.

        Watchmode's plan here carries no per-episode deep links, so this
        cannot make an episode play — but it does answer which episode is
        the newest, which is most of what people mean when they ask."""
        body = await self._cached_get(
            f"/title/{title_id}/episodes/", params={"regions": self.config.region}
        )
        episodes = body.get("episodes", []) if isinstance(body, dict) else body
        listed = []
        for item in episodes or []:
            if not isinstance(item, dict):
                continue
            season = item.get("season_number")
            number = item.get("episode_number")
            if season is None or number is None:
                continue
            listed.append(
                {
                    "season": int(season),
                    "episode": int(number),
                    "name": str(item.get("name") or f"Episode {number}"),
                    "released": _episode_date(item.get("release_date")),
                    "overview": item.get("overview"),
                    "runtime_minutes": item.get("runtime_minutes"),
                }
            )
        listed.sort(key=lambda item: (item["season"], item["episode"]))
        return listed

    async def episode(
        self, title_id: str, season: int, episode: int
    ) -> dict[str, Any] | None:
        body = await self._cached_get(
            f"/title/{title_id}/episodes/", params={"regions": self.config.region}
        )
        episodes = body.get("episodes", []) if isinstance(body, dict) else body
        match = next(
            (
                item
                for item in episodes
                if item.get("season_number") == season
                and item.get("episode_number") == episode
            ),
            None,
        )
        if match is None:
            return None
        return {
            "catalog": "watchmode",
            "catalog_id": str(match["id"]),
            "title": str(match.get("name") or f"Season {season}, Episode {episode}"),
            "media_type": "episode",
            "season": season,
            "episode": episode,
            "imdb_id": match.get("imdb_id"),
            "tmdb_id": int(match["tmdb_id"]) if match.get("tmdb_id") else None,
            "offerings": [
                asdict(self._offering(source))
                for source in match.get("sources", [])
                if source.get("type") == "sub"
            ],
        }

    def _offering(self, source: dict[str, Any]) -> Offering:
        return Offering(
            provider=canonical_provider(str(source.get("name", ""))),
            display_name=str(source.get("name", "")),
            region=str(source.get("region", self.config.region)),
            kind=str(source.get("type", "unknown")),
            web_url=_playable_url(source.get("web_url")),
        )

    # Where a title streams changes on the order of weeks; a play request
    # re-resolves the title it was just handed, so a few hours of reuse
    # saves a quota hit per tool round without going stale.
    _CACHE_TTL_SECONDS = 6 * 3600.0

    async def _cached_get(self, path: str, params: dict[str, Any]) -> Any:
        cache = self.__dict__.setdefault("_cache", {})
        key = f"{path}?{sorted(params.items())!r}"
        cached = cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self._CACHE_TTL_SECONDS:
            return cached[1]
        body = await self._get(path, params)
        cache[key] = (now, body)
        return body

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            response = await client.get(
                f"https://api.watchmode.com/v1{path}",
                params=params,
                headers={"X-API-Key": self.config.watchmode_api_key or ""},
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogError(f"Watchmode request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()


# How far into TMDB's similarity graph a "something like Gravity" request
# reaches. Most neighbours are unavailable on this house's services or too
# obscure to survive the vote filter, so one page of twenty leaves three or
# four usable titles and the rest of the list gets padded with whatever is
# popular this week. Three pages, fifteen availability checks and up to
# eight keepers is the difference between five neighbours and two.
NEIGHBOUR_PAGES = 3
NEIGHBOUR_CHECKS = 15
NEIGHBOUR_KEEP = 8
RETRY_PAUSE_SECONDS = 0.4


class TMDBCatalog:
    def __init__(
        self,
        config: CatalogConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        # Genres of each title named as an example, filled in by _similar.
        self._reference_genres: dict[tuple[str, int], set[str]] = {}

    async def search(self, query: str, limit: int = 5) -> list[CatalogTitle]:
        if not self.config.tmdb_token:
            raise CatalogNotConfigured(
                "TMDB_READ_ACCESS_TOKEN is not configured",
                feature="movie and TV search",
                key="TMDB_READ_ACCESS_TOKEN",
            )
        body = await self._get(
            "/search/multi", {"query": query, "include_adult": "false", "language": "en-US"}
        )
        results = [
            item for item in body.get("results", []) if item.get("media_type") in {"movie", "tv"}
        ][:limit]
        provider_results = await asyncio.gather(
            *(self._providers(str(item["media_type"]), int(item["id"])) for item in results)
        )
        titles: list[CatalogTitle] = []
        for item, offerings in zip(results, provider_results, strict=True):
            media_type = str(item["media_type"])
            release = item.get("release_date") or item.get("first_air_date") or ""
            titles.append(
                CatalogTitle(
                    catalog="tmdb",
                    catalog_id=f"{media_type}-{item['id']}",
                    title=str(item.get("title") or item.get("name")),
                    media_type="movie" if media_type == "movie" else "series",
                    year=int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None,
                    imdb_id=None,
                    tmdb_id=int(item["id"]),
                    offerings=tuple(offerings),
                    release_date=release or None,
                    overview=item.get("overview"),
                    user_rating=(
                        float(item["vote_average"])
                        if item.get("vote_average") is not None
                        else None
                    ),
                )
            )
        return titles

    async def discover(
        self,
        subscriptions: set[str],
        *,
        media_type: str = "any",
        genres: list[str] | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        user_rating_min: float = 7.0,
        sort_by: str = "popularity_desc",
        exclude_titles: set[str] | None = None,
        similar_to: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not self.config.tmdb_token:
            raise CatalogNotConfigured(
                "TMDB_READ_ACCESS_TOKEN is not configured",
                feature="movie and TV search",
                key="TMDB_READ_ACCESS_TOKEN",
            )
        requested_types = ["movie", "tv"] if media_type == "any" else [media_type]
        if any(value not in {"movie", "tv", "series"} for value in requested_types):
            raise CatalogError(f"Unsupported recommendation media type: {media_type}")
        requested_types = ["tv" if value == "series" else value for value in requested_types]
        result_lists = await asyncio.gather(
            *(
                self._discover_type(
                    value,
                    subscriptions,
                    genres=genres or [],
                    year_min=year_min,
                    year_max=year_max,
                    user_rating_min=user_rating_min,
                    sort_by=sort_by,
                )
                for value in requested_types
            )
        )
        candidates = [item for values in result_lists for item in values]

        # "Something like Knives Out": TMDB's own similarity graph knows the
        # neighbours of a named title far better than a genre filter does.
        # Those neighbours are boosted within their genre tier, and the named
        # examples themselves are never handed back as suggestions.
        reference_ids: set[tuple[str, int]] = set()
        reference_titles = {_fold_title(name) for name in similar_to or []}
        similar_items: list[dict[str, Any]] = []
        if similar_to:
            similar_lists = await asyncio.gather(
                *(
                    self._similar(
                        name,
                        requested_types,
                        subscriptions,
                        genres=genres or [],
                        year_min=year_min,
                        year_max=year_max,
                        user_rating_min=user_rating_min,
                        exclude_titles=(exclude_titles or set()) | reference_titles,
                    )
                    for name in similar_to[:3]
                )
            )
            for reference, items in similar_lists:
                if reference is not None:
                    reference_ids.add(reference)
                similar_items.extend(items)
        similar_keys = {(item["tmdb_type"], item["tmdb_id"]) for item in similar_items}
        for item in candidates:
            if (item["tmdb_type"], item["tmdb_id"]) in similar_keys:
                item["similar_to"] = next(
                    other["similar_to"]
                    for other in similar_items
                    if (other["tmdb_type"], other["tmdb_id"]) == (item["tmdb_type"], item["tmdb_id"])
                )
        # Asked for something like Gravity, the neighbours rarely fill five
        # slots on their own, and what filled the rest was whatever TMDB calls
        # popular this week — a romantic comedy handed back as a space
        # thriller. Anything that is not a neighbour now has to at least share
        # a genre with the title they named, and a short honest list beats a
        # padded one.
        if reference_ids:
            reference_genres: set[str] = set()
            for reference in reference_ids:
                reference_genres |= self._reference_genres.get(reference, set())
            if reference_genres:
                candidates = [
                    item
                    for item in candidates
                    if item.get("similar_to")
                    or reference_genres & {str(name).casefold() for name in item["genres"]}
                ]
        known = {(item["tmdb_type"], item["tmdb_id"]) for item in candidates}
        candidates.extend(
            item for item in similar_items if (item["tmdb_type"], item["tmdb_id"]) not in known
        )

        def secondary(item: dict[str, Any]) -> float:
            if sort_by == "release_date_desc":
                date = str(item.get("release_date") or "")
                return float(date[:4]) if date[:4].isdigit() else 0.0
            if sort_by == "relevance_desc":
                return float(item.get("vote_average") or 0)
            return float(item.get("popularity") or 0)

        # Tier 0 = matches every requested genre; tier 1 = widened matches.
        # A widened match never outranks a full one, whatever its popularity.
        candidates.sort(
            key=lambda item: (
                item.get("genre_tier", 0),
                0 if item.get("similar_to") else 1,
                -secondary(item),
            )
        )
        excluded = exclude_titles or set()
        candidates = [
            item
            for item in candidates
            if str(item["title"]).casefold() not in excluded
            and _fold_title(str(item["title"])) not in reference_titles
            and (item["tmdb_type"], item["tmdb_id"]) not in reference_ids
        ][:limit]
        provider_lists = await asyncio.gather(
            *(
                self._providers(str(item["tmdb_type"]), int(item["tmdb_id"]))
                for item in candidates
            )
        )
        results: list[dict[str, Any]] = []
        for item, offerings in zip(candidates, provider_lists, strict=True):
            subscribed_offerings = [
                asdict(offering)
                for offering in offerings
                if offering.provider in subscriptions
            ]
            if not subscribed_offerings:
                continue
            results.append(
                {
                    "catalog": "tmdb",
                    "catalog_id": f"{item['tmdb_type']}-{item['tmdb_id']}",
                    "title": item["title"],
                    "media_type": "movie" if item["tmdb_type"] == "movie" else "series",
                    "year": item["year"],
                    "overview": item["overview"],
                    "genres": item["genres"],
                    "genre_match": "all" if item.get("genre_tier", 0) == 0 else "partial",
                    "similar_to": item.get("similar_to"),
                    "user_rating": item["vote_average"],
                    "vote_count": item["vote_count"],
                    "critic_score": None,
                    "imdb_id": None,
                    "tmdb_id": item["tmdb_id"],
                    "offerings": subscribed_offerings,
                    "available_on_subscription": True,
                    "availability_source": "JustWatch via TMDB",
                }
            )
        return results

    async def _discover_type(
        self,
        media_type: str,
        subscriptions: set[str],
        *,
        genres: list[str],
        year_min: int | None,
        year_max: int | None,
        user_rating_min: float,
        sort_by: str,
    ) -> list[dict[str, Any]]:
        providers, genre_lookup = await asyncio.gather(
            self._cached_get(
                f"/watch/providers/{media_type}",
                {"language": "en-US", "watch_region": self.config.region},
            ),
            self._genre_lookup(media_type),
        )
        provider_ids = [
            str(item["provider_id"])
            for item in providers.get("results", [])
            if canonical_provider(str(item.get("provider_name", ""))) in subscriptions
        ]
        if not provider_ids:
            return []
        resolved_genres = self._resolve_genres(media_type, genres, genre_lookup)
        if genres and not resolved_genres:
            # TMDB's movie and TV genre lists differ (TV has no "Thriller").
            # Nothing here matches for this media type; let the other type
            # carry the request instead of failing the whole recommendation.
            return []
        sort_values = {
            "popularity_desc": "popularity.desc",
            "release_date_desc": (
                "primary_release_date.desc" if media_type == "movie" else "first_air_date.desc"
            ),
            "relevance_desc": "vote_average.desc",
        }
        params: dict[str, Any] = {
            "include_adult": "false",
            "language": "en-US",
            "sort_by": sort_values.get(sort_by, "popularity.desc"),
            "vote_average.gte": user_rating_min,
            "vote_count.gte": 100,
            "watch_region": self.config.region,
            "with_watch_monetization_types": "flatrate",
            "with_watch_providers": "|".join(provider_ids),
        }
        date_name = "primary_release_date" if media_type == "movie" else "first_air_date"
        if year_min is not None:
            params[f"{date_name}.gte"] = f"{year_min}-01-01"
        if year_max is not None:
            params[f"{date_name}.lte"] = f"{year_max}-12-31"
        genre_ids = [genre_lookup[name][0] for name in resolved_genres]
        # "mystery, comedy" means both (a light-hearted mystery), so ask for
        # the intersection first; if that is thin, widen to either genre with
        # the intersection still ranked on top.
        genre_filters = [",".join(genre_ids)] if genre_ids else [None]
        if len(genre_ids) > 1:
            genre_filters.append("|".join(genre_ids))
        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for tier, genre_filter in enumerate(genre_filters):
            if tier > 0 and len(merged) >= 5:
                break  # enough true matches; do not dilute with near-misses
            query = {**params, **({"with_genres": genre_filter} if genre_filter else {})}
            pages = await asyncio.gather(
                *(
                    self._get(f"/discover/{media_type}", {**query, "page": page})
                    for page in (1, 2, 3)
                )
            )
            for body in pages:
                for item in body.get("results", []):
                    identifier = int(item["id"])
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    merged.append({**item, "_genre_tier": tier})
        results = []
        for item in merged:
            release_date = item.get("release_date") or item.get("first_air_date") or ""
            results.append(
                {
                    "tmdb_type": media_type,
                    "tmdb_id": int(item["id"]),
                    "title": str(item.get("title") or item.get("name")),
                    "year": (
                        int(release_date[:4])
                        if len(release_date) >= 4 and release_date[:4].isdigit()
                        else None
                    ),
                    "release_date": release_date,
                    "overview": item.get("overview"),
                    "genres": [
                        name
                        for genre_id in item.get("genre_ids", [])
                        for value, name in genre_lookup.values()
                        if value == str(genre_id)
                    ],
                    "genre_tier": int(item.get("_genre_tier", 0)),
                    "vote_average": item.get("vote_average"),
                    "vote_count": item.get("vote_count"),
                    "popularity": item.get("popularity"),
                }
            )
        return results

    async def details(self, query: str, media_type: str = "any") -> dict[str, Any] | None:
        search = await self._get(
            "/search/multi",
            {"query": query, "language": "en-US", "include_adult": "false"},
        )
        wanted = {"movie", "tv"} if media_type == "any" else {
            "tv" if media_type == "series" else media_type
        }
        chosen = next(
            (
                item
                for item in search.get("results", [])
                if item.get("media_type") in wanted
            ),
            None,
        )
        if chosen is None:
            return None
        tmdb_type = str(chosen["media_type"])
        body = await self._get(
            f"/{tmdb_type}/{int(chosen['id'])}",
            {"language": "en-US", "append_to_response": "credits"},
        )
        credits = body.get("credits") or {}
        release_date = body.get("release_date") or body.get("first_air_date") or ""
        crew = credits.get("crew") or []
        directors = [
            str(person["name"]) for person in crew if person.get("job") == "Director"
        ][:3]
        creators = [str(person["name"]) for person in body.get("created_by") or []][:3]
        return {
            "title": str(body.get("title") or body.get("name")),
            "media_type": "movie" if tmdb_type == "movie" else "series",
            "year": int(release_date[:4]) if release_date[:4].isdigit() else None,
            "overview": body.get("overview"),
            "genres": [str(genre["name"]) for genre in body.get("genres") or []],
            "user_rating": body.get("vote_average"),
            "vote_count": body.get("vote_count"),
            "runtime_minutes": body.get("runtime")
            or (body.get("episode_run_time") or [None])[0],
            "seasons": body.get("number_of_seasons"),
            "episodes": body.get("number_of_episodes"),
            "cast": [
                {"name": str(person["name"]), "character": person.get("character")}
                for person in (credits.get("cast") or [])[:8]
            ],
            "directors": directors,
            "creators": creators,
            "tmdb_id": int(chosen["id"]),
        }


    async def _genre_lookup(self, media_type: str) -> dict[str, tuple[str, str]]:
        genre_body = await self._cached_get(f"/genre/{media_type}/list", {"language": "en-US"})
        return {
            str(item["name"]).casefold(): (str(item["id"]), str(item["name"]))
            for item in genre_body.get("genres", [])
        }

    @staticmethod
    def _resolve_genres(
        media_type: str, genres: list[str], genre_lookup: dict[str, tuple[str, str]]
    ) -> list[str]:
        genre_aliases: dict[str, Any] = {
            "sci fi": "science fiction" if media_type == "movie" else "sci-fi & fantasy",
            "sci-fi": "science fiction" if media_type == "movie" else "sci-fi & fantasy",
            "thriller": "thriller" if media_type == "movie" else "crime",
            "action": "action" if media_type == "movie" else "action & adventure",
            "adventure": "adventure" if media_type == "movie" else "action & adventure",
            "fantasy": "fantasy" if media_type == "movie" else "sci-fi & fantasy",
            "war": "war" if media_type == "movie" else "war & politics",
            "kids": "family" if media_type == "movie" else "kids",
        }
        genre_aliases.update(GENRE_PHRASES)
        resolved_genres: list[str] = []
        for name in genres:
            key = name.casefold().strip()
            mapped = genre_aliases.get(key, key)
            targets = mapped if isinstance(mapped, list) else [mapped]
            for target in targets:
                if target in genre_lookup:
                    resolved_genres.append(target)
                    continue
                # "murder mystery" -> mystery, "dark comedy" -> comedy.
                partial = [g for g in genre_lookup if g in target or target in g]
                resolved_genres.extend(partial[:1])
        return list(dict.fromkeys(resolved_genres))

    async def _similar(
        self,
        reference: str,
        requested_types: list[str],
        subscriptions: set[str],
        *,
        genres: list[str],
        year_min: int | None,
        year_max: int | None,
        user_rating_min: float,
        exclude_titles: set[str],
    ) -> tuple[tuple[str, int] | None, list[dict[str, Any]]]:
        """Subscribed neighbours of a named title, in discover item shape."""
        search = await self._cached_get(
            "/search/multi",
            {"query": reference, "language": "en-US", "include_adult": "false"},
        )
        chosen = next(
            (
                item
                for item in search.get("results", [])
                if item.get("media_type") in set(requested_types)
            ),
            None,
        )
        if chosen is None:
            return None, []
        media_type = str(chosen["media_type"])
        key = (media_type, int(chosen["id"]))
        *pages, genre_lookup = await asyncio.gather(
            *(
                self._cached_get(
                    f"/{media_type}/{key[1]}/recommendations",
                    {"language": "en-US", "page": page},
                )
                for page in range(1, NEIGHBOUR_PAGES + 1)
            ),
            self._genre_lookup(media_type),
        )
        self._reference_genres[key] = {
            name.casefold()
            for genre_id in chosen.get("genre_ids") or []
            for value, name in genre_lookup.values()
            if value == str(genre_id)
        }
        wanted = set(self._resolve_genres(media_type, genres, genre_lookup))
        picked: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in [row for page in pages for row in page.get("results", [])]:
            if int(item["id"]) in seen or int(item["id"]) == key[1]:
                continue
            seen.add(int(item["id"]))
            title = str(item.get("title") or item.get("name") or "")
            # Recently suggested or rejected titles must not eat the slots.
            if title.casefold() in exclude_titles or _fold_title(title) in exclude_titles:
                continue
            release_date = item.get("release_date") or item.get("first_air_date") or ""
            year = int(release_date[:4]) if release_date[:4].isdigit() else None
            if year_min is not None and (year is None or year < year_min):
                continue
            if year_max is not None and (year is None or year > year_max):
                continue
            # Closeness to a title the user loves outweighs a little rating.
            if float(item.get("vote_average") or 0) < user_rating_min - 0.7:
                continue
            if int(item.get("vote_count") or 0) < 100:
                continue
            names = [
                name
                for genre_id in item.get("genre_ids", [])
                for value, name in genre_lookup.values()
                if value == str(genre_id)
            ]
            picked.append(
                {
                    "tmdb_type": media_type,
                    "tmdb_id": int(item["id"]),
                    "title": str(item.get("title") or item.get("name")),
                    "year": year,
                    "release_date": release_date,
                    "overview": item.get("overview"),
                    "genres": names,
                    "genre_tier": 0 if wanted <= {n.casefold() for n in names} else 1,
                    "vote_average": item.get("vote_average"),
                    "vote_count": item.get("vote_count"),
                    "popularity": item.get("popularity"),
                    "similar_to": reference,
                }
            )
        # Discover results are pre-filtered to subscriptions; neighbours are
        # not, so check availability now (cached, so the later pass is free).
        # Widely-seen titles are the likeliest to sit on a major service, so
        # they get the limited availability checks first.
        picked.sort(key=lambda item: -int(item.get("vote_count") or 0))
        picked = picked[:NEIGHBOUR_CHECKS]
        offerings = await asyncio.gather(
            *(self._providers(media_type, item["tmdb_id"]) for item in picked)
        )
        return key, [
            item
            for item, offered in zip(picked, offerings, strict=True)
            if any(offering.provider in subscriptions for offering in offered)
        ][:NEIGHBOUR_KEEP]

    async def _providers(self, media_type: str, title_id: int) -> list[Offering]:
        body = await self._cached_get(
            f"/{media_type}/{title_id}/watch/providers", {}, ttl=self._PROVIDER_TTL_SECONDS
        )
        region = body.get("results", {}).get(self.config.region, {})
        return [
            Offering(
                provider=canonical_provider(str(item.get("provider_name", ""))),
                display_name=str(item.get("provider_name", "")),
                region=self.config.region,
                kind="sub",
            )
            for item in region.get("flatrate", [])
        ]

    # Provider and genre catalogs change on the order of months; refetching
    # them per request is the real TMDB waste. One day of reuse is plenty.
    _CACHE: dict[str, tuple[float, Any]] = {}
    _CACHE_TTL_SECONDS = 86_400.0
    _PROVIDER_TTL_SECONDS = 6 * 3600.0  # where a title streams shifts monthly

    async def _cached_get(
        self, path: str, params: dict[str, Any], *, ttl: float | None = None
    ) -> Any:
        key = f"{path}?{sorted(params.items())!r}"
        cached = self._CACHE.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < (ttl or self._CACHE_TTL_SECONDS):
            return cached[1]
        body = await self._get(path, params)
        self._CACHE[key] = (now, body)
        return body

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            # A timed-out or 5xx TMDB call is usually a blip, and giving up on
            # the first one turns "what is this film about?" into a dead end.
            # A timeout also stringifies to nothing, so the class name stands
            # in: "TMDB request failed: " told nobody anything.
            for attempt in (1, 2):
                try:
                    response = await client.get(
                        f"https://api.themoviedb.org/3{path}",
                        params=params,
                        headers={"Authorization": f"Bearer {self.config.tmdb_token}"},
                    )
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    why = str(exc) or exc.__class__.__name__
                    blip = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or (
                        status is not None and status >= 500
                    )
                    if attempt == 2 or not blip:
                        raise CatalogError(f"TMDB request failed: {why}") from exc
                await asyncio.sleep(RETRY_PAUSE_SECONDS)
            raise CatalogError("TMDB request failed")
        finally:
            if owns_client:
                await client.aclose()


class CatalogService:
    def __init__(self, config: CatalogConfig) -> None:
        self.config = config

    async def search(self, query: str, subscriptions: set[str]) -> dict[str, Any]:
        if self.config.tmdb_token:
            titles = await TMDBCatalog(self.config).search(query)
        elif self.config.watchmode_api_key:
            titles = await WatchmodeCatalog(self.config).search(query)
        else:
            raise CatalogNotConfigured(
                "Catalog search needs TMDB_READ_ACCESS_TOKEN or WATCHMODE_API_KEY in .env",
                feature="movie and TV search",
                key="TMDB_READ_ACCESS_TOKEN",
            )
        return self._filter_search(query, subscriptions, titles)

    async def search_for_playback(
        self, query: str, subscriptions: set[str], media_type: str | None = None
    ) -> dict[str, Any]:
        if self.config.watchmode_api_key:
            title = await WatchmodeCatalog(self.config).resolve(query, media_type)
            titles = [title] if title else []
        elif self.config.tmdb_token:
            titles = await TMDBCatalog(self.config).search(query)
            if media_type in {"movie", "series"}:
                filtered = [t for t in titles if t.media_type == media_type]
                titles = filtered or titles
        else:
            raise CatalogNotConfigured(
                "Playback search needs WATCHMODE_API_KEY or TMDB_READ_ACCESS_TOKEN in .env",
                feature="finding where a title streams",
                key="TMDB_READ_ACCESS_TOKEN",
            )
        return self._filter_search(query, subscriptions, titles)

    def _filter_search(
        self,
        query: str,
        subscriptions: set[str],
        titles: list[CatalogTitle],
    ) -> dict[str, Any]:
        results = []
        for title in titles:
            data = title.to_dict()
            data["offerings"] = [
                offering
                for offering in data["offerings"]
                if offering["provider"] in subscriptions
            ]
            data["available_on_subscription"] = bool(data["offerings"])
            results.append(data)
        return {"query": query, "region": self.config.region, "results": results}

    async def episodes(self, catalog: str, catalog_id: str) -> list[dict[str, Any]]:
        """Every episode of a series. Watchmode only — TMDB's episode data is
        not wired up here, and the two number their seasons the same way."""
        if catalog != "watchmode" or not self.config.watchmode_api_key:
            raise CatalogNotConfigured(
                "Episode listings need WATCHMODE_API_KEY; TMDB is not set up for them here",
                feature="episode listings",
                key="WATCHMODE_API_KEY",
            )
        return await WatchmodeCatalog(self.config).episode_list(catalog_id)

    async def episode(
        self,
        catalog: str,
        catalog_id: str,
        season: int,
        episode: int,
        subscriptions: set[str],
    ) -> dict[str, Any] | None:
        if catalog != "watchmode" or not self.config.watchmode_api_key:
            raise CatalogError(
                "Exact episode playback requires WATCHMODE_API_KEY because TMDB does not "
                "provide provider deep links"
            )
        result = await WatchmodeCatalog(self.config).episode(catalog_id, season, episode)
        if result is None:
            return None
        result["offerings"] = [
            offering
            for offering in result["offerings"]
            if offering["provider"] in subscriptions
        ]
        result["available_on_subscription"] = bool(result["offerings"])
        return result

    async def genre_names(self) -> list[str]:
        if not self.config.tmdb_token:
            return []
        tmdb = TMDBCatalog(self.config)
        bodies = await asyncio.gather(
            tmdb._cached_get("/genre/movie/list", {"language": "en-US"}),
            tmdb._cached_get("/genre/tv/list", {"language": "en-US"}),
        )
        names = {str(g["name"]) for body in bodies for g in body.get("genres", [])}
        return sorted(names)

    async def details(self, query: str, media_type: str = "any") -> dict[str, Any] | None:
        if not self.config.tmdb_token:
            raise CatalogError("Title details require TMDB_READ_ACCESS_TOKEN")
        return await TMDBCatalog(self.config).details(query, media_type)

    async def recommend(
        self,
        subscriptions: set[str],
        *,
        media_type: str = "any",
        genres: list[str] | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        user_rating_min: float = 7.0,
        critic_score_min: int | None = None,
        sort_by: str = "popularity_desc",
        exclude_titles: set[str] | None = None,
        similar_to: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        # TMDB is the reliable discovery path; it has no critic scores, so a
        # critic_score_min request is honored as a stricter user-rating floor
        # rather than routed to Watchmode's discover, which rejects it.
        if self.config.tmdb_token:
            if critic_score_min is not None:
                user_rating_min = max(user_rating_min, min(critic_score_min / 10.0, 8.0))
            return await TMDBCatalog(self.config).discover(
                subscriptions,
                media_type=media_type,
                genres=genres,
                year_min=year_min,
                year_max=year_max,
                user_rating_min=user_rating_min,
                sort_by=sort_by,
                exclude_titles=exclude_titles,
                similar_to=similar_to,
                limit=limit,
            )
        if not self.config.watchmode_api_key:
            raise CatalogNotConfigured(
                "Recommendations require TMDB_READ_ACCESS_TOKEN or WATCHMODE_API_KEY",
                feature="recommendations",
                key="TMDB_READ_ACCESS_TOKEN",
            )
        return await WatchmodeCatalog(self.config).discover(
            subscriptions,
            media_type=media_type,
            genres=genres,
            year_min=year_min,
            year_max=year_max,
            user_rating_min=user_rating_min,
            critic_score_min=critic_score_min,
            sort_by=sort_by,
            exclude_titles=(exclude_titles or set())
            | {name.casefold() for name in similar_to or []},
            limit=limit,
        )
