from pathlib import Path

from house_agent.db import Database


def test_routines_watch_history_and_messages_round_trip(tmp_path: Path) -> None:
    database = Database(tmp_path / "house-agent.db")
    database.initialize()

    database.save_routine(
        "netflix mom",
        "living-room",
        "Launch Netflix and select Mom",
        [{"type": "launch", "app": "Netflix"}, {"type": "key", "key": "Select"}],
    )
    assert database.get_routine("netflix mom")["device_id"] == "living-room"

    database.record_watch("mom", "Netflix", "Dark", "watched", season=1, episode=1)
    history = database.find_watch_history("mom", "Netflix", "Dark")
    assert history[0]["status"] == "watched"

    conversation_id = database.new_conversation_id()
    database.add_message(conversation_id, "user", "Turn on the TV")
    assert database.get_messages(conversation_id) == [
        {"role": "user", "content": "Turn on the TV"}
    ]

    database.seed_subscriptions([("netflix", "Netflix"), ("max", "Max")])
    assert {item["provider"] for item in database.list_subscriptions()} == {"netflix", "max"}

    link = database.save_content_link(
        title="Dark",
        provider="netflix",
        media_type="series",
        app_name="Netflix",
        content_id="80100172",
        source_url="https://www.netflix.com/title/80100172",
        source="test",
        verified=False,
    )
    assert link["verified"] is False
    assert database.find_content_link("dark", "netflix")["content_id"] == "80100172"

    resolver = database.set_provider_resolver_verified("netflix", True, "Test title opened")
    assert resolver["verified"] is True
    assert database.get_provider_resolver("netflix")["notes"] == "Test title opened"

    database.record_recommendations(
        "mom",
        [
            {
                "title": "Palm Springs",
                "media_type": "movie",
                "catalog": "watchmode",
                "catalog_id": "123",
                "offerings": [{"provider": "hulu"}],
            }
        ],
        {"genres": ["Comedy"]},
    )
    preference = database.record_preference(
        "mom",
        "Palm Springs",
        "movie",
        "deferred",
        reason="Not tonight",
    )
    assert preference["status"] == "deferred"
    assert "palm springs" in database.recommendation_exclusions("mom")
    assert database.list_recommendations("mom")[0]["status"] == "deferred"


def test_ignored_suggestions_rotate_back_after_enough_newer_ones(tmp_path: Path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    candidate = {
        "title": "Frasier", "media_type": "series",
        "catalog": "tmdb", "catalog_id": "1", "offerings": [],
    }
    database.record_recommendations("default", [candidate], {})
    assert "frasier" in database.recommendation_exclusions("default")

    # Nine newer suggestions: still cooling down.
    for index in range(9):
        database.record_recommendations(
            "default",
            [{**candidate, "title": f"Filler {index}", "catalog_id": str(index)}],
            {},
        )
    assert "frasier" in database.recommendation_exclusions("default")

    # The tenth releases it.
    database.record_recommendations(
        "default", [{**candidate, "title": "Filler 9", "catalog_id": "9"}], {}
    )
    assert "frasier" not in database.recommendation_exclusions("default")


def test_asked_titles_cool_down_for_fewer_recommendations(tmp_path: Path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    candidate = {
        "title": "Futurama", "media_type": "series",
        "catalog": "tmdb", "catalog_id": "2", "offerings": [],
    }
    database.record_recommendations("default", [candidate], {})
    assert database.mark_recommendation_asked("default", "futurama")
    for index in range(5):
        database.record_recommendations(
            "default",
            [{**candidate, "title": f"Other {index}", "catalog_id": str(index)}],
            {},
        )
    assert "futurama" not in database.recommendation_exclusions("default")


def test_soft_no_and_deferred_have_time_windows(tmp_path: Path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    database.record_preference("default", "Ted Lasso", "series", "soft_no")
    database.record_preference("default", "Friends", "series", "deferred")
    excluded = database.recommendation_exclusions("default")
    assert "ted lasso" in excluded
    assert "friends" in excluded


def test_genre_affinity_learns_from_graded_feedback(tmp_path: Path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    database.record_recommendations(
        "default",
        [
            {"title": "Futurama", "media_type": "series", "catalog": "tmdb",
             "catalog_id": "1", "offerings": [], "genres": ["Animation", "Comedy"]},
            {"title": "Regular Show", "media_type": "series", "catalog": "tmdb",
             "catalog_id": "2", "offerings": [], "genres": ["Animation"]},
        ],
        {},
    )
    database.record_preference("default", "Futurama", "series", "liked")
    database.record_preference("default", "Regular Show", "series", "soft_no")
    affinity = database.genre_affinity("default")
    assert affinity["signals"] == 2
    assert affinity["scores"]["comedy"] > 0
    assert affinity["scores"]["animation"] > 0  # +1.0 liked, -0.4 soft_no


def test_conversation_history_compacts_behind_a_summary(tmp_path: Path) -> None:
    database = Database(tmp_path / "house.db")
    database.initialize()
    for index in range(6):
        database.add_message("conv", "user", f"message {index}")
    state = database.get_history("conv")
    assert state["summary"] is None
    assert len(state["messages"]) == 6

    cutoff = state["messages"][3]["id"]
    database.save_conversation_summary("conv", "the gist so far", cutoff)
    state = database.get_history("conv")
    assert state["summary"] == "the gist so far"
    assert [item["content"] for item in state["messages"]] == ["message 4", "message 5"]
