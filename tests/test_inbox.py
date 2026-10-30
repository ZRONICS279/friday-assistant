"""The messages panel: what Friday puts on the screen instead of saying,
one board per chat, and the DoorDash sign-in that rides on it."""

import asyncio
import stat

import pytest

from house_agent.config import AppConfig, DoorDashConfig, LLMConfig, StorageConfig
from house_agent.db import Database
from house_agent.doordash import DoorDashError, DoorDashNotSignedIn, _callback_params, _login_failure
from house_agent.llm import LocalAgent
from house_agent.tools import CURRENT_SPEAKER, ToolRegistry

LOGIN_URL = (
    "https://identity.doordash.com/authorize?client_id=186&response_type=code"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A4180%2Foauth2%2Fcallback&state=abc123&code_challenge=zzz"
)
FRESH_URL = LOGIN_URL.replace("abc123", "fresh9")


class FakeAnnouncer:
    def __init__(self) -> None:
        self.events = []

    def publish(self, text, **extra):
        self.events.append({"text": text, **extra})
        return self.events[-1]


class FakeProcess:
    def __init__(self) -> None:
        self.stderr = asyncio.StreamReader()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.returncode = None
        self._exit = asyncio.Event()
        self.killed = False

    def say(self, line: str) -> None:
        self.stderr.feed_data((line + "\n").encode())

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stderr.feed_eof()
        self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)


def registry(tmp_path) -> ToolRegistry:
    binary = tmp_path / "dd-cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "tools.db"),
        doordash=DoorDashConfig(binary=binary, timeout_seconds=5),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.announcer = FakeAnnouncer()
    return tools


def speaking_in(conversation_id: str) -> None:
    CURRENT_SPEAKER.set({"status": "identified", "profile": "Alex", "conversation_id": conversation_id})


def spawn_login(tools, process: FakeProcess) -> list:
    spawned = []

    async def spawn(*argv, **kwargs):
        spawned.append((argv, kwargs))
        return process

    tools.doordash._spawn = spawn
    return spawned


# --------------------------------------------------------------- the board

def test_each_chat_has_its_own_board(tmp_path) -> None:
    database = Database(tmp_path / "inbox.db")
    database.initialize()
    first = database.add_inbox_message("Hotel link", "Book here", "https://example.com/book", "link", "c1")
    second = database.add_inbox_message("  Calendar   steps ", "1. Open Settings\n2. Add calendar", conversation_id="c2")
    assert first["read"] is False and first["url"] == "https://example.com/book"
    assert second["title"] == "Calendar steps" and second["kind"] == "note" and second["url"] is None
    assert [m["id"] for m in database.list_inbox("c1")] == [first["id"]]
    assert [m["id"] for m in database.list_inbox("c2")] == [second["id"]]
    assert database.list_inbox("c3") == []
    assert database.unread_inbox_count("c1") == 1 and database.unread_inbox_count("c3") == 0
    # Reading one chat's board leaves the other's badge alone.
    assert database.mark_inbox_read("c1") == 0
    assert database.unread_inbox_count("c2") == 1
    assert database.mark_inbox_read("c2", [second["id"]]) == 0
    with pytest.raises(ValueError):
        database.add_inbox_message("   ")


def test_inbox_send_is_always_offered_and_explained(tmp_path) -> None:
    tools = registry(tmp_path)
    names = {s["function"]["name"] for s in tools.schemas}
    assert "inbox_send" in names and "doordash_login" in names
    agent = LocalAgent(LLMConfig(), tools.database, tools)
    assert "inbox_send puts something on the user's screen" in agent.system_prompt
    assert "never read a link aloud" in agent.system_prompt
    assert "never tell them to run a command" in agent.system_prompt


async def test_inbox_send_lands_on_this_chats_board_and_lights_its_badge(tmp_path) -> None:
    tools = registry(tmp_path)
    speaking_in("c1")
    result = await tools.execute(
        "inbox_send",
        {"title": "Hotel booking link", "text": "Best rate for the 12th.", "url": "https://letsfg.co/book/1"},
    )
    assert result["ok"] and result["unread"] == 1 and result["sent"]["kind"] == "link"
    assert "do not read the link" in result["note"]
    event = tools.announcer.events[-1]
    assert event["type"] == "inbox" and event["source"] == "inbox"
    assert event["conversation_id"] == "c1"  # this chat's screens only
    assert event["message"]["title"] == "Hotel booking link" and event["unread"] == 1
    assert tools.database.list_inbox("c1")[0]["body"] == "Best rate for the 12th."
    assert tools.database.list_inbox("c2") == []


async def test_inbox_send_refuses_empty_or_malformed_messages(tmp_path) -> None:
    tools = registry(tmp_path)
    speaking_in("c1")
    assert (await tools.execute("inbox_send", {"title": ""}))["ok"] is False
    assert (await tools.execute("inbox_send", {"title": "x"}))["ok"] is False
    bad = await tools.execute("inbox_send", {"title": "x", "url": "www.example.com"})
    assert bad["ok"] is False and "http" in bad["error"]
    assert tools.database.unread_inbox_count("c1") == 0


