"""Self-improvement: roj proposes one pull request to its own repository per run.

  survey     repository map, backlog, failure signals from the logs, active Playbook (no tokens)
  propose    planner ranks up to 5 improvements; code drops the ones that touch protected paths
  plan_gate  owner approves the chosen proposal (t2.selfdev_start)
  build      coder agent works in a throwaway git worktree; code gates the result (two attempts)
  review     a model of another family reviews the diff
  publish    commit, patch file, owner approval (t3.open_pull_request), push + pull request

The running installation is never edited. Code reaches the server only through the normal
path: pull request -> owner merges -> CI -> deploy. Gates are code, and the gate code,
policy, budgets, the eval tasks and this file are protected paths the agents cannot write.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from ..agents.loop import run_agent
from ..agents.roles import RoleDef
from ..config import ModelBinding
from ..contracts import Autonomy, BudgetSpec, TaskSpec
from ..core.kernel import Step, StepContext, Workflow
from ..prompting.compiler import Document, RoleSpec
from ..skills.slots import Slot, run_slot
from ..tools.repo import SKIP_DIRS, is_protected, run_pytest
from ..util import canonical_json, day_key

SIGNALS = ("slot.invalid", "llm.fallback", "llm.error", "llm.all_failed", "agent.max_steps", "agent.loop_detected", "task.failed",
           "agent.compacted", "kb.skipped_known")
PLANNER = RoleSpec("selfdev-planner", (
    "Jesteś architektem biblioteki roj i planujesz jej następne ulepszenie. Na podstawie mapy repozytorium, listy zadań właściciela "
    "i sygnałów z logów proponujesz małe, mierzalne zmiany o największym stosunku zysku do ryzyka: mniej tokenów na ten sam wynik, "
    "mniej błędów, lepsza weryfikacja, brakująca funkcja z listy zadań. Każda propozycja mieści się w jednym małym pull requeście "
    "i zawiera plan testu."),
    rules=("Nie proponuj zmian w ścieżkach chronionych wymienionych w danych wejściowych.",
           "Nie proponuj nowych zależności ani zmian w istniejących testach.",
           "Uporządkuj propozycje od najbardziej wartościowej."))
REVIEWER = RoleSpec("selfdev-reviewer", (
    "Recenzujesz zmianę w kodzie biblioteki roj przed pull requestem. Oceniasz wyłącznie na podstawie diffu i opisu propozycji: "
    "czy zmiana robi to, co obiecuje, czy test rzeczywiście ją sprawdza, czy nie osłabia bezpieczeństwa, budżetów ani bramek, "
    "czy nie zwiększa niepotrzebnie zużycia tokenów."),
    rules=("Werdykt reject wymaga co najmniej jednego konkretnego zarzutu ze wskazaniem pliku.",))
CODER = RoleDef(name="repo-coder", summary="Implements one small change in a git worktree with a test", binding="coder",
                tools=["repo.list", "repo.read", "repo.search", "repo.write", "repo.replace", "repo.test"], autonomy=Autonomy.L1,
                instructions={
                    "pl": "Wprowadzasz jedną małą zmianę w repozytorium zgodnie z propozycją. Najpierw czytasz tylko potrzebne fragmenty "
                          "plików, potem zmieniasz kod narzędziem repo.replace (albo repo.write dla nowych plików), dodajesz nowy plik testu "
                          "i uruchamiasz testy narzędziem repo.test. Kończysz dopiero, gdy testy przechodzą, albo gdy wiesz, dlaczego nie "
                          "da się tego zrobić. Nie zmieniasz istniejących testów ani ścieżek chronionych.",
                    "en": "You make one small change in the repository as proposed. Read only the parts of files you need, change code "
                          "with repo.replace (or repo.write for new files), add a new test file and run the tests with repo.test. "
                          "Finish only when tests pass, or when you know why it cannot be done. Never change existing tests or "
                          "protected paths."})


class Proposal(BaseModel):
    title: str = Field(min_length=8, max_length=90)
    problem: str = Field(min_length=10, max_length=600)
    change: str = Field(min_length=10, max_length=900)
    files: list[str] = Field(min_length=1, max_length=8)
    test_plan: str = Field(min_length=10, max_length=400)
    expected_gain: str = Field(min_length=5, max_length=300)
    risk: Literal["low", "medium", "high"] = "medium"
    size_lines: int = Field(60, ge=1, le=2000)


class Proposals(BaseModel):
    items: list[Proposal] = Field(min_length=1, max_length=5)


class Review(BaseModel):
    verdict: Literal["approve", "reject"]
    summary: str = Field(min_length=5, max_length=600)
    issues: list[str] = Field(default_factory=list, max_length=10)


def git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def _auth(cfg: Any) -> list[str]:
    token = os.environ.get(cfg.github_token_env, "")
    if not token:
        return []
    return ["-c", "http.extraheader=AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()]


def repo_path(ctx: StepContext) -> Path:
    cfg = ctx.project.settings.selfdev
    if not cfg.repo and not cfg.clone_url:
        raise ValueError("set [selfdev] clone_url = \"https://github.com/<you>/roj.git\" (or repo = a local git clone) in roj.toml")
    p = Path(cfg.repo).expanduser().resolve() if cfg.repo else ctx.project.inst.home / "repo"
    if not (p / ".git").exists():
        if not cfg.clone_url:
            raise ValueError(f"[selfdev] repo {p} is not a git clone")
        p.parent.mkdir(parents=True, exist_ok=True)
        git(p.parent, *_auth(cfg), "clone", "-q", cfg.clone_url, str(p))
    return p


def repo_map(repo: Path, limit: int = 220) -> list[str]:
    out = []
    for rel in git(repo, "ls-files").splitlines():
        f = repo / rel
        if set(Path(rel).parts) & SKIP_DIRS or not f.is_file() or f.suffix not in (".py", ".md", ".toml", ".yml", ".yaml", ".sh"):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        doc = ""
        if f.suffix == ".py" and text.lstrip().startswith('"""'):
            doc = text.lstrip()[3:].split('"""')[0].strip().splitlines()[0][:110]
        out.append(f"{rel} ({text.count(chr(10)) + 1} lines)" + (f": {doc}" if doc else ""))
    return out[:limit]


