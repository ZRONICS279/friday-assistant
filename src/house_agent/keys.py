"""Features that run on a third-party API key the house hasn't been given.

A service raises MissingKey instead of a plain error; the tool layer turns
it into a web-search fallback (or, with that switched off in Settings, a
pointer to the key) rather than a dead end.
"""

from __future__ import annotations

# Settings switches. Each keyed feature has its own ("web_fallback.movies");
# one never set follows the house-wide default ("web_fallback"), which is on.
WEB_FALLBACK_SETTING = "web_fallback"

# The features that run on a key, as Settings lists them.
KEYED_FEATURES: dict[str, dict[str, str]] = {
    "movies": {
        "label": "Movies & shows",
        "what": "search, recommendations, where a title streams",
        "key": "TMDB or Watchmode key",
    },
    "flights": {
        "label": "Flight search",
        "what": "fares and schedules (hotels need no key)",
        "key": "LetsFG sign-in (letsfg auth)",
    },
}


def fallback_setting(group: str) -> str:
    return f"{WEB_FALLBACK_SETTING}.{group}"


def web_fallback_enabled(database: object, group: str) -> bool:
    value = (database.get_setting(fallback_setting(group)) if group else None) or database.get_setting(  # type: ignore[attr-defined]
        WEB_FALLBACK_SETTING
    )
    return (value or "on") != "off"


class MissingKey(RuntimeError):
    def __init__(self, message: str, *, feature: str = "", key: str = "", group: str = "") -> None:
        super().__init__(message)
        self.feature = feature
        self.key = key
        self.group = group
