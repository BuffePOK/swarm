"""The full model-call stack used by every agent.

exact cache -> pricing -> budget reservation -> governor slot -> gateway
-> classify errors -> retry / fall back -> commit actual cost -> trace in CAS + event log.

Reaction table (the system reacts to API problems; it does not fight them):
  429 RateLimited  wait Retry-After (bounded), governor halves, then next model
  5xx / timeout    exponential backoff with jitter, breaker counts, then next model
  403 Blocked      no retry, model blocked for hours (persisted), alert, next model
  401 AuthError    no retry, alert, next model
  404 NotFound     registry marks it gone, next model
  400 BadRequest   raised to the caller: a fallback must not hide our own bug
If every candidate fails -> AllModelsFailed -> the kernel pauses the task (paused_api).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import ModelBinding, PricingSettings, RetrySettings
from ..core.budget import BudgetLedger
from ..core.cas import CAS
from ..core.eventlog import EventLog
from ..core.store import Store
from ..util import canonical_json, estimate_tokens, sha256_hex, utcnow_iso
from .client import ChatResponse, GatewayClient, Usage
from .errors import (AllModelsFailed, AuthError, BadRequest, Blocked, ModelError, ModelNotFound, RateLimited,
                     Transient, Unpriced)
from .governor import Governor
from .registry import CircuitBreaker, ModelRegistry

_NOT_IN_CACHE_KEY = {"stream", "user", "metadata", "cache"}


@dataclass
class LLMResult:
    text: str
    model: str
    requested_model: str
    usage: Usage
    cost_usd: float
    latency_s: float
    exact_cache_hit: bool
    fallback_index: int
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    request_ref: str | None = None
    response_ref: str | None = None
    prefix_hash: str | None = None


def cache_key(model: str, messages: list[dict[str, Any]], params: dict[str, Any]) -> str:
    p = {k: v for k, v in params.items() if k not in _NOT_IN_CACHE_KEY}
    return sha256_hex(canonical_json({"model": model, "messages": messages, "params": p}))


def cacheable(params: dict[str, Any], use_cache: bool | None) -> bool:
    if use_cache is not None:
        return use_cache
    return float(params.get("temperature", 1.0)) == 0.0


class ExactCache:
    def __init__(self, store: Store):
        self.store = store

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT response FROM llm_cache WHERE key = ?", (key,))
        if not row:
            return None
        self.store.run("UPDATE llm_cache SET hits = hits + 1 WHERE key = ?", (key,))
        import json

        return json.loads(row["response"])

    def put(self, key: str, model: str, response: dict[str, Any]) -> None:
        self.store.run(
            "INSERT OR REPLACE INTO llm_cache(key, model, response, created, hits) VALUES(?,?,?,?,0)",
            (key, model, canonical_json(response), utcnow_iso()),
        )


class LLMCaller:
    def __init__(
        self, *, project: str, client: GatewayClient, registry: ModelRegistry, cache: ExactCache,
        budget: BudgetLedger, events: EventLog, cas: CAS, pricing: PricingSettings, retry: RetrySettings,
        governor: Governor, default_max_tokens: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.project = project
        self.client = client
        self.registry = registry
        self.cache = cache
        self.budget = budget
        self.events = events
        self.cas = cas
        self.pricing = pricing
        self.retry = retry
        self.governor = governor
        self.breaker = CircuitBreaker(retry.breaker_threshold, retry.breaker_cooldown_s)
        self.default_max_tokens = default_max_tokens
        self._sleep = sleep

    def _backoff(self, attempt: int) -> float:
        base = min(self.retry.max_delay_s, self.retry.base_delay_s * (2 ** (attempt - 1)))
        return base * (0.5 + random.random() / 2)  # jitter: 50-100% of the step

    async def chat(
        self, *, binding: ModelBinding, messages: list[dict[str, Any]], agent: str, scope: str = "",
        task_id: str | None = None, trace_id: str | None = None, params: dict[str, Any] | None = None,
        purpose: str = "", prefix_hash: str | None = None, priority: int = 2, use_cache: bool | None = None,
    ) -> LLMResult:
        params = dict(params or {})
        if "max_tokens" not in params and "max_completion_tokens" not in params:
            params["max_tokens"] = self.default_max_tokens
        max_out = int(params.get("max_tokens") or params.get("max_completion_tokens"))
        request_ref = self.cas.put_json({"messages": messages, "params": params})
        common = {"agent": agent, "scope": scope, "purpose": purpose, "request_ref": request_ref,
                  "prefix_hash": prefix_hash, "priority": priority}
        log = lambda t, p: self.events.append(t, {**common, **p}, actor=f"agent:{agent}", task_id=task_id, trace_id=trace_id)  # noqa: E731

        errors: list[ModelError] = []
        candidates = binding.candidates()
        for idx, model in enumerate(candidates):
            if idx > 0:
                log("llm.fallback", {"from": candidates[idx - 1], "to": model,
                                     "reason": errors[-1].as_dict() if errors else "skipped"})
            blocked, reason, until = self.registry.block_info(model)
            if blocked:
                errors.append(Blocked(model, 403, f"blocked until {until}: {reason}"))
                continue
            if not self.breaker.allow(model):
                errors.append(Transient(model, None, "circuit open"))
                continue

            key = cache_key(model, messages, params)
            if cacheable(params, use_cache):
                hit = self.cache.get(key)
                if hit is not None:
                    u = Usage(**hit["usage"])
                    log("llm.cache_hit", {"model": model, "key": key, "saved_tokens": u.total})
                    return LLMResult(text=hit["text"], model=hit["model"], requested_model=binding.model, usage=u,
                                     cost_usd=0.0, latency_s=0.0, exact_cache_hit=True, fallback_index=idx,
                                     finish_reason=hit.get("finish_reason"), request_ref=request_ref,
                                     prefix_hash=prefix_hash)

            price, known = self.pricing.price_for(model)
            if not known and self.pricing.strict:
                errors.append(Unpriced(model, None, "strict pricing: no price configured"))
                continue
            est_prompt = estimate_tokens(canonical_json(messages))
            reservation = self.budget.reserve(scope, est_prompt + max_out, price.cost(est_prompt, 0, max_out), ref=request_ref)

            resp: ChatResponse | None = None
            last: ModelError | None = None
            headers = {"X-Roj-Project": self.project, "X-Roj-Agent": agent, "X-Roj-Priority": str(priority)}
            if task_id:
                headers["X-Roj-Task"] = task_id
            if trace_id:
                headers["X-Roj-Trace"] = trace_id
            attempt = 0
            while attempt < self.retry.max_attempts:
                attempt += 1
                try:
                    async with self.governor.slot():
                        resp = await self.client.chat(model, messages, params, headers)
                    break
                except BadRequest as e:
                    self.budget.release(reservation)
                    log("llm.error", {"model": model, "attempt": attempt, **e.as_dict()})
                    raise
                except RateLimited as e:
                    last = e
                    self.governor.on_overload()
                    log("llm.error", {"model": model, "attempt": attempt, **e.as_dict(), "retry_after": e.retry_after})
                    wait = min(e.retry_after if e.retry_after is not None else self._backoff(attempt),
                               self.retry.rate_limit_max_wait_s)
                    if attempt < self.retry.max_attempts:
                        await self._sleep(wait)
                except Transient as e:
                    last = e
                    if e.status and e.status >= 500:
                        self.governor.on_overload()
                    log("llm.error", {"model": model, "attempt": attempt, **e.as_dict()})
                    if attempt < self.retry.max_attempts:
                        await self._sleep(self._backoff(attempt))
                except (Blocked, AuthError) as e:
                    last = e
                    log("llm.error", {"model": model, "attempt": attempt, **e.as_dict()})
                    self.registry.block(model, self.retry.blocked_cooldown_s, f"{type(e).__name__}: {e.detail}")
                    log("alert", {"kind": "model_blocked" if isinstance(e, Blocked) else "auth_failed",
                                  "model": model, "detail": e.detail})
                    break
                except ModelNotFound as e:
                    last = e
                    log("llm.error", {"model": model, "attempt": attempt, **e.as_dict()})
                    self.registry.mark_gone(model)
                    break

            if resp is None:
                self.budget.release(reservation)
                if isinstance(last, Transient) and self.breaker.failure(model):
                    log("llm.breaker_open", {"model": model, "cooldown_s": self.retry.breaker_cooldown_s})
                errors.append(last or Transient(model, None, "unknown failure"))
                continue

            self.breaker.success(model)
            self.governor.on_success(resp.latency_s)
            cost = price.cost(resp.usage.prompt_tokens, resp.usage.cached_tokens, resp.usage.completion_tokens)
            response_ref = self.cas.put_json(resp.raw)
            self.budget.commit(reservation, resp.usage.total, cost, ref=response_ref)
            log("llm.call", {
                "model": model, "served_by": resp.model, "requested": binding.model, "fallback_index": idx,
                "attempt": attempt, "prompt_tokens": resp.usage.prompt_tokens, "cached_tokens": resp.usage.cached_tokens,
                "completion_tokens": resp.usage.completion_tokens, "usage_estimated": resp.usage.estimated,
                "cost_usd": round(cost, 8), "price_known": known, "latency_s": round(resp.latency_s, 3),
                "finish_reason": resp.finish_reason, "response_ref": response_ref,
                **({"reasoning_tokens": resp.usage.reasoning_tokens} if resp.usage.reasoning_tokens else {}),
                **({"reasoning_chars": resp.reasoning_chars} if resp.reasoning_chars else {}),
            })
            if cacheable(params, use_cache) and resp.finish_reason not in ("length", "max_tokens"):  # never replay a cut answer
                self.cache.put(key, model, {"text": resp.text, "model": resp.model, "finish_reason": resp.finish_reason,
                                            "usage": resp.usage.__dict__})
            return LLMResult(text=resp.text, model=model, requested_model=binding.model, usage=resp.usage,
                             cost_usd=cost, latency_s=resp.latency_s, exact_cache_hit=False, fallback_index=idx,
                             finish_reason=resp.finish_reason, tool_calls=resp.tool_calls, request_ref=request_ref,
                             response_ref=response_ref, prefix_hash=prefix_hash)

        log("llm.all_failed", {"errors": [e.as_dict() for e in errors]})
        raise AllModelsFailed(errors)


async def embed(caller: "LLMCaller", *, binding: ModelBinding, inputs: list[str], agent: str, scope: str = "",
                purpose: str = "embed") -> tuple[list[list[float]], str]:
    """Embeddings through the same budget, breaker and log as chat calls. Returns (vectors, model)."""
    errors: list[ModelError] = []
    for model in binding.candidates():
        if caller.registry.block_info(model)[0] or not caller.breaker.allow(model):
            continue
        price, known = caller.pricing.price_for(model)
        est = sum(estimate_tokens(x) for x in inputs)
        res = caller.budget.reserve(scope, est, price.cost(est, 0, 0))
        try:
            async with caller.governor.slot():
                vectors, usage, latency = await caller.client.embed(model, inputs)
        except ModelError as e:
            caller.budget.release(res)
            caller.events.append("llm.error", {"agent": agent, "model": model, "purpose": purpose, **e.as_dict()}, actor=f"agent:{agent}")
            if isinstance(e, (Blocked, AuthError)):
                caller.registry.block(model, caller.retry.blocked_cooldown_s, f"{type(e).__name__}: {e.detail}")
            errors.append(e)
            continue
        cost = price.cost(usage.prompt_tokens, 0, 0)
        caller.budget.commit(res, usage.prompt_tokens, cost)
        caller.events.append("llm.call", {"agent": agent, "scope": scope, "purpose": purpose, "model": model, "requested": binding.model,
                                          "fallback_index": binding.candidates().index(model), "attempt": 1,
                                          "prompt_tokens": usage.prompt_tokens, "cached_tokens": 0, "completion_tokens": 0,
                                          "usage_estimated": usage.estimated, "cost_usd": round(cost, 8), "price_known": known,
                                          "latency_s": round(latency, 3), "finish_reason": "embedding", "items": len(inputs)},
                             actor=f"agent:{agent}")
        return vectors, model
    raise AllModelsFailed(errors or [Transient(binding.model, None, "no available embedding model")])
