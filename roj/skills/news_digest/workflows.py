"""Morning brief workflows on the durable kernel.

bootstrap (once, expensive):
  probe seeds -> plan (planner) -> probe extra candidates -> critique (other family)
  -> final plan -> write adapters for pages without feeds (coder + sandbox)
  -> compile DigestSpec -> dry run on captured fixtures -> owner approves activation

run (daily, cheap):
  load -> collect (code) -> events (code) -> classify (model only for what rules missed)
  -> market (code) -> analyse (1 call) -> check (1 call, other family) -> gate (code) -> publish (code)

repair (when a source fails N runs in a row):
  re-probe and rediscover feeds (code, 0 tokens) -> coder only if needed -> new candidate spec -> owner approves
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import yaml

from ...config import ModelBinding
from ...contracts import BudgetSpec, TaskSpec
from ...core.kernel import Step, StepContext, Workflow
from ...hands.market import InstrumentSpec, MarketError, fetch_quote
from ...notify.telegram import NotifyError, send_telegram
from ...prompting.compiler import Document
from ...stats import llm_stats
from ...util import canonical_json, utcnow
from ..registry import Pool, SpecRegistry
from ..sandbox import SandboxError, run_adapter
from ..slots import run_slot
from . import engine
from .engine import Event, ExtractError, Item
from .roles import AdapterCode, Analysis, Classified, Critique, SourcePlan, Verdicts, slot
from .seeds import default_seeds, offline_seeds
from .spec import DigestSpec, SourceSpec

SKILL = "news_digest"
MAX_ADAPTERS = 3


# --- helpers --------------------------------------------------------------------------

def _lang(ctx: StepContext) -> str:
    return ctx.project.settings.language.prompt_language


ROLE_FALLBACK = {"scout": "planner", "assessor": "planner", "rulesmith": "planner"}

# Research depth of the bootstrap (ADR-0002 ResearchSpec). The bootstrap is where tokens are
# spent on purpose: every round here makes the daily run cheaper or better.
DEPTH = {
    "shallow": {"scout_rounds": 1, "samples": 4, "rule_rounds": 0, "target": 0.60},
    "standard": {"scout_rounds": 2, "samples": 8, "rule_rounds": 2, "target": 0.75},
    "deep": {"scout_rounds": 3, "samples": 12, "rule_rounds": 3, "target": 0.85},
}
TOPICS_SUITE = "news_digest.topics"


def _depth(ctx: StepContext) -> dict[str, Any]:
    d = ctx.inputs.get("depth", "deep")
    if d not in DEPTH:
        raise ValueError(f"depth must be one of {', '.join(DEPTH)}")
    return {"name": d, **DEPTH[d]}


def _binding(ctx: StepContext, role: str) -> ModelBinding:
    override = ctx.inputs.get("models") or {}
    if role in override or "*" in override:
        m = override.get(role, override.get("*"))
        return ModelBinding(model=m) if isinstance(m, str) else ModelBinding.model_validate(m)
    try:
        return ctx.project.settings.binding_for(role, SKILL)
    except KeyError:
        if role in ROLE_FALLBACK:
            return _binding(ctx, ROLE_FALLBACK[role])
        raise


def _scope(role: str) -> str:
    return f"skill:{SKILL}/role:{role}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "source"


def load_seeds(ref: str) -> dict[str, Any]:
    if ref == "default":
        return default_seeds()
    if ref.startswith("offline:"):
        return offline_seeds(ref.split(":", 1)[1])
    data = yaml.safe_load(Path(ref).read_text("utf-8"))
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError(f"seed file {ref} must contain 'sources' (and optionally 'goal', 'instruments')")
    data.setdefault("goal", default_seeds()["goal"])
    data.setdefault("instruments", [])
    return data


def compact_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("source_id", "name", "url", "feed_url", "tier", "region", "status", "items", "items_24h",
            "newest_age_h", "has_summaries", "links_on_page", "error")
    return [{**{k: r.get(k) for k in keep}, "sample_titles": r.get("sample_titles", [])[:3]} for r in reports]


def clean_html_excerpt(raw: str, limit: int = 8000) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", "", raw)
    s = re.sub(r"(?s)<!--.*?-->", "", s)
    s = re.sub(r"\s+", " ", s)
    body = re.search(r"(?is)<body[^>]*>(.*)</body>", s)
    s = body.group(1) if body else s
    return s[:limit]


def sanitize_plan(plan: SourcePlan, reports: dict[str, dict[str, Any]]) -> tuple[SourcePlan, list[str]]:
    """Keep only sources that really exist and passed the probe. The model never decides alone."""
    notes, selected, seen = [], [], set()
    for s in plan.selected:
        rep = reports.get(s.source_id)
        if rep is None:
            notes.append(f"dropped unknown source id {s.source_id!r} proposed by the planner")
            continue
        if rep["status"] not in ("ok", "needs_adapter"):
            notes.append(f"dropped {s.source_id}: probe status {rep['status']}")
            continue
        if s.source_id in seen:
            continue
        seen.add(s.source_id)
        selected.append(s)
    if not selected:
        raise ValueError("planner selected no usable source; " + "; ".join(notes))
    return plan.model_copy(update={"selected": selected}), notes


def _task_cost(ctx: StepContext) -> dict[str, Any]:
    s = llm_stats(ctx.project.events, task_id=ctx.task_id)
    return {"calls": s["calls"], "tokens": s["prompt_tokens"] + s["completion_tokens"],
            "cached_tokens": s["cached_tokens"], "cost_usd": s["cost_usd"]}


async def build_adapter(ctx: StepContext, *, source_id: str, name: str, url: str, raw: str,
                        previous_code: str | None = None, previous_error: str | None = None) -> tuple[str | None, str, int]:
    """Coder writes parse(); the sandbox decides. Returns (sha | None, message, tokens)."""
    pool = Pool(ctx.project.inst)
    err = previous_error
    tokens = 0
    excerpt = clean_html_excerpt(raw)
    for attempt in (1, 2):
        task = TaskSpec(
            goal=f"Write parse(raw, base_url) that extracts the news list from the page of {name}",
            deliverable="AdapterCode JSON with the complete Python source in 'code'",
            acceptance=["parse returns at least 3 items, each with a non-empty title and an absolute link",
                        "The code uses only the allowed modules and contains no I/O"],
            output_format="JSON only",
            inputs={"base_url": url, **({"previous_error": err} if err else {}),
                    **({"previous_code": previous_code} if previous_code and attempt == 1 else {})},
            budget=BudgetSpec(tokens=20000),
        )
        res = await run_slot(ctx, slot("adapter-coder", _lang(ctx)), task, binding=_binding(ctx, "coder"),
                             scope=_scope("coder"), documents=[Document("page_excerpt", excerpt, url)])
        tokens += res.tokens
        code = res.value.code if isinstance(res.value, AdapterCode) else ""
        try:
            items = run_adapter(code, raw, url)
            if len(items) < 3:
                raise SandboxError(f"only {len(items)} items extracted")
        except SandboxError as e:
            err = str(e)
            ctx.log("adapter.rejected", {"source_id": source_id, "attempt": attempt, "error": err})
            continue
        row = pool.submit("adapter", f"news:{source_id}", code, project=ctx.project.name, created_by="adapter-coder",
                          card={"summary": f"parse() for {name}", "signature": "parse(raw: str, base_url: str) -> list[dict]",
                                "source_url": url},
                          tests={"fixture_items": len(items), "sample_titles": [i["title"][:80] for i in items[:3]]})
        ctx.log("adapter.accepted", {"source_id": source_id, "sha256": row["sha256"], "items": len(items)})
        return row["sha256"], f"{len(items)} items", tokens
    return None, err or "adapter rejected", tokens


def dry_run(ctx: StepContext, spec: DigestSpec) -> dict[str, Any]:
    """Execute the compiled spec on captured fixtures: no network, no model."""
    pool = Pool(ctx.project.inst)
    per_source, items = {}, []
    for s in spec.sources:
        if not s.fixture_ref:
            per_source[s.id] = "no fixture"
            continue
        code = (pool.by_sha(s.adapter_sha) or {}).get("code") if s.adapter_sha else None
        try:
            its = engine.make_items(s, engine.extract(s, ctx.cas.get_bytes(s.fixture_ref), adapter_code=code),
                                    spec.limits.max_items_per_source)
            per_source[s.id] = len(its)
            items += its
        except ExtractError as e:
            per_source[s.id] = f"error: {e}"
    events = engine.cluster(items, spec.ranking.cluster_threshold)
    for e in events:
        e.topics = engine.classify_rules(e, spec.topics)
    now = utcnow()
    ranked = engine.rank(events, spec, now - timedelta(hours=48), now)
    ok_sources = sum(1 for v in per_source.values() if isinstance(v, int) and v > 0)
    classified = sum(1 for e in ranked if e.topics)
    return {"sources_with_items": ok_sources, "per_source": per_source, "items": len(items), "events": len(events),
            "classified_by_rules": classified, "multi_source_events": sum(1 for e in events if e.independent > 1),
            "top_titles": [e.items[0].title[:100] for e in ranked[:5]]}


# --- bootstrap -----------------------------------------------------------------------

async def b_probe(ctx: StepContext) -> dict[str, Any]:
    seeds = load_seeds(ctx.inputs.get("seeds", "default"))
    now = utcnow()
    reports = await asyncio.gather(*(engine.probe_source(ctx.project.fetcher, s, now) for s in seeds["sources"]))
    instruments, inst_reports = [], []
    for raw in seeds.get("instruments", []):
        spec = InstrumentSpec.model_validate(raw)
        instruments.append(spec.model_dump())
        try:
            q = await fetch_quote(ctx.project.fetcher, spec)
            inst_reports.append({"instrument_id": spec.id, "label": spec.label, "provider": spec.provider,
                                 "status": "ok", "as_of": q.as_of})
        except MarketError as e:
            inst_reports.append({"instrument_id": spec.id, "label": spec.label, "provider": spec.provider,
                                 "status": "error", "error": str(e)[:200]})
    by_status: dict[str, int] = {}
    for r in reports:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"goal": seeds["goal"], "sources": list(reports), "instruments": inst_reports,
            "instrument_specs": instruments, "summary": by_status}


def _plan_task(goal: str, revise: bool) -> TaskSpec:
    return TaskSpec(
        goal=("Revise the source plan using the critique and the probes of extra candidates" if revise else
              "Choose the source portfolio, topic rules and market instruments for the morning brief"),
        deliverable="SourcePlan JSON",
        acceptance=[
            "Every selected source_id exists in probe_report with status ok or needs_adapter",
            "At least 4 topic rules, each with at least 3 keywords",
            "Every rejected source has a reason",
            "At most 8 extra candidates, each with a feed URL",
        ],
        output_format="JSON only",
        inputs={"brief_goal": goal},
        budget=BudgetSpec(tokens=60000),
    )


async def b_plan(ctx: StepContext) -> dict[str, Any]:
    probe = ctx.results["probe"]
    docs = [Document("probe_report", canonical_json({"sources": compact_reports(probe["sources"]),
                                                      "instruments": probe["instruments"]}), "roj probe")]
    res = await run_slot(ctx, slot("news-planner", _lang(ctx)), _plan_task(probe["goal"], False),
                         binding=_binding(ctx, "planner"), scope=_scope("planner"), documents=docs)
    plan, notes = sanitize_plan(res.value, {r["source_id"]: r for r in probe["sources"]})
    return {"plan": plan.model_dump(), "notes": notes, "tokens": res.tokens}


async def b_scout(ctx: StepContext) -> dict[str, Any]:
    """Discovery rounds: the planner's proposals first, then a scout per open coverage gap."""
    cfg = _depth(ctx)
    plan = SourcePlan.model_validate(ctx.results["plan"]["plan"])
    probe = ctx.results["probe"]
    known = {r["url"] for r in probe["sources"]}
    reports: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    closed: set[str] = set()
    gaps = [g for g in plan.gaps if g.strip()]
    now = utcnow()

    async def probe_round(cands, rnd: int) -> None:
        todo = []
        for c in cands:
            if c.url in known or not c.url.startswith(("http://", "https://")):
                continue
            known.add(c.url)
            todo.append(({"id": f"x-{_slug(c.name)}", "name": c.name, "url": c.url, "tier": c.tier, "region": c.region}, c))
        reps = await asyncio.gather(*(engine.probe_source(ctx.project.fetcher, d, now) for d, _ in todo))
        for rep, (_, c) in zip(reps, todo):
            reports.append(rep)
            if rep["status"] == "ok":
                closed.update(g for g in gaps if g.lower() in f"{c.why} {c.region}".lower())
        rounds.append({"round": rnd, "proposed": len(cands), "probed": len(todo),
                       "accepted": [r["source_id"] for r in reps if r["status"] == "ok"]})

    await probe_round(plan.extra_candidates, 1)  # the planner's own proposals: no extra call
    for rnd in range(2, cfg["scout_rounds"] + 1):
        open_gaps = [g for g in gaps if g not in closed] or (["more independent coverage"] if rnd == 2 else [])
        if not open_gaps:
            break
        docs = [Document("coverage", canonical_json({"open_gaps": open_gaps, "round": rnd}), "roj"),
                Document("already_probed", canonical_json(compact_reports(probe["sources"] + reports)), "roj probe")]
        task = TaskSpec(goal="Propose news sources that close the open coverage gaps of the morning brief",
                        deliverable="CandidateList JSON", output_format="JSON only",
                        acceptance=["At most 8 candidates", "No candidate URL appears in already_probed",
                                    "Every candidate says in 'why' which gap it closes"],
                        inputs={"round": rnd}, budget=BudgetSpec(tokens=20000))
        res = await run_slot(ctx, slot("news-scout", _lang(ctx)), task, binding=_binding(ctx, "scout"),
                             scope=_scope("scout"), documents=docs)
        if not res.value.candidates:
            rounds.append({"round": rnd, "proposed": 0, "probed": 0, "accepted": []})
            break
        await probe_round(res.value.candidates, rnd)
    return {"sources": reports, "rounds": rounds, "gaps_closed": sorted(closed),
            "accepted": [r["source_id"] for r in reports if r["status"] == "ok"],
            "rejected": {r["source_id"]: r["status"] for r in reports if r["status"] != "ok"}}


