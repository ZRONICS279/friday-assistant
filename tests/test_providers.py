"""Cloud models chosen in Settings: the Anthropic translation, the switch,
the fall-back to the local model, and the consent the API insists on."""

import httpx
import numpy as np

from house_agent import providers
from house_agent.api import create_app
from house_agent.config import AppConfig, LLMConfig, ServerConfig, StorageConfig, VoiceConfig
from house_agent.db import Database
from house_agent.llm import LocalAgent
from house_agent.tools import ToolRegistry
from house_agent.voice import VoiceIdentity


def test_a_turn_translates_into_anthropic_shape() -> None:
    messages = [
        {"role": "system", "content": "You are Friday."},
        {"role": "system", "content": "Smart devices: bedroom lamp."},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "system", "content": "Now: Wednesday 21:00."},
        {"role": "user", "content": "turn off the lamp"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "function": {"name": "device_set", "arguments": '{"device": "lamp", "power": "off"}'}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"ok": true}'},
        {"role": "system", "content": "Reply in one sentence."},
    ]
    system, converted = providers.to_anthropic(messages)
    assert [block["text"] for block in system] == ["You are Friday.", "Smart devices: bedroom lamp."]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in converted] == ["user", "assistant", "user", "assistant", "user"]
    # The clock note travels with the user's words, before them.
    assert converted[2]["content"][0]["text"].startswith("[House note]\nNow: Wednesday")
    assert converted[2]["content"][1]["text"] == "turn off the lamp"
    assert converted[3]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "device_set", "input": {"device": "lamp", "power": "off"}}
    ]
    # Tool results lead the user turn; a later note comes after them.
    assert converted[4]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": '{"ok": true}'}
    assert "Reply in one sentence." in converted[4]["content"][1]["text"]


def test_an_assistant_turn_with_thinking_goes_back_unchanged() -> None:
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]
    _, converted = providers.to_anthropic(
        [{"role": "system", "content": "p"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": None, "_anthropic_content": raw}]
    )
    assert converted[1]["content"] == raw
    assert providers.strip_private([{"role": "assistant", "content": "a", "_anthropic_content": raw}]) == [
        {"role": "assistant", "content": "a"}
    ]


def _agent(tmp_path):
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "agent.db"),
        voice=VoiceConfig(),
        llm=LLMConfig(think_complex=False, model="house-qwen"),
    )
    database = Database(config.storage.database_path)
    database.initialize()
    tools = ToolRegistry(config, database)
    tools.voices = VoiceIdentity(config.voice, database, embedder=lambda wav: np.ones(8))
    return LocalAgent(config.llm, database, tools)


