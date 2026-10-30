"""A reply may not contain a web address that no tool, and nobody, supplied.

The real failure: asked for the calendar address two turns after the tool
had returned it, the model produced a Google Calendar URL with a runaway
string of letters. The address it needed was gone from the prompt, so it
made one up. Now an invented URL sends the model back to the tool once,
and is withheld if it comes back invented again.
"""

from house_agent.llm import URL_NUDGE, URL_WITHHELD, invented_urls

from test_llm_action_claims import agent_for, call, run

FEED = "http://friday.local:8787/calendar.ics"
FAKE = "https://calendar.google.com/calendar/ical/friday_abc123/basic.ics"


def test_urls_are_checked_against_what_was_actually_seen() -> None:
    known = f'{{"address": "{FEED}"}} the user said https://example.com/page.'
    assert invented_urls(f"Enter {FEED}.", known) == []
    assert invented_urls("See https://example.com/page, it is fine", known) == []
    assert invented_urls(f"Enter {FAKE}", known) == [FAKE]
    assert invented_urls("No links here.", "") == []


async def test_an_invented_address_is_sent_back_to_the_tool(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path,
        [
            {"content": f"3. Enter this URL: {FAKE}"},
            call("calendar_setup_help", app="apple"),
            {"content": f"3. Enter this URL: {FEED} then tap Next."},
        ],
    )
    events = await run(agent, "yes, ready for the next steps")
    assert seen[1][-1] == {"role": "system", "content": URL_NUDGE}
    final = [event for event in events if event["type"] == "done"][0]
    assert FEED in final["message"] and FAKE not in final["message"]
    assert [record.get("tool") or record.get("name") for record in final["trace"]] == ["calendar_setup_help"]
    assert any(event["type"] == "reset" for event in events)


async def test_a_second_invented_address_is_withheld(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path,
        [
            {"content": f"Enter {FAKE} in the app."},
            {"content": f"Enter {FAKE} in the app."},
        ],
    )
    events = await run(agent, "what was that address again?")
    final = [event for event in events if event["type"] == "done"][0]
    assert final["message"] == f"Enter {URL_WITHHELD} in the app."
    assert FAKE not in "".join(event.get("text", "") for event in events if event["type"] == "token") or any(
        event["type"] == "reset" for event in events
    )


async def test_an_address_from_a_tool_result_passes(tmp_path) -> None:
    agent, tools, seen = agent_for(
        tmp_path,
        [call("calendar_setup_help", app="apple"), {"content": f"Type this address exactly: {FEED}"}],
    )
    events = await run(agent, "I use Apple")
    final = [event for event in events if event["type"] == "done"][0]
    assert final["message"] == f"Type this address exactly: {FEED}"
    assert len(seen) == 2


async def test_an_address_the_user_gave_passes(tmp_path) -> None:
    agent, tools, seen = agent_for(tmp_path, [{"content": "Got it, https://example.com/x is saved."}])
    events = await run(agent, "remember https://example.com/x for me")
    final = [event for event in events if event["type"] == "done"][0]
    assert "https://example.com/x" in final["message"]
