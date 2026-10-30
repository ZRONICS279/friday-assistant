from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

from .catalog import (
    PROVIDER_APP_NAMES,
    CatalogService,
    canonical_provider,
)
from .config import AppConfig
from .db import Database
from .roku import RokuClient, RokuError, RokuTimeout
from .title_guess import guess_title_online
from .websearch import web_search

log = logging.getLogger(__name__)


class ContentError(RuntimeError):
    pass




def episode_title(title: str, season: int, episode: int) -> str:
    return f"{title} S{season:02d}E{episode:02d}"


def content_id_from_url(provider: str, url: str) -> str | None:
    provider = canonical_provider(provider)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if provider == "paramount_plus" and query.get("u"):
        parsed = urlparse(query["u"][0])
        query = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")
    if provider == "prime_video":
        gti = query.get("gti", [])
        if gti and gti[0]:
            return gti[0]
    if provider == "apple_tv_plus":
        show_id = query.get("showId", [])
        if show_id and show_id[0]:
            return show_id[0]
    patterns = {
        "apple_tv_plus": [r"/(?:episode|show|movie)/[^/]+/(umc\.cmc\.[^/]+)$"],
        "netflix": [r"/(?:watch|title)/(\d+)$"],
        "disney_plus": [
            r"/video/([^/]+)$",
            r"/browse/entity-([0-9a-f-]+)$",
            r"/play/([0-9a-f-]{8,})/?$",
        ],
        "hulu": [r"/(?:watch|series|movie)/([^/]+)$"],
        "max": [r"/(?:video/watch|movies|shows|movie|show)/([^/]+)$"],
        "prime_video": [r"/(?:detail|gp/video/detail)/([^/]+)(?:/.*)?$"],
        "paramount_plus": [r"/(?:shows|movies)/[^/]+/video/([^/]+)"],
    }
    for pattern in patterns.get(provider, []):
        match = re.search(pattern, path, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


class PlaybackService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        catalog: CatalogService | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.catalog = catalog or CatalogService(config.catalog)
        # When the catalog has never heard of a title, ask the web what was
        # meant (a misspelling, a description) and try again with that.
        self.guess_title = lambda query: guess_title_online(query, web_search)
        self.devices = {device.id: device for device in config.tv_devices}

    def subscriptions(self) -> set[str]:
        return {
            str(item["provider"])
            for item in self.database.list_subscriptions(enabled_only=True)
        }

    async def search(self, query: str) -> dict[str, Any]:
        result = await self.catalog.search(query, self.subscriptions())
        if result.get("results"):
            return result
        guess = await self.guess_title(query)
        if not guess:
            return result
        again = await self.catalog.search(guess["title"], self.subscriptions())
        if not again.get("results"):
            return {**result, "did_you_mean": {**guess, "asked_for": query}}
        return {**again, "query": query, "did_you_mean": {**guess, "asked_for": query}}

    async def _find_series(self, title: str) -> dict[str, Any] | None:
        search = await self.catalog.search_for_playback(title, self.subscriptions(), "series")
        matches = [item for item in search["results"] if item["media_type"] == "series"]
        return matches[0] if matches else None

    async def episodes(
        self, title: str, season: int | None = None, limit: int = 12
    ) -> dict[str, Any]:
        """Which episodes a series has, and which one is the newest to have
        aired. Nothing here can make a particular episode play — the catalog
        plan carries no per-episode deep links — but "the newest episode of
        Always Sunny" is a question with an answer, and it was going
        unanswered while the show played from wherever it left off."""
        show = await self._find_series(title)
        if show is None:
            # Nobody says "It's Always Sunny in Philadelphia" out loud: ask the
            # web what the short name meant, the same way planning does.
            guess = await self.guess_title(title)
            show = await self._find_series(guess["title"]) if guess else None
            if show is None:
                return {"status": "not_found", "title": title}
        listed = await self.catalog.episodes(show["catalog"], show["catalog_id"])
        if not listed:
            return {"status": "no_episodes", "title": show["title"]}

        today = date.today().isoformat()
        aired = [item for item in listed if item["released"] and item["released"] <= today]
        upcoming = [item for item in listed if item["released"] and item["released"] > today]
        # A series with no dates at all: the last one listed is the best guess.
        latest = aired[-1] if aired else (listed[-1] if not upcoming else None)
        chosen = [item for item in listed if item["season"] == season] if season else None

        result: dict[str, Any] = {
            "status": "ok",
            "title": show["title"],
            "seasons": max(item["season"] for item in listed),
            "episode_count": len(listed),
            "latest": latest,
            "available_on": [offering["provider"] for offering in show["offerings"]],
            "note": (
                "Episodes cannot be deep-linked on this catalog plan. Say which episode is "
                "newest, then play the series and tell them to pick it from the app."
            ),
        }
        if upcoming:
            result["next_episode"] = upcoming[0]
        if season is not None:
            if not chosen:
                result["status"] = "no_such_season"
                result["error"] = f"{show['title']} has no season {season}."
            else:
                result["season"] = season
                result["episodes"] = chosen[-limit:]
        return result

    async def plan(
        self,
        title: str,
        provider: str | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        """Plan playback of a title; when the catalog does not know it, work
        out what was meant and plan that instead, saying so."""
        result = await self._plan(title, provider, media_type, season, episode)
        if result.get("status") != "not_found":
            return result
        guess = await self.guess_title(title)
        if not guess:
            return result
        did_you_mean = {**guess, "asked_for": title}
        again = await self._plan(guess["title"], provider, media_type, season, episode)
        if again.get("status") == "not_found":
            return {**result, "did_you_mean": did_you_mean}
        return {**again, "did_you_mean": did_you_mean}

    async def _plan(
        self,
        title: str,
        provider: str | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        if (season is None) != (episode is None):
            raise ContentError("Season and episode must be provided together")
        if season is not None and (season < 1 or episode is None or episode < 1):
            raise ContentError("Season and episode must be positive integers")
        exact_episode = season is not None and episode is not None
        if exact_episode and not self.config.catalog.exact_episode_playback_enabled:
            return {
                "status": "feature_disabled",
                "title": title,
                "season": season,
                "episode": episode,
                "reason": (
                    "Exact episode playback is disabled because the current Watchmode plan "
                    "does not provide episode deep links. Omit season and episode to resume "
                    "the series."
                ),
                "enable_with": "EXACT_EPISODE_PLAYBACK_ENABLED=true",
            }
        stored_title = episode_title(title, season, episode) if exact_episode else title
        stored_media_type = "episode" if exact_episode else media_type
        requested_provider = canonical_provider(provider) if provider else None
        if requested_provider and requested_provider not in self.subscriptions():
            raise ContentError(f"{provider} is not enabled in household subscriptions")

        if not requested_provider:
            stored_links = [
                self.database.find_content_link(stored_title, subscribed, stored_media_type)
                for subscribed in self.subscriptions()
            ]
            verified_links = [link for link in stored_links if link and link["verified"]]
            if verified_links:
                return {"status": "ready", "link": verified_links[0]}
            trusted_links = [
                link
                for link in stored_links
                if link and self._resolver_verified(str(link["provider"]))
            ]
            if trusted_links:
                return {
                    "status": "ready",
                    "link": trusted_links[0],
                    "resolver_verified": True,
                }

        if requested_provider:
            stored = self.database.find_content_link(
                stored_title, requested_provider, stored_media_type
            )
            if stored:
                resolver_verified = self._resolver_verified(requested_provider)
                return {
                    "status": "ready" if stored["verified"] or resolver_verified else "needs_verification",
                    "link": stored,
                    "resolver_verified": resolver_verified,
                }

        if hasattr(self.catalog, "search_for_playback"):
            search = await self.catalog.search_for_playback(
                title, self.subscriptions(), media_type
            )
        else:
            search = await self.catalog.search(title, self.subscriptions())
        matches = search["results"]
        if not matches:
            return {"status": "not_found", "title": title, "catalog": search}
        if media_type in {"movie", "series"}:
            typed = [item for item in matches if item["media_type"] == media_type]
            matches = typed or matches
        folded = re.sub(r"[^a-z0-9]+", "", title.casefold())
        exact = [
            item
            for item in matches
            if re.sub(r"[^a-z0-9]+", "", item["title"].casefold()) == folded
        ]
        selected = (exact or matches)[0]
        series = selected
        if exact_episode:
            selected = await self.catalog.episode(
                str(series["catalog"]),
                str(series["catalog_id"]),
                season,
                episode,
                self.subscriptions(),
            )
            if selected is None:
                return {
                    "status": "episode_not_found",
                    "title": series,
                    "season": season,
                    "episode": episode,
                }
        offerings = selected["offerings"]
        if requested_provider:
            offerings = [item for item in offerings if item["provider"] == requested_provider]
        if not offerings:
            return {
                "status": "not_on_subscription",
                "title": selected,
                "subscriptions": sorted(self.subscriptions()),
            }

        offering = offerings[0]
        content_id = (
            content_id_from_url(offering["provider"], offering["web_url"])
            if offering.get("web_url")
            else None
        )
        if not content_id:
            return {
                "status": "app_only",
                "title": selected,
                "offering": offering,
                "reason": "The catalog confirms availability but did not provide a usable provider content ID.",
            }
        resolved_type = "episode" if exact_episode else media_type or selected["media_type"]
        link = self.database.save_content_link(
            title=stored_title if exact_episode else selected["title"],
            provider=offering["provider"],
            media_type=resolved_type,
            app_name=PROVIDER_APP_NAMES.get(offering["provider"], offering["display_name"]),
            content_id=content_id,
            source_url=offering["web_url"],
            source=selected["catalog"],
            verified=False,
        )
        resolver_verified = self._resolver_verified(offering["provider"])
        return {
            "status": "ready" if resolver_verified else "needs_verification",
            "title": selected,
            "link": link,
            "resolver_verified": resolver_verified,
            "reason": (
                "The provider URL pattern was previously verified on this Roku."
                if resolver_verified
                else "This provider URL pattern needs one successful Roku test; individual titles do not."
            ),
            **(
                {
                    "series_title": series["title"],
                    "episode_title": selected["title"],
                    "season": season,
                    "episode": episode,
                }
                if exact_episode
                else {}
            ),
        }

    async def _play_elsewhere(
        self,
        device: Any,
        link: dict[str, Any],
        plan: dict[str, Any],
        title: str,
        profile: str | None,
    ) -> dict[str, Any]:
        """Deep-link on a non-Roku TV. The command is confirmed as accepted,
        never as playing, because these platforms report nothing back."""
        from .tv import TVError, client_for

        client = client_for(device, self.database.tv_credentials(device.id))
        try:
            await client.deep_link(link["app_name"], link["content_id"], link["media_type"])
        except TVError as exc:
            raise ContentError(str(exc)) from exc
        # Same taste bookkeeping the Roku path does, so watching on a second
        # brand still feeds recommendations.
        preference = self.database.record_preference(
            profile or self.config.household_profile,
            link["title"],
            link["media_type"],
            "liked",
            reason="User requested playback",
            source="playback_request",
        )
        return {
            "ok": True,
            "status": "launched",
            "device_id": device.id,
            "platform": device.platform,
            "title": link["title"],
            "provider": link["provider"],
            "season": plan.get("season"),
            "episode": plan.get("episode"),
            "profile_confirmation": "not_observable",
            "preference": preference,
            "verification": (
                f"{device.name} accepted the launch. A {device.platform} TV reports "
                "no playback state over the network, so ask the user to confirm the "
                "right title started."
            ),
        }

    async def play(
        self,
        *,
        device_id: str,
        title: str,
        provider: str | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        profile: str | None = None,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        plan = await self.plan(title, provider, media_type, season, episode)
        link = plan.get("link")
        if link is None:
            return {"ok": False, **plan}
        trusted = bool(link["verified"] or plan.get("resolver_verified"))
        # A provider's first play is a supervised test, not a refusal: the
        # launch goes ahead, the Roku's media player confirms playback (which
        # verifies the provider for good), and the user is asked to glance at
        # the screen. allow_unverified is accepted for compatibility.
        del allow_unverified
        device = self.devices.get(device_id)
        if device is None:
            known = ", ".join(self.devices) or "none"
            raise ContentError(f"Unknown TV '{device_id}'. Configured: {known}")
        if device.platform != "roku":
            # Only Roku exposes a media-player query, so on other brands the
            # launch is reported as sent rather than as confirmed playing.
            return await self._play_elsewhere(device, link, plan, title, profile)
        client = RokuClient(device)
        app = await client.find_app(link["app_name"])
        try:
            await client.deep_link(app.id, link["content_id"], link["media_type"])
        except RokuTimeout:
            # The Roku took the launch and is busy starting the app; whether
            # it worked is what the checks below find out.
            log.info("content: %s slow to answer the launch; checking playback", device.name)
        # Confirm the video actually started; clear a profile picker if one
        # is in the way. Waits grow because a TV waking from standby can take
        # 20+ seconds to cold-boot an app. A status query the TV drops while
        # it does is skipped, not the end of the checks: giving up there left
        # people on Prime Video's "who's watching?" screen.
        profile_confirmation = "not_confirmed"
        answered = False
        waits = (4, 4, 6, 8)
        for attempt, wait_seconds in enumerate(waits):
            await asyncio.sleep(wait_seconds)
            try:
                player = await client.media_player()
            except RokuError:
                continue
            answered = True
            if player.get("state") in {"play", "pause", "buffer"}:
                profile_confirmation = (
                    "already_playing" if attempt == 0 else "select_confirmed"
                )
                break
            if attempt < len(waits) - 1:
                try:
                    await client.keypress("Select")
                except RokuError:
                    continue
        if not answered:
            profile_confirmation = "status_unavailable"
        newly_verified = False
        if profile_confirmation in {"already_playing", "select_confirmed"} and not trusted:
            # The TV confirmed video is actually playing, which is the
            # supervised test the verification gate was waiting for.
            self.database.set_provider_resolver_verified(
                str(link["provider"]),
                True,
                "Auto-verified: Roku media player confirmed playback.",
            )
            self.database.save_content_link(
                title=str(link["title"]),
                provider=str(link["provider"]),
                media_type=str(link["media_type"]),
                app_name=str(link["app_name"]),
                content_id=str(link["content_id"]),
                source_url=link.get("source_url"),
                source=str(link["source"]),
                verified=True,
            )
            newly_verified = True
        preference_profile = profile or self.config.household_profile
        preference = self.database.record_preference(
            preference_profile,
            link["title"],
            link["media_type"],
            "liked",
            reason="User requested playback",
            source="playback_request",
        )
        return {
            "ok": True,
            "status": "launched",
            "device_id": device_id,
            "title": link["title"],
            "provider": link["provider"],
            "season": season,
            "episode": episode,
            "app": {"id": app.id, "name": app.name},
            "verified_before_launch": link["verified"],
            "resolver_verified_before_launch": bool(plan.get("resolver_verified")),
            "profile_confirmation": profile_confirmation,
            "provider_newly_verified": newly_verified,
            "first_play_on_provider": not trusted,
            "preference": preference,
            **(
                {
                    "next_step": (
                        f"First play on {link['provider']} — tell the user that, and "
                        "ask them to confirm the right title started. If they say "
                        "the wrong thing played, do not deep-link this provider "
                        "again this conversation."
                    )
                }
                if not trusted and not newly_verified
                else {}
            ),
            "verification": (
                "Roku accepted the deep link and playback was confirmed on the device."
                if profile_confirmation in {"already_playing", "select_confirmed"}
                else "Roku accepted the deep link, but playback could not be confirmed "
                "over the network — the launch itself did NOT fail; ask the user to "
                "glance at the screen."
            ),
        }

    def _resolver_verified(self, provider: str) -> bool:
        return bool(self.database.get_provider_resolver(provider)["verified"])