async def b_critique(ctx: StepContext) -> dict[str, Any]:
    probe, extra = ctx.results["probe"], ctx.results["scout"]
    task = TaskSpec(
        goal="Find the weakest points of the source plan for the morning economic brief",
        deliverable="Critique JSON",
        acceptance=["Each issue has a severity and a concrete fix",
                    "verdict is revise only if at least one issue is medium or high"],
        output_format="JSON only", budget=BudgetSpec(tokens=30000),
    )
    docs = [Document("plan", canonical_json(ctx.results["plan"]["plan"]), "planner"),
            Document("probe_report", canonical_json({"sources": compact_reports(probe["sources"] + extra["sources"])}), "roj probe")]
    res = await run_slot(ctx, slot("news-critic", _lang(ctx)), task, binding=_binding(ctx, "critic"),
                         scope=_scope("critic"), documents=docs)
    if _binding(ctx, "critic").model == _binding(ctx, "planner").model:
        ctx.log("quality.warning", {"issue": "critic uses the same model as the planner; use another family"})
    return {"critique": res.value.model_dump(), "tokens": res.tokens}


async def b_finalize(ctx: StepContext) -> dict[str, Any]:
    probe, extra = ctx.results["probe"], ctx.results["scout"]
    critique = Critique.model_validate(ctx.results["critique"]["critique"])
    needs = critique.verdict == "revise" or any(i.severity in ("high", "medium") for i in critique.issues) or extra["accepted"]
    reports = {r["source_id"]: r for r in probe["sources"] + extra["sources"]}
    if not needs:
        return {"plan": ctx.results["plan"]["plan"], "revised": False, "notes": ctx.results["plan"]["notes"]}
    docs = [Document("probe_report", canonical_json({"sources": compact_reports(list(reports.values())),
                                                      "instruments": probe["instruments"]}), "roj probe"),
            Document("plan_v1", canonical_json(ctx.results["plan"]["plan"]), "planner"),
            Document("critique", canonical_json(critique.model_dump()), "critic")]
    res = await run_slot(ctx, slot("news-planner", _lang(ctx)), _plan_task(probe["goal"], True),
                         binding=_binding(ctx, "planner"), scope=_scope("planner"), documents=docs)
    plan, notes = sanitize_plan(res.value, reports)
    return {"plan": plan.model_dump(), "revised": True, "notes": ctx.results["plan"]["notes"] + notes}


