import asyncio

import pytest

from roj.config import ModelBinding
from roj.core.budget import BudgetExceeded
from roj.models.errors import AllModelsFailed, BadRequest
from roj.stats import llm_stats

MSG = [{"role": "system", "content": "You are a test. " * 50}, {"role": "user", "content": "hello"}]


def call(project, model, fallbacks=(), messages=MSG, **params):
    params.setdefault("temperature", 0)
    return asyncio.run(project.caller.chat(binding=ModelBinding(model=model, fallbacks=list(fallbacks)),
                                           messages=messages, agent="tester", params=params))


def test_ok_call_is_traced_and_charged(project, gateway):
    r = call(project, "fake-ok")
    assert r.text.startswith("[fake-ok]") and r.cost_usd > 0 and not r.exact_cache_hit
    ev = project.events.query(type="llm.call")[-1].payload
    assert ev["model"] == "fake-ok" and ev["request_ref"] and ev["response_ref"]
    assert project.cas.get_json(ev["request_ref"])["messages"] == MSG
    tokens, usd = project.budget.usage("")
    assert tokens == r.usage.total and usd == pytest.approx(r.cost_usd)


def test_403_falls_back_once_and_blocks_model(project, gateway, inst):
    state, _ = gateway
    r = call(project, "fake-blocked", ["fake-ok"])
    assert r.model == "fake-ok" and r.fallback_index == 1
    assert state.requests["fake-blocked"] == 1  # never retried
    assert inst.registry.block_info("fake-blocked")[0]
    alerts = project.events.query(type="alert")
    assert alerts and alerts[-1].payload["kind"] == "model_blocked"
    call(project, "fake-blocked", ["fake-ok"], messages=[{"role": "user", "content": "again"}])
    assert state.requests["fake-blocked"] == 1  # blocked model is skipped, not hammered


def test_all_models_failed(project, gateway):
    with pytest.raises(AllModelsFailed) as e:
        call(project, "fake-blocked", ["fake-auth"])
    kinds = [x["error"] for x in e.value.summary()]
    assert kinds == ["Blocked", "AuthError"]
    assert project.budget.usage("") == (0, 0.0)  # reservations released


def test_429_retries_then_falls_back_and_slows_down(project, gateway):
    state, _ = gateway
    before = project.caller.governor.limit
    r = call(project, "fake-ratelimit", ["fake-ok"])
    assert r.model == "fake-ok"
    assert state.requests["fake-ratelimit"] == 3
    assert project.caller.governor.limit < before


def test_transient_errors_are_retried(project, gateway):
    state, _ = gateway
    r = call(project, "fake-flaky")
    assert r.model == "fake-flaky" and state.requests["fake-flaky"] == 3


def test_bad_request_is_not_hidden_by_fallback(project, gateway):
    state, _ = gateway
    with pytest.raises(BadRequest):
        call(project, "fake-bad", ["fake-ok"])
    assert state.requests["fake-ok"] == 0
    assert project.budget.usage("") == (0, 0.0)


def test_unknown_model_falls_back(project, gateway):
    r = call(project, "no-such-model", ["fake-ok"])
    assert r.model == "fake-ok"


def test_exact_cache_hit_costs_nothing(project, gateway):
    state, _ = gateway
    a = call(project, "fake-ok")
    b = call(project, "fake-ok")
    assert b.exact_cache_hit and b.cost_usd == 0 and b.text == a.text
    assert state.requests["fake-ok"] == 1


def test_nonzero_temperature_is_not_cached(project, gateway):
    state, _ = gateway
    call(project, "fake-ok", temperature=0.7)
    call(project, "fake-ok", temperature=0.7)
    assert state.requests["fake-ok"] == 2


def test_provider_prefix_cache_is_measured_and_cheaper(project, gateway):
    first = call(project, "fake-ok", messages=[MSG[0], {"role": "user", "content": "question A"}])
    second = call(project, "fake-ok", messages=[MSG[0], {"role": "user", "content": "question B"}])
    assert first.usage.cached_tokens == 0 and second.usage.cached_tokens > 0
    assert second.cost_usd < first.cost_usd
    s = llm_stats(project.events)
    assert s["provider_cache_ratio"] > 0 and s["calls"] == 2


def test_budget_blocks_before_any_request(inst, gateway):
    state, _ = gateway
    p = inst.create_project("tiny", usd=0.0001)
    with pytest.raises(BudgetExceeded):
        asyncio.run(p.caller.chat(binding=ModelBinding(model="fake-ok"), messages=MSG, agent="t", params={"temperature": 0}))
    assert state.requests["fake-ok"] == 0
    p.close()


def test_strict_pricing_skips_unpriced(project, gateway):
    project.caller.pricing = project.caller.pricing.model_copy(update={"strict": True, "models": {"fake-ok": project.caller.pricing.models["fake-ok"]}})
    r = call(project, "fake-cheap", ["fake-ok"])
    assert r.model == "fake-ok"


def test_registry_refresh(inst, gateway):
    diff = asyncio.run(inst.registry.refresh())
    assert "fake-ok" in diff["added"] and diff["total"] >= 5
    assert any(m["id"] == "fake-ok" for m in inst.registry.list())
