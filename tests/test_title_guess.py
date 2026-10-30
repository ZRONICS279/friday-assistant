"""Guessing the real title behind a misspelling or a description."""

from house_agent.title_guess import guess_title, guess_title_online, title_from_result

UNICVERSE = [
    {"title": "Masters of the Universe (2026) - IMDb", "url": "https://www.imdb.com/title/tt0427340/"},
    {"title": "Masters of the Universe (2026 film) - Wikipedia", "url": "https://en.wikipedia.org/wiki/x"},
    {"title": "Masters of the Universe (2026) - Full cast & crew - IMDb", "url": "https://www.imdb.com/x"},
    {"title": "Prime Video: Masters of the Universe", "url": "https://www.primevideo.com/x"},
    {"title": "Masters of the Universe (2026) | Rotten Tomatoes", "url": "https://www.rottentomatoes.com/x"},
]


def test_result_headings_reduce_to_titles() -> None:
    assert title_from_result("Masters of the Universe (2026) - IMDb") == "Masters of the Universe"
    assert title_from_result("Masters of the Universe (2026 film) - Wikipedia") == "Masters of the Universe"
    assert title_from_result("Prime Video: Masters of the Universe") == "Masters of the Universe"
    assert title_from_result("E.T. the Extra-Terrestrial (1982) - Plot - IMDb") == "E.T. the Extra-Terrestrial"
    assert title_from_result("Home") is None


def test_a_misspelled_title_is_corrected_from_what_the_web_agrees_on() -> None:
    guess = guess_title("Master of the Unicverse", UNICVERSE)
    assert guess is not None
    assert guess["title"] == "Masters of the Universe" and guess["year"] == 2026


def test_a_description_is_resolved_when_results_agree() -> None:
    results = [
        {"title": "E.T. the Extra-Terrestrial (1982) - IMDb"},
        {"title": "E.T. the Extra-Terrestrial - Wikipedia"},
        {"title": "20 movies about aliens - Screen Rant"},
    ]
    guess = guess_title("movie where a kid hides an alien in his closet", results)
    assert guess and guess["title"] == "E.T. the Extra-Terrestrial"


def test_no_guess_when_results_do_not_agree_or_resemble_the_query() -> None:
    results = [{"title": "Best pancake recipes - Allrecipes"}, {"title": "Ubuntu 26.04 - Wikipedia"}]
    assert guess_title("Master of the Unicverse", results) is None
    assert guess_title("anything", []) is None


def test_no_guess_when_the_web_just_repeats_the_query() -> None:
    results = [{"title": "Lanterns (2026) - IMDb"}, {"title": "Lanterns - Wikipedia"}]
    assert guess_title("Lanterns", results) is None


async def test_online_guess_is_best_effort() -> None:
    async def search(query: str, limit: int):
        assert "Unicverse" in query and "movie" in query
        return UNICVERSE

    guess = await guess_title_online("Master of the Unicverse", search)
    assert guess and guess["title"] == "Masters of the Universe"

    async def broken(query: str, limit: int):
        raise RuntimeError("no network")

    assert await guess_title_online("Master of the Unicverse", broken) is None
