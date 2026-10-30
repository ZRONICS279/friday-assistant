"""The facts pinned into a turn's context, which follow-up questions rely on."""

from house_agent.llm import _titles_offered

RECOMMENDATION = (
    "A few gentle fall mystery audiobooks:\n"
    "**The Murder of Margaret R.** (2020)\n"
    "**Murder on the Orient Express** (2011)\n"
    "**The Mysterious Affair at Styles** (2014)\n"
    "Want details on any, or should I add more?"
)


def test_the_offered_list_is_pinned_for_follow_ups() -> None:
    line = _titles_offered(
        [{"role": "user", "content": "Books on tape."},
         {"role": "assistant", "content": RECOMMENDATION}]
    )
    assert line is not None
    # Every title survives with the year it was actually given, so "the 2014
    # one" lands on Styles rather than on whichever title comes to mind.
    assert "The Mysterious Affair at Styles (2014)" in line
    assert "Murder on the Orient Express (2011)" in line
    assert "The Murder of Margaret R. (2020)" in line
    assert line.index("Margaret") < line.index("Orient") < line.index("Styles")


def test_only_the_latest_reply_counts() -> None:
    # An older list is stale: a follow-up refers to what was just said.
    assert _titles_offered(
        [{"role": "assistant", "content": RECOMMENDATION},
         {"role": "user", "content": "Pause it."},
         {"role": "assistant", "content": "Paused."}]
    ) is None


def test_nothing_to_pin_from_plain_replies() -> None:
    assert _titles_offered([]) is None
    assert _titles_offered([{"role": "user", "content": "**Hi** (2020)"}]) is None
    assert _titles_offered([{"role": "assistant", "content": "Done."}]) is None


def test_a_single_titled_reply_is_pinned_too() -> None:
    line = _titles_offered([{"role": "assistant", "content": "**Heat** (1995) is on Netflix."}])
    assert line is not None and "Heat (1995)" in line


def test_a_title_without_a_year_survives() -> None:
    line = _titles_offered([{"role": "assistant", "content": "**Frasier**\n**Cheers** (1982)"}])
    assert line is not None and "Frasier;" in line and "Cheers (1982)" in line


def test_the_same_title_is_not_repeated() -> None:
    line = _titles_offered(
        [{"role": "assistant", "content": "**Heat** (1995) and **Heat** (1995)."}]
    )
    assert line is not None and line.count("Heat (1995)") == 1
