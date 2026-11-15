"""Paired prompt experiments: the same cases, the same model, baseline profile vs one changed knob.

Token discipline:
- baselines are measured once and shared by every experiment (and by the model cards)
- temperature 0 + the exact cache: a repeated study re-pays nothing for unchanged prompts
- stage 1 is small; only promising-but-unproven effects buy a second stage
- only adopted changes are confirmed, once, on hold-out cases no earlier stage has seen
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ..config import ModelBinding
from ..core.kernel import StepContext
from ..models.errors import AllModelsFailed
from ..prompting.compiler import RoleSpec, compile_prompt
from ..prompting.profiles import PromptProfile, family
from ..skills.slots import extract_json
from .tasks import EvalCase

SOLVER = {
    "pl": "Wykonujesz krótkie, precyzyjne zadania na dostarczonych danych. Odpowiadasz dokładnie w wymaganym formacie.",
    "en": "You carry out short, precise tasks on the provided data. You answer exactly in the required format.",
}
MIN_EFFECT = 0.03  # smaller mean differences are treated as noise
MARGIN = 0.03  # "not worse" tolerance when a variant is cheaper
SAVING = 0.10  # relative token saving that justifies a change at equal quality


def compile_case(case: EvalCase, profile: PromptProfile, settings: Any):
    lang = profile.language or settings.language.prompt_language
    return compile_prompt(RoleSpec("eval-solver", SOLVER[lang]), case.spec(lang), language=settings.language, settings=settings.prompt,
                          documents=case.documents, profile=profile, examples=case.examples)


async def run_trial(ctx: StepContext, model: str, case: EvalCase, profile: PromptProfile) -> dict[str, Any] | None:
    compiled = compile_case(case, profile, ctx.project.settings)
    try:
        r = await ctx.chat(binding=ModelBinding(model=model), messages=compiled.messages, agent="eval", scope="team:lab/agent:eval",
                           prefix_hash=compiled.prefix_hash, purpose=f"eval:{case.kind}",
                           params={"temperature": 0, "max_tokens": 700 if profile.reasoning == "brief" else 400})
    except AllModelsFailed as e:
        ctx.log("lab.model_unavailable", {"model": model, "error": str(e)[:300]})
        return None
    try:
        value, valid = extract_json(r.text), True
    except ValueError:
        value, valid = None, False
    return {"score": round(case.score(value), 4), "tokens": r.usage.total, "valid": valid, "latency_s": round(r.latency_s, 3),
            "cost_usd": r.cost_usd}


async def measure(ctx: StepContext, models: list[str], cases: list[EvalCase], profile: PromptProfile,
                  concurrency: int = 6) -> dict[str, dict[str, Any]]:
    """-> {"model|case_id": trial}. A model that fails once is skipped for the rest of the batch."""
    out: dict[str, dict[str, Any]] = {}
    dead: set[str] = set()
    sem = asyncio.Semaphore(concurrency)

    async def one(model: str, case: EvalCase) -> None:
        async with sem:
            if model in dead:
                return
            t = await run_trial(ctx, model, case, profile)
            if t is None:
                dead.add(model)
            else:
                out[f"{model}|{case.id}"] = t
    await asyncio.gather(*(one(m, c) for m in models for c in cases))
    return {k: v for k, v in out.items() if k.split("|")[0] not in dead}


def paired_stats(diffs: list[float], *, seed: int = 7, rounds: int = 2000) -> dict[str, Any]:
    n = len(diffs)
    if n == 0:
        return {"n": 0, "mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "wins": 0, "losses": 0}
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(diffs) for _ in range(n)) / n for _ in range(rounds))
    return {"n": n, "mean": round(sum(diffs) / n, 4), "ci_low": round(means[int(0.025 * rounds)], 4),
            "ci_high": round(means[int(0.975 * rounds) - 1], 4), "wins": sum(d > 0 for d in diffs), "losses": sum(d < 0 for d in diffs)}


def decide(stats: dict[str, Any], tokens_delta: float) -> str:
    if stats["n"] < 6:
        return "inconclusive"
    if stats["ci_low"] > 0 and stats["mean"] >= MIN_EFFECT:
        return "supported"  # better answers
    if stats["ci_high"] < 0:
        return "refuted"
    if stats["ci_low"] > -MARGIN and tokens_delta <= -SAVING:
        return "supported"  # same answers for fewer tokens
    if abs(stats["mean"]) < MIN_EFFECT and stats["ci_low"] > -2 * MARGIN and tokens_delta >= 0:
        return "refuted"  # no gain and not cheaper: keep the default
    return "inconclusive"


def compare(base: dict[str, dict[str, Any]], var: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per model family: paired statistics over the trials both arms have."""
    groups: dict[str, list[str]] = {}
    for key in base.keys() & var.keys():
        groups.setdefault(family(key.split("|")[0]), []).append(key)
    out = {}
    for fam, keys in sorted(groups.items()):
        keys.sort()
        st = paired_stats([var[k]["score"] - base[k]["score"] for k in keys])
        tb, tv = sum(base[k]["tokens"] for k in keys), sum(var[k]["tokens"] for k in keys)
        delta = round((tv - tb) / tb, 4) if tb else 0.0
        out[fam] = {**st, "tokens_delta": delta, "base_score": round(sum(base[k]["score"] for k in keys) / len(keys), 4),
                    "status": decide(st, delta), "models": sorted({k.split("|")[0] for k in keys})}
    return out


