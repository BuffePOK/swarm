import asyncio
import textwrap

import pytest

from roj.blueprint import manage
from roj.blueprint.linter import lint
from roj.blueprint.runner import RUN_REF
from roj.blueprint.schema import eval_condition
from roj.core.kernel import StepContext, TaskStatus, WaitForApproval
from roj.knowledge.packs import KnowledgeBase
from roj.tools import registry

BP = textwrap.dedent("""
project: t
goal: Prepare a short macro note with verified facts
acceptance: ["Every fact cites a source", "At most 300 words"]
teams:
  - name: research
    goal: Collect the latest policy rate decisions with sources
    acceptance: ["At least 1 source cited"]
    mode: orchestrator
    budget: {usd: 1.0}
    members:
      - {name: lead, role: orchestrator, model: fake-smart, fallbacks: [fake-ok]}
      - {name: researcher, role: researcher, model: fake-smart, fallbacks: [fake-ok], knowledge: [macro], tools: [kb.search, calc]}
  - name: brief
    goal: Write the note from the research and verify it
    acceptance: ["Every claim has a source", "Verifier status is done"]
    mode: graph
    depends_on: [research]
    members:
      - {name: writer, role: writer, model: fake-smart, fallbacks: [fake-ok], tools: []}
      - {name: verifier, role: verifier, model: fake-smart-b, fallbacks: [fake-ok], tools: []}
    graph:
      start: write
      max_visits: 3
      nodes:
        - {id: write, member: writer, task: "Write the note using: {state.research}"}
        - {id: verify, member: verifier, task: "Verify the draft: {state.write}"}
      edges:
        - {source: write, target: verify}
        - {source: verify, target: write, when: "state['verify']['status'] != 'done'"}
integration:
  guardian: {name: guardian, role: guardian, model: fake-smart-b, fallbacks: [fake-ok]}
""")


def run(c):
    return asyncio.run(c)


def ctx(project):
    return StepContext(project, "task-tools", "task-tools", "s", 0, 1, {}, {})


def test_lint_catches_structural_errors(project):
    bp = manage.parse(BP)
    assert [i for i in lint(bp, project) if i.severity == "error"] == [
        i for i in lint(bp, project) if i.code == "BP08" and i.severity == "error"]  # pack 'macro' not created yet
    bad = manage.parse(BP.replace("role: orchestrator", "role: researcher").replace("depends_on: [research]", "depends_on: [brief]")
                       .replace("state['verify']['status'] != 'done'", "__import__('os')"))
    codes = {i.code for i in lint(bad) if i.severity == "error"}
    assert {"BP05", "BP11"} <= codes


def test_safe_conditions():
    assert eval_condition("state['v']['status'] != 'done'", {"v": {"status": "failed"}})
    assert not eval_condition("state['missing']['x'] == 1", {})
    with pytest.raises(ValueError):
        eval_condition("state.__class__", {})
    with pytest.raises(ValueError):
        eval_condition("open('x')", {})


def test_tools_cards_calc_workspace_and_approval(project):
    tools = registry.all_tools()
    assert "kb.search(pack: str, query: str, k: int = 5)" in tools["kb.search"].card(1)
    c = ctx(project)
    assert run(registry.call(c, "calc", {"expression": "2**10 + 1"}, agent="t"))["result"] == 1025
    assert run(registry.call(c, "calc", {"expression": "__import__('os')"}, agent="t"))["ok"] is False
    assert run(registry.call(c, "files.write", {"path": "notes/a.md", "text": "hi"}, agent="t"))["ok"]
    assert "outside" in run(registry.call(c, "files.read", {"path": "../../roj.toml"}, agent="t"))["error"]
    with pytest.raises(WaitForApproval):
        run(registry.call(c, "shell.run", {"command": "echo hi"}, agent="t"))  # T2: the owner decides


def test_blueprint_propose_approve_run(project, gateway):
    kb = KnowledgeBase(project)
    kb.create("macro", "Central bank notes")
    run(kb.add_text("macro", "nordland", "Nordland policy", "# Nordland\n\nThe policy rate decision kept rates at 4.25%."))
    out = manage.propose(project, manage.parse(BP))
    assert out["status"] == "waiting_approval"
    with pytest.raises(ValueError):
        manage.activate(project, out["version"])  # not approved yet
    project.policy.decide(out["approval_id"], True)
    act = manage.activate(project, out["version"])
    assert act["status"] == "active" and (project.dir / "adr" / "blueprint-v001.md").exists()
    tid = project.kernel.submit(RUN_REF, {})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.DONE, res.error
    teams = res.results["teams"]["teams"]
    assert teams["research"]["status"] == "done" and "Nordland policy" in teams["research"]["summary"]
    assert teams["brief"]["path"] == ["write", "verify", "write", "verify"]  # verifier sent it back once
    assert res.results["integrate"]["verdict"] == "consistent"
    tool_events = [e.payload["tool"] for e in project.events.query(type="tool.call", task_id=tid)]
    assert "kb.search" in tool_events and "calc" in tool_events
    assert project.budget.limit_of("team:research") == (None, 1.0)  # blueprint budgets became allocations
    # replaying the task does not redo finished teams or nodes
    before = project.store.one("SELECT COUNT(*) n FROM events WHERE type = 'llm.call'")["n"]
    project.store.run("UPDATE tasks SET status = 'pending' WHERE id = ?", (tid,))
    project.store.run("DELETE FROM steps WHERE task_id = ? AND name != 'load'", (tid,))
    assert run(project.kernel.run(tid)).status == TaskStatus.DONE
    assert project.store.one("SELECT COUNT(*) n FROM events WHERE type = 'llm.call'")["n"] == before


def test_auto_mode_is_not_free(project):
    bp = manage.parse(BP.replace("project: t\n", "project: t\ncontrol_mode: auto\n").replace("knowledge: [macro], ", ""))
    out = manage.propose(project, bp)
    assert out["status"] == "waiting_approval" and "not unlocked" in out["note"]
