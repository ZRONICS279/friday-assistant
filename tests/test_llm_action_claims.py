"""A reply may not report work that no tool did.

The real failure: "Turn it off, please" -> "Done. Your bedroom lamp is now
off." with no device_set call, then "Let me check that for you." with no
tool either. The prompt already forbids it; this makes it mechanical.
"""

import json

import numpy as np

from house_agent.config import AppConfig, LLMConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import NOTHING_DONE, LocalAgent, claims_action
from house_agent.tools import ToolRegistry
from house_agent.voice import VoiceIdentity


def agent_for(tmp_path, script):
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "agent.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(think_complex=False),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    agent = LocalAgent(config.llm, database, tools)
    seen: list[list[dict]] = []

    async def fake_stream(client, messages, effort, allow_tools=True):
        seen.append(list(messages))
        reply = script[len(seen) - 1]
        for piece in reply.get("content", "").split(" "):
            if piece:
                yield "token", piece + " "
        yield "message", reply

    agent._stream_completion = fake_stream
    return agent, tools, seen


def call(tool, **arguments):
    return {"tool_calls": [{"id": "1", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}


async def run(agent, message, conversation_id="c1"):
    events = []
    async for event in agent.chat_stream(message, conversation_id):
        events.append(event)
    return events


def test_what_counts_as_claiming_an_action() -> None:
    for reply in (
        "Done. Your bedroom lamp is now off.",
        "Done.",
        "I've turned it off.",
        "Let me check that for you.",
        "I'll take a look.",
        "Listo, apagado.",
        # Verbatim hallucinations from the 2026-09-03 zigbee session:
        "The bedroom light is now set to warm white.",
        "The bedroom light is now dimmed.",
        "The bedroom light has been successfully paired and is now under your control.",
        "I've successfully detected your Zigbee light!",
        "I've opened the Zigbee join window.",
        "I just flashed the first light. Did you see it?",
        "I'll flash the light again. Please wait a moment.",
    ):
        assert claims_action(reply), reply
    for reply in (
        "It's 59 degrees and clear.",
        "A few gentle mysteries:\n**Rebecca** (1938)",
        "Let me know if you want more.",
        "I can't reach the lamp right now.",
    ):
        assert not claims_action(reply), reply


async def test_a_tool_less_done_is_sent_back_for_the_real_call(tmp_path) -> None:
    script = [
        {"content": "Done. Your bedroom lamp is now off."},  # the lie
        call("device_list"),  # after the nudge: an actual tool call
        {"content": "Done."},
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Turn it off, please")
    done = events[-1]
    assert [record["tool"] for record in done["trace"]] == ["device_list"]
    assert done["message"] == "Done."
    # The streamed lie was withdrawn from the client before the retry.
    assert any(event["type"] == "reset" for event in events)
    # The nudge went in as a system line right after the false reply.
    assert seen[1][-1]["role"] == "system" and "no tool" in seen[1][-1]["content"]
    assert seen[1][-2]["content"] == "Done. Your bedroom lamp is now off."


async def test_a_second_tool_less_claim_becomes_an_honest_answer(tmp_path) -> None:
    script = [
        {"content": "Done. It's off now."},  # the lie: nudged, discarded
        {"content": "Let me check that for you."},  # a promise: continued
        {"content": "Let me check that for you."},  # promised again: continued
        {"content": "Let me check that for you."},  # and again: last continuation
        {"content": "Let me check that for you."},  # still no tool: give up
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Turn it off")
    assert len(seen) == 5  # nudged, continued three times, then cut off — not forever
    assert events[-1]["message"] == NOTHING_DONE
    assert events[-1]["trace"] == []


async def test_an_ordinary_answer_needs_no_tool(tmp_path) -> None:
    agent, tools, seen = agent_for(tmp_path, [{"content": "Hello! How can I help?"}])
    events = await run(agent, "Hi Friday")
    assert len(seen) == 1 and events[-1]["message"] == "Hello! How can I help?"


async def test_done_after_a_real_tool_is_accepted(tmp_path) -> None:
    agent, tools, seen = agent_for(tmp_path, [call("device_list"), {"content": "Done."}])
    events = await run(agent, "List my devices")
    assert len(seen) == 2 and events[-1]["message"] == "Done."


async def test_an_empty_reply_with_no_tool_is_not_called_done(tmp_path) -> None:
    # Each empty reply is first retried with reasoning off (an existing
    # safeguard), so four empties cover: empty, retry, nudge, retry.
    agent, tools, seen = agent_for(tmp_path, [{"content": ""}] * 4)
    events = await run(agent, "Turn off the lamp")
    assert events[-1]["message"] == NOTHING_DONE


def test_repeats_last_reply_matching() -> None:
    from house_agent.llm import repeats_last_reply

    history = [
        {"role": "user", "content": "It looks great. Thank you."},
        {"role": "assistant", "content": "You're welcome! If you need anything else, just let me know. 😊"},
    ]
    assert repeats_last_reply(
        "You're welcome!  If you need anything else, just let me know. 😊", history
    )
    assert not repeats_last_reply("Kitchen light is red now.", history)
    assert not repeats_last_reply("", history)
    assert not repeats_last_reply("Anything at all.", [])


async def test_a_verbatim_repeat_is_sent_back_to_answer_the_question(tmp_path) -> None:
    pleasantry = "You're welcome! If you need anything else, just let me know."
    agent, tools, seen = agent_for(tmp_path, [{"content": pleasantry}])
    await run(agent, "It looks great, thanks!")  # pleasantry lands in history

    script = [
        {"content": pleasantry},  # replayed verbatim to a new request
        {"content": "I can't reach the kitchen light right now."},
    ]
    agent2, tools2, seen2 = agent_for(tmp_path, script)
    events = await run(agent2, "Can you make the kitchen light red?")
    assert events[-1]["message"] == "I can't reach the kitchen light right now."
    assert any(event["type"] == "reset" for event in events)
    assert seen2[1][-1]["role"] == "system" and "repeat" in seen2[1][-1]["content"]


async def test_a_stubborn_repeat_is_let_through_not_falsified(tmp_path) -> None:
    pleasantry = "You're welcome! Anytime."
    agent, tools, seen = agent_for(tmp_path, [{"content": pleasantry}])
    await run(agent, "thanks")

    script = [{"content": pleasantry}, {"content": pleasantry}]  # repeats twice
    agent2, tools2, seen2 = agent_for(tmp_path, script)
    events = await run(agent2, "thanks again!")
    assert len(seen2) == 2  # nudged once, then accepted
    assert events[-1]["message"] == pleasantry  # not replaced with NOTHING_DONE


async def test_a_promise_is_kept_and_the_work_done_in_the_same_turn(tmp_path) -> None:
    """The 2026-09-03 stall: 'I found 3 new lights. I'll flash it so you can
    see which one it is.' — then the turn ended and nothing flashed until the
    user said 'ok, flash it'. Now the promise keeps the turn alive: the text
    is spoken, the model is sent back, and the flash happens unprompted."""
    script = [
        call("device_list"),
        {"content": "I found a new light. I'll flash it now."},
        call("device_flash", device="zigbee-0xaaa", times=3),
        {"content": "Did you see it blink?"},
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Set up my zigbee lights")
    done = events[-1]
    assert [record["tool"] for record in done["trace"]] == ["device_list", "device_flash"]
    # The interim promise stays part of the reply — never withdrawn.
    assert done["message"] == "I found a new light. I'll flash it now.\nDid you see it blink?"
    assert not any(event["type"] == "reset" for event in events)
    # The model was pointed at the exact tool it promised.
    nudge = seen[2][-1]
    assert nudge["role"] == "system" and "device_flash" in nudge["content"]


async def test_a_fulfilled_promise_ends_the_turn_normally(tmp_path) -> None:
    """'I'm flashing a light now — tell me where it is' right after a real
    device_flash call is the scripted, correct reply; no continuation."""
    script = [
        call("device_flash", device="zigbee-0xaaa"),
        {"content": "I'm flashing a light now — please tell me where it is."},
    ]
    agent, tools, seen = agent_for(tmp_path, script)

    async def flash_ok(name, arguments):
        return {"ok": True, "device": {"id": "zigbee-0xaaa"}}

    tools.execute = flash_ok
    events = await run(agent, "Flash the first light")
    assert len(seen) == 2  # no extra round: the promise was already kept
    assert events[-1]["message"] == "I'm flashing a light now — please tell me where it is."


def test_promised_tool_missing_matching() -> None:
    from house_agent.llm import promised_tool_missing

    flash_ran = [{"tool": "device_flash", "result": {"ok": True}}]
    flash_failed = [{"tool": "device_flash", "result": {"ok": False, "error": "no such light"}}]
    list_ran = [{"tool": "device_list", "result": {"devices": []}}]  # no ok key = fine
    search_ran = [{"tool": "catalog_search", "result": {"ok": True}}]
    # Verbatim from the 2026-09-03 sessions:
    assert promised_tool_missing("I'll flash it so you can see which physical light it is.", list_ran) == ("device_flash", "missing")
    assert promised_tool_missing("I'm flashing the next light now — please tell me where it is.", []) == ("device_flash", "missing")
    assert promised_tool_missing("Let's flash it so you can identify it. I'll flash it a few times now.", []) == ("device_flash", "missing")
    assert promised_tool_missing("I'll pair it now, then help you name it.", []) == ("device_pair", "missing")
    assert promised_tool_missing("I'm flashing a light now — please tell me where it is.", flash_ran) is None
    assert promised_tool_missing("Let me check the device list.", []) == ("device_list", "missing")
    assert promised_tool_missing("Let me check the device list.", list_ran) is None
    assert promised_tool_missing("Put the light in pairing mode now.", []) is None
    assert promised_tool_missing("Done. The lamp is off.", []) is None
    # A promise backed only by a FAILED call is describing work that did not happen.
    assert promised_tool_missing("I'm flashing a light now — please tell me where it is.", flash_failed) == ("device_flash", "failed")
    # Session 1c7065d1: 'Let me check what it's called.' after a failed flash —
    # the failed flash is not the promised check.
    assert promised_tool_missing("I see the new light is already named. Let me check what it's called.", flash_failed) == ("device_list", "missing")
    # Session 1c7065d1 #789: a rename that waits on the user's answer is a finished
    # reply, not a stall — forcing it onward made the model invent the answer.
    assert promised_tool_missing("Sure! Let me know the room or location where the light is, and I'll rename it accordingly.", []) is None
    assert promised_tool_missing("I'll rename it now.", []) == ("device_rename", "missing")
    # The stall that survived the first fix (2026-09-03, second session):
    assert (
        promised_tool_missing(
            "I can help with that! To add your new Zigbee lights, I'll need to "
            "open the join window on your home's Zigbee network. Let me do that now.",
            [],
        )
        == ("device_pair", "missing")
    )
    assert promised_tool_missing("Let me do that now.", []) == ("the tool that does it", "missing")
    assert promised_tool_missing("Let me do that now.", list_ran) is None
    # The content-side stall seen the same evening: announced "putting it on",
    # then only searched.
    assert (
        promised_tool_missing(
            "Masters of the Universe, the 2026 one — putting it on. "
            "Let me check if it's available on your subscriptions.",
            search_ran,
        )
        == ("content_play", "missing")
    )
    assert promised_tool_missing("Would you like me to put it on?", []) is None
    assert promised_tool_missing(
        "Putting it on.", [{"tool": "content_play", "result": {"ok": True}}]
    ) is None


async def test_a_check_promised_after_a_failed_tool_still_happens(tmp_path) -> None:
    """Session 1c7065d1 #787: device_flash failed (nothing had joined), the
    reply ended 'Let me check what it's called.' — and never came back. The
    failed call must not count as the promised check; the turn goes on to
    device_list and the answer arrives in the same breath."""
    script = [
        call("device_flash", device="last flashed", times=3),  # fails: nothing waiting
        {"content": "Let me check what it's called."},
        call("device_list"),
        {"content": "It's already set up as kitchen light one."},
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Okay, I've set them into pairing mode.")
    done = events[-1]
    assert [record["tool"] for record in done["trace"]] == ["device_flash", "device_list"]
    assert done["trace"][0]["result"]["ok"] is False
    assert done["message"] == (
        "Let me check what it's called.\nIt's already set up as kitchen light one."
    )
    nudge = seen[2][-1]
    assert nudge["role"] == "system" and "device_list" in nudge["content"]


async def test_a_promise_backed_only_by_a_failed_call_is_not_taken_as_done(tmp_path) -> None:
    script = [
        call("device_flash", device="zigbee-0xaaa"),  # fails: no such device here
        {"content": "I'm flashing a light now — please tell me where it is."},
        {"content": "I couldn't flash that light — it isn't reachable."},
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Flash the first light")
    nudge = seen[2][-1]
    assert nudge["role"] == "system" and "FAILED" in nudge["content"]
    assert events[-1]["message"].endswith("it isn't reachable.")


async def test_a_promise_conditioned_on_the_users_answer_waits(tmp_path) -> None:
    """Session 1c7065d1 #789: sent onward, the model answered its own
    question ('The light is in the bathroom. Let's call it...'). Asking the
    user for the room and name IS the finished reply."""
    script = [
        {
            "content": "Sure! Let me know the room and what you'd like to call it, "
            "and I'll rename it accordingly."
        }
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "And what was it called?")
    assert len(seen) == 1
    assert events[-1]["message"].startswith("Sure!")


async def test_the_verbatim_join_window_stall_now_continues(tmp_path) -> None:
    """Session 3228227a: 'I'll need to open the join window... Let me do
    that now.' ended the turn with no tool call, and the user had to say
    'Hello?'. The promise now keeps the turn alive through the pair call."""
    script = [
        {
            "content": "I can help with that! To add your new Zigbee lights, "
            "I'll need to open the join window on your home's Zigbee network. "
            "Let me do that now."
        },
        call("device_pair", network="zigbee"),
        {"content": "The window is open — put the light in pairing mode now."},
    ]
    agent, tools, seen = agent_for(tmp_path, script)
    events = await run(agent, "Hey Friday, I'm trying to set up some new Zigbee smart lights.")
    done = events[-1]
    assert [record["tool"] for record in done["trace"]] == ["device_pair"]
    assert done["message"].endswith("put the light in pairing mode now.")
    assert "Let me do that now." in done["message"]
    assert not any(event["type"] == "reset" for event in events)
    nudge = seen[1][-1]
    assert nudge["role"] == "system" and "device_pair" in nudge["content"]


def test_a_schedule_claimed_but_not_saved_is_caught() -> None:
    """Friday said "I'll set up the automation" after light_schedule_add
    failed, and later "is it all set" got a cheerful non-answer."""
    from house_agent.llm import promised_tool_missing

    failed = [{"tool": "light_schedule_add", "result": {"ok": False, "error": "ambiguous"}}]
    saved = [{"tool": "light_schedule_add", "result": {"ok": True}}]
    reply = "I'll set up the automation for the bedroom lights. Let me know if you'd like to adjust anything else."
    assert promised_tool_missing(reply, failed) == ("light_schedule_add", "failed")
    assert promised_tool_missing("Your automation is all set for 9:05 pm.", []) == ("light_schedule_add", "missing")
    assert promised_tool_missing("Done — they fade from 9:05 and are off at 9:10 every night.", saved) is None
    assert promised_tool_missing("I'll set up the new light now — what room is it in?", []) is None


def test_a_title_found_but_not_played_is_not_called_playing() -> None:
    """The exact failure: content_plan_playback found the film on Prime Video
    and the reply said "It's now playing. Enjoy!" with the TV untouched."""
    from house_agent.llm import promised_tool_missing

    planned = [{"tool": "content_plan_playback", "result": {"status": "ready"}}]
    reply = 'I found "The Love Hypothesis" on Prime Video. It\'s now playing. Enjoy!'
    assert promised_tool_missing(reply, planned) == ("content_play", "missing")
    assert promised_tool_missing("It's on Prime Video. Want me to put it on?", planned) is None
    played = [*planned, {"tool": "content_play", "result": {"ok": True}}]
    assert promised_tool_missing("Putting it on now. Enjoy!", played) is None


async def test_a_typed_turn_speaks_as_the_screens_person(tmp_path) -> None:
    from house_agent.tools import CURRENT_SPEAKER

    agent, tools, seen = agent_for(tmp_path, [{"content": "Hi Zac."}])
    events = [event async for event in agent.chat_stream("hi", "c1", spoken=False, typed_by="Zac")]
    system = "\n".join(m["content"] for m in seen[0] if m["role"] == "system")
    assert "Speaker: Zac (typing on their own screen)" in system
    assert CURRENT_SPEAKER.get()["profile"] == "Zac"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["speaker"]["name"] == "Zac"


def test_tv_control_claims_need_a_key_press() -> None:
    from house_agent.llm import promised_tool_missing

    # Said with nothing pressed, or with only a status read behind it.
    assert promised_tool_missing("Paused **Thor: The Dark World** on Disney+.", []) == ("tv_press", "missing")
    assert promised_tool_missing("Resumed **Thor** on Disney+. Enjoy!", []) == ("tv_press", "missing")
    status_only = [{"tool": "tv_get_status", "result": {"device": {}}}]
    assert promised_tool_missing("I've restarted it for you.", status_only) == ("tv_press", "missing")
    # Backed by a press, or just describing what the user did.
    assert promised_tool_missing("Paused **Thor**.", [{"tool": "tv_press", "result": {"ok": True}}]) is None
    assert promised_tool_missing("Great, glad it's paused now. Take your time!", []) is None


def test_restart_is_a_tv_request() -> None:
    from house_agent.routing import DOMAINS

    tv = next(d for d in DOMAINS if d.name == "tv")
    for said in ("Can you please restart it?", "start the movie over", "play it from the beginning"):
        assert tv.keywords.search(said), said
