"""Handing the request back unanswered is not an answer.

Two real failures, both 2026-09-25. Asked how *The Love Hypothesis* was
like *Gravity*, the catalog timed out, the search returned five results,
two pages answered 429 and 403, and the turn ended on "I couldn't fetch a
summary" with three results unread. Asked "what flavors do they have?" mid
DoorDash order, the turn called no tool at all and said "I don't have
specific information" — it took four more messages to get the drink list.
Both now send the model back: to the tool that knows first, the web after.
"""

from house_agent.keys import WEB_FALLBACK_SETTING
from house_agent.llm import (
    GIVING_UP,
    OFFERED_TO_LOOK,
    WEB_SEARCH_NUDGE,
    gap_nudge,
    web_fetch_nudge,
)

from test_llm_action_claims import agent_for, call, run

SEARCH_OK = {
    "tool": "web_search",
    "arguments": {"query": "The Love Hypothesis plot"},
    "result": {
        "ok": True,
        "results": [
            {"title": "SuperSummary", "url": "https://sup.example/s", "snippet": "a"},
            {"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/x", "snippet": "b"},
        ],
    },
}


def fetch(url: str, ok: bool) -> dict:
    return {"tool": "web_fetch", "arguments": {"url": url}, "result": {"ok": ok}}


def test_what_counts_as_giving_up() -> None:
    for reply in (
        "I couldn't fetch a summary of *The Love Hypothesis* from the web.",
        "I could not find anything about that.",
        "I wasn't able to get details on it.",
        "No information available on that one.",
        "No pude encontrar nada.",
    ):
        assert GIVING_UP.search(reply), reply
    # An action that failed is a real report and must stand.
    for reply in (
        "I couldn't turn the lamp on — the hub is not reachable.",
        "The TV didn't respond to that.",
        "It's 59 degrees and clear.",
    ):
        assert not GIVING_UP.search(reply), reply


def test_offering_to_look_it_up_is_the_same_failure() -> None:
    for reply in (
        "Would you like me to search for analysis or summaries of the book?",
        "Want me to look it up?",
        "Should I search the web for that?",
        "¿Quieres que lo busque?",
    ):
        assert OFFERED_TO_LOOK.search(reply), reply
    # Offers that are not about looking something up.
    for reply in (
        "Want details on any, or should I put one on?",
        "Want me to put it on?",
        "Should I turn the lamp off?",
    ):
        assert not OFFERED_TO_LOOK.search(reply), reply
    # Answered from memory and asked permission to look: straight back out.
    memory = (
        "I don't have specific information about that book. Would you like me to "
        "search for summaries?"
    )
    assert gap_nudge(memory, []) == WEB_SEARCH_NUDGE
    # Having already searched, an offer to dig further is allowed to stand.
    assert gap_nudge("Want me to look it up in more depth?", [SEARCH_OK]) is None


def test_the_nudge_fires_only_while_the_web_has_more_to_give() -> None:
    catalog_failed = [{"tool": "content_details", "result": {"ok": False, "error": "timeout"}}]
    gave_up = "I couldn't fetch a summary."

    # Nothing searched at all.
    assert gap_nudge(gave_up, catalog_failed) == WEB_SEARCH_NUDGE
    # Searched, one page refused, the second result never opened.
    assert gap_nudge(
        gave_up, [*catalog_failed, SEARCH_OK, fetch("https://sup.example/s", False)]
    ) == web_fetch_nudge("https://en.wikipedia.org/wiki/x")
    # Every result tried: the answer stands.
    assert (
        gap_nudge(
            gave_up,
            [
                SEARCH_OK,
                fetch("https://sup.example/s", False),
                fetch("https://en.wikipedia.org/wiki/x", False),
            ],
        )
        is None
    )
    # A page was read: whatever it said, the web was tried.
    assert gap_nudge(gave_up, [SEARCH_OK, fetch("https://sup.example/s", True)]) is None
    # Both engines down — saying so is honest, not a failure to try.
    assert gap_nudge(gave_up, [{"tool": "web_search", "result": {"ok": False}}]) is None
    # A reply that answered the question is left alone.
    assert gap_nudge("It is a romance, not a space thriller.", catalog_failed) is None