async def b_adapters(ctx: StepContext) -> dict[str, Any]:
    plan = SourcePlan.model_validate(ctx.results["finalize"]["plan"])
    reports = {r["source_id"]: r for r in ctx.results["probe"]["sources"] + ctx.results["scout"]["sources"]}
    built, failed = {}, {}
    for s in [s for s in plan.selected if reports[s.source_id]["status"] == "needs_adapter"][:MAX_ADAPTERS]:
        rep = reports[s.source_id]
        raw = ctx.cas.get_text(rep["fixture_ref"])
        sha, msg, _ = await build_adapter(ctx, source_id=s.source_id, name=rep["name"], url=rep["url"], raw=raw)
        (built if sha else failed)[s.source_id] = sha or msg
    return {"built": built, "failed": failed}


def _all_reports(ctx: StepContext) -> dict[str, dict[str, Any]]:
    return {r["source_id"]: r for r in ctx.results["probe"]["sources"] + ctx.results["scout"]["sources"]}


def _sources_from_plan(ctx: StepContext, quality: dict[str, int] | None = None) -> tuple[list[SourceSpec], list[str]]:
    plan = SourcePlan.model_validate(ctx.results["finalize"]["plan"])
    reports = _all_reports(ctx)
    built = ctx.results["adapters"]["built"]
    sources, notes = [], []
    for ps in plan.selected:
        rep = reports[ps.source_id]
        q = (quality or {}).get(ps.source_id)
        weight = min(3.0, ps.weight * (0.7 + 0.1 * q)) if q else ps.weight
        common = dict(id=_slug(ps.source_id), name=rep["name"], tier=ps.tier or rep["tier"], region=ps.region, topics=ps.topics,
                      weight=round(weight, 3), independence_key=ps.independence_key, fixture_ref=rep["fixture_ref"], quality=q)
        if rep["status"] == "ok":
            sources.append(SourceSpec(url=rep["feed_url"] or rep["url"], kind="feed", **common))
        elif ps.source_id in built:
            sources.append(SourceSpec(url=rep["url"], kind="adapter", adapter_sha=built[ps.source_id], **common))
        else:
            notes.append(f"left out {ps.source_id}: no working adapter ({ctx.results['adapters']['failed'].get(ps.source_id)})")
    return sources, notes


def _fixture_items(ctx: StepContext, sources: list[SourceSpec], per_source: int) -> list[Item]:
    pool = Pool(ctx.project.inst)
    items: list[Item] = []
    for s in sources:
        if not s.fixture_ref:
            continue
        code = (pool.by_sha(s.adapter_sha) or {}).get("code") if s.adapter_sha else None
        try:
            items += engine.make_items(s, engine.extract(s, ctx.cas.get_bytes(s.fixture_ref), adapter_code=code), per_source)
        except ExtractError:
            continue
    return items


def _metric_summary(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k != "errors"}