def test_switching_models_and_back(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.use_model("openai", "gpt-5", "sk-test")
    assert agent.config.base_url == "https://api.openai.com/v1" and agent.config.api_key == "sk-test"
    assert agent.context_tokens == 64000
    options = agent._request_options("none", 1200)
    assert "temperature" not in options and options["max_completion_tokens"] >= 1200
    agent.use_model("local", "house-gpt")
    assert agent.config.base_url == agent.local_config.base_url and agent.config.model == "house-gpt"


async def test_a_failing_cloud_model_hands_the_round_to_the_local_one(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path)
    agent.use_model("anthropic", "claude-sonnet-5", "bad-key")

    async def broken(**_):
        raise RuntimeError("401 invalid x-api-key")
        yield  # pragma: no cover - makes this an async generator

    local_calls = []

    async def local(client, messages, effort, allow_tools=True):
        local_calls.append(agent.config.model)
        yield "message", {"role": "assistant", "content": "local answer"}

    monkeypatch.setattr(providers, "anthropic_stream", broken)
    agent._stream_openai = local
    pieces = [piece async for piece in agent._stream_completion(None, [{"role": "user", "content": "hi"}])]
    assert pieces == [("message", {"role": "assistant", "content": "local answer"})]
    assert local_calls == ["house-qwen"]
    assert "401" in agent.fell_back
    # The choice itself is kept: the next turn tries the cloud again.
    assert agent.provider == "anthropic" and agent.config.model == "claude-sonnet-5"


async def test_switching_to_a_cloud_model_needs_consent_and_a_working_key(tmp_path) -> None:
    config = AppConfig(
        storage=StorageConfig(database_path=tmp_path / "api.db"),
        server=ServerConfig(api_token="abc123"),
        config_path=tmp_path / "config.toml",
    )
    app = create_app(config)
    headers = {"Authorization": "Bearer abc123"}
    tested = []

    async with app.router.lifespan_context(app):
        async def fake_test(provider, model, key=None, base_url=None):
            tested.append((provider, model, key))
            return "OK"

        assistant = app.state.assistant_model
        assistant.test = fake_test
        unloaded = []

        async def fake_unload(model):
            unloaded.append(model)

        assistant.unload = fake_unload
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            body = {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant-x"}
            refused = await client.put("/api/assistant-model", json=body, headers=headers)
            assert refused.status_code == 400 and "Confirm" in refused.json()["detail"]
            assert tested == []

            switched = await client.put("/api/assistant-model", json={**body, "accept_cloud": True}, headers=headers)
            assert switched.status_code == 200, switched.text
            assert switched.json()["provider"] == "anthropic" and switched.json()["cloud"] is True
            assert "sk-ant-x" not in switched.text  # the key never comes back
            assert "ANTHROPIC_API_KEY=" in (tmp_path / ".env").read_text()
            assert unloaded == [app.state.config.llm.model]  # the local model leaves the GPU

            health = (await client.get("/api/health", headers=headers)).json()
            assert health["llm_cloud"] is True and health["llm_label"] == "Anthropic"

            forgot = await client.delete("/api/assistant-model/key?provider=anthropic", headers=headers)
            assert forgot.json()["provider"] == "local"


async def test_downloading_an_offered_model_pulls_then_builds_it(tmp_path, monkeypatch) -> None:
    import asyncio
    import json

    from house_agent.assistant_model import AssistantModel
    from house_agent.settings import HouseSettings

    agent = _agent(tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/api/pull":
            lines = [{"status": "pulling", "total": 100, "completed": 40}, {"status": "success"}]
            return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))
        return httpx.Response(200, json={"status": "success"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "house_agent.assistant_model.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    model = AssistantModel(agent, agent.database, HouseSettings(tmp_path / "config.toml"))
    assert any(m["base"] == "qwen3.6:35b-a3b" for m in model.status()["local_catalog"])
    model.start_download("qwen3.6:35b-a3b")
    await model._download_task
    assert model.download["status"] == "done" and model.download["name"] == "house-qwen3.6"
    assert calls[0] == ("/api/pull", {"model": "qwen3.6:35b-a3b", "stream": True})
    assert calls[1][1]["from"] == "qwen3.6:35b-a3b" and calls[1][1]["parameters"] == {"num_ctx": 32768}
    await asyncio.sleep(0)


async def test_a_custom_endpoint_must_call_tools(tmp_path, monkeypatch) -> None:
    import json

    import pytest

    from house_agent.assistant_model import AssistantModel
    from house_agent.settings import HouseSettings

    agent = _agent(tmp_path)
    tool_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        message = {"role": "assistant", "content": "OK"}
        if body.get("tools"):
            message["tool_calls"] = tool_calls
        return httpx.Response(200, json={"choices": [{"message": message}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "house_agent.assistant_model.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    model = AssistantModel(agent, agent.database, HouseSettings(tmp_path / "config.toml"))
    with pytest.raises(RuntimeError, match="didn't call a tool"):
        await model.test("custom", "hermes-4", None, "http://192.168.0.20:8000/v1")
    tool_calls.append({"id": "1", "type": "function", "function": {"name": "ping", "arguments": "{}"}})
    assert await model.test("custom", "hermes-4", None, "http://192.168.0.20:8000/v1") == "OK"
    model.choose("custom", "hermes-4", "http://192.168.0.20:8000/v1/")
    assert agent.config.base_url == "http://192.168.0.20:8000/v1" and agent.provider == "custom"
    # Survives a restart without any key.
    agent.use_model("local", "house-qwen")
    model.apply_saved()
    assert agent.provider == "custom" and agent.config.model == "hermes-4"


async def test_a_raw_local_model_runs_as_its_house_build(tmp_path, monkeypatch) -> None:
    import json

    from house_agent.assistant_model import AssistantModel, house_name
    from house_agent.settings import HouseSettings

    agent = _agent(tmp_path)
    windows = {"qwen3:14b": "temperature 0.6", "llama3.1:8b": "", "house-qwen": "num_ctx 32768"}
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            if body["model"] not in windows:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"parameters": windows[body["model"]]})
        created.append(body)
        return httpx.Response(200, json={"status": "success"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "house_agent.assistant_model.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    model = AssistantModel(agent, agent.database, HouseSettings(tmp_path / "config.toml"))
    # An offered base becomes its catalog name, so it matches house-qwen exactly.
    assert await model.house_build("qwen3:14b") == "house-qwen"
    assert created[-1] == {"model": "house-qwen", "from": "qwen3:14b", "parameters": {"num_ctx": 32768}, "stream": False}
    # Anything else gets house- plus its own name.
    assert await model.house_build("llama3.1:8b") == "house-llama3.1-8b"
    # Already big enough, or unknown to Ollama: used as it is.
    count = len(created)
    assert await model.house_build("house-qwen") == "house-qwen"
    assert await model.house_build("missing:1b") == "missing:1b"
    assert len(created) == count
    assert house_name("hf.co/Org/Some_Model:Q4_K_M") == "house-some_model-q4_k_m"

    agent.use_model("local", "qwen3:14b")
    await model.settle_local()
    assert agent.config.model == "house-qwen"
