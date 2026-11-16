import asyncio
import json
import subprocess

import pytest

from roj.agents.compaction import maybe_compact, size, split
from roj.config import ModelBinding
from roj.contracts import BudgetSpec, TaskSpec
from roj.core.kernel import StepContext, TaskStatus
from roj.knowledge.packs import KnowledgeBase
from roj.knowledge.router import retrieve
from roj.lab import playbook as pbk
from roj.lab.experiments import decide, paired_stats
from roj.lab.selfdev import SELFDEV_REF
from roj.lab.study import STUDY_REF
from roj.lab.tasks import KINDS, make_case
from roj.prompting.compiler import Document, RoleSpec, compile_prompt
from roj.prompting.profiles import DEFAULT, PromptProfile, family
from roj.tools.repo import is_protected


def run(c):
    return asyncio.run(c)


def approve_all(project):
    for a in project.policy.pending():
        project.policy.decide(a["id"], True)
    return run(project.kernel.resume())


def ctx_for(project):
    return StepContext(project, None, None, "test", 0, 1, {}, {})


SPEC = TaskSpec(goal="Summarise the document in one sentence", deliverable="one sentence", acceptance=["Exactly 1 sentence"],
                budget=BudgetSpec(tokens=1000))


def test_profiles_change_layout_not_content(project):
    s = project.settings
    kw = dict(language=s.language, settings=s.prompt, documents=[Document("d1", "Body <b>text</b>", "src")])
    role = RoleSpec("r", "You summarise documents.")
    base = compile_prompt(role, SPEC, **kw)
    assert compile_prompt(role, SPEC, profile=DEFAULT, **kw).prefix_hash == base.prefix_hash
    md = compile_prompt(role, SPEC, profile=PromptProfile(structure="markdown", reminder=False), **kw)
    assert "<role" not in md.messages[0]["content"] and "## ROLE" in md.messages[0]["content"]
    assert "Body <b>text</b>" in md.messages[1]["content"] and "REMINDER" not in md.messages[1]["content"]
    think = compile_prompt(role, SPEC, profile=PromptProfile(reasoning="brief", language="en"), **kw)
    assert "thinking" in think.messages[0]["content"] and "If the provided material" in think.messages[0]["content"]
    late = compile_prompt(role, SPEC, profile=PromptProfile(documents_first=False), **kw).messages[1]["content"]
    assert late.index("<task>") < late.index("<documents>")
    shots = compile_prompt(role, SPEC, examples=[("in1", "out1"), ("in2", "out2"), ("in3", "out3")], **kw).messages[1]["content"]
    assert shots.count("<example>") == 2
    assert family("google/gemini-3-flash") == "gemini" and family("fake-smart-b") == "fake-smart-b"
    with pytest.raises(ValueError):
        PromptProfile().with_knob("structure", "yaml")


def test_eval_cases_are_deterministic_and_scored_by_code():
    for kind in KINDS:
        a, b = make_case(kind, 3), make_case(kind, 3)
        assert a.answer == b.answer and a.score(a.answer) == 1.0 and a.score({}) < 0.5 and a.score("junk") == 0.0
        assert make_case(kind, 4).answer != a.answer or kind == "grounded"
        assert a.spec("pl").goal != a.spec("en").goal
    inj = make_case("injection", 1)
    assert inj.score({"city": "HACKED", "temperature": 0}) == 0.0


def test_statistics_and_decisions():
    up = paired_stats([1, 1, 0, 1, 0, 1, 1, 0, 1, 1])
    assert up["ci_low"] > 0 and decide(up, 0.2) == "supported"
    flat = paired_stats([0.0] * 12)
    assert decide(flat, -0.2) == "supported" and decide(flat, 0.05) == "refuted"  # cheaper wins, costlier loses
    assert decide(paired_stats([-1, -1, 0, -1, -1, 0, -1, -1]), 0) == "refuted"
    assert decide(paired_stats([1, -1, 1, -1, 0, 0, 1, -1]), 0) == "inconclusive"
    assert decide(paired_stats([1, 1, 1]), 0) == "inconclusive"  # too few cases


def test_compaction_keeps_prefix_and_tool_pairs(project):
    ctx = ctx_for(project)
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "type": "function",
                                                                         "function": {"name": "web__fetch", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + "lorem ipsum " * 400})
    b = ModelBinding(model="fake-smart")
    assert run(maybe_compact(ctx, msgs, binding=b, agent="a")) is msgs  # under the limit: untouched
    project.settings.context.compact_at_tokens = 11000
    one = run(maybe_compact(ctx, msgs, binding=b, agent="a"))
    assert one[:2] == msgs[:2] and len(one) == len(msgs) and size(one) < size(msgs) / 2
    assert "elided" in one[3]["content"] and one[-1]["content"] == msgs[-1]["content"]
    project.settings.context.compact_at_tokens = 1500
    two = run(maybe_compact(ctx, msgs, binding=b, agent="a"))
    assert two[:2] == msgs[:2] and "<progress_notes" in two[2]["content"]
    _, turns = split(two[:2] + two[3:])
    assert len(turns) == 3 and all(t[0]["tool_calls"][0]["id"] == t[1]["tool_call_id"] for t in turns)
    assert project.store.one("SELECT COUNT(*) n FROM events WHERE type = 'agent.compacted'")["n"] == 2