async def b_assess(ctx: StepContext) -> dict[str, Any]:
    """A strong model labels sample items: the teacher's labels become the eval set for the rules."""
    from ...verify.ratchet import Ratchet

    cfg = _depth(ctx)
    plan = SourcePlan.model_validate(ctx.results["finalize"]["plan"])
    sources, _ = _sources_from_plan(ctx)
    items = _fixture_items(ctx, sources, cfg["samples"])
    allowed = [t.name for t in plan.topics]
    labels: dict[str, Any] = {}
    quality: dict[str, list[int]] = {}
    batches = [items[k: k + 60] for k in range(0, len(items), 60)]
    for batch in batches:
        docs = [Document("items", canonical_json([{"item_id": i.id, "source_id": i.source_id, "title": i.title,
                                                   "summary": i.summary[:240]} for i in batch]), "fixtures")]
        task = TaskSpec(goal="Label every item with its economic relevance and topics, and rate each source",
                        deliverable="Labels JSON", output_format="JSON only",
                        acceptance=["Exactly one label per item in the items document", "Topics only from allowed_topics",
                                    "relevance is an integer from 0 to 3", "Each source_id gets a quality from 1 to 5"],
                        inputs={"allowed_topics": allowed}, budget=BudgetSpec(tokens=40000))
        res = await run_slot(ctx, slot("news-assessor", _lang(ctx)), task, binding=_binding(ctx, "assessor"),
                             scope=_scope("assessor"), documents=docs)
        for lab in res.value.items:
            labels[lab.item_id] = lab
        for sa in res.value.sources:
            quality.setdefault(sa.source_id, []).append(sa.quality)
    by_id = {i.id: i for i in items}
    cases = [{"case_id": iid, "text": by_id[iid].snippet(), "source_id": by_id[iid].source_id,
              "topics": sorted(set(lab.topics) & set(allowed)), "relevance": lab.relevance}
             for iid, lab in labels.items() if iid in by_id]
    added = Ratchet(ctx.project).add_cases(TOPICS_SUITE, cases, source=f"bootstrap:{ctx.task_id[:8]}")
    slug_to_plan = {_slug(ps.source_id): ps.source_id for ps in plan.selected}
    q = {slug_to_plan.get(k, k): round(sum(v) / len(v)) for k, v in quality.items() if v}
    return {"labelled": len(cases), "cases_added": added, "batches": len(batches), "quality": q,
            "teacher": {iid: lab.relevance for iid, lab in labels.items()},
            "unlabelled": len([i for i in items if i.id not in labels])}


async def b_rules(ctx: StepContext) -> dict[str, Any]:
    """Rule induction: the rule-smith edits keyword rules, code measures them on the teacher's labels."""
    from ...verify.ratchet import Ratchet

    cfg = _depth(ctx)
    plan = SourcePlan.model_validate(ctx.results["finalize"]["plan"])
    rat = Ratchet(ctx.project)
    every = rat.cases(TOPICS_SUITE)
    visible = [c for c in every if not c["hidden"]]
    hidden = [c for c in every if c["hidden"]]
    names = [t.name for t in plan.topics]
    best = plan.topics
    best_m = engine.rule_metrics(best, visible)
    history = [{"round": 0, **_metric_summary(best_m)}]
    for rnd in range(1, cfg["rule_rounds"] + 1):
        if best_m["score"] >= cfg["target"]:
            break
        docs = [Document("rules", canonical_json([t.model_dump() for t in best]), "current rules"),
                Document("metrics", canonical_json(_metric_summary(best_m)), "roj"),
                Document("errors", canonical_json(best_m["errors"][:40]), "visible eval cases")]
        task = TaskSpec(goal="Improve the keyword rules so they agree with the teacher labels",
                        deliverable="RuleSet JSON", output_format="JSON only",
                        acceptance=["Topic names stay exactly the same", "Every topic keeps at least 3 keywords",
                                    "Fixes at least one error listed in the errors document"],
                        inputs={"target_score": cfg["target"], "round": rnd}, budget=BudgetSpec(tokens=20000))
        res = await run_slot(ctx, slot("rule-smith", _lang(ctx)), task, binding=_binding(ctx, "rulesmith"),
                             scope=_scope("rulesmith"), documents=docs)
        proposed = {t.name: t for t in res.value.topics if t.name in names}
        candidate = [proposed.get(t.name, t) for t in best]
        m = engine.rule_metrics(candidate, visible)
        history.append({"round": rnd, **_metric_summary(m)})
        if m["score"] > best_m["score"]:
            best, best_m = candidate, m
    final_all = engine.rule_metrics(best, every)
    final_hidden = engine.rule_metrics(best, hidden) if hidden else None
    gate = rat.evaluate(TOPICS_SUITE, "score", final_all["score"], ref=ctx.task_id)
    return {"topics": [t.model_dump() for t in best], "rounds": history, "target": cfg["target"],
            "met_target": best_m["score"] >= cfg["target"], "final_visible": _metric_summary(best_m),
            "final_all": _metric_summary(final_all), "final_hidden": _metric_summary(final_hidden) if final_hidden else None,
            "gate_threshold": gate.threshold, "visible_cases": len(visible), "hidden_cases": len(hidden)}


async def b_calibrate(ctx: StepContext) -> dict[str, Any]:
    """Ranking weights fitted to the teacher's relevance (NDCG@6): pure code, zero tokens."""
    from .spec import TopicRule

    sources, _ = _sources_from_plan(ctx)
    topics = [TopicRule.model_validate(t) for t in ctx.results["rules"]["topics"]]
    spec = DigestSpec(goal=ctx.results["probe"]["goal"], sources=sources, topics=topics)
    items = _fixture_items(ctx, sources, spec.limits.max_items_per_source)
    events = engine.cluster(items, spec.ranking.cluster_threshold)
    for e in events:
        e.topics = engine.classify_rules(e, topics)
    teacher = {k: float(v) for k, v in ctx.results["assess"]["teacher"].items()}
    now = utcnow()
    return engine.calibrate_ranking(events, teacher, spec, now - timedelta(hours=48), now)