def test_a_menu_question_goes_to_the_menu_not_the_web() -> None:
    from house_agent.llm import LOOKUP_TOOLS, lookup_nudge, widen_nudge

    menu = LOOKUP_TOOLS["doordash"]
    unknown = "I don't have specific information about the flavors of the Baja Blast."

    # Nothing looked up, mid-order: the store's own menu, not a search.
    assert gap_nudge(unknown, [], ["doordash"]) == lookup_nudge(menu)
    # The menu answered and the reply still gave up: ask it again, wider.
    answered = [{"tool": "doordash_menu", "result": {"ok": True, "items": []}}]
    assert gap_nudge(unknown, answered, ["doordash"]) == widen_nudge(menu)
    # The menu itself failed: the web is what is left.
    broken = [{"tool": "doordash_menu", "result": {"ok": False}}]
    assert gap_nudge(unknown, broken, ["doordash"]) == WEB_SEARCH_NUDGE
    # A reply that says what there is instead is a good answer, not a gap.
    offered = (
        "They don't have an Olivia Rodrigo one, but they do have **Baja Midnight** and "
        "**Baja Blast Freeze**."
    )
    assert gap_nudge(offered, answered, ["doordash"]) is None
    # With nothing pointing at a domain, no tool gets named for it.
    assert gap_nudge(unknown, [], []) == WEB_SEARCH_NUDGE


async def test_a_flavors_question_is_sent_to_the_menu(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path,
        [
            {"content": "I don't have specific information about the flavors of that."},
            call("doordash_menu", store_id="s1", query="baja blast"),
            {"content": "They have **Baja Blast**, **Baja Midnight** and the **Freeze**."},
        ],
    )
    # A store in play is what makes this a DoorDash question at all.
    tools.active_domains = lambda conversation_id: ["doordash"]

    async def menu(arguments):
        return {"ok": True, "items": [{"name": "MTN DEW Baja Blast"}]}

    tools._handlers["doordash_menu"] = menu
    events = await run(agent, "What flavors do they have?")

    from house_agent.llm import LOOKUP_TOOLS, lookup_nudge

    assert seen[1][-1] == {"role": "system", "content": lookup_nudge(LOOKUP_TOOLS["doordash"])}
    assert "Baja Midnight" in failure_message(events)
    assert "don't have specific information" not in failure_message(events)


async def test_a_failed_catalog_call_is_sent_to_the_web(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path,
        [
            call("content_details", title="The Love Hypothesis"),
            {"content": "I couldn't fetch a summary of that from the web."},
            {"content": "It is a 2026 romantic comedy — nothing like Gravity."},
        ],
    )

    async def fails(arguments):
        from house_agent.catalog import CatalogError

        raise CatalogError("TMDB request failed: ")

    tools._handlers["content_details"] = fails
    events = await run(agent, "How is the love hypothesis like gravity?")

    failure = [event for event in events if event["type"] == "done"][0]["trace"][0]["result"]
    assert failure["fallback"] == "web_search"  # the tool itself points at the web
    assert seen[2][-1] == {"role": "system", "content": WEB_SEARCH_NUDGE}
    assert "nothing like Gravity" in failure_message(events)
    assert "couldn't fetch" not in failure_message(events)
    assert any(event["type"] == "reset" for event in events)


def failure_message(events) -> str:
    return [event for event in events if event["type"] == "done"][0]["message"]


async def test_the_household_can_switch_the_web_fallback_off(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path, [{"content": "I couldn't find anything on that."}]
    )
    tools.database.set_setting(WEB_FALLBACK_SETTING, "off")
    events = await run(agent, "what is the love hypothesis about")
    assert len(seen) == 1  # no nudge, no second round
    assert "couldn't find" in failure_message(events)


def test_a_food_turn_that_read_nothing_cannot_have_found_anything() -> None:
    """DoorDash results are never kept, so in a food turn with no tool call a
    menu claim came from nowhere. The real one (2026-09-25): "I found the
    following flavors at Haggen Grocery: ..." with nothing behind it."""
    from house_agent.llm import LOOKUP_TOOLS, lookup_nudge

    menu = lookup_nudge(LOOKUP_TOOLS["doordash"])
    for reply in (
        "I found the following flavors at Haggen Grocery: Lime, Orange, Grapefruit.",
        "Haggen Grocery has the Baja Blast cans priced at $10.39.",
        "They have several Baja Blast options.",
    ):
        assert gap_nudge(reply, [], ["doordash"]) == menu, reply
    # An offer to add to the cart does not excuse the list above it.
    assert gap_nudge(
        "Haggen Grocery has Baja Blast, Coke and Pepsi. Add any to your cart?", [], ["doordash"]
    ) == menu
    # Asking the user, and the cart and order the house itself holds, stand.
    for reply in (
        "What would you like to have?",
        "Sure, what would you like from Taco Bell?",
        "Here is what is in your cart: a Cheesy Gordita Crunch.",
        "Your cart has a Cheesy Gordita Crunch; the total is $12.40.",
        "Your order is on its way.",
    ):
        assert gap_nudge(reply, [], ["doordash"]) is None, reply
    # With a menu call behind it, the same sentence is an answer.
    read = [{"tool": "doordash_menu", "result": {"ok": True, "items": []}}]
    assert gap_nudge("They have several Baja Blast options.", read, ["doordash"]) is None