def test_router_gives_only_relevant_knowledge_to_the_right_role(project):
    kb = KnowledgeBase(project)
    kb.create("ops", "Deployment runbooks: systemd units, rollback, backups", audience=["devops"])
    kb.create("style", "Editorial style guide for reports")
    run(kb.add_text("ops", "rollback", "Rollback runbook", "To roll back a deployment run deploy.sh with the previous commit. "
                                                           "The systemd unit restarts the service after rollback."))
    run(kb.add_text("style", "tone", "Tone", "Reports use short sentences and plain words."))
    r = run(retrieve(project, "How do I roll back a deployment?", role="devops"))
    assert [h.doc_id for h in r.hits] == ["rollback"]
    assert run(retrieve(project, "How do I roll back a deployment?", role="writer")).hits == []  # not this role's pack
    assert run(retrieve(project, "What is the boiling point of nitrogen?", role="devops")).hits == []  # nothing fits: nothing injected


def test_novelty_gate_skips_what_the_model_knows(project):
    kb = KnowledgeBase(project)
    kb.create("facts", "Facts for agents")
    known = "The capital of France is Paris. The boiling point of water is 100 degrees. The largest planet is Jupiter."
    internal = "The internal port of service Zefir is 7431. The owner of module billing is Team Kolibri. Our retention period is 45 days."
    assert run(kb.add_text("facts", "school", "School facts", known, novelty=True)) == 0
    assert run(kb.add_text("facts", "internal", "Internal facts", internal, novelty=True)) > 0
    docs = {r["doc_id"]: json.loads(r["meta"]) for r in project.store.all("SELECT doc_id, meta FROM kb_docs")}
    assert docs["school"]["skipped"] == "known" and docs["internal"]["novelty"] == 1.0
    assert [h.doc_id for h in run(kb.search("facts", "port of service Zefir"))] == ["internal"]


