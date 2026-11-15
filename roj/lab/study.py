"""The Lab team's study: what each model can do and how each model family should be prompted.

  inventory   which models, which cases, does it fit the budget (no tokens)
  probe       technical card per model: latency, usage reporting, provider cache, tool calling
  research    read the providers' own prompting guides into a knowledge pack; claims are kept
              as labelled hypotheses with their source (never applied without an experiment)
  experiment  paired A/B per prompt knob, scored by code
  confirm     adopted changes re-tested together on hold-out cases
  report      research report + Playbook proposal + model cards
  activate    owner approval (t2.activate_playbook) -> the compiler starts using the profiles
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import ModelBinding
from ..contracts import BudgetSpec, TaskSpec
from ..core.kernel import Step, StepContext, Workflow
from ..models.errors import AllModelsFailed
from ..prompting.compiler import Document, RoleSpec
from ..prompting.profiles import KNOBS, PromptProfile, family
from ..skills.slots import Slot, run_slot
from ..util import day_key, estimate_tokens
from . import playbook as pbk
from .experiments import compile_case, confirm, run_experiments
from .tasks import KINDS, make_cases

SEEDS = [  # official guides; unreachable ones are skipped and reported
    ("anthropic-prompting", "claude", "https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview"),
    ("openai-prompting", "gpt", "https://platform.openai.com/docs/guides/prompt-engineering"),
    ("gemini-prompting", "gemini", "https://ai.google.dev/gemini-api/docs/prompting-strategies"),
    ("deepseek-docs", "deepseek", "https://api-docs.deepseek.com/"),
]
PACK = "prompting"
RESEARCHER = RoleSpec("lab-researcher", (
    "Czytasz oficjalne poradniki dostawców modeli i wypisujesz z nich konkretne, sprawdzalne zalecenia dotyczące pisania promptów. "
    "Każde zalecenie przypisujesz do rodziny modeli i do dokumentu źródłowego. Jeśli zalecenie odpowiada jednemu z pokręteł "
    "kompilatora podanych w danych wejściowych, wskazujesz pokrętło i wartość; w przeciwnym razie zostawiasz knob pusty."),
    rules=("Nie dodawaj zaleceń, których nie ma w dokumentach.",))


class ReadClaim(BaseModel):
    family: str
    claim: str = Field(min_length=10, max_length=400)
    source: str
    knob: str | None = None
    value: Any = None


class ReadClaims(BaseModel):
    claims: list[ReadClaim] = Field(default_factory=list, max_length=24)


def _bind(ctx: StepContext, *roles: str) -> ModelBinding:
    for r in roles:
        try:
            return ctx.project.settings.binding_for(r)
        except KeyError:
            continue
    raise KeyError(f"bind one of these roles in roj.toml: {', '.join(roles)}")


def bound_models(ctx: StepContext) -> list[str]:
    s = ctx.project.settings
    out: list[str] = []
    for b in list(s.models.values()) + [ModelBinding.model_validate(r) for cfg in s.skills.values() for r in (cfg.get("models") or {}).values()]:
        out += b.candidates()
    return sorted({m for m in out if "embed" not in m.lower()})


def holdout_offset(home: Path) -> int:
    path = pbk.pb_dir(home) / ".holdout"
    if not path.exists():
        path.write_text(str(random.SystemRandom().randrange(100_000, 800_000)), encoding="utf-8")
    return int(path.read_text("utf-8").strip())


def _variants(base: PromptProfile, only: list[str] | None) -> list[tuple[str, Any]]:
    out = []
    for knob, values in KNOBS.items():
        if only and knob not in only:
            continue
        out.append((knob, next(v for v in values if v != getattr(base, knob))))
    return out


# --- steps -------------------------------------------------------------------------------------

async def s_inventory(ctx: StepContext) -> dict[str, Any]:
    inp = ctx.inputs
    models = inp.get("models") or bound_models(ctx)
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if not models:
        raise ValueError("no models to study: bind roles in roj.toml or pass models=a,b,c")
    try:
        available = {str(m.get("id")) for m in await ctx.project.inst.client.list_models()}
        missing = [m for m in models if m not in available]
        models = [m for m in models if m in available]
    except Exception as e:  # noqa: BLE001 - the gateway may not list models; trials will tell
        missing = []
        ctx.log("lab.inventory_no_list", {"error": str(e)[:200]})
    base = PromptProfile(language=ctx.project.settings.language.prompt_language)
    knobs = inp.get("knobs")
    variants = _variants(base, knobs.split(",") if isinstance(knobs, str) else knobs)
    per_kind, kinds = int(inp.get("per_kind", 2)), list(inp.get("kinds") or KINDS)
    budget = int(inp.get("budget_tokens", 400_000))

    def estimate(pk: int, ks: list[str]) -> int:
        cases = make_cases(pk, kinds=ks)
        avg = sum(estimate_tokens(json.dumps(compile_case(c, base, ctx.project.settings).messages)) + 120 for c in cases) / len(cases)
        return int(len(models) * len(cases) * (1 + len(variants)) * avg * 1.35)
    notes = []
    while estimate(per_kind, kinds) > budget and per_kind > 1:
        per_kind -= 1
        notes.append(f"per_kind reduced to {per_kind} to fit the budget")
    if estimate(per_kind, kinds) > budget and "needle" in kinds:
        kinds.remove("needle")
        notes.append("long-context kind 'needle' dropped to fit the budget")
    est = estimate(per_kind, kinds)
    if est > budget:
        raise ValueError(f"study needs about {est:,} tokens for {len(models)} models, budget_tokens is {budget:,}: "
                         "raise budget_tokens or pass fewer models/knobs")
    return {"models": models, "missing": missing, "variants": variants, "per_kind": per_kind, "kinds": kinds,
            "estimated_tokens": est, "budget_tokens": budget, "notes": notes, "base": base.as_dict()}


TOOL = {"type": "function", "function": {"name": "get_time", "description": "Current time in a timezone",
                                         "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}},
                                                        "required": ["timezone"]}}}


async def probe_model(ctx: StepContext, model: str) -> dict[str, Any]:
    b = ModelBinding(model=model)
    card: dict[str, Any] = {"model": model, "family": family(model), "available": True}
    kw = {"binding": b, "agent": "probe", "scope": "team:lab/agent:probe", "use_cache": False}
    try:
        r = await ctx.chat(messages=[{"role": "user", "content": "Reply with the single word OK."}], purpose="probe:ping",
                           params={"temperature": 0, "max_tokens": 8}, **kw)
        card.update(latency_s=round(r.latency_s, 2), usage_reported=not r.usage.estimated)
        prefix = "Reference text for a cache probe. " * 300
        msgs = [{"role": "system", "content": prefix}, {"role": "user", "content": "Reply with the single word OK."}]
        await ctx.chat(messages=msgs, purpose="probe:cache1", params={"temperature": 0, "max_tokens": 8}, **kw)
        r2 = await ctx.chat(messages=msgs, purpose="probe:cache2", params={"temperature": 0, "max_tokens": 8}, **kw)
        card["provider_cache"] = r2.usage.cached_tokens > 0
        try:
            r3 = await ctx.chat(messages=[{"role": "user", "content": "Call the tool get_time for Europe/Warsaw."}], purpose="probe:tools",
                                params={"temperature": 0, "max_tokens": 80, "tools": [TOOL], "tool_choice": "auto"}, **kw)
            card["tool_calling"] = bool(r3.tool_calls)
        except AllModelsFailed:
            card["tool_calling"] = False
    except AllModelsFailed as e:
        card.update(available=False, error=str(e)[:200])
    return card


async def s_probe(ctx: StepContext) -> dict[str, Any]:
    cards = {}
    for m in ctx.results["inventory"]["models"]:
        cards[m] = await ctx.memo(f"probe:{m}", lambda m=m: probe_model(ctx, m))
    return {"cards": cards}


async def s_research(ctx: StepContext) -> dict[str, Any]:
    if not ctx.inputs.get("research", True):
        return {"claims": [], "read": [], "skipped": ["research disabled"]}
    from ..hands.feeds import html_to_text
    from ..knowledge.packs import KnowledgeBase

    kb = KnowledgeBase(ctx.project)
    if PACK not in {p["pack"] for p in kb.packs()}:
        kb.create(PACK, "Official prompting guides of model providers: structure, examples, reasoning, long documents, tools",
                  audience=["lab-researcher", "architect", "planner"])
    read, skipped = [], []
    for sid, fam, url in [tuple(x) for x in (ctx.inputs.get("seeds") or SEEDS)]:
        r = await ctx.project.fetcher.get(url, purpose="lab:research")
        text = html_to_text(r.text(), limit=None) if r.ok else ""
        if len(text) < 400:
            skipped.append(f"{sid}: {r.error or ('HTTP ' + str(r.status)) if not r.ok else 'too little text (page needs a browser)'}")
            continue
        await kb.add_text(PACK, sid, f"{fam} prompting guide", text[:60_000], source=url)
        read.append({"id": sid, "family": fam, "url": url, "chars": len(text)})
    if not read:
        return {"claims": [], "read": [], "skipped": skipped}
    docs = []
    for d in read:
        hits = await kb.search(PACK, f"{d['family']} prompt structure examples reasoning documents format instructions", k=12)
        docs += [Document(f"{h.doc_id}#{h.chunk_id}", h.text[:1500], h.source) for h in hits if h.doc_id == d["id"]][:3]
    task = TaskSpec(goal="Wypisz zalecenia dotyczące pisania promptów z dostarczonych fragmentów poradników",
                    deliverable="ReadClaims JSON", output_format="Wyłącznie JSON",
                    acceptance=["Każde zalecenie zawiera identyfikator dokumentu źródłowego w polu source",
                                "Co najwyżej 6 zaleceń na rodzinę modeli", "Pole knob zawiera wyłącznie nazwę z listy knobs albo null"],
                    inputs={"knobs": KNOBS, "families": sorted({d["family"] for d in read})}, budget=BudgetSpec(tokens=30_000))
    res = await run_slot(ctx, Slot("lab-researcher", RESEARCHER, ReadClaims, max_tokens=2500), task,
                         binding=_bind(ctx, "researcher", "planner", "analyst"), scope="team:lab/agent:researcher", documents=docs)
    known = {d.id for d in docs}
    claims = [c.model_dump() for c in res.value.claims if c.source in known]  # a claim without a real source is dropped
    return {"claims": claims, "read": read, "skipped": skipped}


async def s_experiment(ctx: StepContext) -> dict[str, Any]:
    inv = ctx.results["inventory"]
    models = [m for m in inv["models"] if ctx.results["probe"]["cards"].get(m, {}).get("available", True)]
    base = PromptProfile.from_dict(inv["base"])
    stage1 = make_cases(inv["per_kind"], kinds=inv["kinds"])
    stage2 = make_cases(inv["per_kind"], offset=1000, kinds=inv["kinds"])
    return await run_experiments(ctx, models, [tuple(v) for v in inv["variants"]], stage1, stage2, base)


async def s_confirm(ctx: StepContext) -> dict[str, Any]:
    inv, exp = ctx.results["inventory"], ctx.results["experiment"]
    adopted: dict[str, dict[str, Any]] = {}
    for r in exp["results"]:
        if r["status"] == "supported":
            adopted.setdefault(r["family"], {})[r["knob"]] = r["value"]
    holdout = make_cases(inv["per_kind"], offset=holdout_offset(ctx.project.inst.home), kinds=inv["kinds"])
    return {"adopted": adopted, "holdout": await confirm(ctx, exp["models"], adopted, holdout, PromptProfile.from_dict(inv["base"]))}


def build_playbook(ctx: StepContext) -> pbk.Playbook:
    exp, conf = ctx.results["experiment"], ctx.results["confirm"]
    ok = {f for f, c in conf["holdout"].items() if c["confirmed"]}
    rules = []
    for r in exp["results"]:
        status, claim = r["status"], f"{r['knob']} = {r['value']!r} instead of {r['baseline']!r}"
        if status == "supported" and r["family"] not in ok:
            status, claim = "inconclusive", claim + " (won stage tests, failed the hold-out confirmation)"
        rules.append(pbk.Rule(id=f"{r['knob']}={r['value']}@{r['family']}", scope=r["family"], knob=r["knob"], value=r["value"],
                              baseline=r["baseline"], status=status, claim=claim, effect=r["mean"], ci_low=r["ci_low"],
                              ci_high=r["ci_high"], n=r["n"], tokens_delta=r["tokens_delta"]))
    for i, c in enumerate(ctx.results["research"]["claims"], 1):
        knob = c["knob"] if c.get("knob") in KNOBS else None
        rules.append(pbk.Rule(id=f"read-{i}", scope=c["family"], knob=knob, value=c.get("value") if knob else None, status="hypothesis",
                              claim=c["claim"], source=c["source"]))
    profiles = {f: conf["adopted"][f] for f in sorted(ok)}
    lang = ctx.project.settings.language.prompt_language
    for f, knobs in profiles.items():  # keep the base language explicit so a later settings change is visible
        knobs.setdefault("language", lang)
    return pbk.Playbook(study_task=ctx.task_id, rules=rules, profiles=profiles)


def render_report(ctx: StepContext, pb: pbk.Playbook, cards: dict[str, dict[str, Any]]) -> str:
    inv, exp, conf, res = (ctx.results[k] for k in ("inventory", "experiment", "confirm", "research"))
    trials = exp["baseline"]
    count = lambda t: ctx.project.store.one("SELECT COUNT(*) n FROM events WHERE task_id = ? AND type = ?", (ctx.task_id, t))["n"]  # noqa: E731
    spent = f"{count('llm.call')} paid, {count('llm.cache_hit')} answered from the local cache"
    out = [f"# Lab study {day_key(ctx.project.settings.timezone)} (task {ctx.task_id[:8]})", "",
           f"Models: {len(exp['models'])}; cases per stage: {inv['per_kind']} x {len(inv['kinds'])} kinds; knobs: {len(inv['variants'])}; "
           f"model calls so far: {spent}; estimate was {inv['estimated_tokens']:,} tokens.", ""]
    out += [f"- note: {n}" for n in inv["notes"]] + [f"- not on the gateway: {m}" for m in inv["missing"]]
    out += ["", "## Model cards (baseline prompt)", "", "| model | family | score | tokens/case | latency | cache | tools | weakest kinds |", "|---|---|---|---|---|---|---|---|"]
    for m, card in sorted(cards.items()):
        if not card.get("available", True):
            out.append(f"| {m} | {card['family']} | unavailable | | | | | {card.get('error', '')[:60]} |")
            continue
        weak = ", ".join(f"{k} {v:.2f}" for k, v in sorted(card.get("by_kind", {}).items(), key=lambda x: x[1])[:2])
        out.append(f"| {m} | {card['family']} | {card.get('score', 0):.2f} | {card.get('tokens_per_case', 0):.0f} | {card.get('latency_s', 0)}s | "
                   f"{'yes' if card.get('provider_cache') else 'no'} | {'yes' if card.get('tool_calling') else 'no'} | {weak} |")
    out += ["", "## Experiments (variant minus baseline, paired, 95% bootstrap interval)", "",
            "| knob | family | n | effect | interval | tokens | verdict |", "|---|---|---|---|---|---|---|"]
    for r in pb.rules:
        if r.status != "hypothesis":
            out.append(f"| {r.knob}={r.value} | {r.scope} | {r.n} | {r.effect:+.3f} | {r.ci_low:+.3f}..{r.ci_high:+.3f} | "
                       f"{r.tokens_delta:+.1%} | {r.status} |")
    out += ["", "## Hold-out confirmation of the combined profiles", ""]
    out += [f"- {f}: {c['knobs']} -> effect {c['mean']:+.3f}, tokens {c['tokens_delta']:+.1%}, "
            f"{'CONFIRMED' if c['confirmed'] else 'not confirmed, default kept'}" for f, c in conf["holdout"].items()] or ["- nothing was adopted"]
    out += ["", "## Proposed prompt profiles", ""]
    out += [f"- {f}: {k}" for f, k in pb.profiles.items()] or ["- no change: the default profile stays for every family"]
    out += ["", "## Read, not yet tested (hypotheses with sources)", ""]
    out += [f"- [{r.scope}] {r.claim} ({r.source})" + (f" -> knob {r.knob}={r.value}" if r.knob else "") for r in pb.rules if r.status == "hypothesis"]
    out += [f"- skipped source: {s}" for s in res["skipped"]]
    out += ["", f"Baseline trials: {len(trials)}. Scores are computed by code from generated cases (roj/lab/tasks.py); "
            "an effect is adopted only if its interval excludes zero or it saves tokens without losing quality.", ""]
    return "\n".join(out)


async def s_report(ctx: StepContext) -> dict[str, Any]:
    exp = ctx.results["experiment"]
    cards = {m: dict(c) for m, c in ctx.results["probe"]["cards"].items()}
    for m in exp["models"]:
        ts = {k.split("|")[1]: t for k, t in exp["baseline"].items() if k.startswith(m + "|")}
        by_kind: dict[str, list[float]] = {}
        for cid, t in ts.items():
            by_kind.setdefault(cid.split(":")[0], []).append(t["score"])
        cards[m].update(score=round(sum(t["score"] for t in ts.values()) / max(1, len(ts)), 4),
                        tokens_per_case=round(sum(t["tokens"] for t in ts.values()) / max(1, len(ts)), 1),
                        valid_json=round(sum(t["valid"] for t in ts.values()) / max(1, len(ts)), 3),
                        by_kind={k: round(sum(v) / len(v), 3) for k, v in by_kind.items()})
    home = ctx.project.inst.home
    mdir = pbk.pb_dir(home) / "models"
    mdir.mkdir(exist_ok=True)
    for m, card in cards.items():
        (mdir / (re.sub(r"[^A-Za-z0-9_.-]", "_", m) + ".json")).write_text(json.dumps(card, indent=2), encoding="utf-8")
    pb = build_playbook(ctx)
    proposal = pbk.save_proposal(home, pb, ctx.task_id)
    rdir = ctx.project.dir / "reports"
    rdir.mkdir(exist_ok=True)
    path = rdir / f"study-{day_key(ctx.project.settings.timezone)}-{ctx.task_id[:8]}.md"
    path.write_text(render_report(ctx, pb, cards), encoding="utf-8")
    return {"report": str(path), "proposal": str(proposal), "profiles": pb.profiles,
            "supported": sum(r.status == "supported" for r in pb.rules), "rules": len(pb.rules)}


async def s_activate(ctx: StepContext) -> dict[str, Any]:
    rep = ctx.results["report"]
    ctx.require("t2.activate_playbook", {"report": rep["report"], "profiles": rep["profiles"], "rules": rep["rules"],
                                         "supported": rep["supported"]})
    pb = pbk.activate(ctx.project.inst.home, Path(rep["proposal"]))
    ctx.log("playbook.activated", {"version": pb.version, "profiles": pb.profiles})
    return {"version": pb.version, "profiles": pb.profiles}


STUDY = Workflow("lab.study", (
    Step("inventory", s_inventory), Step("probe", s_probe), Step("research", s_research), Step("experiment", s_experiment),
    Step("confirm", s_confirm), Step("report", s_report), Step("activate", s_activate),
), "Measure models and prompt knobs, propose a Playbook")
STUDY_REF = "roj.lab.study:STUDY"
