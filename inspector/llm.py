"""Клиент OpenAI-совместимого API (в решении — vLLM локально)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("inspector.llm")


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.content)


class LLMError(RuntimeError):
    pass


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


class ChatClient:
    def __init__(self, url: str, model: str, provider: str = "vllm", api_key: str = "", concurrency: int = 16,
                 timeout_s: float = 600.0, retries: int = 3, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.retries = max(1, retries)
        self.timeout_s = timeout_s
        self._sem = asyncio.Semaphore(concurrency)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.stats = {"calls": 0, "errors": 0, "in_tokens": 0, "out_tokens": 0}

    async def __aenter__(self) -> "ChatClient":
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s), headers=headers,
                                         transport=self._transport)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def body(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.0,
             json_schema: dict | None = None, extra: dict | None = None) -> dict:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature}
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": json_schema.get("name", "answer"), "schema": json_schema["schema"], "strict": True}}
        if self.provider == "openrouter":
            body["reasoning"] = {"enabled": False}
            body["usage"] = {"include": True}
            body["provider"] = {"zdr": True, "data_collection": "deny",
                                "quantizations": ["fp8", "bf16", "fp16", "fp32"]}
        else:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if extra:
            body.update(extra)
        return body

    async def chat(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.0,
                   json_schema: dict | None = None, extra: dict | None = None) -> ChatResult:
        """Запрос с повторами (сеть, 429/5xx, битый JSON при структурном выводе). Бросает LLMError после попыток."""
        if self._client is None:
            raise RuntimeError("ChatClient используется вне async with")
        body = self.body(messages, max_tokens=max_tokens, temperature=temperature, json_schema=json_schema,
                         extra=extra)
        last = "нет ответа"
        async with self._sem:
            for attempt in range(self.retries):
                try:
                    resp = await self._client.post(f"{self.url}/chat/completions", json=body)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    resp.raise_for_status()
                    data = resp.json()
                    choice = data["choices"][0]
                    content = choice["message"].get("content") or ""
                    usage = data.get("usage") or {}
                    self.stats["calls"] += 1
                    self.stats["in_tokens"] += usage.get("prompt_tokens") or 0
                    self.stats["out_tokens"] += usage.get("completion_tokens") or 0
                    if json_schema is not None:
                        json.loads(content)            # битый JSON -> повтор
                    return ChatResult(content, choice.get("finish_reason"), usage)
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
                    last = f"{type(exc).__name__}: {exc}"[:300]
                    self.stats["errors"] += 1
                    log.warning("llm call failed", extra={"model": self.model, "attempt": attempt + 1, "error": last})
                    if attempt + 1 < self.retries:
                        await asyncio.sleep(min(30.0, 2.0 * (2 ** attempt)) + random.random())
        raise LLMError(last)


def llm_client(settings, transport: httpx.AsyncBaseTransport | None = None, concurrency: int | None = None) -> ChatClient:
    return ChatClient(settings.llm_url, settings.llm_model, settings.llm_provider, settings.llm_api_key,
                      concurrency or settings.llm_concurrency, settings.llm_timeout_s, settings.llm_retries, transport)


def ocr_client(settings, transport: httpx.AsyncBaseTransport | None = None) -> ChatClient:
    return ChatClient(settings.ocr_url_eff, settings.ocr_model_eff, settings.ocr_provider_eff, settings.llm_api_key,
                      settings.ocr_concurrency, settings.llm_timeout_s, settings.llm_retries, transport)