def render_research(ctx: StepContext, spec: DigestSpec, validation: dict[str, Any]) -> str:
    r = ctx.results
    cost = _task_cost(ctx)
    probe = {x["source_id"]: x for x in r["probe"]["sources"] + r["scout"]["sources"]}
    lines = [f"# Research report: news_digest bootstrap ({_depth(ctx)['name']})", "",
             f"Task `{ctx.task_id}` · {cost['calls']} model calls · {cost['tokens']:,} tokens · ${cost['cost_usd']:.4f} so far", "",
             "## Sources probed", "", "| Source | Status | Items | 24h | Newest (h) | Tier | Region |", "|---|---|---|---|---|---|---|"]
    for x in probe.values():
        lines.append(f"| {x['name']} | {x['status']} | {x['items']} | {x['items_24h']} | {x['newest_age_h'] or ''} | {x['tier']} | {x['region']} |")
    lines += ["", "## Discovery rounds", ""] + [f"- round {d['round']}: proposed {d['proposed']}, probed {d['probed']}, accepted {', '.join(d['accepted']) or 'none'}"
                                                for d in r["scout"]["rounds"]]
    crit = r["critique"]["critique"]
    lines += ["", f"## Critique: {crit['verdict']}", ""] + [f"- **{i['severity']}** {i['issue']} → {i['fix']}" for i in crit["issues"]]
    lines += ["", "## Selected portfolio", "", "| Id | Kind | Tier | Weight | Quality | URL |", "|---|---|---|---|---|---|"]
    lines += [f"| {s.id} | {s.kind} | {s.tier} | {s.weight} | {s.quality or ''} | {s.url} |" for s in spec.sources]
    ru = r["rules"]
    lines += ["", "## Topic rules learned from teacher labels", "",
              f"Teacher labelled {r['assess']['labelled']} items in {r['assess']['batches']} call(s); "
              f"{ru['visible_cases']} visible and {ru['hidden_cases']} hidden test cases.", "",
              "| Round | Score | Macro F1 | Relevance F1 |", "|---|---|---|---|"]
    lines += [f"| {h['round']} | {h['score']} | {h['macro_f1']} | {h['relevance_f1']} |" for h in ru["rounds"]]
    hid = ru["final_hidden"]
    lines += ["", f"Target {ru['target']} {'met' if ru['met_target'] else 'NOT met'}; on hidden cases: "
              f"{hid['score'] if hid else 'n/a'}; gate threshold set to {ru['gate_threshold']:.3f}.", "",
              "| Topic | Keywords |", "|---|---|"]
    lines += [f"| {t.name} | {', '.join(t.keywords)} |" for t in spec.topics]
    ca = r["calibrate"]
    lines += ["", "## Ranking calibration", "", f"NDCG@{ca['k']} {ca['ndcg_before']} → {ca['ndcg_after']} with {ca['weights']} on {ca['events']} events.",
              "", "## Dry run on captured fixtures", "", f"`{json.dumps(validation, ensure_ascii=False)}`", "",
              "## Notes", ""] + [f"- {n}" for n in spec.plan_notes] + [f"- gap: {g}" for g in spec.gaps]
    return "\n".join(lines) + "\n"


async def b_compile(ctx: StepContext) -> dict[str, Any]:
    from .spec import TopicRule

    probe = ctx.results["probe"]
    plan = SourcePlan.model_validate(ctx.results["finalize"]["plan"])
    sources, notes = _sources_from_plan(ctx, ctx.results["assess"]["quality"])
    notes = list(ctx.results["finalize"]["notes"]) + notes
    ok_instruments = {r["instrument_id"] for r in probe["instruments"] if r["status"] == "ok"}
    wanted = {i.instrument_id for i in plan.instruments} or ok_instruments
    instruments = [InstrumentSpec.model_validate(i) for i in probe["instrument_specs"] if i["id"] in ok_instruments & wanted]
    missing = [r for r in probe["instruments"] if r["status"] != "ok"]
    if missing:
        notes.append("market data unavailable: " + "; ".join(f"{r['instrument_id']} ({r.get('error', '')[:80]})" for r in missing))
    ranking = DigestSpec.model_fields["ranking"].default.model_copy(update=ctx.results["calibrate"]["weights"])
    rules = ctx.results["rules"]
    spec = DigestSpec(goal=probe["goal"], sources=sources, instruments=instruments,
                      topics=[TopicRule.model_validate(t) for t in rules["topics"]], ranking=ranking,
                      plan_notes=notes + plan.notes, gaps=[g for g in plan.gaps if g not in ctx.results["scout"]["gaps_closed"]],
                      research={"depth": _depth(ctx)["name"], "rules": rules["final_all"], "rules_hidden": rules["final_hidden"],
                                "ndcg": ctx.results["calibrate"]["ndcg_after"], "labelled": ctx.results["assess"]["labelled"],
                                "discovery_rounds": len(ctx.results["scout"]["rounds"])})
    validation = dry_run(ctx, spec)
    problems = []
    if validation["sources_with_items"] < 2:
        problems.append(f"only {validation['sources_with_items']} sources produce items")
    if validation["events"] < 3:
        problems.append(f"only {validation['events']} events on fixtures")
    if validation["classified_by_rules"] == 0:
        problems.append("topic rules classify nothing")
    if problems:
        raise ValueError("compiled spec failed validation: " + "; ".join(problems))
    report = render_research(ctx, spec, validation)
    rdir = ctx.project.dir / "reports"
    rdir.mkdir(exist_ok=True)
    path = rdir / f"research-news_digest-{utcnow().strftime('%Y-%m-%d')}-{ctx.task_id[:8]}.md"
    path.write_text(report, encoding="utf-8")
    return {"spec": spec.model_dump(mode="json"), "validation": validation, "research_report": str(path)}


async def b_register(ctx: StepContext) -> dict[str, Any]:
    reg = SpecRegistry(ctx.project)
    row = ctx.project.store.one("SELECT version FROM skill_specs WHERE skill = ? AND task_id = ?", (SKILL, ctx.task_id))
    spec = ctx.results["compile"]["spec"]
    version = row["version"] if row else reg.add(SKILL, spec, created_by="bootstrap", task_id=ctx.task_id,
                                                  notes="; ".join(spec["plan_notes"])[:2000])
    adapters = [s["adapter_sha"] for s in spec["sources"] if s.get("adapter_sha")]
    ctx.require("t2.activate_skill", {"skill": SKILL, "version": version,
                                      "sources": [f"{s['id']} ({s['kind']}, {s['tier']})" for s in spec["sources"]],
                                      "instruments": [i["id"] for i in spec["instruments"]],
                                      "adapters_to_install": adapters,
                                      "validation": ctx.results["compile"]["validation"],
                                      "research_report": ctx.results["compile"]["research_report"]})
    reg.activate(SKILL, version, by="owner")
    return {"version": version, "activated": True, "sources": len(spec["sources"]),
            "adapters": len(adapters), "bootstrap": _task_cost(ctx)}


BOOTSTRAP = Workflow("news_digest.bootstrap", (
    Step("probe", b_probe), Step("plan", b_plan), Step("scout", b_scout), Step("critique", b_critique),
    Step("finalize", b_finalize), Step("adapters", b_adapters), Step("assess", b_assess), Step("rules", b_rules),
    Step("calibrate", b_calibrate), Step("compile", b_compile), Step("register", b_register),
), "R&D: discover and measure sources, learn topic rules from teacher labels, calibrate ranking, owner activates")


# --- daily run -----------------------------------------------------------------------

def _spec(ctx: StepContext) -> DigestSpec:
    return DigestSpec.model_validate(SpecRegistry(ctx.project).get(SKILL, ctx.results["load"]["version"]))