def test_promising_to_check_a_menu_needs_the_menu_call() -> None:
    """"Let me check their menu to see if they have it" after a store search:
    the search never touched a menu, so the promise is still open."""
    from house_agent.llm import promised_tool_missing

    reply = 'I found a "Baja Blast" at Taco Bell. Let me check their menu to see if they have it.'
    searched = [{"tool": "doordash_search", "result": {"ok": True, "stores": [{"name": "Taco Bell"}]}}]
    assert promised_tool_missing(reply, searched) == ("doordash_menu", "missing")
    read = [*searched, {"tool": "doordash_menu", "result": {"ok": True}}]
    assert promised_tool_missing(reply, read) is None


def test_every_domain_has_facts_it_cannot_know_without_looking() -> None:
    """A turn that called no tool cannot have read a temperature, a fare, a
    cooking time or a light's state. What the prompt DOES carry — device names,
    the TVs, today's plans — is grounded, and must not be nudged."""
    from house_agent.llm import LOOKUP_TOOLS, lookup_nudge

    unfounded = [
        ("weather", "It's 59 degrees and clear."),
        ("weather", "It'll be raining by the time you leave."),
        ("weather", "High of 72 today, no rain."),
        ("weather", "It's about 25 minutes to the airport."),
        ("content", "A few movies similar to Gravity:\n**Interstellar** (2014)"),
        ("content", "It's on Netflix."),
        ("content", "It stars Sandra Bullock."),
        ("travel", "Alaska has a nonstop for $320 round trip."),
        ("travel", "The Cosmopolitan is $180 per night."),
        ("recipes", "Preheat the oven to 350."),
        ("recipes", "Ingredients: flour, sugar, eggs."),
        ("devices", "The bedroom lamp is on."),
        ("tv", "The TV is on and nothing is playing."),
        ("tv", "Volume is at 12."),
    ]
    for domain, reply in unfounded:
        assert gap_nudge(reply, [], [domain]) == lookup_nudge(LOOKUP_TOOLS[domain]), reply

    grounded = [
        # In the prompt every turn: the house line and the Now line.
        ("devices", "You have a bedroom lamp and a kitchen light."),
        ("calendar", "You have a meeting at 2pm today."),
        # Asking the user, which is not a claim about anything.
        ("weather", "Which town do you live in?"),
        ("content", "Want details on any, or should I put one on?"),
        ("travel", "Chicago has two airports — which did you mean?"),
        ("recipes", "What kind of dinner are you after?"),
        ("devices", "Is there a QR code on the box?"),
        ("tv", "Which TV did you mean?"),
        # A tool ran, so the same sentence is an answer.
    ]
    for domain, reply in grounded:
        assert gap_nudge(reply, [], [domain]) is None, reply
    assert gap_nudge("It's 59 degrees and clear.", [{"tool": "weather_now", "result": {"ok": True}}], ["weather"]) is None


async def test_a_spoken_answer_is_not_answered_again(tmp_path) -> None:
    """Out loud, a second rewritten answer after the first one is what sounded
    like Friday repeating itself. Typed, the rewrite replaces the text."""
    script = [
        {"content": "After *Iron Man 3* comes **Thor: The Dark World** (2013)."},
        {"content": "Having checked, it's **Thor: The Dark World** (2013)."},
    ]
    agent, tools, seen = agent_for(tmp_path, list(script))
    tools.active_domains = lambda conversation_id: ["content"]
    events = [e async for e in agent.chat_stream("What's next after Iron Man 3?", "c1", spoken=True)]
    assert len(seen) == 1
    assert "After *Iron Man 3*" in failure_message(events)

    agent, tools, seen = agent_for(tmp_path / "typed", list(script) + [{"content": "It's **Thor**."}])
    tools.active_domains = lambda conversation_id: ["content"]
    [e async for e in agent.chat_stream("What's next after Iron Man 3?", "c2", spoken=False)]
    assert len(seen) > 1  # typed: sent back to look it up
