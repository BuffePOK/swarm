"""Usage statistics computed from the event log (the log is the source of truth)."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .core.eventlog import EventLog


def llm_stats(events: EventLog, *, task_id: str | None = None) -> dict[str, Any]:
    per_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    errors: Counter[str] = Counter()
    fallbacks = exact_hits = saved_tokens = alerts = 0
    for e in events.query(type_prefix="llm.", task_id=task_id):
        p = e.payload
        if e.type == "llm.call":
            m = per_model[p["model"]]
            m["calls"] += 1
            m["prompt_tokens"] += p["prompt_tokens"]
            m["cached_tokens"] += p["cached_tokens"]
            m["completion_tokens"] += p["completion_tokens"]
            m["cost_usd"] += p["cost_usd"]
            m["latency_s"] += p["latency_s"]
        elif e.type == "llm.cache_hit":
            exact_hits += 1
            saved_tokens += p.get("saved_tokens", 0)
        elif e.type == "llm.error":
            errors[f"{p.get('model')}:{p.get('error')}"] += 1
        elif e.type == "llm.fallback":
            fallbacks += 1
    alerts = len(events.query(type="alert", task_id=task_id))
    totals = defaultdict(float)
    for m in per_model.values():
        for k, v in m.items():
            totals[k] += v
    prompt = totals["prompt_tokens"]
    return {
        "calls": int(totals["calls"]),
        "prompt_tokens": int(prompt),
        "cached_tokens": int(totals["cached_tokens"]),
        "completion_tokens": int(totals["completion_tokens"]),
        "provider_cache_ratio": round(totals["cached_tokens"] / prompt, 4) if prompt else 0.0,
        "cost_usd": round(totals["cost_usd"], 6),
        "exact_cache_hits": exact_hits,
        "exact_cache_saved_tokens": saved_tokens,
        "fallbacks": fallbacks,
        "errors": dict(errors),
        "alerts": alerts,
        "per_model": {
            k: {
                "calls": int(v["calls"]),
                "prompt_tokens": int(v["prompt_tokens"]),
                "cached_tokens": int(v["cached_tokens"]),
                "completion_tokens": int(v["completion_tokens"]),
                "cost_usd": round(v["cost_usd"], 6),
                "avg_latency_s": round(v["latency_s"] / v["calls"], 3) if v["calls"] else 0.0,
            }
            for k, v in sorted(per_model.items())
        },
    }