def concept_digest(repo: Path, limit: int = 9000) -> str:
    """The owner's statement of what the project is and what improvement means (docs/CONCEPT.md, digest block)."""
    f = repo / "docs" / "CONCEPT.md"
    if not f.exists():
        return ""
    text = f.read_text(encoding="utf-8")
    m = re.search(r"<!-- digest:start -->(.*?)<!-- digest:end -->", text, re.S)
    return (m.group(1) if m else text).strip()[:limit]


def signals(ctx: StepContext, days: int = 7) -> dict[str, Any]:
    inst, out, errors = ctx.project.inst, {}, []
    for name in inst.project_names():
        p = ctx.project if name == ctx.project.name else inst.project(name)
        try:
            rows = p.store.all(f"SELECT type, COUNT(*) n FROM events WHERE ts >= datetime('now', '-{int(days)} days') "
                               f"AND type IN ({','.join('?' * len(SIGNALS))}) GROUP BY type", SIGNALS)
            for r in rows:
                out[r["type"]] = out.get(r["type"], 0) + r["n"]
            errors += [f"{name}: {r['workflow']}: {str(r['error'])[:160]}" for r in p.store.all(
                "SELECT workflow, error FROM tasks WHERE status = 'failed' AND error IS NOT NULL ORDER BY updated DESC LIMIT 3")]
        except Exception as e:  # noqa: BLE001 - column names differ between versions; signals are best effort
            errors.append(f"{name}: signals unavailable ({type(e).__name__})")
        finally:
            if p is not ctx.project:
                p.close()
    return {"counts": out, "recent_failures": errors[:8]}


async def s_survey(ctx: StepContext) -> dict[str, Any]:
    repo = repo_path(ctx)
    cfg = ctx.project.settings.selfdev
    git(repo, *_auth(cfg), "fetch", cfg.remote, cfg.base, check=False)
    remote_ref = f"{cfg.remote}/{cfg.base}"  # the deployed checkout may be detached: the remote branch is the truth
    base_sha = git(repo, "rev-parse", "--verify", "-q", remote_ref, check=False) or git(repo, "rev-parse", cfg.base)
    backlog = repo / "docs" / "BACKLOG.md"
    from .playbook import load_active

    collected = await asyncio.to_thread(run_pytest, repo, "", 120, ("--collect-only",))
    m = re.search(r"(\d+) tests? collected", collected["tail"])
    return {"repo": str(repo), "base_sha": base_sha, "map": repo_map(repo), "signals": signals(ctx), "base_tests": int(m.group(1)) if m else 0,
            "backlog": backlog.read_text("utf-8")[:6000] if backlog.exists() else "",
            "playbook": load_active(ctx.project.inst.home).profiles, "goal": ctx.inputs.get("goal", ""), "concept": concept_digest(repo)}


def _bind(ctx: StepContext, *roles: str) -> ModelBinding:
    for r in roles:
        try:
            return ctx.project.settings.binding_for(r)
        except KeyError:
            continue
    raise KeyError(f"bind one of these roles in roj.toml: {', '.join(roles)}")


