"""Where Friday's thinking happens: the local model, or a cloud API the
household chose in Settings.

The agent speaks the OpenAI chat-completions shape throughout (Ollama,
OpenAI and OpenRouter all accept it as-is). Anthropic's Messages API is a
different shape, so this module translates a turn into it and streams the
answer back as the same ("token" | "tool_start" | "message", data) pieces
the agent already consumes.

Cloud providers see everything said to Friday. The UI says so before a key
can be saved; nothing here goes out unless the household picked it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

LOCAL = "local"
OPENAI = "openai"
ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
# Anything else that speaks the OpenAI chat-completions API: an agent
# gateway, Hermes on Nous Portal, LM Studio or vLLM on another PC. Its
# address is a setting, not a constant.
CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    base_url: str
    key_env: str | None
    # Suggestions for the model box; the provider's own list is fetched live.
    suggested: tuple[str, ...] = ()
    # Cloud models get a larger history window than the local one: they have
    # it to spare, and prompt caching keeps the repeat cost low.
    context_tokens: int = 64000


PROVIDERS: dict[str, Provider] = {
    LOCAL: Provider(LOCAL, "This computer (Ollama)", "", None),
    OPENAI: Provider(OPENAI, "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ANTHROPIC: Provider(
        ANTHROPIC,
        "Anthropic",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        suggested=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    ),
    OPENROUTER: Provider(OPENROUTER, "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    CUSTOM: Provider(CUSTOM, "Custom endpoint", "", "CUSTOM_LLM_API_KEY", context_tokens=32768),
}


@dataclass(frozen=True, slots=True)
class LocalModel:
    """A model worth running Friday on, offered by the installer and by
    Settings > Assistant model. Each is built in Ollama as its own house-*
    model: the base with a 32k-token window, since Ollama's default is too
    small for Friday's prompt, tools and history and silently cuts the front."""

    name: str
    base: str
    size_gb: float
    needs: str
    note: str


LOCAL_CONTEXT = 32768
LOCAL_MODELS: tuple[LocalModel, ...] = (
    LocalModel("house-qwen", "qwen3:14b", 9.3, "12 GB+ GPU or 24 GB+ Mac", "best balance"),
    LocalModel("house-qwen-8b", "qwen3:8b", 5.2, "8 GB+ GPU or 16 GB+ Mac", "smaller and quicker"),
    LocalModel("house-qwen3.6", "qwen3.6:35b-a3b", 22, "24 GB+ GPU or 32 GB+ Mac", "newer, quick for its size (mixture of experts)"),
    LocalModel("house-qwen3.8", "qwen3.8:27b", 17, "20 GB+ GPU or 32 GB+ Mac", "newest and sharpest, a bit slow"),
    LocalModel("house-gpt", "gpt-oss:20b", 13, "16 GB+ GPU or 32 GB+ Mac", "OpenAI's open model"),
    LocalModel("house-qwen-32b", "qwen3:32b", 20, "24 GB+ GPU or 48 GB+ Mac", "sharp but slow"),
)


def local_model(name: str) -> LocalModel | None:
    name = name.removesuffix(":latest")
    return next((m for m in LOCAL_MODELS if name in (m.name, m.base)), None)


def is_cloud(provider: str) -> bool:
    return provider != LOCAL


# ---------------------------------------------------------------- Anthropic

