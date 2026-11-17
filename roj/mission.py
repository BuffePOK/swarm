"""Missions: the entry point for a goal. The owner states what he wants to the Architect and
the Leader; they ask, plan one stage at a time, staff it with a minimal team, and learn.

  roj mission start  -> Architect reads the goal and the project's facts, asks the owner questions
  roj mission answer -> the owner answers (or accepts the defaults)
  roj mission next   -> one stage: plan (Architect) -> critique (Leader) + lint (code) -> owner
                        approves the stage -> team works -> Leader reviews against the criteria ->
                        lessons, questions, change requests, proposal for the next stage
  roj chat           -> talk to the Architect, the Leader or (by control mode) any agent

Only the next stage is planned in detail (rolling wave): what stage N teaches decides stage
N+1. Mission agents read, analyse and build knowledge. Code is changed only by the selfdev
workflow, which a stage can request and which has its own gates and approvals (ADR-0008).

Control modes (per mission, switchable at any time):
  leads    the owner talks to the Architect and the Leader, approves stages
  partial  + talks to the agents of the current stage, their questions come to the owner
  full     + talks to every agent of any stage and approves each agent's task before it runs
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from .agents.loop import run_agent
from .agents.roles import RoleDef
from .config import ModelBinding
from .contracts import Autonomy, BudgetSpec, TaskSpec
from .core.kernel import Step, StepContext, Workflow
from .prompting.compiler import Document, RoleSpec
from .prompting.linter import errors, lint_taskspec
from .skills.slots import Slot, run_slot
from .tools import registry
from .util import canonical_json, day_key, utcnow_iso

if TYPE_CHECKING:
    from .project import Project

CONTROL = ("leads", "partial", "full")
LEADS = ("architect", "leader")
CODE_WRITERS = {"repo.write", "repo.replace", "repo.test", "shell.run"}  # missions do not change code; selfdev does
MAX_TEAM = 5

ARCHITECT = (
    "Jesteś Architektem systemu agentów roj. Właściciel podaje cel, a Ty projektujesz drogę do niego etapami: najpierw minimalny "
    "zespół i minimalny zakres, który daje sprawdzalny wynik i wiedzę potrzebną do następnego etapu. Decydujesz, czego potrzeba: "
    "jakich agentów, narzędzi, pakietów wiedzy, decyzji właściciela. Szczegółowo planujesz tylko najbliższy etap; dalsze etapy "
    "to krótka mapa drogowa, którą poprawiasz po każdym etapie. Opierasz się na faktach o projekcie z dokumentów, a nie na założeniach.")
ARCH_RULES = ("Każdy agent ma jedno zadanie z mierzalnymi kryteriami i tylko te narzędzia, których potrzebuje.",
              "Agent badawczy szuka źródeł przez web.search i papers.search, czyta je przez web.fetch, ważne witryny (np. dokumentacje) "
              "indeksuje przez web.crawl i potem przeszukuje je przez web.search ze źródłem index; każdy wniosek zapisuje przez "
              "kb.note razem z adresem źródła i datą publikacji; dostaje max_steps 30-50. Wiedza z pamięci modelu bez źródła "
              "to hipoteza, nie wynik.",
              "Używaj wyłącznie narzędzi z dokumentu tools i modeli z dokumentu models (dokładne id).",
              "Każdemu agentowi przypisz model i 1-2 zamienniki, najlepiej z innej rodziny; wybieraj najtańszy model, który według "
              "kart Lab poradzi sobie z zadaniem; agentom z narzędziami dawaj modele z tool calling; model bez karty to ryzyko, nazwij je.",
              "Agenci misji czytają, analizują i budują wiedzę; zmiany w kodzie zgłaszasz jako change request, nie jako zadanie agenta.",
              "Jeśli brakuje decyzji właściciela, zadaj pytanie z opcjami i wartością domyślną zamiast zgadywać.")
LEADER = (
    "Jesteś Przewódcą projektu w systemie roj. Odpowiadasz przed właścicielem za wynik: sprawdzasz plan Architekta przed startem "
    "etapu, a po etapie oceniasz wyniki zespołu względem kryteriów, na podstawie dowodów z wyników agentów. Zapisujesz wnioski, "
    "wskazujesz, co poprawić, i proponujesz następny etap. Nie przyjmujesz deklaracji bez dowodu.")


class OwnerQuestion(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,24}$")
    question: str = Field(min_length=8, max_length=400)
    why: str = Field("", max_length=300)
    options: list[str] = Field(default_factory=list, max_length=6)
    default: str = ""


class Intake(BaseModel):
    understanding: str = Field(min_length=20, max_length=1500)
    questions: list[OwnerQuestion] = Field(default_factory=list, max_length=6)


class Need(BaseModel):
    kind: Literal["knowledge", "tool", "role", "access", "decision", "data"]
    name: str = Field(min_length=2, max_length=80)
    why: str = Field(min_length=5, max_length=300)


class Member(BaseModel):
    agent: str = Field(pattern=r"^[a-z][a-z0-9-]{2,30}$")
    summary: str = Field(min_length=5, max_length=200)
    instructions: str = Field(min_length=40, max_length=2000)
    model: str = Field(min_length=3, max_length=120, description="exact id from the document models")
    fallbacks: list[str] = Field(default_factory=list, max_length=3)

    def binding(self) -> ModelBinding:
        return ModelBinding(model=self.model, fallbacks=self.fallbacks)
    tools: list[str] = Field(default_factory=list, max_length=10)
    task: str = Field(min_length=15, max_length=600)
    acceptance: list[str] = Field(min_length=1, max_length=5)
    deliverable: str = Field(min_length=5, max_length=200)
    max_steps: int = Field(20, ge=3, le=60, description="tool-use steps; research with search and reading needs 30-50")
    budget_tokens: int = Field(200_000, ge=5_000, le=1_000_000)


class StagePlan(BaseModel):
    objective: str = Field(min_length=15, max_length=500)
    acceptance: list[str] = Field(min_length=1, max_length=6)
    needs: list[Need] = Field(default_factory=list, max_length=10)
    team: list[Member] = Field(min_length=1, max_length=MAX_TEAM)
    risks: list[str] = Field(default_factory=list, max_length=6)


class Charter(BaseModel):
    problem: str = Field(min_length=20, max_length=800)
    success_criteria: list[str] = Field(min_length=1, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=8)
    out_of_scope: list[str] = Field(default_factory=list, max_length=8)


class MissionPlan(BaseModel):
    charter: Charter
    roadmap: list[str] = Field(min_length=1, max_length=10)
    stage: StagePlan


class Critique(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[str] = Field(default_factory=list, max_length=8)


class ChangeRequest(BaseModel):
    title: str = Field(min_length=8, max_length=90)
    goal: str = Field(min_length=20, max_length=600)


class StageReview(BaseModel):
    verdict: Literal["done", "rework", "blocked"]
    summary: str = Field(min_length=20, max_length=1500)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    lessons: list[str] = Field(default_factory=list, max_length=8)
    questions: list[OwnerQuestion] = Field(default_factory=list, max_length=4)
    change_requests: list[ChangeRequest] = Field(default_factory=list, max_length=2)
    mission_complete: bool = False


class ChatReply(BaseModel):
    reply: str = Field(min_length=1, max_length=4000)
    directive: str | None = Field(None, max_length=400)


# --- state -------------------------------------------------------------------------------------

def create(project: "Project", goal: str, control: str = "leads") -> str:
    if control not in CONTROL:
        raise ValueError(f"control must be one of {CONTROL}")
    mid = uuid.uuid4().hex
    state = {"goal": goal, "understanding": "", "qa": [], "directives": [], "charter": None, "roadmap": [], "stages": [], "lessons": [],
             "threads": {}}
    project.store.run("INSERT INTO missions(id, goal, status, control, state, created, updated) VALUES(?,?,?,?,?,?,?)",
                      (mid, goal, "new", control, canonical_json(state), utcnow_iso(), utcnow_iso()))
    project.events.append("mission.created", {"mission": mid, "goal": goal[:300], "control": control}, actor="owner")
    return mid


def load(project: "Project", mid: str | None = None) -> dict[str, Any]:
    row = (project.store.one("SELECT * FROM missions WHERE id LIKE ?", (mid + "%",)) if mid
           else project.store.one("SELECT * FROM missions ORDER BY created DESC LIMIT 1"))
    if not row:
        raise KeyError("no such mission; start one with `roj mission start`")
    return {"id": row["id"], "status": row["status"], "control": row["control"], **json.loads(row["state"])}


def save(project: "Project", m: dict[str, Any], status: str | None = None) -> None:
    state = {k: v for k, v in m.items() if k not in ("id", "status", "control")}
    project.store.run("UPDATE missions SET state = ?, status = ?, updated = ? WHERE id = ?",
                      (canonical_json(state), status or m["status"], utcnow_iso(), m["id"]))
    if status:
        m["status"] = status


def set_control(project: "Project", mid: str, control: str) -> None:
    if control not in CONTROL:
        raise ValueError(f"control must be one of {CONTROL}")
    project.store.run("UPDATE missions SET control = ?, updated = ? WHERE id = ?", (control, utcnow_iso(), mid))
    project.events.append("mission.control", {"mission": mid, "control": control}, actor="owner")


def answer(project: "Project", mid: str, answers: dict[str, str]) -> dict[str, Any]:
    m = load(project, mid)
    for q in m["qa"]:
        if q.get("answer") is None:
            q["answer"] = answers.get(q["id"], q.get("default") or "(no answer: decide yourself and state the assumption)")
            q["answered_by"] = "owner" if q["id"] in answers else "default"
    save(project, m, "ready" if m["status"] in ("needs_answers", "new") else m["status"])
    project.events.append("mission.answered", {"mission": m["id"], "answers": {q["id"]: q["answer"] for q in m["qa"]}}, actor="owner")
    return m


def open_questions(m: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in m["qa"] if q.get("answer") is None]


# --- context the leads see (built by code, no tokens) ------------------------------------------

async def ensure_catalog(ctx: StepContext) -> None:
    if not ctx.project.inst.store.one("SELECT COUNT(*) n FROM models WHERE status = 'available'")["n"]:
        await ctx.project.inst.registry.refresh()  # first mission on a fresh installation


def _bind(ctx: StepContext, *roles: str) -> ModelBinding:
    s = ctx.project.settings
    for r in roles:
        try:
            return s.binding_for(r)
        except KeyError:
            continue
    raise KeyError(f"bind [models.{roles[0]}] in roj.toml (missions need [models.architect] and [models.leader])")


def repo_of(ctx: StepContext):
    cfg = ctx.project.settings.selfdev
    if not (cfg.repo or cfg.clone_url):
        return None
    from .lab.selfdev import repo_path

    return repo_path(ctx)


def facts(ctx: StepContext, m: dict[str, Any]) -> list[Document]:
    from .knowledge.packs import KnowledgeBase
    from .lab.playbook import load_active

    from .models.catalog import chat_models, render

    home = ctx.project.inst.home
    tools = [f"{t.name} [{t.tier}]: {t.summary}" for _, t in sorted(registry.all_tools().items()) if t.name not in CODE_WRITERS]
    packs = [f"{p['pack']}: {p['description']} ({p['docs']} docs)" for p in KnowledgeBase(ctx.project).packs()]
    brief = {"goal": m["goal"], "answers": [{"q": q["question"], "a": q["answer"], "by": q.get("answered_by")} for q in m["qa"] if q.get("answer")],
             "owner_directives": m["directives"], "charter": m["charter"], "roadmap": m["roadmap"], "lessons": m["lessons"][-12:],
             "stages_done": [{"n": st["n"], "objective": st["plan"]["objective"], "verdict": (st.get("review") or {}).get("verdict"),
                              "summary": (st.get("review") or {}).get("summary", "")[:600]} for st in m["stages"]]}
    docs = [Document("mission", canonical_json(brief), "mission state"),
            Document("tools", "\n".join(tools), "tool registry"),
            Document("models", render(chat_models(ctx.project)), "gateway catalog + prices + Lab cards (run `roj lab study` for cards)"),
            Document("knowledge", "\n".join(packs) or "(no knowledge packs yet)", "knowledge base"),
            Document("playbook", canonical_json(load_active(home).profiles), "active Playbook")]
    repo = repo_of(ctx)
    if repo:
        from .lab.selfdev import concept_digest, repo_map

        digest = concept_digest(repo)
        if digest:
            docs.insert(1, Document("concept", digest, "docs/CONCEPT.md (owner): what the project is and what improvement means"))
        docs.append(Document("repo_map", "\n".join(repo_map(repo, 160)), "git ls-files of the project's repository"))
    return docs


# --- intake ------------------------------------------------------------------------------------

async def s_intake(ctx: StepContext) -> dict[str, Any]:
    await ensure_catalog(ctx)
    m = load(ctx.project, ctx.inputs["mission"])
    task = TaskSpec(goal="Zrozum cel właściciela i ustal, czego brakuje do zaplanowania pierwszego etapu", deliverable="Intake JSON",
                    output_format="Wyłącznie JSON",
                    acceptance=["Pole understanding streszcza cel w 2-5 zdaniach i nazywa największą niewiadomą",
                                "Najwyżej 6 pytań; każde ma id, uzasadnienie i wartość domyślną",
                                "Zero pytań o rzeczy, które wynikają z dokumentów"], budget=BudgetSpec(tokens=30_000))
    res = await run_slot(ctx, Slot("mission-intake", RoleSpec("mission-intake", ARCHITECT, rules=ARCH_RULES), Intake, max_tokens=2000), task,
                         binding=_bind(ctx, "architect", "planner"), scope="team:mission/agent:architect", documents=facts(ctx, m))
    m = load(ctx.project, m["id"])
    m["understanding"] = res.value.understanding
    known = {q["id"] for q in m["qa"]}
    m["qa"] += [{**q.model_dump(), "from": "architect", "answer": None} for q in res.value.questions if q.id not in known]
    save(ctx.project, m, "needs_answers" if open_questions(m) else "ready")
    return {"understanding": res.value.understanding, "questions": [q.model_dump() for q in res.value.questions]}


INTAKE = Workflow("mission.intake", (Step("intake", s_intake),), "Architect reads the goal and asks the owner what is missing")
INTAKE_REF = "roj.mission:INTAKE"


# --- one stage ---------------------------------------------------------------------------------

def lint_stage(ctx: StepContext, st: StagePlan) -> list[str]:
    from .models.catalog import chat_models

    catalog = {m["id"]: m for m in chat_models(ctx.project) if not m["blocked"]}
    issues, known, seen = [], registry.all_tools(), set()
    for mem in st.team:
        if mem.agent in seen or mem.agent in LEADS:
            issues.append(f"{mem.agent}: agent names must be unique and not 'architect' or 'leader'")
        seen.add(mem.agent)
        for t in mem.tools:
            if t not in known:
                issues.append(f"{mem.agent}: tool {t} does not exist (see document tools)")
            elif t in CODE_WRITERS:
                issues.append(f"{mem.agent}: tool {t} changes code; raise a change request after the stage instead")
        for mid in [mem.model, *mem.fallbacks]:
            if mid not in catalog:
                issues.append(f"{mem.agent}: model {mid} is not an available chat model (use exact ids from document models)")
            elif mem.tools and catalog[mid]["card"].get("tool_calling") is False:
                issues.append(f"{mem.agent}: model {mid} failed the tool-calling probe but the agent uses tools")
        spec = TaskSpec(goal=mem.task, deliverable=mem.deliverable, acceptance=mem.acceptance, budget=BudgetSpec(tokens=1))
        issues += [f"{mem.agent}: {i.message}" for i in errors(lint_taskspec(spec))]
    return issues


async def s_plan(ctx: StepContext) -> dict[str, Any]:
    m = load(ctx.project, ctx.inputs["mission"])
    if open_questions(m):
        raise ValueError("the mission has unanswered questions: `roj mission answer` first")
    n = len(m["stages"]) + 1
    await ensure_catalog(ctx)
    docs = facts(ctx, m)
    slot = Slot("mission-architect", RoleSpec("mission-architect", ARCHITECT, rules=ARCH_RULES), MissionPlan, max_tokens=4000)
    acceptance = ["Pole stage opisuje wyłącznie najbliższy etap, z zespołem najwyżej 5 agentów",
                  "Każde kryterium etapu i każdego agenta da się sprawdzić po wynikach (liczba, lista, plik, wpis w pakiecie wiedzy)",
                  "Roadmap ma od 1 do 10 krótkich tytułów kolejnych etapów", "Charter zachowuje ustalenia z poprzednich etapów"]
    issues: list[str] = []
    for attempt in (1, 2, 3):
        task = TaskSpec(goal=f"Zaplanuj etap {n} misji i zaktualizuj kartę misji oraz mapę drogową", deliverable="MissionPlan JSON",
                        output_format="Wyłącznie JSON", acceptance=acceptance,
                        inputs={"stage_number": n, **({"issues_to_fix": issues} if issues else {})}, budget=BudgetSpec(tokens=60_000))
        plan = (await run_slot(ctx, slot, task, binding=_bind(ctx, "architect", "planner"), scope="team:mission/agent:architect",
                               documents=docs)).value
        issues = lint_stage(ctx, plan.stage)
        crit = None
        if not issues:
            ctask = TaskSpec(goal="Oceń plan etapu przed pokazaniem go właścicielowi", deliverable="Critique JSON", output_format="Wyłącznie JSON",
                             acceptance=["Werdykt to approve albo revise", "Każdy zarzut wskazuje agenta albo kryterium i mówi, co zmienić"],
                             budget=BudgetSpec(tokens=30_000))
            crit = (await run_slot(ctx, Slot("mission-critic", RoleSpec("mission-critic", LEADER), Critique, max_tokens=1200), ctask,
                                   binding=_bind(ctx, "leader", "critic", "planner"), scope="team:mission/agent:leader",
                                   documents=[docs[0], Document("plan", canonical_json(plan.model_dump()), "architect")])).value
            if crit.verdict == "approve" or attempt == 3:
                return {"n": n, "plan": plan.model_dump(), "critique": crit.model_dump(), "attempts": attempt}
            issues = crit.issues or ["the Leader asked for a revision without details: simplify the stage"]
        ctx.log("mission.plan_revision", {"attempt": attempt, "issues": issues})
    raise ValueError(f"the Architect could not produce a valid stage plan in 3 attempts: {issues}")


async def s_gate(ctx: StepContext) -> dict[str, Any]:
    p = ctx.results["plan"]
    st = p["plan"]["stage"]
    ctx.require("t2.mission_stage", {"mission": ctx.inputs["mission"][:8], "stage": p["n"], "objective": st["objective"],
                                     "acceptance": st["acceptance"], "team": [f"{t['agent']} ({t['model']}, fallbacks {', '.join(t['fallbacks']) or 'none'}; up to {t.get('max_steps', 20)} steps, "
                                              f"{t.get('budget_tokens', 200_000):,} tokens; tools: {', '.join(t['tools']) or 'none'}): {t['task']}" for t in st["team"]],
                                     "needs": [f"{x['kind']}: {x['name']}" for x in st["needs"]], "leader": p["critique"]})
    return {"approved": True}


async def s_team(ctx: StepContext) -> dict[str, Any]:
    p = ctx.results["plan"]
    st, mid = StagePlan.model_validate(p["plan"]["stage"]), ctx.inputs["mission"]
    control = load(ctx.project, mid)["control"]
    repo = repo_of(ctx)
    if repo:
        ctx.repo_root = str(repo)  # read tools only: lint_stage keeps code-writing tools out of missions
    results: dict[str, Any] = {}
    for mem in st.team:
        if control == "full":
            ctx.require("t2.mission_agent_run", {"mission": mid[:8], "stage": p["n"], "agent": mem.agent, "task": mem.task,
                                                 "tools": mem.tools, "acceptance": mem.acceptance}, target=mem.agent)

        async def work(mem: Member = mem) -> dict[str, Any]:
            role = RoleDef(name=mem.agent, summary=mem.summary, binding=mem.agent, tools=mem.tools, autonomy=Autonomy.L1,
                           instructions=mem.instructions)
            task = TaskSpec(goal=mem.task, deliverable=mem.deliverable, acceptance=mem.acceptance,
                            inputs={"stage_objective": st.objective}, budget=BudgetSpec(tokens=mem.budget_tokens))
            docs = [Document(f"result-{a}", canonical_json(r), a) for a, r in results.items()]
            run = await run_agent(ctx, role=role, task=task, binding=mem.binding(), agent_name=mem.agent,
                                  scope=f"team:mission/agent:{mem.agent}", documents=docs, max_steps=int(ctx.inputs.get("max_steps", mem.max_steps)))
            return {**run.output.model_dump(), "steps": run.steps, "tools_used": [c.get("tool") or c.get("name") for c in run.tool_calls]}
        results[mem.agent] = await ctx.memo(f"agent:{mem.agent}", work)
    return {"results": results}


async def s_review(ctx: StepContext) -> dict[str, Any]:
    p, m = ctx.results["plan"], load(ctx.project, ctx.inputs["mission"])
    task = TaskSpec(goal="Oceń wyniki etapu względem jego kryteriów i zdecyduj, co dalej", deliverable="StageReview JSON",
                    output_format="Wyłącznie JSON",
                    acceptance=["Werdykt done wymaga dowodu dla każdego kryterium etapu w polu evidence",
                                "Wnioski (lessons) są konkretne i przydatne przy planowaniu następnego etapu",
                                "Najwyżej 2 change requests; każdy opisuje jedną małą zmianę w kodzie z mierzalnym celem"],
                    budget=BudgetSpec(tokens=60_000))
    docs = [Document("mission", canonical_json({"goal": m["goal"], "charter": p["plan"]["charter"], "directives": m["directives"]}), "mission"),
            Document("stage", canonical_json(p["plan"]["stage"]), "approved stage plan"),
            Document("results", canonical_json(ctx.results["team"]["results"]), "agents' results")]
    res = await run_slot(ctx, Slot("mission-review", RoleSpec("mission-review", LEADER), StageReview, max_tokens=3000), task,
                         binding=_bind(ctx, "leader", "critic", "planner"), scope="team:mission/agent:leader", documents=docs)
    return res.value.model_dump()


async def s_close(ctx: StepContext) -> dict[str, Any]:
    p, rv, results = ctx.results["plan"], ctx.results["review"], ctx.results["team"]["results"]
    m = load(ctx.project, ctx.inputs["mission"])
    if any(st["n"] == p["n"] for st in m["stages"]):  # the step is being retried after a crash: already recorded
        return {"stage": p["n"], "status": m["status"]}
    submitted = []
    if rv["change_requests"] and (ctx.project.settings.selfdev.repo or ctx.project.settings.selfdev.clone_url):
        from .lab.selfdev import SELFDEV_REF

        for cr in rv["change_requests"]:
            tid = ctx.project.kernel.submit(SELFDEV_REF, {"goal": f"{cr['title']}. {cr['goal']}", "mission": m["id"]})
            submitted.append({"title": cr["title"], "task": tid})
    for q in rv["questions"]:
        if q["id"] not in {x["id"] for x in m["qa"]}:
            m["qa"].append({**q, "from": "leader", "answer": None})
    for a, r in results.items():
        if r.get("status") == "question" and r.get("question"):
            m["qa"].append({"id": f"s{p['n']}_{re.sub(r'[^a-z0-9]', '_', a)}"[:24], "question": r["question"], "why": f"asked by agent {a}",
                            "options": [], "default": "", "from": a, "answer": None})
    m["charter"], m["roadmap"] = p["plan"]["charter"], p["plan"]["roadmap"]
    m["lessons"] += [f"stage {p['n']}: {x}" for x in rv["lessons"]]
    m["stages"].append({"n": p["n"], "plan": p["plan"]["stage"], "critique": p["critique"], "results": results, "review": rv,
                        "change_requests": submitted, "task": ctx.task_id, "closed": utcnow_iso()})
    status = "complete" if rv["mission_complete"] else ("needs_answers" if open_questions(m) else "stage_done")
    save(ctx.project, m, status)
    lines = [f"# Mission {m['id'][:8]}, stage {p['n']}: {rv['verdict']}", "", f"Goal: {m['goal']}", "", f"Stage objective: {p['plan']['stage']['objective']}", "",
             "## Criteria", ""] + [f"- {c}" for c in p["plan"]["stage"]["acceptance"]] + ["", "## Team", ""]
    for mem in p["plan"]["stage"]["team"]:
        r = results.get(mem["agent"], {})
        lines += [f"### {mem['agent']} ({mem['model']})", "", f"Task: {mem['task']}", "", f"Result ({r.get('status')}, {r.get('steps')} steps, "
                  f"confidence {r.get('confidence')}): {r.get('summary')}", ""]
    lines += ["## Leader's review", "", rv["summary"], ""] + [f"- evidence: {e}" for e in rv["evidence"]] + [f"- lesson: {x}" for x in rv["lessons"]]
    lines += [f"- question for the owner: {q['question']}" for q in rv["questions"]]
    lines += [f"- change request -> selfdev task {s['task'][:8]}: {s['title']}" for s in submitted]
    lines += [f"- change request (selfdev not configured, not started): {c['title']}" for c in rv["change_requests"] if not submitted]
    lines += ["", "## Roadmap", ""] + [f"{i}. {t}" for i, t in enumerate(p["plan"]["roadmap"], 1)]
    rdir = ctx.project.dir / "reports"
    rdir.mkdir(exist_ok=True)
    path = rdir / f"mission-{m['id'][:8]}-stage-{p['n']}-{day_key(ctx.project.settings.timezone)}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx.log("mission.stage_closed", {"mission": m["id"], "stage": p["n"], "verdict": rv["verdict"], "status": status})
    return {"stage": p["n"], "status": status, "report": str(path), "change_requests": submitted}


STAGE = Workflow("mission.stage", (Step("plan", s_plan), Step("gate", s_gate), Step("team", s_team), Step("review", s_review),
                                   Step("close", s_close)), "One mission stage: plan, owner approval, team, review, lessons")
STAGE_REF = "roj.mission:STAGE"


# --- chat --------------------------------------------------------------------------------------

def may_talk(m: dict[str, Any], to: str) -> dict[str, Any] | None:
    """Returns the agent's stage member record (None for the leads). Raises if the control mode forbids it."""
    if to in LEADS:
        return None
    if m["control"] == "leads":
        raise PermissionError(f"control mode 'leads': talk to the architect or the leader, or switch with `roj mission control {m['id'][:8]} partial`")
    stages = m["stages"] if m["control"] == "full" else m["stages"][-1:]
    for st in reversed(stages):
        for mem in st["plan"]["team"]:
            if mem["agent"] == to:
                return {**mem, "stage": st["n"], "result": st["results"].get(to)}
    scope = "any stage" if m["control"] == "full" else "the latest stage"
    raise PermissionError(f"no agent '{to}' in {scope} of this mission (control mode '{m['control']}')")


