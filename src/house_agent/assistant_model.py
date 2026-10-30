"""The household's choice of model, as set in Settings.

Which provider and model are in use is a database setting; API keys live in
.env beside the house's other secrets and are never sent back to a browser.
A choice is tested with a one-line request before it is switched to, so a
typo in a key cannot leave the house without a brain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import asdict
from typing import Any

import httpx

from . import providers
from .settings import HouseSettings

log = logging.getLogger(__name__)

PROVIDER_KEY = "llm.provider"
CUSTOM_URL_KEY = "llm.base_url.custom"


def house_name(model: str) -> str:
    """house- plus the model's name, in characters Ollama accepts:
    "llama3.1:8b" -> "house-llama3.1-8b"."""
    stem = re.sub(r"[^a-z0-9._-]+", "-", model.removesuffix(":latest").split("/")[-1].lower()).strip("-.")
    return f"house-{stem}" if not stem.startswith("house-") else stem


def _model_key(provider: str) -> str:
    return f"llm.model.{provider}"


class AssistantModel:
    def __init__(self, agent: Any, database: Any, settings: HouseSettings) -> None:
        self.agent = agent
        self.database = database
        self.settings = settings
        # One model download at a time, watched by Settings.
        self.download: dict[str, Any] = {}
        self._download_task: asyncio.Task | None = None

    # ---- keys ----------------------------------------------------------------
    def api_key(self, provider: str) -> str:
        env = providers.PROVIDERS[provider].key_env
        if not env:
            return ""
        return os.environ.get(env) or self.settings.env_value(env) or ""

    def save_key(self, provider: str, key: str) -> None:
        env = providers.PROVIDERS[provider].key_env
        if not env:
            return
        self.settings.set_env(env, key, secret=True)
        os.environ[env] = key

    def forget_key(self, provider: str) -> None:
        env = providers.PROVIDERS[provider].key_env
        if env:
            self.settings.unset_env(env)
            os.environ.pop(env, None)

    def base_url(self, provider: str) -> str:
        if provider == providers.CUSTOM:
            return (self.database.get_setting(CUSTOM_URL_KEY) or "").rstrip("/")
        if provider == providers.LOCAL:
            return self.agent.local_config.base_url.rstrip("/")
        return providers.PROVIDERS[provider].base_url

    # ---- the choice ---------------------------------------------------------
    def saved(self) -> tuple[str, str]:
        provider = self.database.get_setting(PROVIDER_KEY) or providers.LOCAL
        if provider not in providers.PROVIDERS:
            provider = providers.LOCAL
        model = self.database.get_setting(_model_key(provider)) or ""
        if provider == providers.LOCAL:
            model = model or self.agent.local_config.model
        return provider, model

    def apply_saved(self) -> None:
        """At start-up: pick up whatever Settings last chose. A cloud choice
        whose key has gone missing falls back to local rather than failing
        every turn."""
        provider, model = self.saved()
        key = self.api_key(provider)
        # A custom endpoint may need no key (a server on the home network);
        # it needs an address instead.
        missing = not model or (
            not self.base_url(provider) if provider == providers.CUSTOM else providers.is_cloud(provider) and not key
        )
        if missing:
            log.warning("LLM: saved %s choice is incomplete; using local", provider)
            provider, model = providers.LOCAL, self.agent.local_config.model
        self.agent.use_model(provider, model, key, self.base_url(provider))

    def choose(self, provider: str, model: str, base_url: str | None = None) -> None:
        if base_url:
            self.database.set_setting(CUSTOM_URL_KEY, base_url.rstrip("/"))
        self.database.set_setting(PROVIDER_KEY, provider)
        self.database.set_setting(_model_key(provider), model)
        self.agent.use_model(provider, model, self.api_key(provider), self.base_url(provider))

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.agent.provider,
            "label": providers.PROVIDERS[self.agent.provider].label,
            "model": self.agent.config.model,
            "cloud": providers.is_cloud(self.agent.provider),
            "fell_back": self.agent.fell_back,
            "providers": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "cloud": providers.is_cloud(spec.id),
                    "has_key": bool(self.api_key(spec.id)) if spec.key_env else None,
                    "model": self.database.get_setting(_model_key(spec.id))
                    or (self.agent.local_config.model if spec.id == providers.LOCAL else ""),
                    "suggested": list(spec.suggested),
                    **({"base_url": self.base_url(spec.id)} if spec.id == providers.CUSTOM else {}),
                }
                for spec in providers.PROVIDERS.values()
            ],
            "local_catalog": [asdict(model) for model in providers.LOCAL_MODELS],
            "download": self.download,
        }

    # ---- every local model gets Friday's window ------------------------------
    async def house_build(self, model: str) -> str:
        """The name to run a local model under. A raw Ollama model runs with
        Ollama's default window, too small for Friday's instructions and
        tools, and Ollama silently cuts the front off an over-long turn. So
        picking one builds its house-* version (the same model with a 32k
        window) and uses that, which makes "qwen3:14b" behave exactly like
        "house-qwen". A model already set to a big enough window is used as
        it is. If Ollama can't be asked, the name comes back unchanged."""
        base = self._ollama_base()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
                shown = await client.post(f"{base}/api/show", json={"model": model})
                if shown.status_code >= 400:
                    return model
                window = re.search(r"num_ctx\s+(\d+)", str(shown.json().get("parameters") or ""))
                if window and int(window.group(1)) >= providers.LOCAL_CONTEXT:
                    return model
                offered = providers.local_model(model)
                name = offered.name if offered and offered.base == model.removesuffix(":latest") else house_name(model)
                created = await client.post(
                    f"{base}/api/create",
                    json={
                        "model": name,
                        "from": model,
                        "parameters": {"num_ctx": providers.LOCAL_CONTEXT},
                        "stream": False,
                    },
                )
                if created.status_code >= 400:
                    log.warning("LLM: could not build %s from %s: %s", name, model, created.text[:200])
                    return model
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("LLM: could not check %s's window: %s", model, exc)
            return model
        log.info("LLM: built %s from %s with a %d-token window", name, model, providers.LOCAL_CONTEXT)
        return name

    async def keep_warm(self, every: float = 240.0) -> None:
        """Hold the local model in memory. Ollama unloads a model after five
        idle minutes (its default, and each chat request resets the timer to
        it), and the next reply then waits several seconds for a reload.
        Asking Ollama to load the model with no prompt costs nothing when it
        is already loaded, so this asks every four minutes."""
        while True:
            if self.agent.provider == providers.LOCAL:
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0)) as client:
                        await client.post(
                            f"{self._ollama_base()}/api/generate",
                            json={"model": self.agent.config.model, "keep_alive": "10m"},
                        )
                except httpx.HTTPError:
                    pass  # Ollama down or starting; try again next time
            await asyncio.sleep(every)

    async def unload(self, model: str) -> None:
        """Free a local model's GPU memory now. After a switch, Ollama would
        otherwise keep the old model loaded for minutes beside the new one,
        and two 12 GB models don't fit on one card with room left for
        speech recognition."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{self._ollama_base()}/api/generate", json={"model": model, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    async def settle_local(self) -> None:
        """At start-up: if the local model in use is a raw one (from .env or
        an old choice), switch to its house build."""
        if self.agent.provider != providers.LOCAL:
            return
        name = await self.house_build(self.agent.config.model)
        if name != self.agent.config.model:
            self.choose(providers.LOCAL, name)

    # ---- downloading a local model ------------------------------------------
    def _ollama_base(self) -> str:
        base = self.agent.local_config.base_url.rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    def start_download(self, name: str) -> dict[str, Any]:
        """Pull one of the offered models into Ollama and build its house-*
        version, in the background. Settings polls download_status()."""
        model = providers.local_model(name)
        if model is None:
            raise ValueError(f"{name} isn't one of the offered models")
        if self._download_task and not self._download_task.done():
            raise RuntimeError(f"Already downloading {self.download.get('base')}")
        self.download = {"name": model.name, "base": model.base, "status": "starting", "completed": 0, "total": 0}
        self._download_task = asyncio.create_task(self._pull_and_build(model))
        return self.download

    async def _pull_and_build(self, model: providers.LocalModel) -> None:
        base = self._ollama_base()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0)) as client:
                async with client.stream(
                    "POST", f"{base}/api/pull", json={"model": model.base, "stream": True}
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("error"):
                            raise RuntimeError(event["error"])
                        self.download["status"] = "downloading"
                        if event.get("total"):
                            self.download["total"] = int(event["total"])
                            self.download["completed"] = int(event.get("completed") or 0)
                self.download["status"] = "building"
                response = await client.post(
                    f"{base}/api/create",
                    json={
                        "model": model.name,
                        "from": model.base,
                        "parameters": {"num_ctx": providers.LOCAL_CONTEXT},
                        "stream": False,
                    },
                )
                if response.status_code >= 400:
                    raise RuntimeError(response.text[:200])
            self.download["status"] = "done"
            log.info("LLM: downloaded %s and built %s", model.base, model.name)
        except Exception as exc:  # noqa: BLE001 - shown in Settings as-is
            log.warning("LLM: downloading %s failed: %s", model.base, exc)
            self.download["status"] = "failed"
            self.download["error"] = str(exc)[:300]

    # ---- what each provider offers -----------------------------------------
    async def models(self, provider: str, key: str | None = None) -> list[str]:
        key = key or self.api_key(provider)
        spec = providers.PROVIDERS[provider]
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == providers.LOCAL:
                base = self.agent.local_config.base_url.rstrip("/")
                base = base[:-3] if base.endswith("/v1") else base
                response = await client.get(f"{base}/api/tags")
                response.raise_for_status()
                names = [m["name"].removesuffix(":latest") for m in response.json().get("models", [])]
                # The house's own tuned models first.
                return sorted(names, key=lambda n: (not n.startswith("house-"), n))
            if provider == providers.ANTHROPIC:
                if not key:
                    return list(spec.suggested)
                import anthropic

                client_sdk = anthropic.AsyncAnthropic(api_key=key, timeout=10.0, max_retries=1)
                try:
                    listed = [model.id async for model in client_sdk.models.list()]
                finally:
                    await client_sdk.close()
                return listed or list(spec.suggested)
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            base = self.base_url(provider)
            if not base:
                return []
            response = await client.get(f"{base}/models", headers=headers)
            response.raise_for_status()
            ids = [str(m.get("id")) for m in response.json().get("data", []) if m.get("id")]
        if provider == providers.OPENAI:
            # Chat models only: no embeddings, audio, image or moderation.
            skip = ("embedding", "whisper", "tts", "dall-e", "davinci", "babbage", "moderation", "audio", "realtime", "transcribe", "image", "search")
            ids = [i for i in ids if not any(word in i for word in skip)]
        return sorted(ids)

    async def test(self, provider: str, model: str, key: str | None = None, base_url: str | None = None) -> str:
        """One tiny request. Returns the reply, or raises with the provider's
        own complaint (bad key, unknown model, no credit)."""
        key = key or self.api_key(provider)
        prompt = "Reply with just the word OK."
        if provider == providers.ANTHROPIC:
            return await providers.anthropic_text(api_key=key, model=model, prompt=prompt, max_tokens=64, timeout=30.0)
        base = (base_url or self.base_url(provider)).rstrip("/")
        body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        body["max_completion_tokens" if provider == providers.OPENAI else "max_tokens"] = 400
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key or self.agent.local_config.api_key}"},
                json=body,
            )
            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", {})
                    detail = detail.get("message") if isinstance(detail, dict) else detail
                except ValueError:
                    detail = response.text[:200]
                raise RuntimeError(f"{response.status_code}: {detail or 'request refused'}")
            reply = str(response.json()["choices"][0]["message"].get("content") or "")
            if provider == providers.CUSTOM:
                await self._check_tools(client, base, key, model)
            return reply

    async def _check_tools(self, client: httpx.AsyncClient, base: str, key: str | None, model: str) -> None:
        """Friday works by calling tools. An endpoint that chats but never
        returns tool calls (many agent gateways run their own tools instead)
        could talk, but not turn on a light, so it isn't switched to."""
        response = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key or 'none'}"},
            json={
                "model": model,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": "Call the ping tool now."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "ping",
                            "description": "Checks the connection. Call it when asked.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
        )
        calls = []
        if response.status_code < 400:
            with contextlib.suppress(KeyError, IndexError, ValueError, TypeError):
                calls = response.json()["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            raise RuntimeError(
                "it answered, but didn't call a tool when asked. Friday controls the house through "
                "tool calls, so this endpoint or model can't be its brain. Pick a model with tool "
                "calling, or an endpoint that passes Friday's tools through."
            )