async def s_propose(ctx: StepContext) -> dict[str, Any]:
    sv, cfg = ctx.results["survey"], ctx.project.settings.selfdev
    goal = sv["goal"] or "Zaproponuj ulepszenia biblioteki o największym stosunku zysku do ryzyka"
    task = TaskSpec(goal=goal, deliverable="Proposals JSON", output_format="Wyłącznie JSON",
                    acceptance=["Od 1 do 5 propozycji, każda z planem testu", f"Każda propozycja zmienia najwyżej {cfg.max_diff_lines} linii",
                                "Żadna propozycja nie dotyka ścieżek z listy protected", "Każda ścieżka w files istnieje w mapie albo jest nowym plikiem"],
                    inputs={"protected": cfg.protected, "owner_goal": sv["goal"]}, budget=BudgetSpec(tokens=40_000))
    docs = [Document("repo_map", "\n".join(sv["map"]), "git ls-files"), Document("signals", canonical_json(sv["signals"]), "event logs"),
            Document("backlog", sv["backlog"] or "(empty)", "docs/BACKLOG.md"), Document("playbook", canonical_json(sv["playbook"]), "active Playbook")]
    if sv.get("concept"):
        docs.insert(0, Document("concept", sv["concept"], "docs/CONCEPT.md (owner): what the project is and what improvement means"))
    res = await run_slot(ctx, Slot("selfdev-planner", PLANNER, Proposals, max_tokens=3000), task, binding=_bind(ctx, "planner", "analyst"),
                         scope="team:selfdev/agent:worker", documents=docs)
    dropped, valid = [], []
    for p in res.value.items:
        bad = [f for f in p.files if is_protected(f, cfg.protected)]
        if bad:
            dropped.append({"title": p.title, "why": f"touches protected paths: {bad}"})
        elif p.size_lines > cfg.max_diff_lines:
            dropped.append({"title": p.title, "why": f"too large: {p.size_lines} lines"})
        elif p.risk == "high":
            dropped.append({"title": p.title, "why": "high risk: needs the owner to request it explicitly"})
        else:
            valid.append(p.model_dump())
    if not valid:
        return {"chosen": None, "others": [], "dropped": dropped}
    return {"chosen": valid[0], "others": valid[1:], "dropped": dropped}


async def s_plan_gate(ctx: StepContext) -> dict[str, Any]:
    ch = ctx.results["propose"]["chosen"]
    if not ch:
        return {"go": False}
    ctx.require("t2.selfdev_start", {"title": ch["title"], "problem": ch["problem"], "change": ch["change"], "files": ch["files"],
                                     "risk": ch["risk"], "expected_gain": ch["expected_gain"]})
    return {"go": True}


def worktree(ctx: StepContext) -> tuple[Path, str]:
    repo, sv = Path(ctx.results["survey"]["repo"]), ctx.results["survey"]
    branch = f"roj/selfdev-{ctx.task_id[:8]}"
    wt = ctx.project.inst.home / "selfdev" / ctx.task_id[:8]
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-b", branch, str(wt), sv["base_sha"])
    return wt, branch


def gate(ctx: StepContext, wt: Path) -> dict[str, Any]:
    """Deterministic checks of the worktree against its base commit. Code decides, not a model."""
    cfg, base = ctx.project.settings.selfdev, ctx.results["survey"]["base_sha"]
    git(wt, "add", "-A")
    status = [line.split("\t") for line in git(wt, "diff", "--cached", "--name-status", base).splitlines() if line]
    numstat = [line.split("\t") for line in git(wt, "diff", "--cached", "--numstat", base).splitlines() if line]
    changed = sum(int(a) + int(d) for a, d, _ in numstat if a.isdigit() and d.isdigit())
    files = [s[-1] for s in status]
    checks = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    add("diff is not empty", bool(files))
    prot = [f for s in status for f in s[1:] if is_protected(f, cfg.protected)]
    add("protected paths untouched", not prot, ", ".join(prot))
    old_tests = [s[-1] for s in status if s[0][0] != "A" and any(f.startswith("tests/") for f in s[1:])]
    add("existing tests unchanged", not old_tests, ", ".join(old_tests))
    add(f"diff size <= {cfg.max_diff_lines} lines", changed <= cfg.max_diff_lines, f"{changed} lines")
    code_changed = any(f.endswith(".py") and not f.startswith("tests/") for f in files)
    new_tests = [s[-1] for s in status if s[0][0] == "A" and s[-1].startswith("tests/")]
    add("code change comes with a new test", bool(new_tests) or not code_changed, ", ".join(new_tests))
    tests = {"passed": 0, "failed": 0, "tail": "", "network_isolated": False}
    if files and all(c["ok"] for c in checks):
        tests = run_pytest(wt, timeout_s=cfg.test_timeout_s)
        add("all tests pass", tests["exit"] == 0 and tests["failed"] == 0, f"{tests['passed']} passed, {tests['failed']} failed")
        add("test count did not drop", tests["passed"] >= ctx.results["survey"].get("base_tests", 0),
            f"{tests['passed']} vs {ctx.results['survey'].get('base_tests', 0)} before")
    return {"ok": bool(files) and all(c["ok"] for c in checks), "checks": checks, "files": files, "lines": changed,
            "tests_tail": tests["tail"][-1500:], "network_isolated": tests["network_isolated"]}


