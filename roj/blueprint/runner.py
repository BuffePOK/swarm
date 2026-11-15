"""Run the active blueprint: teams in dependency order, each in its mode, then integration.

Modes in this phase:
  strict        nodes (or members) in a fixed order, each sees the previous outputs
  graph         nodes with conditional edges over the shared state, visit limits
  orchestrator  a lead keeps a task/progress ledger, delegates one instruction per round
                to a worker, replans when stalled, finishes when acceptance is met
Every team, node visit and round is a memo checkpoint: after a crash nothing finished is redone.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..agents.loop import run_agent
from ..agents.roles import RoleRegistry
from ..config import ModelBinding
from ..contracts import BudgetSpec, TaskSpec
from ..core.kernel import Step, StepContext, Workflow
from ..prompting.compiler import Document
from ..skills.slots import Slot, run_slot
from ..stats import llm_stats
from ..tools import registry
from ..util import canonical_json, utcnow
from . import manage
from .schema import Blueprint, MemberSpec, TeamSpec, eval_condition


class LeadDecision(BaseModel):
    done: bool
    next_member: str | None = None
    instruction: str = ""
    reason: str = ""
    facts: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    final_summary: str | None = None


class GuardianIssue(BaseModel):
    team: str
    issue: str
    evidence: str
    severity: str = "medium"


class GuardianReview(BaseModel):
    verdict: str  # consistent | issues
    issues: list[GuardianIssue] = Field(default_factory=list)


def _binding(m: MemberSpec) -> ModelBinding:
    return ModelBinding(model=m.model, fallbacks=m.fallbacks)


def _fill(text: str, state: dict[str, Any]) -> str:
    def rep(m: re.Match) -> str:
        v = state.get(m.group(1))
        if v is None:
            return ""
        return str(v.get("summary") or "") if isinstance(v, dict) else str(v)
    return re.sub(r"\{state\.([a-zA-Z0-9_\-]+)\}", rep, text)


def _docs(prior: dict[str, Any]) -> list[Document]:
    return [Document(k, canonical_json({"summary": v.get("summary"), "status": v.get("status"), "output": v.get("output")})[:6000], "team")
            for k, v in prior.items() if isinstance(v, dict)]


async def _agent(ctx: StepContext, roles: RoleRegistry, team: TeamSpec, m: MemberSpec, goal: str, docs: list[Document]) -> dict[str, Any]:
    role = roles.get(m.role)
    tools = list(m.tools if m.tools is not None else role.tools)
    if m.knowledge and "kb.search" not in tools:
        tools.append("kb.search")
    task = TaskSpec(goal=goal, deliverable=team.output_format, acceptance=team.acceptance or ["The result answers the goal with sources"],
                    output_format="Call submit_result with status, summary and output",
                    inputs={"team_goal": team.goal, **({"knowledge_packs": m.knowledge} if m.knowledge else {})},
                    budget=BudgetSpec(tokens=team.budget.tokens) if team.budget.tokens else None)
    run = await run_agent(ctx, role=role, task=task, binding=_binding(m), agent_name=f"{team.name}.{m.name}", tools=tools,
                          autonomy=m.autonomy or role.autonomy, scope=f"team:{team.name}/agent:{m.name}", documents=docs,
                          lead=next((x.name for x in team.members if x.role == "orchestrator"), None))
    return {**run.output.model_dump(), "member": m.name, "steps": run.steps, "tools": run.tool_calls}


async def run_strict(ctx, roles, team, prior) -> dict[str, Any]:
    members = {m.name: m for m in team.members}
    nodes = team.graph.nodes if team.graph else []
    state: dict[str, Any] = dict(prior)
    order = [(n.id, n) for n in nodes] or [(m.name, None) for m in team.members]
    for nid, node in order:
        async def go(nid=nid, node=node):
            if node and node.kind == "tool":
                return {"status": "done", "summary": json.dumps((await registry.call(ctx, node.tool, node.args, agent=team.name)))[:2000]}
            m = members[node.member] if node else members[nid]
            goal = _fill(node.task, state) if node and node.task else team.goal
            return await _agent(ctx, roles, team, m, goal, _docs(state))
        state[nid] = await ctx.memo(f"team:{team.name}:node:{nid}", go)
    last = state[order[-1][0]]
    return {"status": last.get("status", "done"), "summary": last.get("summary", ""), "outputs": {k: state[k] for k, _ in order}}


async def run_graph(ctx, roles, team, prior) -> dict[str, Any]:
    g = team.graph
    nodes = {n.id: n for n in g.nodes}
    members = {m.name: m for m in team.members}
    state: dict[str, Any] = dict(prior)
    visits: dict[str, int] = {}
    path: list[str] = []
    current: str | None = g.start
    while current:
        visits[current] = visits.get(current, 0) + 1
        if visits[current] > g.max_visits:
            ctx.log("graph.visit_limit", {"team": team.name, "node": current})
            return {"status": "failed", "summary": f"visit limit reached at {current}", "outputs": {p: state.get(p) for p in path}, "path": path}
        node = nodes[current]

        async def go(node=node):
            if node.kind == "tool":
                return {"status": "done", "summary": "", "output": await registry.call(ctx, node.tool, node.args, agent=team.name)}
            return await _agent(ctx, roles, team, members[node.member], _fill(node.task, state) or team.goal, _docs(state))
        state[current] = await ctx.memo(f"team:{team.name}:node:{current}:{visits[current]}", go)
        path.append(current)
        nxt = None
        for e in (e for e in g.edges if e.source == current):
            if e.when is None or eval_condition(e.when, state):
                nxt = e.target
                break
        ctx.log("graph.step", {"team": team.name, "node": current, "visit": visits[current], "next": nxt})
        current = nxt
    last = state[path[-1]]
    return {"status": last.get("status", "done"), "summary": last.get("summary", ""), "outputs": {p: state[p] for p in dict.fromkeys(path)}, "path": path}


async def run_orchestrator(ctx, roles, team, prior) -> dict[str, Any]:
    lead = next(m for m in team.members if m.role == "orchestrator")
    workers = {m.name: m for m in team.members if m.role != "orchestrator"}
    progress: list[dict[str, Any]] = []
    facts: list[str] = []
    slot = Slot(f"{team.name}.lead", roles.get("orchestrator").spec(ctx.project.settings.language.prompt_language), LeadDecision, max_tokens=1500)
    for rnd in range(1, team.max_rounds + 1):
        stalled = len(progress) >= 2 and progress[-1]["summary"] == progress[-2]["summary"]
        task = TaskSpec(goal=f"Lead the team to: {team.goal}", deliverable="LeadDecision JSON", output_format="JSON only",
                        acceptance=team.acceptance or ["The goal is answered with sources"],
                        inputs={"round": rnd, "stalled": stalled, "workers": {k: roles.get(w.role).summary for k, w in workers.items()}},
                        budget=BudgetSpec(tokens=20000))
        docs = [Document("ledger", canonical_json({"facts": facts, "progress": progress[-6:]}), "lead")] + _docs(prior)

        async def decide():
            res = await run_slot(ctx, slot, task, binding=_binding(lead), scope=f"team:{team.name}/agent:{lead.name}", documents=docs)
            return res.value.model_dump()
        d = LeadDecision.model_validate(await ctx.memo(f"team:{team.name}:lead:{rnd}", decide))
        facts = list(dict.fromkeys(facts + d.facts))[-30:]
        if d.done:
            return {"status": "done", "summary": d.final_summary or (progress[-1]["summary"] if progress else ""), "rounds": rnd,
                    "facts": facts, "outputs": {f"round{p['round']}": p for p in progress}}
        if d.next_member not in workers:
            progress.append({"round": rnd, "member": d.next_member, "status": "failed", "summary": f"unknown worker {d.next_member}"})
            continue
        out = await ctx.memo(f"team:{team.name}:round:{rnd}",
                             lambda: _agent(ctx, roles, team, workers[d.next_member], d.instruction, docs))
        progress.append({"round": rnd, "member": d.next_member, "instruction": d.instruction, "status": out["status"], "summary": out["summary"]})
    return {"status": "failed", "summary": f"no result after {team.max_rounds} rounds", "rounds": team.max_rounds,
            "outputs": {f"round{p['round']}": p for p in progress}}


RUNNERS = {"strict": run_strict, "graph": run_graph, "orchestrator": run_orchestrator}


def _order(bp: Blueprint) -> list[TeamSpec]:
    done: list[str] = []
    teams = list(bp.teams)
    while teams:
        ready = [t for t in teams if all(d in done for d in t.depends_on)]
        if not ready:
            raise ValueError("dependency cycle")
        for t in ready:
            done.append(t.name)
            teams.remove(t)
    return [bp.team(n) for n in done]


async def b_load(ctx: StepContext) -> dict[str, Any]:
    act = manage.active(ctx.project)
    if not act:
        raise ValueError("no active blueprint: `roj blueprint propose` and approve one first")
    version, bp = act
    for t in bp.teams:
        if t.budget.tokens or t.budget.usd:
            ctx.project.budget.allocate(f"team:{t.name}", t.budget.tokens, t.budget.usd, set_by="blueprint")
    return {"version": version, "blueprint": bp.model_dump(mode="json")}


async def b_teams(ctx: StepContext) -> dict[str, Any]:
    bp = Blueprint.model_validate(ctx.results["load"]["blueprint"])
    roles = RoleRegistry(ctx.project.inst)
    results: dict[str, Any] = {}
    for team in _order(bp):
        prior = {d: results[d] for d in team.depends_on}
        if any(p.get("status") != "done" for p in prior.values()):
            results[team.name] = {"status": "skipped", "summary": "a dependency did not finish"}
            continue

        async def go(team=team, prior=prior):
            r = await RUNNERS[team.mode](ctx, roles, team, prior)
            return {**r, "mode": team.mode}
        results[team.name] = await ctx.memo(f"team:{team.name}", go)
        ctx.log("team.finished", {"team": team.name, "mode": team.mode, "status": results[team.name]["status"]})
    return {"teams": results}


async def b_integrate(ctx: StepContext) -> dict[str, Any]:
    bp = Blueprint.model_validate(ctx.results["load"]["blueprint"])
    g = bp.integration.guardian
    if not g:
        return {"skipped": True}
    roles = RoleRegistry(ctx.project.inst)
    contracts = [{"team": t.name, "goal": t.goal, "acceptance": t.acceptance, "depends_on": t.depends_on,
                  "result": {k: v for k, v in ctx.results["teams"]["teams"][t.name].items() if k in ("status", "summary")}}
                 for t in bp.teams]
    task = TaskSpec(goal="Check that team results meet their contracts and are consistent with each other",
                    deliverable="GuardianReview JSON", output_format="JSON only",
                    acceptance=["Every issue names the owning team and quotes evidence", "verdict is consistent only with zero issues"],
                    budget=BudgetSpec(tokens=20000))
    slot = Slot("guardian", roles.get("guardian").spec(ctx.project.settings.language.prompt_language), GuardianReview, max_tokens=1500)
    res = await run_slot(ctx, slot, task, binding=_binding(g), scope="role:guardian",
                         documents=[Document("contracts", canonical_json(contracts), "blueprint")])
    return res.value.model_dump()


async def b_report(ctx: StepContext) -> dict[str, Any]:
    teams = ctx.results["teams"]["teams"]
    lines = [f"# Blueprint run v{ctx.results['load']['version']}", "", f"Task `{ctx.task_id}`", ""]
    for name, r in teams.items():
        lines += [f"## {name} ({r.get('mode', '')}): {r['status']}", "", r.get("summary", ""), ""]
        if r.get("path"):
            lines.append(f"Path: {' → '.join(r['path'])}")
    integ = ctx.results["integrate"]
    if not integ.get("skipped"):
        lines += ["## Guardian", "", f"Verdict: {integ['verdict']}", ""] + [f"- **{i['severity']}** {i['team']}: {i['issue']} ({i['evidence']})"
                                                                            for i in integ["issues"]]
    s = llm_stats(ctx.project.events, task_id=ctx.task_id)
    lines += ["", f"_{s['calls']} model calls · {s['prompt_tokens'] + s['completion_tokens']:,} tokens · ${s['cost_usd']:.4f}_"]
    rdir = ctx.project.dir / "reports"
    rdir.mkdir(exist_ok=True)
    path = rdir / f"blueprint-run-{utcnow().strftime('%Y-%m-%d')}-{ctx.task_id[:8]}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"report": str(path), "teams": {k: v["status"] for k, v in teams.items()}}


RUN = Workflow("blueprint.run", (Step("load", b_load), Step("teams", b_teams), Step("integrate", b_integrate), Step("report", b_report)),
               "run the active blueprint: teams in dependency order, then the guardian")
RUN_REF = "roj.blueprint.runner:RUN"