# ------------------------------------------------------------- signing in

def test_callback_parsing_takes_a_full_address_or_a_bare_query() -> None:
    assert _callback_params("http://localhost:4180/oauth2/callback?code=AbC-1&state=abc123") == ("AbC-1", "abc123")
    assert _callback_params("code=xyz&state=s1") == ("xyz", "s1")
    assert _callback_params("here it is: http://localhost:4180/oauth2/callback?state=s2&code=q.9") == ("q.9", "s2")
    assert _callback_params("https://www.doordash.com/home") == (None, None)


def test_a_failed_sign_in_is_described_without_the_link() -> None:
    assert _login_failure(["Listening for OAuth callback on http://localhost:4180/oauth2/callback",
                           "Open in browser: " + LOGIN_URL]) == "the sign-in window closed before it was completed"
    keyring = _login_failure(["Open in browser: " + LOGIN_URL, "Error: keyring backend unavailable"])
    assert keyring.startswith("Error: keyring backend unavailable") and "export-token" in keyring
    assert LOGIN_URL not in keyring


async def test_a_signed_out_request_runs_the_sign_in_and_puts_doordashs_link_on_the_board(tmp_path) -> None:
    tools = registry(tmp_path)
    process = FakeProcess()
    spawned = spawn_login(tools, process)
    process.say("Listening for OAuth callback on http://localhost:4180/oauth2/callback")
    process.say("If the browser does not open, visit:")
    process.say("  " + LOGIN_URL)
    speaking_in("c1")

    async def runner(argv, env, timeout):
        return 1, b"", b"Error: Failed to execute command due to missing credentials. Sign in with dd-cli login"

    tools.doordash._runner = runner
    result = await tools.execute("doordash_search", {"query": "thai"})
    assert result["ok"] is False and result["needs_setup"] is True and result["sign_in_link_sent"] is True
    assert "Never tell them to run a command" in result["note"]
    assert "dd-cli" not in result["error"]
    board = tools.database.list_inbox("c1")
    assert len(board) == 1 and board[0]["url"] == LOGIN_URL and board[0]["title"] == "Sign in to DoorDash"
    assert "paste it into Friday's chat" in board[0]["body"] and "five minutes" in board[0]["body"]
    argv, kwargs = spawned[0]
    assert argv[1] == "login" and kwargs["env"]["BROWSER"] == "/bin/true" and kwargs["start_new_session"] is True
    # Asking again while still signed out reuses the waiting attempt: no second card.
    await tools.execute("doordash_order_history", {})
    assert len(tools.database.list_inbox("c1")) == 1 and len(spawned) == 1
    # A new chat gets its own card, with the same waiting link.
    speaking_in("c2")
    await tools.execute("doordash_search", {"query": "pizza"})
    assert [m["url"] for m in tools.database.list_inbox("c2")] == [LOGIN_URL]
    assert len(tools.database.list_inbox("c1")) == 1 and len(spawned) == 1


async def test_asking_to_sign_in_runs_dd_cli_and_the_pc_finishing_it_is_announced(tmp_path) -> None:
    tools = registry(tmp_path)
    process = FakeProcess()
    spawned = spawn_login(tools, process)
    speaking_in("c1")
    process.say("Opening browser for DoorDash Identity sign-in…")
    process.say("Open in browser: " + LOGIN_URL)
    result = await tools.execute("doordash_login", {})
    assert result["ok"] is True and result["sign_in_link_sent"] is True
    assert "envelope icon" in result["note"] and "http://localhost:4180" in result["note"]
    assert tools.database.list_inbox("c1")[0]["url"] == LOGIN_URL
    assert len(spawned) == 1 and tools.doordash.login_in_progress()
    # Asking twice does not spawn twice or add a card.
    again = await tools.execute("doordash_login", {})
    assert again["ok"] and "already" in again["note"]
    assert len(spawned) == 1 and len(tools.database.list_inbox("c1")) == 1
    # The browser on the house PC reached localhost:4180 itself; dd-cli exits happy.
    process.exit(0)
    await asyncio.sleep(0.05)
    said = [e for e in tools.announcer.events if e.get("source") == "doordash"]
    assert said[-1]["text"] == "DoorDash is signed in now — ordering works."
    assert said[-1]["conversation_id"] is None
    assert not tools.doordash.login_in_progress()
    notes = [m["content"] for m in tools.database.get_messages("c1") if "sign-in completed" in m["content"]]
    assert notes