def test_study_finds_planted_effects_and_playbook_changes_prompts(project, web, inst):
    _, web_url = web
    seeds = [[f"guide-{f}", f, f"{web_url}/docs/prompting-{f}"] for f in ("fake-smart", "fake-smart-b")]
    tid = project.kernel.submit(STUDY_REF, {"models": "fake-smart,fake-smart-b,fake-blocked", "per_kind": 3, "seeds": seeds, "budget_tokens": 2_000_000})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.WAITING_APPROVAL, res.error
    r = res.results
    assert r["probe"]["cards"]["fake-blocked"]["available"] is False and r["experiment"]["models"] == ["fake-smart", "fake-smart-b"]
    by = {(x["knob"], x["family"]): x for x in r["experiment"]["results"]}
    assert by[("reasoning", "fake-smart-b")]["status"] == "supported" and by[("reasoning", "fake-smart-b")]["mean"] > 0.1
    assert by[("structure", "fake-smart")]["status"] == "refuted"
    assert r["confirm"]["holdout"]["fake-smart-b"]["confirmed"] is True
    assert len(r["research"]["claims"]) == 3 and all(c["source"].startswith("guide-") for c in r["research"]["claims"])  # the claim with an invented source was dropped
    report = open(r["report"]["report"], encoding="utf-8").read()
    assert "Model cards" in report and "CONFIRMED" in report and "Read, not yet tested" in report
    assert project.profile_for("fake-smart-b") == DEFAULT  # nothing changes before the owner approves
    [done] = approve_all(project)
    assert done.status == TaskStatus.DONE
    assert project.profile_for("fake-smart-b-2").reasoning == "brief" and project.profile_for("fake-smart").structure == "xml"
    pb = pbk.load_active(inst.home)
    assert pb.version == 1 and any(x.status == "hypothesis" for x in pb.rules)
    assert (inst.home / "playbook" / "models" / "fake-smart.json").exists()
    calls = project.store.one("SELECT COUNT(*) n FROM events WHERE task_id = ? AND type = 'llm.call'", (tid,))["n"]
    tid2 = project.kernel.submit(STUDY_REF, {"models": "fake-smart,fake-smart-b", "per_kind": 3, "research": False, "budget_tokens": 2_000_000})
    run(project.kernel.run(tid2))
    fresh = project.store.one("SELECT COUNT(*) n FROM events WHERE task_id = ? AND type = 'llm.call'", (tid2,))["n"]
    assert fresh < calls / 5  # a repeated study re-pays almost nothing: exact cache


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def toy_repo(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "toy-repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    repo.mkdir()
    (repo / "toy").mkdir()
    (repo / "tests").mkdir()
    (repo / "toy" / "__init__.py").write_text('"""Toy package."""\n')
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    return repo, remote


def test_selfdev_builds_gates_reviews_and_opens_a_pull_request(home, web, toy_repo, monkeypatch):
    from roj.project import Installation

    state, web_url = web
    repo, remote = toy_repo
    monkeypatch.setenv("ROJ_GITHUB_TOKEN", "ghp_test")
    with open(home / "roj.toml", "a", encoding="utf-8") as f:
        f.write(f'\n[selfdev]\nrepo = "{repo.as_posix()}"\ngithub_repo = "me/toy"\ngithub_api = "{web_url}"\n')
    inst = Installation(home)
    project = inst.create_project("lab")
    tid = project.kernel.submit(SELFDEV_REF, {})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.WAITING_APPROVAL and res.results["propose"]["chosen"]["title"].startswith("Add a slugify")
    assert "protected" in res.results["propose"]["dropped"][0]["why"]
    [res] = approve_all(project)  # owner agrees to the plan -> build, gates, review, then asks again before publishing
    assert res.status == TaskStatus.WAITING_APPROVAL, res.error
    b = res.results["build"]
    assert b["ok"] and sorted(b["files"]) == ["tests/test_slug.py", "toy/text.py"] and res.results["review"]["verdict"] == "approve"
    assert not (repo / "toy" / "text.py").exists()  # the checkout itself is never edited
    assert state.pulls == []
    [res] = approve_all(project)
    assert res.status == TaskStatus.DONE, res.error
    assert res.results["publish"]["pull_request"] == "https://example.test/pull/1" and state.pulls[0]["head"] == b["branch"]
    branches = subprocess.run(["git", "-C", str(remote), "branch", "--list"], capture_output=True, text=True,
                              encoding="utf-8").stdout
    assert b["branch"] in branches
    report = open(res.results["publish"]["report"], encoding="utf-8").read()
    assert "[x] all tests pass" in report and "dropped: Loosen the permission checks" in report
    assert "ghp_test" not in json.dumps([dict(r) for r in project.store.all("SELECT payload FROM events")])
    project.close()
    inst.close()


def test_protected_paths():
    pats = ["roj/core/policy.py", "roj/verify/*", ".github/*"]
    assert is_protected("roj/verify/ratchet.py", pats) and is_protected(".github/workflows/ci.yml", pats)
    assert not is_protected("roj/agents/loop.py", pats)


def test_serve_ticks_backs_up_and_health(home, inst, project, tmp_path):
    import tarfile
    import urllib.request

    from roj.panel.server import serve
    from roj.server import backup, serve_forever

    inst.settings.storage.backup_dir = str(tmp_path / "bucket")
    run(serve_forever(inst, panel=False, ticks=1, interval_s=0))
    assert (home / "serve.heartbeat").exists()
    [auto] = list((tmp_path / "bucket").glob("roj-backup-*.tar.gz"))  # the daily backup ran on the first tick
    names = tarfile.open(auto).getnames()
    assert "roj-home/roj.db" in names and "roj-home/projects/t/state.db" in names and "roj-home/roj.toml" in names
    inst.settings.storage.backup_keep = 1
    backup(inst)
    assert len(list((tmp_path / "bucket").glob("*.tar.gz"))) <= 2
    server, _, _ = serve(home, "127.0.0.1", 0)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/healthz", timeout=5).read().decode()
        assert body.startswith("ok 20")
    finally:
        server.shutdown()
        server.server_close()


def test_cli_lab_and_kb_flags(home, web, capsys):
    from roj.cli import main

    assert main(["--home", str(home), "project", "create", "m"]) == 0
    capsys.readouterr()
    assert main(["--home", str(home), "lab", "study", "-p", "m", "--models", "fake-smart,fake-smart-b", "--no-research",
                 "--knobs", "reasoning", "--per-kind", "3", "--budget-tokens", "2000000"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "waiting_approval"
    assert main(["--home", str(home), "lab", "playbook"]) == 0
    assert main(["--home", str(home), "kb", "create", "-p", "m", "ops", "--description", "Runbooks", "--audience", "devops,sre"]) == 0
    assert main(["--home", str(home), "backup"]) == 0
    assert list((home / "backups").glob("*.tar.gz"))


def test_concept_digest_reaches_the_planners_and_is_owner_only():
    from pathlib import Path

    from roj.config import SelfdevSettings
    from roj.lab.selfdev import concept_digest

    digest = concept_digest(Path(__file__).resolve().parents[1])
    assert "roj improves roj" in digest and "No baseline, no claim" in digest and len(digest) < 9000
    assert "digest:start" not in digest
    assert is_protected("docs/CONCEPT.md", SelfdevSettings().protected)


def test_selftest_passes_offline():
    import asyncio

    from roj.obs.selftest import run_selftest

    results, root = asyncio.run(run_selftest())
    assert [r.name for r in results if not r.ok] == [] and len(results) == 9
    assert not root.exists()  # the throwaway home is removed