async def s_build(ctx: StepContext) -> dict[str, Any]:
    if not ctx.results["plan_gate"]["go"]:
        return {"ok": False, "skipped": True, "checks": []}
    ch = ctx.results["propose"]["chosen"]
    wt, branch = worktree(ctx)
    ctx.repo_root = str(wt)
    ctx.repo_base_files = set(git(wt, "ls-tree", "-r", "--name-only", ctx.results["survey"]["base_sha"]).splitlines())
    feedback, result = "", {"ok": False, "checks": []}
    for attempt in (1, 2):
        async def work(feedback: str = feedback) -> dict[str, Any]:
            task = TaskSpec(goal=f"Wprowadź w repozytorium zmianę: {ch['title']}", deliverable="Zmienione pliki w worktree i przechodzące testy",
                            acceptance=["Wszystkie testy przechodzą (repo.test zwraca failed = 0)", "Dodano co najmniej 1 nowy plik testu w tests/",
                                        "Zmienione są wyłącznie pliki potrzebne do tej zmiany"],
                            inputs={"proposal": ch, **({"previous_attempt_failed_checks": feedback} if feedback else {})},
                            budget=BudgetSpec(tokens=250_000))
            run = await run_agent(ctx, role=CODER, task=task, binding=_bind(ctx, "coder", "analyst"), agent_name="repo-coder",
                                  scope="team:selfdev/agent:worker", max_steps=int(ctx.inputs.get("max_steps", 30)))
            return {"agent": run.output.model_dump(), "steps": run.steps, **await asyncio.to_thread(gate, ctx, wt)}
        result = await ctx.memo(f"build:{attempt}", work)
        if result["ok"]:
            break
        feedback = "; ".join(f"{c['name']}: {c['detail']}" for c in result["checks"] if not c["ok"]) + "\n" + result.get("tests_tail", "")[-800:]
    return {**result, "worktree": str(wt), "branch": branch, "attempts": attempt}


async def s_review(ctx: StepContext) -> dict[str, Any]:
    b = ctx.results["build"]
    if not b["ok"]:
        return {"verdict": "skipped", "summary": "the build did not pass the gates", "issues": []}
    diff = git(Path(b["worktree"]), "diff", "--cached", ctx.results["survey"]["base_sha"])
    task = TaskSpec(goal="Oceń zmianę przed pull requestem i wydaj werdykt approve albo reject", deliverable="Review JSON",
                    output_format="Wyłącznie JSON", acceptance=["Werdykt to approve albo reject", "Każdy zarzut wskazuje plik z diffu"],
                    budget=BudgetSpec(tokens=40_000))
    res = await run_slot(ctx, Slot("selfdev-reviewer", REVIEWER, Review, max_tokens=1500), task, binding=_bind(ctx, "critic", "checker"),
                         scope="team:selfdev/agent:worker", documents=[Document("proposal", canonical_json(ctx.results["propose"]["chosen"]), "planner"),
                                                               Document("diff", diff[:40_000], "git diff")])
    return res.value.model_dump()


