import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from roj.core.kernel import TaskStatus
from roj.project import Installation

HELLO = "roj.examples.hello:WORKFLOW"


def run(coro):
    return asyncio.run(coro)


def test_hello_workflow_done(project, gateway):
    tid = project.kernel.submit(HELLO, {"topic": "kernels", "model": "fake-ok"})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.DONE
    assert res.results["gate"]["passed"] and res.results["publish"]["published"] is False
    assert project.events.verify().ok


def test_approval_parks_and_resumes_without_rerunning_done_steps(project, gateway):
    state, _ = gateway
    tid = project.kernel.submit(HELLO, {"topic": "x", "model": "fake-ok", "publish": True})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.WAITING_APPROVAL
    assert run(project.kernel.resume()) == []  # nothing to do until the owner decides
    pending = project.policy.pending()
    assert len(pending) == 1 and pending[0]["action"] == "t3.publish_note"
    project.policy.decide(pending[0]["id"], True)
    [res2] = run(project.kernel.resume())
    assert res2.status == TaskStatus.DONE and res2.results["publish"]["published"] is True
    assert state.requests["fake-ok"] == 1  # the model step was not executed again


def test_rejected_approval_fails_task(project, gateway):
    tid = project.kernel.submit(HELLO, {"topic": "x", "model": "fake-ok", "publish": True})
    run(project.kernel.run(tid))
    project.policy.decide(project.policy.pending()[0]["id"], False, note="not now")
    [res] = run(project.kernel.resume())
    assert res.status == TaskStatus.FAILED and "policy denied" in res.error


def test_api_outage_pauses_task(project, gateway):
    tid = project.kernel.submit(HELLO, {"topic": "x", "model": "fake-blocked", "fallbacks": ["fake-auth"]})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.PAUSED_API
    assert any(e.payload.get("kind") == "api_unavailable" for e in project.events.query(type="alert"))
    assert run(project.kernel.resume()) == []  # paused tasks wait for an explicit retry


def test_budget_exhaustion_pauses_task(inst, gateway):
    p = inst.create_project("poor", usd=0.00001)
    tid = p.kernel.submit(HELLO, {"topic": "x", "model": "fake-ok"})
    res = run(p.kernel.run(tid))
    assert res.status == TaskStatus.PAUSED_BUDGET
    p.close()


def test_step_bug_fails_task_cleanly(project, tmp_path, monkeypatch):
    monkeypatch.setenv("ROJ_TEST_DIR", str(tmp_path))
    tid = project.kernel.submit("wf_fixtures:BUGGY", {})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.FAILED and "boom" in res.error
    assert project.kernel.get(tid)["steps"][1]["status"] == "failed"


def test_in_process_hard_stop_resumes(project, tmp_path, monkeypatch):
    monkeypatch.setenv("ROJ_TEST_DIR", str(tmp_path))
    tid = project.kernel.submit("wf_fixtures:CRASH", {})
    with pytest.raises(KeyboardInterrupt):
        run(project.kernel.run(tid))
    assert project.kernel.get(tid)["status"] == "running"
    [res] = run(project.kernel.resume())
    assert res.status == TaskStatus.DONE and res.results["after_crash"]["value"] == 12
    log = (tmp_path / "executions.log").read_text().split()
    assert log == ["one", "crash_once", "crash_once", "after_crash"]  # "one" not repeated


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics")
def test_kill_minus_9_and_resume(home, tmp_path):
    """Phase 0 acceptance: kill the worker mid-task, resume without losing finished work."""
    inst = Installation(home)
    p = inst.create_project("killme")
    tid = p.kernel.submit("wf_fixtures:SLOW", {})
    p.close()
    inst.close()

    env = {**os.environ, "ROJ_TEST_DIR": str(tmp_path), "ROJ_TEST_SLOW": "1",
           "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, "-m", "roj", "--home", str(home), "task", "resume", "-p", "killme", tid],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    marker = tmp_path / "in_two.marker"
    deadline = time.time() + 20
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists(), proc.stderr.read().decode() if proc.poll() is not None else "worker never reached step two"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(10)

    env["ROJ_TEST_SLOW"] = "0"
    out = subprocess.run([sys.executable, "-m", "roj", "--home", str(home), "--json", "task", "resume", "-p", "killme"],
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr

    inst = Installation(home)
    p = inst.project("killme")
    t = p.kernel.get(tid)
    assert t["status"] == "done"
    assert (tmp_path / "executions.log").read_text().split() == ["one", "two", "two", "three"]
    assert p.events.verify().ok
    types = [e.type for e in p.events.query(task_id=tid)]
    assert "task.resumed" in types
    p.close()
    inst.close()