def _tools_for_anthropic(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = []
    for schema in schemas:
        function = schema.get("function") or schema
        tools.append(
            {
                "name": function["name"],
                "description": function.get("description") or "",
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return tools


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split an OpenAI-shaped turn into Anthropic's (system, messages).

    The leading system messages (the prompt, the house line, the summary)
    become the top-level system, the first of them marked for caching. A
    system line later in the conversation (the clock, a nudge after a tool
    round) rides along as a text block on the user side, where every Claude
    model reads it; mid-conversation system roles are not accepted by all
    of them. Assistant turns keep their original content blocks when the
    agent carried them (thinking blocks must go back unchanged)."""
    system: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    pending_notes: list[str] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        text = str(messages[index].get("content") or "")
        if text:
            system.append({"type": "text", "text": text})
        index += 1
    if system:
        system[0]["cache_control"] = {"type": "ephemeral"}

    def user_blocks(blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == "user":
            content = out[-1]["content"]
            content.extend(blocks)
            # Tool results must lead the user turn that answers a tool call.
            content.sort(key=lambda block: block.get("type") != "tool_result")
        else:
            out.append({"role": "user", "content": blocks})

    def flush_notes() -> None:
        if pending_notes:
            user_blocks([{"type": "text", "text": "[House note]\n" + "\n".join(pending_notes)}])
            pending_notes.clear()

    for message in messages[index:]:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "")
            if text:
                pending_notes.append(text)
        elif role == "user":
            flush_notes()
            user_blocks([{"type": "text", "text": str(message.get("content") or "") or "(empty)"}])
        elif role == "tool":
            user_blocks(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": str(message.get("content") or ""),
                    }
                ]
            )
        elif role == "assistant":
            flush_notes()
            raw = message.get("_anthropic_content")
            if raw:
                blocks = list(raw)
            else:
                blocks = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": str(message["content"])})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id") or "",
                            "name": function.get("name") or "",
                            "input": arguments if isinstance(arguments, dict) else {},
                        }
                    )
            if not blocks:
                continue
            if out and out[-1]["role"] == "assistant":
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": "assistant", "content": blocks})
    flush_notes()
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    return system, out


def _thinking_options(model: str, effort: str) -> dict[str, Any]:
    """Home turns are short: the smallest effort that still thinks. Haiku
    4.5 predates effort and adaptive thinking, so it gets neither."""
    if "haiku" in model:
        return {}
    level = "low" if effort in {"none", "minimal", "low"} else "medium"
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}


async def anthropic_stream(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    allow_tools: bool,
    effort: str,
    timeout: float,
) -> AsyncIterator[tuple[str, Any]]:
    import anthropic

    system, converted = to_anthropic(messages)
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "system": system,
        "messages": converted,
        **_thinking_options(model, effort),
    }
    if tools:
        # Tools stay declared even on the closing round: history holds
        # tool_use blocks, which need their definitions.
        request["tools"] = _tools_for_anthropic(tools)
        request["tool_choice"] = {"type": "auto"} if allow_tools else {"type": "none"}
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)
    try:
        async with client.messages.stream(**request) as stream:
            async for event in stream:
                if event.type == "text":
                    yield "token", event.text
                elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                    yield "tool_start", event.content_block.name
            final = await stream.get_final_message()
    finally:
        await client.close()
    if final.stop_reason == "refusal":
        log.warning("anthropic: %s declined the request", model)
    text = "".join(block.text for block in final.content if block.type == "text")
    calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {"name": block.name, "arguments": json.dumps(block.input)},
        }
        for block in final.content
        if block.type == "tool_use"
    ]
    assembled: dict[str, Any] = {
        "role": "assistant",
        "content": text or None,
        # Replayed as-is on the next round of this turn; never persisted.
        "_anthropic_content": [block.model_dump(exclude_none=True) for block in final.content],
    }
    if final.stop_reason == "refusal" and not text and not calls:
        assembled["content"] = "I can't help with that one."
    if calls:
        assembled["tool_calls"] = calls
    yield "message", assembled


async def anthropic_text(*, api_key: str, model: str, prompt: str, max_tokens: int, timeout: float) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max(max_tokens, 1024),
            messages=[{"role": "user", "content": prompt}],
            **_thinking_options(model, "none"),
        ) as stream:
            final = await stream.get_final_message()
    finally:
        await client.close()
    return "".join(block.text for block in final.content if block.type == "text")


def strip_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The OpenAI shape without this module's bookkeeping keys."""
    return [
        {key: value for key, value in message.items() if not key.startswith("_")}
        if any(key.startswith("_") for key in message)
        else message
        for message in messages
    ]