async def test_an_unused_link_is_renewed_in_the_same_card_quietly(tmp_path) -> None:
    from house_agent import doordash as module

    tools = registry(tmp_path)
    first, second = FakeProcess(), FakeProcess()
    processes = iter([first, second])

    async def spawn(*argv, **kwargs):
        return next(processes)

    tools.doordash._spawn = spawn
    speaking_in("c1")
    first.say(LOGIN_URL)
    await tools.execute("doordash_login", {})
    card = tools.database.list_inbox("c1")[0]
    # dd-cli's five minutes run out: it exits 1 with nothing signed in.
    tools.doordash._login["started"] -= module._DD_CLI_LOGIN_WINDOW
    second.say(FRESH_URL)
    first.say("Error: Authentication/Authorization failed")
    first.exit(1)
    await asyncio.sleep(0.1)
    renewed = tools.database.list_inbox("c1")
    assert len(renewed) == 1 and renewed[0]["id"] == card["id"] and renewed[0]["url"] == FRESH_URL
    assert tools.doordash.login_in_progress() and tools.doordash.login_url() == FRESH_URL
    pushed = [e for e in tools.announcer.events if e.get("type") == "inbox"]
    assert pushed[-1]["updated"] is True and pushed[-1]["message"]["url"] == FRESH_URL
    assert pushed[-1]["conversation_id"] == "c1"
    # Nothing spoken about it: the house was not waiting on that link.
    assert not any(e.get("source") == "doordash" for e in tools.announcer.events)


async def test_renewal_stops_after_half_an_hour(tmp_path) -> None:
    from house_agent import doordash as module

    tools = registry(tmp_path)
    process = FakeProcess()
    spawned = spawn_login(tools, process)
    speaking_in("c1")
    process.say(LOGIN_URL)
    await tools.execute("doordash_login", {})
    tools._login_offer["since"] -= tools._LOGIN_REFRESH_SECONDS + 1
    tools.doordash._login["started"] -= module._DD_CLI_LOGIN_WINDOW
    process.exit(1)
    await asyncio.sleep(0.1)
    assert len(spawned) == 1 and not tools.doordash.login_in_progress()
    assert tools._login_offer is None
    notes = [m["content"] for m in tools.database.get_messages("c1") if "expired" in m["content"]]
    assert notes


async def test_a_pasted_callback_is_delivered_to_dd_cli_and_the_reply_covers_it(tmp_path) -> None:
    tools = registry(tmp_path)
    process = FakeProcess()
    spawn_login(tools, process)
    fetched = []

    async def http_get(url):
        fetched.append(url)
        process.exit(0)

    tools.doordash._http_get = http_get
    speaking_in("c1")
    process.say(LOGIN_URL)
    await tools.execute("doordash_login", {})
    wrong = await tools.execute("doordash_login", {"callback_url": "http://localhost:4180/oauth2/callback?code=c&state=other"})
    assert wrong["ok"] is False and "earlier sign-in attempt" in wrong["error"]
    result = await tools.execute(
        "doordash_login", {"callback_url": "http://localhost:4180/oauth2/callback?code=the-code&state=abc123"}
    )
    assert result == {"ok": True, "signed_in": True, "message": "DoorDash is signed in."}
    assert fetched == ["http://localhost:4180/oauth2/callback?code=the-code&state=abc123"]
    await asyncio.sleep(0.05)
    assert not any(e.get("source") == "doordash" for e in tools.announcer.events)
    nothing_waiting = await tools.execute("doordash_login", {"callback_url": "code=x&state=abc123"})
    assert nothing_waiting["ok"] is True and nothing_waiting["signed_in"] is True


async def test_a_login_that_never_shows_a_link_is_an_error_not_a_hang(tmp_path) -> None:
    from house_agent import doordash as module

    tools = registry(tmp_path)
    process = FakeProcess()
    spawn_login(tools, process)
    module_wait = module._LOGIN_URL_WAIT
    module._LOGIN_URL_WAIT = 0.05
    try:
        process.say("Error: keyring backend unavailable")
        process.exit(1)
        speaking_in("c1")
        result = await tools.execute("doordash_login", {})
    finally:
        module._LOGIN_URL_WAIT = module_wait
    assert result["ok"] is False and "keyring backend unavailable" in result["error"]
    assert not tools.doordash.login_in_progress() and tools.database.list_inbox("c1") == []


async def test_finishing_without_a_login_waiting_says_so(tmp_path) -> None:
    tools = registry(tmp_path)
    result = await tools.execute("doordash_login", {"callback_url": "code=a&state=b"})
    assert result["ok"] is False and "start one with doordash_login" in result["error"]


async def test_close_abandons_a_waiting_login(tmp_path) -> None:
    tools = registry(tmp_path)
    process = FakeProcess()
    spawn_login(tools, process)
    speaking_in("c1")
    process.say(LOGIN_URL)
    await tools.execute("doordash_login", {})
    await tools.close()
    assert process.killed and not tools.doordash.login_in_progress()
    await asyncio.sleep(0.05)
    assert not any(e.get("source") == "doordash" for e in tools.announcer.events)


async def test_not_signed_in_is_raised_as_its_own_kind(tmp_path) -> None:
    tools = registry(tmp_path)

    async def runner(argv, env, timeout):
        return 1, b"", b"Error: missing credentials. Sign in with dd-cli login"

    tools.doordash._runner = runner
    with pytest.raises(DoorDashNotSignedIn):
        await tools.doordash.addresses()