async def run_experiments(ctx: StepContext, models: list[str], variants: list[tuple[str, Any]], stage1: list[EvalCase],
                          stage2: list[EvalCase], base: PromptProfile) -> dict[str, Any]:
    baseline = await ctx.memo("base:1", lambda: measure(ctx, models, stage1, base))
    alive = sorted({k.split("|")[0] for k in baseline})
    results: list[dict[str, Any]] = []
    for knob, value in variants:
        prof = base.with_knob(knob, value)
        var = await ctx.memo(f"var:{knob}:{value}:1", lambda p=prof: measure(ctx, alive, stage1, p))
        by_family = compare(baseline, var)
        promising = sorted(f for f, s in by_family.items() if s["status"] == "inconclusive" and abs(s["mean"]) >= MIN_EFFECT)
        if promising and stage2:
            ms = [m for m in alive if family(m) in promising]
            b2 = await ctx.memo(f"base:2:{','.join(ms)}", lambda ms=ms: measure(ctx, ms, stage2, base))
            v2 = await ctx.memo(f"var:{knob}:{value}:2", lambda p=prof, ms=ms: measure(ctx, ms, stage2, p))
            baseline2, var2 = {**baseline, **b2}, {**var, **v2}
            again = compare(baseline2, var2)
            by_family.update({f: {**again[f], "stages": 2} for f in promising if f in again})
        for fam, s in by_family.items():
            results.append({"knob": knob, "value": value, "baseline": getattr(base, knob), "family": fam, **s})
        ctx.log("lab.experiment", {"knob": knob, "value": value,
                                    "families": {f: [s["status"], s["mean"], s["tokens_delta"]] for f, s in by_family.items()}})
    return {"baseline": baseline, "results": results, "models": alive}


async def confirm(ctx: StepContext, models: list[str], adopted: dict[str, dict[str, Any]], holdout: list[EvalCase],
                  base: PromptProfile) -> dict[str, dict[str, Any]]:
    """Combined profile per family vs the default, on unseen cases. Knob effects are not assumed to add up."""
    out: dict[str, dict[str, Any]] = {}
    for fam, knobs in sorted(adopted.items()):
        ms = [m for m in models if family(m) == fam]
        prof = base
        for k, v in knobs.items():
            prof = prof.with_knob(k, v)
        b = await ctx.memo(f"hold:base:{fam}", lambda ms=ms: measure(ctx, ms, holdout, base))
        v = await ctx.memo(f"hold:var:{fam}", lambda ms=ms, p=prof: measure(ctx, ms, holdout, p))
        s = compare(b, v).get(fam) or {"n": 0, "mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "tokens_delta": 0.0}
        better = s["mean"] >= MIN_EFFECT and s["ci_high"] > 0
        cheaper = s["mean"] > -MARGIN and s["tokens_delta"] <= -SAVING / 2
        out[fam] = {**s, "knobs": knobs, "confirmed": bool(s["n"] >= 6 and (better or cheaper))}
    return out
