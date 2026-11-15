"""Blueprint linter: deterministic checks before a blueprint can be proposed or run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agents.roles import RoleRegistry
from ..obs.doctor import family
from ..prompting.linter import _MEASURABLE, _VAGUE, Issue
from ..tools.registry import all_tools
from .schema import Blueprint, check_condition

if TYPE_CHECKING:
    from ..project import Project

REVIEWERS = {"critic", "verifier", "guardian"}


def _criteria(prefix: str, items: list[str], issues: list[Issue], where: str) -> None:
    if not items:
        issues.append(Issue(f"{prefix}02", "error", f"{where}: no acceptance criteria"))
    for c in items:
        if _VAGUE.search(c) and not _MEASURABLE.search(c):
            issues.append(Issue(f"{prefix}03", "error", f"{where}: vague criterion {c!r}"))
        elif not _MEASURABLE.search(c):
            issues.append(Issue(f"{prefix}03", "warning", f"{where}: criterion without a measurable marker {c!r}"))


def lint(bp: Blueprint, project: "Project | None" = None, roles: RoleRegistry | None = None) -> list[Issue]:
    roles = roles or RoleRegistry(project.inst if project else None)
    tools = all_tools()
    issues: list[Issue] = []
    if len(bp.goal.strip()) < 10:
        issues.append(Issue("BP01", "error", "project goal missing or shorter than 10 characters"))
    _criteria("BP", bp.acceptance, issues, "project")
    names = [t.name for t in bp.teams]
    for dup in {n for n in names if names.count(n) > 1}:
        issues.append(Issue("BP04", "error", f"duplicate team name {dup}"))
    packs = set()
    if project:
        packs = {r["pack"] for r in project.store.all("SELECT pack FROM kb_packs")}
    for t in bp.teams:
        where = f"team {t.name}"
        if len(t.goal.strip()) < 10:
            issues.append(Issue("BP01", "error", f"{where}: goal missing or too short"))
        _criteria("TM", t.acceptance, issues, where)
        for d in t.depends_on:
            if d not in names:
                issues.append(Issue("BP05", "error", f"{where}: depends on unknown team {d}"))
        members = {m.name: m for m in t.members}
        if len(members) != len(t.members):
            issues.append(Issue("BP04", "error", f"{where}: duplicate member names"))
        for m in t.members:
            if m.role not in roles.roles:
                issues.append(Issue("BP06", "error", f"{where}: member {m.name} has unknown role {m.role}"))
            for tool in m.tools or []:
                if tool not in tools:
                    issues.append(Issue("BP07", "error", f"{where}: member {m.name} uses unknown tool {tool}"))
            for k in m.knowledge:
                if project and k not in packs:
                    issues.append(Issue("BP08", "error", f"{where}: member {m.name} uses unknown knowledge pack {k}"))
            if not m.fallbacks:
                issues.append(Issue("BP09", "warning", f"{where}: member {m.name} has no fallback model"))
            if m.autonomy and m.autonomy.value == "L3" and t.budget.usd is None and t.budget.tokens is None:
                issues.append(Issue("BP14", "error", f"{where}: L3 member {m.name} needs a team budget"))
        authors = [m for m in t.members if m.role not in REVIEWERS]
        for r in (m for m in t.members if m.role in REVIEWERS):
            if any(family(a.model) == family(r.model) for a in authors):
                issues.append(Issue("BP13", "warning", f"{where}: reviewer {r.name} shares a model family with an author"))
        if t.mode == "orchestrator":
            leads = [m for m in t.members if m.role == "orchestrator"]
            if len(leads) != 1:
                issues.append(Issue("BP11", "error", f"{where}: orchestrator mode needs exactly one member with role orchestrator"))
            if len(t.members) < 2:
                issues.append(Issue("BP11", "error", f"{where}: orchestrator mode needs at least one worker"))
        if t.mode in ("graph", "strict") and t.graph:
            ids = [n.id for n in t.graph.nodes]
            if t.graph.start not in ids:
                issues.append(Issue("BP11", "error", f"{where}: graph start {t.graph.start} is not a node"))
            for n in t.graph.nodes:
                if n.kind == "agent" and n.member not in members:
                    issues.append(Issue("BP11", "error", f"{where}: node {n.id} references unknown member {n.member}"))
                if n.kind == "tool" and n.tool not in tools:
                    issues.append(Issue("BP11", "error", f"{where}: node {n.id} uses unknown tool {n.tool}"))
            for e in t.graph.edges:
                if e.source not in ids or e.target not in ids:
                    issues.append(Issue("BP11", "error", f"{where}: edge {e.source}->{e.target} references unknown nodes"))
                if e.when:
                    try:
                        check_condition(e.when)
                    except (ValueError, SyntaxError) as ex:
                        issues.append(Issue("BP11", "error", f"{where}: edge condition {e.when!r}: {ex}"))
            if t.mode == "strict" and any(e.when for e in t.graph.edges):
                issues.append(Issue("BP11", "error", f"{where}: strict mode has no conditional edges (use graph)"))
        if t.mode == "graph" and not t.graph:
            issues.append(Issue("BP11", "error", f"{where}: graph mode needs a graph"))
    # dependency cycles
    deps = {t.name: set(t.depends_on) for t in bp.teams}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 1:
            issues.append(Issue("BP05", "error", f"dependency cycle: {' -> '.join(path + [n])}"))
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps.get(n, ()):
            visit(d, path + [n])
        state[n] = 2

    for n in deps:
        visit(n, [])
    if project:
        pb = project.settings.budget
        tokens = sum(t.budget.tokens or 0 for t in bp.teams)
        usd = sum(t.budget.usd or 0 for t in bp.teams)
        if tokens > pb.tokens_per_day:
            issues.append(Issue("BP10", "error", f"team token budgets {tokens:,} exceed the project's {pb.tokens_per_day:,}/day"))
        if usd > pb.usd_per_day + 1e-9:
            issues.append(Issue("BP10", "error", f"team USD budgets {usd:.2f} exceed the project's {pb.usd_per_day:.2f}/day"))
    if len(bp.teams) > 1 and not bp.integration.guardian:
        issues.append(Issue("BP12", "warning", "several teams without an integration guardian"))
    return issues
