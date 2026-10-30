from __future__ import annotations

from typing import Any

from .catalog import CatalogService
from .config import AppConfig
from .db import Database


def _taste_rank(
    candidates: list[dict[str, Any]],
    affinity: dict[str, Any],
    limit: int,
    *,
    explicit_genres: bool = False,
) -> list[dict[str, Any]]:
    """Blend catalog order with learned genre taste.

    Trust scales with evidence: a fresh profile ranks almost purely by the
    catalog's order, and taste weight grows with graded feedback until it
    caps at 65% — strict only once it has earned the right to be.
    """
    signals = int(affinity.get("signals") or 0)
    scores: dict[str, float] = affinity.get("scores") or {}
    weight = min(0.65, signals / 25.0)
    if explicit_genres:
        # The user said what they want; taste may only nudge the order.
        weight = min(weight, 0.3)
    if not scores or weight <= 0 or len(candidates) <= 1:
        return candidates[:limit]
    span = max(len(candidates) - 1, 1)
    ranked = []
    for index, candidate in enumerate(candidates):
        catalog_score = 1 - index / span
        genres = [str(genre).casefold() for genre in candidate.get("genres") or []]
        taste = (
            sum(scores.get(genre, 0.0) for genre in genres) / len(genres)
            if genres
            else 0.0
        )
        blended = (1 - weight) * catalog_score + weight * taste
        tier = 0 if candidate.get("genre_match", "all") == "all" else 1
        # A title the catalog found next to the example the user named is what
        # they asked for; learned taste may order those among themselves, but
        # it never promotes a merely popular title over one of them.
        example = 0 if candidate.get("similar_to") else 1
        ranked.append((tier, example, blended, index, candidate))
    # Never let taste promote a partial genre match above a full one.
    ranked.sort(key=lambda item: (item[0], item[1], -item[2], item[3]))
    return [candidate for *_, candidate in ranked[:limit]]


class RecommendationService:
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

    async def recommend(
        self,
        *,
        profile: str | None = None,
        media_type: str = "any",
        genres: list[str] | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        user_rating_min: float = 7.0,
        critic_score_min: int | None = None,
        sort_by: str = "popularity_desc",
        similar_to: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        profile = profile or self.config.household_profile
        similar_to = [name.strip() for name in similar_to or [] if name.strip()][:3]
        if year_min is not None and year_max is not None and year_min > year_max:
            raise ValueError("Recommendation start year cannot be after end year")
        limit = max(1, min(limit, 10))
        subscriptions = {
            str(item["provider"])
            for item in self.database.list_subscriptions(enabled_only=True)
        }
        context = {
            "media_type": media_type,
            "genres": genres or [],
            "year_min": year_min,
            "year_max": year_max,
            "user_rating_min": user_rating_min,
            "critic_score_min": critic_score_min,
            "sort_by": sort_by,
            "similar_to": similar_to,
        }
        candidates = await self.catalog.recommend(
            subscriptions,
            media_type=media_type,
            genres=genres,
            year_min=year_min,
            year_max=year_max,
            user_rating_min=user_rating_min,
            critic_score_min=critic_score_min,
            sort_by=sort_by,
            exclude_titles=self.database.recommendation_exclusions(profile),
            similar_to=similar_to,
            limit=min(limit * 3, 12),
        )
        affinity = self.database.genre_affinity(profile)
        candidates = _taste_rank(
            candidates, affinity, limit, explicit_genres=bool(genres)
        )
        self.database.record_recommendations(profile, candidates, context)
        hint = None
        if not candidates:
            names = await self.catalog.genre_names()
            hint = (
                "No matches for these criteria. Retry with fewer or different "
                "constraints (drop the year range or rating floor, or use these "
                f"genre names: {', '.join(names)})."
            )
        return {
            "profile": profile,
            "criteria": context,
            "candidates": [
                {
                    key: (value[:240] if key == "overview" and isinstance(value, str) else value)
                    for key, value in candidate.items()
                }
                for candidate in candidates
            ],
            "taste_profile": {
                "genre_scores": affinity["scores"],
                "feedback_signals": affinity["signals"],
                "taste_weight": round(min(0.65, affinity["signals"] / 25.0), 2),
            },
            "taste_memory": [
                {k: item[k] for k in ("title", "media_type", "status", "reason")}
                for item in self.database.list_preferences(profile)[:25]
            ],
            "retry_hint": hint,
            "excluded_examples": similar_to,
            "history_complete": False,
            "history_note": (
                "Streaming services do not expose complete profile history. Results exclude "
                "locally recorded likes, dislikes, recent deferrals, and watch history."
            ),
        }

    def record_feedback(
        self,
        *,
        title: str,
        status: str,
        media_type: str = "unknown",
        profile: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        profile = profile or self.config.household_profile
        if status == "asked":
            marked = self.database.mark_recommendation_asked(profile, title)
            return {"ok": True, "marked_asked": marked, "title": title}
        preference = self.database.record_preference(
            profile,
            title,
            media_type,
            status,
            reason=reason,
            source="recommendation_feedback",
        )
        return {"ok": True, "preference": preference}

    def memory(self, profile: str | None = None) -> dict[str, Any]:
        profile = profile or self.config.household_profile
        household = self.config.household_profile
        return {
            "profile": profile,
            "notes": self.database.list_profile_notes(profile),
            "household_notes": (
                self.database.list_profile_notes(household) if profile != household else []
            ),
            "style_preferences": self.database.list_style_preferences(
                [profile, household] if profile != household else [household]
            ),
            "household_members": [
                name for name in self.database.known_profiles() if name != household
            ],
            "preferences": [
                {k: item[k] for k in ("title", "media_type", "status", "reason")}
                for item in self.database.list_preferences(profile)[:30]
            ],
            "recent_recommendations": [
                {k: item[k] for k in ("title", "media_type", "status", "updated_at")}
                for item in self.database.list_recommendations(profile, limit=15)
            ],
            "watch_history": self.database.find_watch_history(profile)[:25],
            "watch_history_complete": False,
        }