async def r_load(ctx: StepContext) -> dict[str, Any]:
    reg = SpecRegistry(ctx.project)
    active = reg.active(SKILL)
    if not active:
        raise ValueError("no active news_digest spec: run `roj skill bootstrap news_digest` and approve it first")
    version, raw = active
    spec = DigestSpec.model_validate(raw)
    now = utcnow()
    last = reg.last_window_end(SKILL)
    start = _dt(last) if last else now - timedelta(hours=spec.window.fallback_hours)
    start = max(start, now - timedelta(hours=spec.window.max_hours))
    if start >= now:
        start = now - timedelta(hours=1)
    return {"version": version, "window_start": _iso(start), "window_end": _iso(now)}


async def r_collect(ctx: StepContext) -> dict[str, Any]:
    spec, reg, pool = _spec(ctx), SpecRegistry(ctx.project), Pool(ctx.project.inst)
    start, end = _dt(ctx.results["load"]["window_start"]), _dt(ctx.results["load"]["window_end"])
    results = await engine.fetch_sources(ctx.project.fetcher, spec.sources)
    items: list[Item] = []
    status = []
    for s in spec.sources:
        r = results[s.id]
        try:
            if not r.ok:
                raise ExtractError(r.error or f"HTTP {r.status}")
            code = (pool.by_sha(s.adapter_sha) or {}).get("code") if s.adapter_sha else None
            its = engine.make_items(s, engine.extract(s, r.content, adapter_code=code), spec.limits.max_items_per_source)
            if not its:
                raise ExtractError("0 items")
            fails = reg.record_health(SKILL, s.id, True)
            if s.adapter_sha:
                pool.record_use(s.adapter_sha, ctx.project.name)
            window = engine.in_window(its, start, end)
            items += window
            status.append({"source_id": s.id, "ok": True, "items": len(its), "in_window": len(window), "from_cache": r.from_cache})
        except ExtractError as e:
            fails = reg.record_health(SKILL, s.id, False, str(e))
            status.append({"source_id": s.id, "ok": False, "error": str(e)[:200], "consecutive_failures": fails})
    broken = [st["source_id"] for st in status if not st["ok"] and st["consecutive_failures"] >= spec.limits.repair_after_failures]
    repair_task = None
    if broken:
        pending = ctx.project.store.one("SELECT id FROM tasks WHERE workflow = ? AND status IN ('pending','running','waiting_approval')",
                                        (REPAIR_REF,))
        repair_task = pending["id"] if pending else ctx.project.kernel.submit(REPAIR_REF, {"source_ids": broken})
        ctx.log("alert", {"kind": "source_broken", "sources": broken, "repair_task": repair_task})
    if not items:
        raise ValueError("no items from any source in the window")
    return {"items": [i.__dict__ for i in items], "sources": status, "repair_task": repair_task}


def _history(ctx: StepContext, days: int = 3) -> list[set[str]]:
    rows = ctx.project.store.all("SELECT tokens FROM digest_history ORDER BY day DESC LIMIT 200")
    return [set(json.loads(r["tokens"])) for r in rows][: days * 10]


async def r_events(ctx: StepContext) -> dict[str, Any]:
    spec = _spec(ctx)
    items = [Item(**d) for d in ctx.results["collect"]["items"]]
    events = engine.cluster(items, spec.ranking.cluster_threshold)
    for e in events:
        e.topics = engine.classify_rules(e, spec.topics)
    engine.mark_continuing(events, _history(ctx))
    start, end = _dt(ctx.results["load"]["window_start"]), _dt(ctx.results["load"]["window_end"])
    ranked = engine.rank(events, spec, start, end)[: spec.limits.candidate_events]
    return {"candidates": [e.as_dict() for e in ranked], "total_events": len(events),
            "unclassified": [e.id for e in ranked if not e.topics]}


async def r_classify(ctx: StepContext) -> dict[str, Any]:
    ev = ctx.results["events"]
    if not ev["unclassified"]:
        return {"skipped": True, "candidates": ev["candidates"]}
    spec = _spec(ctx)
    events = {d["id"]: Event.from_dict(d) for d in ev["candidates"]}
    names = [t.name for t in spec.topics]
    docs = [Document("events", canonical_json([
        {"event_id": i, "headlines": [it.title for it in events[i].items[:3]], "summary": events[i].items[0].summary[:200]}
        for i in ev["unclassified"]]), "roj")]
    task = TaskSpec(goal="Tag each event with topics and rate its relevance for the world economy",
                    deliverable="Classified JSON", output_format="JSON only",
                    acceptance=["Exactly one entry per event in the events document", "Topics come only from the allowed list",
                                "relevance is an integer from 0 to 3"],
                    inputs={"allowed_topics": names}, budget=BudgetSpec(tokens=10000))
    res = await run_slot(ctx, slot("news-classifier", _lang(ctx)), task, binding=_binding(ctx, "classifier"),
                         scope=_scope("classifier"), documents=docs)
    for c in res.value.events if isinstance(res.value, Classified) else []:
        if c.event_id in events:
            e = events[c.event_id]
            e.topics.update({t: 1.0 for t in c.topics if t in names})
            e.relevance = c.relevance
    start, end = _dt(ctx.results["load"]["window_start"]), _dt(ctx.results["load"]["window_end"])
    ranked = engine.rank(list(events.values()), spec, start, end)
    return {"skipped": False, "candidates": [e.as_dict() for e in ranked]}


async def r_market(ctx: StepContext) -> dict[str, Any]:
    quotes, errors = await engine.fetch_market(ctx.project.fetcher, _spec(ctx).instruments)
    return {"quotes": [q.model_dump() for q in quotes], "errors": errors}


def _top(ctx: StepContext) -> list[Event]:
    spec = _spec(ctx)
    cands = [Event.from_dict(d) for d in ctx.results["classify"]["candidates"]]
    relevant = [e for e in cands if e.topics and (e.relevance is None or e.relevance > 0)]
    return (relevant or cands)[: spec.limits.analysed_events]


async def r_analyse(ctx: StepContext) -> dict[str, Any]:
    top = _top(ctx)
    docs = [
        Document("events", canonical_json([{
            "event_id": e.id, "continuing_story": e.continuing, "topics": sorted(e.topics),
            "items": [{"item_id": i.id, "source": i.source_name, "tier": i.tier, "title": i.title,
                       "summary": i.summary[:350], "published": i.published} for i in e.items[:5]],
        } for e in top]), "roj collect"),
        Document("market_table", canonical_json([{"label": q["label"], "value": q["value"], "change_pct": q["change_pct"],
                                                   "as_of": q["as_of"]} for q in ctx.results["market"]["quotes"]]), "market data"),
    ]
    task = TaskSpec(
        goal="Explain what happened overnight in these events and why it matters for the world economy and markets",
        deliverable="Analysis JSON for the morning brief",
        acceptance=[f"Exactly one entry per event in the events document ({len(top)} events)",
                    "Every claim cites at least one item_id of the same event",
                    "Numbers appear only if present in the cited items or in market_table",
                    "Each headline has at most 12 words", "watch_today has at most 5 entries"],
        output_format="JSON only", budget=BudgetSpec(tokens=40000),
    )
    res = await run_slot(ctx, slot("news-analyst", _lang(ctx)), task, binding=_binding(ctx, "analyst"),
                         scope=_scope("analyst"), documents=docs)
    return {"analysis": res.value.model_dump(), "event_ids": [e.id for e in top]}


