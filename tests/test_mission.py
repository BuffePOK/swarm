import asyncio
import json
import subprocess

import pytest

from roj import mission as ms
from roj.core.kernel import TaskStatus
from roj.knowledge.packs import KnowledgeBase
from roj.project import Installation


def run(c):
    return asyncio.run(c)


def approve_all(project):
    for a in project.policy.pending():
        project.policy.decide(a["id"], True)
    return run(project.kernel.resume())


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def lab(home, tmp_path):
    repo = tmp_path / "toy-repo"
    (repo / "toy").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "toy" / "__init__.py").write_text('"""Toy package."""\n')
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    with open(home / "roj.toml", "a", encoding="utf-8") as f:
        f.write(f'\n[selfdev]\nrepo = "{repo.as_posix()}"\n')
    inst = Installation(home)
    project = inst.create_project("lab")
    yield project
    project.close()
    inst.close()


def start(project, control="leads"):
    mid = ms.create(project, "Improve roj with roj, step by step", control)
    res = run(project.kernel.run(project.kernel.submit(ms.INTAKE_REF, {"mission": mid})))
    assert res.status == TaskStatus.DONE, res.error
    return mid


def test_mission_goal_questions_stage_review_and_change_request(lab):
    mid = start(lab)
    m = ms.load(lab, mid)
    assert m["status"] == "needs_answers" and [q["id"] for q in ms.open_questions(m)] == ["focus", "pace"]
    blocked = run(lab.kernel.run(lab.kernel.submit(ms.STAGE_REF, {"mission": mid})))
    assert blocked.status == TaskStatus.FAILED and "unanswered" in blocked.error  # no planning on guesses
    m = ms.answer(lab, mid, {"focus": "token cost"})
    assert m["status"] == "ready" and m["qa"][0]["answered_by"] == "owner" and m["qa"][1]["answer"] == "1" and m["qa"][1]["answered_by"] == "default"

    res = run(lab.kernel.run(lab.kernel.submit(ms.STAGE_REF, {"mission": mid})))
    assert res.status == TaskStatus.WAITING_APPROVAL, res.error
    plan = res.results["plan"]
    assert plan["attempts"] == 2  # the first plan used a tool that does not exist; code caught it, the Architect fixed it
    assert plan["plan"]["stage"]["team"][0]["tools"] == ["repo.list", "kb.note"]
    [pending] = lab.policy.pending()
    assert pending["action"] == "t2.mission_stage"
    assert ms.load(lab, mid)["stages"] == [] and KnowledgeBase(lab).packs() == []  # nothing runs before the owner agrees

    results = approve_all(lab)
    stage = next(r for r in results if "close" in r.results)
    assert stage.status == TaskStatus.DONE, stage.error
    m = ms.load(lab, mid)
    assert m["status"] == "stage_done" and m["stages"][0]["review"]["verdict"] == "done" and m["lessons"][0].startswith("stage 1:")
    assert [p["pack"] for p in KnowledgeBase(lab).packs()] == ["project-map"]  # the agent built shared knowledge
    report = open(stage.results["close"]["report"], encoding="utf-8").read()
    assert "w-code-analyst" in report and "change request -> selfdev task" in report
    cr = m["stages"][0]["change_requests"][0]
    row = lab.store.one("SELECT workflow, status FROM tasks WHERE id = ?", (cr["task"],))
    assert row["workflow"].endswith("selfdev:SELFDEV")  # code changes go through selfdev with its own gates and approvals
    assert any(a["action"] == "t2.selfdev_start" for a in lab.policy.pending()) or row["status"] in ("pending", "waiting_approval")

    res2 = run(lab.kernel.run(lab.kernel.submit(ms.STAGE_REF, {"mission": mid})))
    assert res2.status == TaskStatus.WAITING_APPROVAL and res2.results["plan"]["n"] == 2


def test_control_modes_decide_who_the_owner_talks_to_and_what_he_approves(lab):
    mid = start(lab, "leads")
    ms.answer(lab, mid, {})
    chat = lambda to, text: run(lab.kernel.run(lab.kernel.submit(ms.CHAT_REF, {"mission": mid, "to": to, "text": text})))  # noqa: E731
    r = chat("architect", "Zawsze zaczynaj od najtańszego modelu.")
    assert r.status == TaskStatus.DONE and r.results["chat"]["directive"]
    m = ms.load(lab, mid)
    assert m["directives"][0]["via"] == "architect" and len(m["threads"]["architect"]) == 2
    with pytest.raises(PermissionError):
        ms.may_talk(m, "w-code-analyst")

    ms.set_control(lab, mid, "full")
    res = run(lab.kernel.run(lab.kernel.submit(ms.STAGE_REF, {"mission": mid})))
    assert [a["action"] for a in lab.policy.pending()] == ["t2.mission_stage"]
    [res] = [r for r in approve_all(lab) if r.task_id == res.task_id]
    assert res.status == TaskStatus.WAITING_APPROVAL  # full control: each agent's task waits for the owner
    agent_gate = [a for a in lab.policy.pending() if a["action"] == "t2.mission_agent_run"]
    assert len(agent_gate) == 1 and "w-code-analyst" in json.dumps(agent_gate[0]["details"])
    done = [r for r in approve_all(lab) if r.task_id == res.task_id]
    assert done and done[0].status == TaskStatus.DONE, done[0].error
    m = ms.load(lab, mid)
    assert ms.may_talk(m, "w-code-analyst")["result"]["status"] == "done"
    assert chat("w-code-analyst", "What did you find?").results["chat"]["reply"]
    ms.set_control(lab, mid, "leads")
    with pytest.raises(PermissionError):
        ms.may_talk(ms.load(lab, mid), "w-code-analyst")


def test_cli_mission_flow(home, capsys):
    from roj.cli import main

    base = ["--home", str(home)]
    assert main(base + ["project", "create", "lab"]) == 0
    capsys.readouterr()
    assert main(base + ["mission", "start", "-p", "lab", "Improve roj with roj"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "needs_answers" and out["next"] == "roj mission answer" and len(out["open_questions"]) == 2
    assert main(base + ["logs", "raw", "-p", "lab"]) == 0  # the gateway's own answers of the intake
    raw = json.loads(capsys.readouterr().out)
    assert raw["answers"] and raw["answers"][0]["finish_reason"] == "stop" and "understanding" in raw["answers"][0]["content"]
    assert main(base + ["mission", "intake", "-p", "lab"]) == 0  # intake again: known questions are not duplicated
    assert len(json.loads(capsys.readouterr().out)["open_questions"]) == 2
    assert main(base + ["mission", "next", "-p", "lab"]) == 2
    capsys.readouterr()
    assert main(base + ["mission", "answer", "-p", "lab", "focus=reliability"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert main(base + ["mission", "next", "-p", "lab"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "waiting_approval"
    assert main(base + ["chat", "-p", "lab", "--to", "w-code-analyst", "hi"]) != 0  # leads mode
