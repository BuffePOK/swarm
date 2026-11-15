"""Minimal OpenAI-compatible gateway client (chat completions + model list).

~150 lines of our own code instead of a proxy library in the core (ADR-0001).
Native provider adapters (thinking budgets, explicit cache objects, batch) plug in
behind the same `chat()` signature later.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import GatewaySettings
from ..util import canonical_json, estimate_tokens
from .errors import Timeout, Transient, classify


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResponse:
    model: str
    text: str
    finish_reason: str | None
    usage: Usage
    latency_s: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_usage(raw: dict[str, Any], messages: list[dict[str, Any]], text: str) -> Usage:
    u = raw.get("usage") or {}
    prompt = u.get("prompt_tokens", u.get("input_tokens"))
    completion = u.get("completion_tokens", u.get("output_tokens"))
    details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") or u.get("cache_read_input_tokens") or u.get("cached_tokens") or 0
    if prompt is None or completion is None:
        return Usage(
            prompt_tokens=estimate_tokens(canonical_json(messages)),
            completion_tokens=estimate_tokens(text),
            cached_tokens=0,
            estimated=True,
        )
    return Usage(int(prompt), int(completion), int(cached or 0))


class GatewayClient:
    def __init__(self, settings: GatewaySettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._clients: dict[int, httpx.AsyncClient] = {}

    def _client(self) -> httpx.AsyncClient:
        loop_id = id(asyncio.get_running_loop())
        client = self._clients.get(loop_id)
        if client is None or client.is_closed:
            timeout = httpx.Timeout(self.settings.timeout_s, connect=self.settings.connect_timeout_s)
            client = httpx.AsyncClient(
                base_url=self.settings.base_url.rstrip("/") + "/",
                timeout=timeout,
                transport=self.transport,
            )
            self._clients[loop_id] = client
        return client

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self.settings.api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        h.update(extra or {})
        return h

    async def chat(
        self, model: str, messages: list[dict[str, Any]], params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChatResponse:
        body = {"model": model, "messages": messages, **(params or {})}
        t0 = time.monotonic()
        try:
            r = await self._client().post("chat/completions", content=canonical_json(body).encode("utf-8"),
                                          headers=self._headers(headers))
        except httpx.TimeoutException as e:
            raise Timeout(model, None, f"timeout: {e}") from e
        except httpx.TransportError as e:
            raise Transient(model, None, f"transport: {e}") from e
        latency = time.monotonic() - t0
        if r.status_code >= 400:
            raise classify(model, r.status_code, r.text, dict(r.headers))
        try:
            raw = r.json()
            choice = (raw.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if isinstance(text, list):  # some gateways return content parts
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        except (ValueError, AttributeError, IndexError) as e:
            raise Transient(model, r.status_code, f"unparseable response: {r.text[:200]}") from e
        return ChatResponse(
            model=raw.get("model") or model,
            text=text,
            finish_reason=choice.get("finish_reason"),
            usage=parse_usage(raw, messages, text),
            latency_s=latency,
            tool_calls=msg.get("tool_calls") or [],
            raw=raw,
        )

    async def embed(self, model: str, inputs: list[str], headers: dict[str, str] | None = None) -> tuple[list[list[float]], Usage, float]:
        t0 = time.monotonic()
        try:
            r = await self._client().post("embeddings", content=canonical_json({"model": model, "input": inputs}).encode("utf-8"),
                                          headers=self._headers(headers))
        except httpx.TimeoutException as e:
            raise Timeout(model, None, f"timeout: {e}") from e
        except httpx.TransportError as e:
            raise Transient(model, None, f"transport: {e}") from e
        if r.status_code >= 400:
            raise classify(model, r.status_code, r.text, dict(r.headers))
        raw = r.json()
        vectors = [d["embedding"] for d in sorted(raw.get("data", []), key=lambda d: d.get("index", 0))]
        u = raw.get("usage") or {}
        usage = Usage(int(u.get("prompt_tokens", u.get("total_tokens", 0)) or sum(estimate_tokens(x) for x in inputs)), 0, 0,
                      estimated="prompt_tokens" not in u and "total_tokens" not in u)
        return vectors, usage, time.monotonic() - t0

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self._client().get("models", headers=self._headers(None))
        except httpx.TimeoutException as e:
            raise Timeout("*", None, f"timeout: {e}") from e
        except httpx.TransportError as e:
            raise Transient("*", None, f"transport: {e}") from e
        if r.status_code >= 400:
            raise classify("*", r.status_code, r.text, dict(r.headers))
        data = r.json()
        items = data.get("data", data if isinstance(data, list) else [])
        return [m if isinstance(m, dict) else {"id": str(m)} for m in items]

    async def aclose(self) -> None:
        loop_id = id(asyncio.get_running_loop())
        client = self._clients.pop(loop_id, None)
        if client is not None:
            await client.aclose()