async def s_chat(ctx: StepContext) -> dict[str, Any]:
    m, to, text = load(ctx.project, ctx.inputs["mission"]), ctx.inputs["to"], ctx.inputs["text"]
    mem = may_talk(m, to)
    persona = ARCHITECT if to == "architect" else LEADER if to == "leader" else mem["instructions"]
    binding = (_bind(ctx, "architect", "planner") if to == "architect" else _bind(ctx, "leader", "critic", "planner") if to == "leader"
               else ModelBinding(model=mem["model"], fallbacks=mem.get("fallbacks", [])))
    thread = m["threads"].setdefault(to, [])
    brief = {"goal": m["goal"], "status": m["status"], "charter": m["charter"], "roadmap": m["roadmap"], "directives": m["directives"],
             "lessons": m["lessons"][-8:], "open_questions": [q["question"] for q in open_questions(m)],
             "last_stage": ({"n": m["stages"][-1]["n"], "objective": m["stages"][-1]["plan"]["objective"],
                             "review": m["stages"][-1]["review"]["summary"]} if m["stages"] else None)}
    docs = [Document("mission", canonical_json(brief), "mission state"), Document("thread", canonical_json(thread[-8:]), "earlier messages")]
    if mem:
        docs.append(Document("my_work", canonical_json({k: mem[k] for k in ("stage", "task", "acceptance", "result")}), "this agent's task and result"))
    task = TaskSpec(goal="Odpowiedz właścicielowi na jego wiadomość w kontekście misji", deliverable="ChatReply JSON", output_format="Wyłącznie JSON",
                    acceptance=["Odpowiedź odnosi się do wiadomości i opiera się na stanie misji z dokumentów",
                                "Jeśli wiadomość zawiera trwałe polecenie właściciela, pole directive streszcza je w 1 zdaniu; w innym razie null"],
                    inputs={"owner_message": text}, budget=BudgetSpec(tokens=20_000))
    res = await run_slot(ctx, Slot("mission-chat", RoleSpec("mission-chat", persona), ChatReply, max_tokens=1500), task, binding=binding,
                         scope=f"team:mission/agent:{to}", documents=docs)
    m = load(ctx.project, m["id"])
    thread = m["threads"].setdefault(to, [])
    thread += [{"from": "owner", "text": text[:2000]}, {"from": to, "text": res.value.reply[:2000]}]
    m["threads"][to] = thread[-20:]
    if res.value.directive:
        m["directives"].append({"text": res.value.directive, "via": to, "at": utcnow_iso()})
    save(ctx.project, m)
    return {"to": to, "reply": res.value.reply, "directive": res.value.directive}


CHAT = Workflow("mission.chat", (Step("chat", s_chat),), "One message from the owner to a mission agent")
CHAT_REF = "roj.mission:CHAT"