def write_report(ctx: StepContext, outcome: str, extra: dict[str, Any]) -> str:
    pr, b, rv = ctx.results["propose"], ctx.results["build"], ctx.results["review"]
    ch = pr["chosen"] or {}
    lines = [f"# Self-improvement {day_key(ctx.project.settings.timezone)} (task {ctx.task_id[:8]}): {outcome}", "",
             f"**{ch.get('title', 'no valid proposal')}**", "", f"Problem: {ch.get('problem', '-')}", "", f"Change: {ch.get('change', '-')}", "",
             f"Expected gain: {ch.get('expected_gain', '-')}; risk: {ch.get('risk', '-')}", "", "## Gates", ""]
    lines += [f"- [{'x' if c['ok'] else ' '}] {c['name']}" + (f" ({c['detail']})" if c["detail"] else "") for c in b.get("checks", [])]
    lines += ["", f"Files: {', '.join(b.get('files', [])) or '-'}; {b.get('lines', 0)} lines; attempts: {b.get('attempts', 0)}; "
              f"tests ran without network: {'yes' if b.get('network_isolated') else 'no'}", "", "## Review", "",
              f"{rv.get('verdict')}: {rv.get('summary')}"] + [f"- {i}" for i in rv.get("issues", [])]
    lines += ["", "## Other proposals", ""] + [f"- {o['title']}: {o['expected_gain']}" for o in pr["others"]]
    lines += [f"- dropped: {d['title']} ({d['why']})" for d in pr["dropped"]]
    lines += ["", "## Result", ""] + [f"- {k}: {v}" for k, v in extra.items()]
    rdir = ctx.project.dir / "reports"
    rdir.mkdir(exist_ok=True)
    path = rdir / f"selfdev-{day_key(ctx.project.settings.timezone)}-{ctx.task_id[:8]}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def open_pull_request(cfg: Any, branch: str, title: str, body: str) -> dict[str, Any]:
    token = os.environ.get(cfg.github_token_env, "")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{cfg.github_api.rstrip('/')}/repos/{cfg.github_repo}/pulls",
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                         json={"title": title, "head": branch, "base": cfg.base, "body": body})
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub pull request failed: HTTP {r.status_code} {r.text[:300]}")
    return {"url": r.json().get("html_url"), "number": r.json().get("number")}


def cleanup(ctx: StepContext) -> None:
    b = ctx.results["build"]
    if b.get("worktree"):
        repo = Path(ctx.results["survey"]["repo"])
        git(repo, "worktree", "remove", "--force", b["worktree"], check=False)
        git(repo, "branch", "-D", b["branch"], check=False)


async def s_publish(ctx: StepContext) -> dict[str, Any]:
    b, rv, cfg = ctx.results["build"], ctx.results["review"], ctx.project.settings.selfdev
    if not b["ok"] or rv["verdict"] != "approve":
        outcome = "no proposal" if b.get("skipped") else ("rejected by gates" if not b["ok"] else "rejected by review")
        cleanup(ctx)
        return {"outcome": outcome, "report": write_report(ctx, outcome, {"branch": "removed"})}
    wt, ch = Path(b["worktree"]), ctx.results["propose"]["chosen"]
    body = (f"{ch['problem']}\n\n{ch['change']}\n\nExpected gain: {ch['expected_gain']}\n\nGates: "
            + "; ".join(f"{c['name']} ok" for c in b["checks"]) + f"\n\nReview ({rv['verdict']}): {rv['summary']}\n\n"
            f"Proposed by roj selfdev task {ctx.task_id[:8]}. Not merged automatically.")
    if git(wt, "status", "--porcelain"):
        git(wt, "-c", "user.name=roj selfdev", "-c", "user.email=roj-selfdev@localhost", "commit", "-q", "-m", f"{ch['title']}\n\n{body}")
    patch = ctx.project.dir / "reports" / f"selfdev-{ctx.task_id[:8]}.patch"
    patch.parent.mkdir(exist_ok=True)
    patch.write_text(git(wt, "format-patch", "-1", "--stdout"), encoding="utf-8")
    ctx.require("t3.open_pull_request", {"title": ch["title"], "branch": b["branch"], "files": b["files"], "lines": b["lines"],
                                         "review": rv["summary"], "patch": str(patch)})
    extra: dict[str, Any] = {"branch": b["branch"], "patch": str(patch)}
    token = os.environ.get(cfg.github_token_env, "")
    if cfg.github_repo and token:
        git(wt, *_auth(cfg), "push", "-u", cfg.remote, b["branch"])
        extra["pull_request"] = (await open_pull_request(cfg, b["branch"], ch["title"], body))["url"]
        outcome = "pull request opened"
    else:
        outcome = "branch and patch ready (no [selfdev] github_repo or token: open the pull request yourself)"
    git(Path(ctx.results["survey"]["repo"]), "worktree", "remove", "--force", str(wt), check=False)  # the branch stays
    ctx.log("selfdev.published", {"outcome": outcome, **extra})
    return {"outcome": outcome, "report": write_report(ctx, outcome, extra), **extra}


SELFDEV = Workflow("lab.selfdev", (
    Step("survey", s_survey), Step("propose", s_propose), Step("plan_gate", s_plan_gate), Step("build", s_build),
    Step("review", s_review), Step("publish", s_publish),
), "One gated improvement of roj's own repository per run")
SELFDEV_REF = "roj.lab.selfdev:SELFDEV"