async def r_check(ctx: StepContext) -> dict[str, Any]:
    analysis = Analysis.model_validate(ctx.results["analyse"]["analysis"])
    items = {i.id: i for e in _top(ctx) for i in e.items}
    claims = [{"event_id": ae.event_id, "claim_index": k, "text": c.text,
               "cited": [{"item_id": i, "snippet": items[i].snippet()[:500]} for i in c.item_ids if i in items]}
              for ae in analysis.events for k, c in enumerate(ae.claims)]
    task = TaskSpec(goal="Check each claim against the snippets it cites", deliverable="Verdicts JSON",
                    acceptance=["Exactly one verdict per claim", "supported is true only if the cited snippets state the claim",
                                "Every number in a supported claim matches a cited snippet"],
                    output_format="JSON only", budget=BudgetSpec(tokens=20000))
    res = await run_slot(ctx, slot("news-checker", _lang(ctx)), task, binding=_binding(ctx, "checker"),
                         scope=_scope("checker"), documents=[Document("claims", canonical_json(claims), "analyst")])
    if _binding(ctx, "checker").model == _binding(ctx, "analyst").model:
        ctx.log("quality.warning", {"issue": "checker uses the same model as the analyst; use another family"})
    return {"verdicts": res.value.model_dump()["verdicts"] if isinstance(res.value, Verdicts) else []}


async def r_gate(ctx: StepContext) -> dict[str, Any]:
    spec = _spec(ctx)
    from ...hands.market import Quote

    quotes = [Quote.model_validate(q) for q in ctx.results["market"]["quotes"]]
    top = {e.id: e for e in _top(ctx)}
    analysis = Analysis.model_validate(ctx.results["analyse"]["analysis"])
    verdict = {(v["event_id"], v["claim_index"]): v["supported"] for v in ctx.results["check"]["verdicts"]}
    notes, out = [], []
    all_snippets = [i.snippet() for e in top.values() for i in e.items]
    for ae in analysis.events:
        ev = top.get(ae.event_id)
        if ev is None:
            notes.append(f"ignored analysis of unknown event {ae.event_id}")
            continue
        ids = {i.id for i in ev.items}
        supported = [c for k, c in enumerate(ae.claims) if verdict.get((ae.event_id, k)) and set(c.item_ids) <= ids]
        if not supported:
            notes.append(f"dropped '{ae.headline}': no claim passed verification")
            continue
        allowed = engine.allowed_numbers([i.snippet() for i in ev.items], quotes)
        summary, d1 = engine.drop_unsupported_sentences(ae.summary, allowed)
        why, d2 = engine.drop_unsupported_sentences(ae.why_it_matters, allowed)
        headline = ae.headline if not engine.unsupported_numbers(ae.headline, allowed) else ev.items[0].title
        if d1 or d2:
            notes.append(f"'{headline}': removed {len(d1) + len(d2)} sentence(s) with numbers not found in sources")
        if not summary:
            summary, _ = engine.drop_unsupported_sentences(supported[0].text, allowed)
        if not summary:
            notes.append(f"dropped '{headline}': nothing left after the numbers gate")
            continue
        cited = {i for c in supported for i in c.item_ids}
        sources, seen = [], set()
        for it in ev.items:
            if it.id in cited and it.source_id not in seen:
                seen.add(it.source_id)
                sources.append({"name": it.source_name, "link": it.link})
        out.append({"event_id": ev.id, "headline": headline, "summary": summary, "why": why, "channels": ae.channels,
                    "confidence": ae.confidence, "continuing": ev.continuing, "sources": sources,
                    "tokens": ev.tokens or sorted(engine.tokens(ev.items[0].title))})
    if len(out) < spec.limits.min_events:
        raise ValueError("no event passed the gates: " + "; ".join(notes))
    allowed_all = engine.allowed_numbers(all_snippets, quotes)
    watch = [w for w in analysis.watch_today if not engine.unsupported_numbers(w, allowed_all)]
    one_line, _ = engine.drop_unsupported_sentences(analysis.one_line, allowed_all)
    errors = ctx.results["market"]["errors"]
    if errors:
        notes.append("market data missing: " + ", ".join(sorted(errors)))
    failed_sources = [s["source_id"] for s in ctx.results["collect"]["sources"] if not s["ok"]]
    if failed_sources:
        notes.append("sources failed today: " + ", ".join(failed_sources))
    tz = ZoneInfo(ctx.project.settings.timezone)
    fmt = lambda s: _dt(s).astimezone(tz).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    report = {
        "date": _dt(ctx.results["load"]["window_end"]).astimezone(tz).strftime("%Y-%m-%d"),
        "window_start": fmt(ctx.results["load"]["window_start"]), "window_end": fmt(ctx.results["load"]["window_end"]),
        "one_line": one_line, "events": out, "watch": watch, "notes": notes,
        "markets": [{"label": q.label, "value": q.display_value(), "change": q.display_change(), "as_of": q.as_of,
                     "provider": q.provider} for q in quotes],
        "stats": {"sources_ok": sum(1 for s in ctx.results["collect"]["sources"] if s["ok"]),
                  "items": len(ctx.results["collect"]["items"]), "events": ctx.results["events"]["total_events"]},
    }
    return {"report": report}


