"""`roj selftest`: the whole chain, offline, in a throwaway home. Zero tokens, zero network.

It answers one question: does this installation (image, Python, git, pytest, SQLite, file
permissions) work, independently of the real gateway and models? It starts the stand-in
gateway and web from roj.testing on random localhost ports, builds a temporary home and a
temporary git repository, and runs: mission (questions, answers, stage, approval, team,
review, change request), chat, selfdev from that change request up to a pull request on a
fake GitHub, a small Lab study up to an active Playbook, Telegram delivery, backup, log
export and the event-log chain check. The real ROJ_HOME is never touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import secrets as _secrets

ENV = {"ROJ_SELFTEST_KEY": "sk-st-" + _secrets.token_hex(12), "ROJ_SELFTEST_TG": "123:" + _secrets.token_hex(12),
       "ROJ_SELFTEST_GH": "ghp_" + _secrets.token_hex(12)}  # distinctive values, so the leak check cannot match ordinary text

TOML = """\
timezone = "Europe/Warsaw"
[gateway]
base_url = "{gw}"
api_key_env = "ROJ_SELFTEST_KEY"
timeout_s = 10
[budget]
tokens_per_day = 5000000
usd_per_day = 100.0
default_max_tokens = 512
[pricing.models."fake-*"]
input = 0.5
output = 1.0
[web]
per_host_interval_s = 0
timeout_s = 5
[notify]
telegram_chat_id = "42"
telegram_token_env = "ROJ_SELFTEST_TG"
telegram_api = "{web}"
[storage]
backup_dir = "{backup}"
[selfdev]
repo = "{repo}"
github_repo = "selftest/roj"
github_api = "{web}"
github_token_env = "ROJ_SELFTEST_GH"
test_timeout_s = 120
[retry]
base_delay_s = 0.001
max_delay_s = 0.01
rate_limit_max_wait_s = 0
[models.architect]
model = "fake-smart"
[models.planner]
model = "fake-smart"
[models.critic]
model = "fake-smart-b"
[models.analyst]
model = "fake-smart"
[models.coder]
model = "fake-smart"
[models.checker]
model = "fake-smart-b"
[models.classifier]
model = "fake-smart-b"
"""


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float


def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, timeout=60)


def _toy_repo(root: Path) -> Path:
    remote, repo = root / "remote.git", root / "repo"
    _git(root, "init", "-q", "--bare", "-b", "main", str(remote))
    (repo / "toy").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "toy" / "__init__.py").write_text('"""Toy package for roj selftest."""\n', encoding="utf-8")
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=selftest", "-c", "user.email=selftest@localhost", "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    return repo


async def _approve_all(project, rounds: int = 6) -> list[Any]:
    out: list[Any] = []
    for _ in range(rounds):
        pending = project.policy.pending()
        if not pending:
            break
        for a in pending:
            project.policy.decide(a["id"], True, note="selftest")
        out += await project.kernel.resume()
    return out


async def run_selftest(keep: bool = False) -> tuple[list[Result], Path]:
    from .. import mission as ms
    from ..knowledge.packs import KnowledgeBase
    from ..lab.playbook import load_active
    from ..lab.study import STUDY_REF
    from ..notify.outbox import flush_all
    from ..notify.telegram import send_telegram
    from ..project import Installation
    from ..server import backup
    from ..testing.fake_gateway import start_fake_gateway
    from ..testing.fake_web import start_fake_web
    from ..util import remove_tree
    from .export import export_bundle

    root = Path(tempfile.mkdtemp(prefix="roj-selftest-"))
    saved = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    gw_server, _, gw = start_fake_gateway()
    web_server, web_state, web = start_fake_web()
    results: list[Result] = []
    state: dict[str, Any] = {}
    inst = project = None

    async def check(name: str, fn: Callable[[], Any]) -> None:
        t0 = time.monotonic()
        try:
            detail = await fn()
            results.append(Result(name, True, str(detail or ""), time.monotonic() - t0))
        except Exception as e:  # noqa: BLE001 - every failure is reported, the rest still runs
            results.append(Result(name, False, f"{type(e).__name__}: {e}"[:400], time.monotonic() - t0))

    try:
        async def setup() -> str:
            nonlocal inst, project
            for tool in ("git",):
                if not shutil.which(tool):
                    raise RuntimeError(f"{tool} is not installed")
            repo = _toy_repo(root)
            home = root / "home"
            Installation.init(home)
            (home / "roj.toml").write_text(TOML.format(gw=gw, web=web, backup=(root / "bucket").as_posix(), repo=repo.as_posix()),
                                           encoding="utf-8")
            inst = Installation(home)
            project = inst.create_project("lab")
            n = len(await inst.client.list_models())
            return f"temporary home {home}, stand-in gateway with {n} models"
        await check("setup: home, project, git repo, gateway", setup)
        if project is None:
            return results, root

        async def intake() -> str:
            mid = ms.create(project, "Improve roj with roj, step by step", "leads")
            res = await project.kernel.run(project.kernel.submit(ms.INTAKE_REF, {"mission": mid}))
            assert res.status.value == "done", res.error
            m = ms.load(project, mid)
            assert ms.open_questions(m), "the Architect asked no questions"
            state["mid"] = mid
            return f"{len(ms.open_questions(m))} questions from the Architect"
        await check("mission: goal -> Architect's questions", intake)

        async def stage() -> str:
            ms.answer(project, state["mid"], {"focus": "documentation"})
            res = await project.kernel.run(project.kernel.submit(ms.STAGE_REF, {"mission": state["mid"]}))
            assert res.status.value == "waiting_approval", f"{res.status.value}: {res.error}"
            assert [a["action"] for a in project.policy.pending()] == ["t2.mission_stage"]
            assert KnowledgeBase(project).packs() == [], "work started before the owner approved"
            state["stage_task"] = res.task_id
            return f"plan ready after {res.results['plan']['attempts']} attempt(s), waiting for the owner"
        await check("mission: stage plan, lint, critique, owner gate", stage)

        async def stage_run() -> str:
            for a in project.policy.pending():
                project.policy.decide(a["id"], True, note="selftest")
            res = await project.kernel.run(state["stage_task"])
            assert res.status.value == "done", f"{res.status.value}: {res.error}"
            close = res.results["close"]
            assert Path(close["report"]).exists() and [p["pack"] for p in KnowledgeBase(project).packs()] == ["project-map"]
            assert close["change_requests"], "no change request was raised"
            state["selfdev_task"] = close["change_requests"][0]["task"]
            return f"stage done, knowledge pack written, report {Path(close['report']).name}, 1 change request -> selfdev"
        await check("mission: team, Leader review, report", stage_run)

        async def chat() -> str:
            res = await project.kernel.run(project.kernel.submit(ms.CHAT_REF, {"mission": state["mid"], "to": "leader",
                                                                              "text": "Always prefer the cheapest model."}))
            assert res.status.value == "done", res.error
            assert ms.load(project, state["mid"])["directives"], "the directive was not recorded"
            return "reply received, directive recorded"
        await check("chat: owner -> Leader", chat)

        async def selfdev() -> str:
            res = await project.kernel.run(state["selfdev_task"])
            assert res.status.value == "waiting_approval", f"{res.status.value}: {res.error}"
            await _approve_all(project)
            row = project.store.one("SELECT status, error FROM tasks WHERE id = ?", (state["selfdev_task"],))
            assert row["status"] == "done", f"{row['status']}: {row['error']}"
            out = json.loads(project.cas.get_text(project.store.one(
                "SELECT result_ref FROM steps WHERE task_id = ? AND name = 'publish'", (state["selfdev_task"],))["result_ref"]))
            assert web_state.pulls, f"no pull request reached the fake GitHub: {out.get('outcome')}"
            return f"worktree build, gates green, review approve, pull request opened ({out.get('pull_request')})"
        await check("selfdev: change request -> gates -> pull request", selfdev)

        async def lab() -> str:
            res = await project.kernel.run(project.kernel.submit(STUDY_REF, {"models": "fake-smart,fake-smart-b", "per_kind": 1,
                                                                            "knobs": "reasoning,structure", "research": False,
                                                                            "budget_tokens": 3_000_000}))
            assert res.status.value == "waiting_approval", f"{res.status.value}: {res.error}"
            await _approve_all(project)
            pb = load_active(inst.home)
            assert pb.version == 1, "the Playbook was not activated"
            return f"study report written, Playbook v{pb.version} active, profiles for {sorted(pb.profiles) or 'no family'}"
        await check("lab: model cards, A/B, Playbook", lab)

        async def telegram() -> str:
            await send_telegram(project.settings.notify, "roj selftest")
            await flush_all(inst)
            assert web_state.telegram, "nothing reached the fake Telegram"
            return f"{len(web_state.telegram)} message(s) delivered"
        await check("telegram: delivery", telegram)

        async def ops() -> str:
            b = backup(inst)
            z = export_bundle(inst, days=1)
            names = zipfile.ZipFile(z).namelist()
            assert any(n.endswith(".log") for n in names), "no task logs in the export"
            blob = b"".join(zipfile.ZipFile(z).read(n) for n in names)
            leaked = [k for k, v in ENV.items() if v.encode() in blob]
            assert not leaked, f"secrets in the export: {leaked}"
            v = project.events.verify()
            assert v.ok, f"event chain broken at {v.first_bad_seq}"
            return f"backup {b.name}, export with {len(names)} files and no secrets, event chain ok ({v.count} events)"
        await check("ops: backup, export, event log", ops)
    finally:
        for s in (gw_server, web_server):
            s.shutdown()
            s.server_close()
        if project is not None:
            project.close()
        if inst is not None:
            await inst.client.aclose()
            inst.close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not keep:
            remove_tree(root)
    return results, root


def render(results: list[Result]) -> str:
    ok = sum(r.ok for r in results)
    lines = [f"roj selftest: {ok}/{len(results)} passed (offline, stand-in models, no tokens)"]
    for r in results:
        lines.append(f"[{'PASS' if r.ok else 'FAIL'}] {r.name:<50} {r.seconds:5.1f}s  {r.detail}")
    return "\n".join(lines) + "\n"


def main(keep: bool = False) -> int:
    results, root = asyncio.run(run_selftest(keep=keep))
    print(render(results), end="")
    if keep:
        print(f"kept: {root}")
    return 0 if results and all(r.ok for r in results) else 1