async def r_publish(ctx: StepContext) -> dict[str, Any]:
    spec = _spec(ctx)
    report = ctx.results["gate"]["report"]
    md = engine.render_markdown(report)
    reports_dir = ctx.project.dir / "reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"{report['date']}.md"
    path.write_text(md, encoding="utf-8")
    ref = ctx.cas.put_text(md)
    tg_text = engine.render_telegram(report, spec.limits.telegram_events)
    deliver_at = ctx.inputs.get("deliver_at")
    if deliver_at:
        tz = ZoneInfo(ctx.project.settings.timezone)
        now_local = datetime.now(tz)
        h, m = map(int, deliver_at.split(":"))
        target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        wait = (target - now_local).total_seconds()
        if 0 < wait <= 3 * 3600:
            ctx.log("publish.waiting", {"until": target.isoformat(), "seconds": round(wait)})
            await asyncio.sleep(wait)
    telegram: dict[str, Any] = {"sent": False}
    if ctx.inputs.get("send", True):
        try:
            ctx.require("t1.notify_owner", {"channel": "telegram", "report_ref": ref})
            ids = await send_telegram(ctx.project.settings.notify, tg_text,
                                      buttons=[[(str(n), f"rt:{ctx.task_id[:16]}:{n}") for n in range(1, 6)]])
            telegram = {"sent": True, "message_ids": ids}
        except NotifyError as e:
            telegram = {"sent": False, "reason": str(e)}
            ctx.log("publish.telegram_skipped", {"reason": str(e)})
    else:
        telegram = {"sent": False, "reason": "send=false"}
    with ctx.project.store.tx() as c:
        for e in report["events"]:
            c.execute("INSERT OR IGNORE INTO digest_history(day, fingerprint, headline, tokens) VALUES(?,?,?,?)",
                      (report["date"], engine.fingerprint(e["tokens"]), e["headline"], json.dumps(sorted(e["tokens"]))))
    SpecRegistry(ctx.project).mark_run(SKILL, ctx.task_id, ctx.results["load"]["window_end"])
    return {"report_path": str(path), "report_ref": ref, "telegram": telegram, "events": len(report["events"]),
            "run": _task_cost(ctx)}


RUN = Workflow("news_digest.run", (
    Step("load", r_load), Step("collect", r_collect), Step("events", r_events), Step("classify", r_classify),
    Step("market", r_market), Step("analyse", r_analyse), Step("check", r_check), Step("gate", r_gate),
    Step("publish", r_publish),
), "daily morning brief from the active spec")


# --- repair --------------------------------------------------------------------------

async def p_probe(ctx: StepContext) -> dict[str, Any]:
    reg = SpecRegistry(ctx.project)
    active = reg.active(SKILL)
    if not active:
        raise ValueError("nothing to repair: no active spec")
    version, raw = active
    spec = DigestSpec.model_validate(raw)
    now = utcnow()
    out = []
    for sid in ctx.inputs.get("source_ids", []):
        try:
            s = spec.source(sid)
        except KeyError:
            continue
        rep = None
        # deterministic first: the same URL, its directory, the site root (feed moved?)
        parts = urlsplit(s.url)
        for url in dict.fromkeys([s.url, urljoin(s.url, "./"), f"{parts.scheme}://{parts.netloc}/"]):
            r = await engine.probe_source(ctx.project.fetcher, {"id": s.id, "name": s.name, "url": url, "tier": s.tier}, now)
            if r["status"] == "ok" or (s.kind == "adapter" and r["status"] == "needs_adapter" and url == s.url):
                rep = r
                break
        out.append({"source_id": sid, "kind": s.kind, "report": rep})
    return {"version": version, "probes": out}


async def p_fix(ctx: StepContext) -> dict[str, Any]:
    spec = DigestSpec.model_validate(SpecRegistry(ctx.project).get(SKILL, ctx.results["probe"]["version"]))
    pool = Pool(ctx.project.inst)
    fixes, unfixed = {}, {}
    for p in ctx.results["probe"]["probes"]:
        s, rep = spec.source(p["source_id"]), p["report"]
        if rep and rep["status"] == "ok":
            fixes[s.id] = {"kind": "feed", "url": rep["feed_url"] or rep["url"], "fixture_ref": rep["fixture_ref"],
                           "adapter_sha": None, "how": "feed rediscovered (0 tokens)"}
            continue
        if rep and rep["status"] == "needs_adapter":
            old = (pool.by_sha(s.adapter_sha) or {}).get("code") if s.adapter_sha else None
            err = None
            if old:
                try:
                    run_adapter(old, ctx.cas.get_text(rep["fixture_ref"]), s.url)
                except SandboxError as e:
                    err = str(e)
            sha, msg, tokens = await build_adapter(ctx, source_id=s.id, name=s.name, url=s.url,
                                                   raw=ctx.cas.get_text(rep["fixture_ref"]), previous_code=old, previous_error=err)
            if sha:
                fixes[s.id] = {"kind": "adapter", "url": s.url, "fixture_ref": rep["fixture_ref"], "adapter_sha": sha,
                               "how": f"adapter rewritten ({tokens} tokens)"}
                continue
            unfixed[s.id] = msg
            continue
        unfixed[s.id] = (rep or {}).get("status", "unreachable")
    return {"fixes": fixes, "unfixed": unfixed}


async def p_compile(ctx: StepContext) -> dict[str, Any]:
    spec = DigestSpec.model_validate(SpecRegistry(ctx.project).get(SKILL, ctx.results["probe"]["version"]))
    fixes, unfixed = ctx.results["fix"]["fixes"], ctx.results["fix"]["unfixed"]
    sources = []
    for s in spec.sources:
        if s.id in fixes:
            f = fixes[s.id]
            sources.append(s.model_copy(update={k: f[k] for k in ("kind", "url", "fixture_ref", "adapter_sha")}))
        elif s.id in unfixed:
            continue
        else:
            sources.append(s)
    notes = [f"repair: {sid} {f['how']}" for sid, f in fixes.items()] + [f"repair: removed {sid} ({why})" for sid, why in unfixed.items()]
    new = spec.model_copy(update={"sources": sources, "plan_notes": spec.plan_notes + notes})
    validation = dry_run(ctx, new)
    if validation["sources_with_items"] < 2:
        raise ValueError(f"repaired spec has only {validation['sources_with_items']} working sources")
    return {"spec": new.model_dump(mode="json"), "validation": validation, "notes": notes}


async def p_register(ctx: StepContext) -> dict[str, Any]:
    reg = SpecRegistry(ctx.project)
    row = ctx.project.store.one("SELECT version FROM skill_specs WHERE skill = ? AND task_id = ?", (SKILL, ctx.task_id))
    spec = ctx.results["compile"]["spec"]
    version = row["version"] if row else reg.add(SKILL, spec, created_by="repair", task_id=ctx.task_id,
                                                  notes="; ".join(ctx.results["compile"]["notes"]))
    ctx.require("t2.activate_skill", {"skill": SKILL, "version": version, "repair": ctx.results["compile"]["notes"],
                                      "adapters_to_install": [s["adapter_sha"] for s in spec["sources"] if s.get("adapter_sha")]})
    reg.activate(SKILL, version, by="owner")
    return {"version": version, "activated": True, "repair": _task_cost(ctx)}


REPAIR = Workflow("news_digest.repair", (
    Step("probe", p_probe), Step("fix", p_fix), Step("compile", p_compile), Step("register", p_register),
), "fix broken sources: rediscover feeds first, rewrite adapters only if needed")

BOOTSTRAP_REF = "roj.skills.news_digest.workflows:BOOTSTRAP"
RUN_REF = "roj.skills.news_digest.workflows:RUN"
REPAIR_REF = "roj.skills.news_digest.workflows:REPAIR"
