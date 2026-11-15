import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from roj.cli import main
from roj.core.kernel import TaskStatus
from roj.hands.market import Quote
from roj.scheduler import due, tick
from roj.skills.news_digest import engine
from roj.skills.news_digest.spec import DigestSpec
from roj.skills.news_digest.workflows import BOOTSTRAP_REF, REPAIR_REF, RUN_REF
from roj.skills.registry import Pool, SpecRegistry
from roj.stats import llm_stats


def run(coro):
    return asyncio.run(coro)


def bootstrap(project, web_url):
    tid = project.kernel.submit(BOOTSTRAP_REF, {"seeds": f"offline:{web_url}"})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.WAITING_APPROVAL, res.error
    return tid, res


def approve_all(project):
    for a in project.policy.pending():
        project.policy.decide(a["id"], True)
    return run(project.kernel.resume())


def reset_window(project):
    project.store.run("UPDATE skill_state SET last_window_end = NULL")


def test_bootstrap_researches_verifies_and_waits_for_owner(project, web, inst):
    _, url = web
    tid, res = bootstrap(project, url)
    probe = {r["source_id"]: r for r in res.results["probe"]["sources"]}
    assert probe["private-letter"]["status"] == "robots_disallowed"
    assert probe["broken-feed"]["status"] == "parse_error"
    assert probe["stale-feed"]["status"] == "stale"
    assert probe["pacifica-post"]["status"] == "ok" and probe["pacifica-post"]["feed_url"].endswith("/asia/feed.xml")
    assert probe["energy-desk"]["status"] == "needs_adapter"
    # discovery: hallucinated candidates are rejected by the probe, a real one closes the gap
    scout = res.results["scout"]
    assert scout["rejected"] == {"x-imaginary-times": "unreachable", "x-ghost-gazette": "unreachable"}
    assert scout["accepted"] == ["x-andes-business-journal"] and scout["gaps_closed"] == ["Latin America"]
    assert res.results["finalize"]["revised"] is True  # critique asked for a revision
    assert "energy-desk" in res.results["adapters"]["built"]
    # rule induction: the draft rules were weak, the rule-smith fixed them, code measured it
    rules = res.results["rules"]
    assert rules["rounds"][0]["score"] < 0.5 and rules["final_visible"]["score"] >= rules["target"]
    # hidden cases were never shown to the rule-smith: they measure generalisation, not memorisation
    assert rules["hidden_cases"] > 0 and rules["final_hidden"] is not None
    assert rules["final_all"]["score"] > rules["rounds"][0]["score"]
    assert res.results["assess"]["labelled"] >= 20
    spec = res.results["compile"]["spec"]
    assert {s["id"] for s in spec["sources"]} == {"nordland-cb", "global-wire", "market-daily", "pacifica-post",
                                                    "energy-desk", "x-andes-business-journal"}
    assert res.results["compile"]["validation"]["sources_with_items"] == 6
    report = Path(res.results["compile"]["research_report"]).read_text()
    assert "Topic rules learned from teacher labels" in report and "Andes Business Journal" in report
    assert SpecRegistry(project).active("news_digest") is None  # nothing active before the owner says yes
    [pending] = project.policy.pending()
    assert pending["action"] == "t2.activate_skill" and pending["details"]["adapters_to_install"]
    [done] = approve_all(project)
    assert done.status == TaskStatus.DONE and done.results["register"]["version"] == 1
    assert SpecRegistry(project).active("news_digest")[0] == 1
    assert [a["status"] for a in Pool(inst).list("adapter")] == ["candidate"]


def test_daily_run_is_cheap_verified_and_delivered(project, web):
    state, url = web
    btid, _ = bootstrap(project, url)
    approve_all(project)
    tid = project.kernel.submit(RUN_REF, {})
    res = run(project.kernel.run(tid))
    assert res.status == TaskStatus.DONE, res.error
    out = res.results["publish"]
    report = Path(out["report_path"]).read_text()
    assert "Central Bank of Nordland holds policy rate at 4.25%" in report
    assert "7.7%" not in report  # invented number removed by the numbers gate
    assert "rally to continue" not in report  # unverifiable claim dropped by the checker
    assert "| EUR/USD | 1.1215 | -0.04% |" in report  # market numbers come from data
    assert any("numbers not found in sources" in n for n in res.results["gate"]["report"]["notes"])
    assert out["telegram"]["sent"] and state.telegram[-1]["chat_id"] == "42"
    assert res.results["classify"]["skipped"] is True  # rules classified everything: 0 tokens
    boot, daily = llm_stats(project.events, task_id=btid), llm_stats(project.events, task_id=tid)
    assert daily["calls"] == 2 and boot["calls"] >= 7
    assert daily["prompt_tokens"] + daily["completion_tokens"] < boot["prompt_tokens"] + boot["completion_tokens"]
    assert project.events.verify().ok


def test_broken_sources_are_repaired(project, web):
    state, url = web
    bootstrap(project, url)
    approve_all(project)
    old = DigestSpec.model_validate(SpecRegistry(project).get("news_digest", 1))
    state.moved_wire = True   # feed moved: /wire/atom.xml -> /wire/v2/atom.xml
    state.energy_v2 = True    # page redesign: the old adapter finds nothing
    for _ in range(2):
        reset_window(project)
        r = run(project.kernel.run(project.kernel.submit(RUN_REF, {"send": False})))
        assert r.status == TaskStatus.DONE  # the brief still ships from the healthy sources
    repair_task = r.results["collect"]["repair_task"]
    assert repair_task and project.kernel.get(repair_task)["workflow"] == REPAIR_REF
    assert any(e.payload.get("kind") == "source_broken" for e in project.events.query(type="alert"))
    [rep] = run(project.kernel.resume(repair_task))
    assert rep.status == TaskStatus.WAITING_APPROVAL
    fixes = rep.results["fix"]["fixes"]
    assert fixes["global-wire"]["how"].startswith("feed rediscovered")  # deterministic, no model
    assert fixes["energy-desk"]["how"].startswith("adapter rewritten")
    approve_all(project)
    new = DigestSpec.model_validate(SpecRegistry(project).active("news_digest")[1])
    assert new.source("global-wire").url.endswith("/wire/v2/atom.xml")
    assert new.source("energy-desk").adapter_sha != old.source("energy-desk").adapter_sha
    reset_window(project)
    r3 = run(project.kernel.run(project.kernel.submit(RUN_REF, {"send": False})))
    assert r3.status == TaskStatus.DONE and all(s["ok"] for s in r3.results["collect"]["sources"])


def test_numbers_gate_units():
    q = Quote(instrument_id="eurusd", label="EUR/USD", value=1.1215, previous=1.1219, change_pct=-0.0357, provider="x", decimals=4)
    allowed = engine.allowed_numbers(["Brent rose 3% to 84.10 dollars", "supply cut of 400,000 barrels"], [q])
    assert engine.unsupported_numbers("Brent rose 3% to 84.10 and 400,000 barrels", allowed) == []
    assert engine.unsupported_numbers("fuel costs to rise 7.7%", allowed) == ["7.7"]
    assert engine.unsupported_numbers("in 2026 two banks, EUR/USD at 1.1215 (-0.04%)", allowed) == []
    text, dropped = engine.drop_unsupported_sentences("Brent rose 3%. Costs will rise 7.7% next month.", allowed)
    assert text == "Brent rose 3%." and len(dropped) == 1


def test_clustering_merges_same_story_not_same_country():
    from roj.skills.news_digest.engine import Item, cluster

    def it(i, t, s="", src="a"):
        return Item(i, src, src, "secondary", t, f"https://x/{i}", None, s, src)

    events = cluster([
        it("1", "Atlantica inflation slows to 2.9% in October", src="wire"),
        it("2", "Atlantica inflation cools to 2.9%, boosting rate-cut bets", src="md"),
        it("3", "Atlantica and Pacifica to resume tariff talks on steel", src="wire"),
    ], 0.34)
    sizes = sorted(len(e.items) for e in events)
    assert sizes == [1, 2]


def test_scheduler_once_per_day_with_grace(project, web, inst):
    _, url = web
    bootstrap(project, url)
    approve_all(project)
    with open(project.dir / "project.toml", "a", encoding="utf-8") as f:
        f.write('\n[[schedules]]\nskill = "news_digest"\nat = "08:00"\nlead_minutes = 20\ngrace_hours = 4\n')
    p = inst.project("t")
    tz = ZoneInfo("Europe/Warsaw")
    day = datetime.now(tz).date()
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=tz)  # noqa: E731
    assert due(p, at(7, 30)) == []
    assert [(s.skill, late) for s, _, late in due(p, at(7, 45))] == [("news_digest", False)]
    assert [(s.skill, late) for s, _, late in due(p, at(9, 0))] == [("news_digest", True)]
    assert due(p, at(12, 30)) == []  # beyond the grace window
    p.close()
    report = run(tick(inst, at(8, 30)))
    runs = [r for r in report if r.get("skill") == "news_digest"]
    assert runs and runs[0]["status"] == "done" and runs[0]["late"] is True
    assert [r for r in run(tick(inst, at(8, 45))) if r.get("skill")] == []  # once per day


def test_cli_bootstrap_approve_run(home, web, capsys):
    _, url = web
    h = ["--home", str(home)]

    def j(*argv):
        code = main(["--json", *h, *argv])
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    j("project", "create", "brief")
    code, b = j("skill", "bootstrap", "-p", "brief", "news_digest", "--seeds", f"offline:{url}")
    assert code == 0 and b["status"] == "waiting_approval" and len(b["sources"]) == 6
    _, pending = j("approvals", "list", "-p", "brief")
    j("approvals", "approve", "-p", "brief", pending[0]["id"])
    j("task", "resume", "-p", "brief")
    code, r = j("skill", "run", "-p", "brief", "news_digest", "--no-send")
    assert code == 0 and r["status"] == "done" and r["telegram"]["reason"] == "send=false"
    _, st = j("skill", "status", "-p", "brief", "news_digest")
    assert st["active_version"] == 1 and st["last_run"]["tokens"] > 0
    code, sch = j("schedule", "add", "-p", "brief", "news_digest", "--at", "08:00")
    assert code == 0
    _, lst = j("schedule", "list", "-p", "brief")
    assert lst[0]["at"] == "08:00" and lst[0]["lead_minutes"] == 20
    code = main(["--home", str(home), "scheduler", "windows-task"])
    assert "schtasks /Create" in capsys.readouterr().out


def test_task_logs_and_export_bundle(project, web, inst):
    import zipfile

    from roj.obs import runlog
    from roj.obs.export import export_bundle

    _, url = web
    tid, _ = bootstrap(project, url)
    path = runlog.task_log_path(project, tid)
    text = path.read_text()
    assert "MODEL USAGE" in text and "news-planner" in text and "disallowed by robots.txt" in text
    assert path.with_suffix(".jsonl").exists()
    bundle = export_bundle(inst, days=1)
    names = zipfile.ZipFile(bundle).namelist()
    assert any(n.startswith("logs/t/") and n.endswith(".log") for n in names)
    assert "meta/settings.json" in names and "projects/t/health.json" in names
    assert any(n.startswith("prompts/t/") and n.endswith("request.json") for n in names)
    blob = b"".join(zipfile.ZipFile(bundle).read(n) for n in names)
    assert b"123:TEST" not in blob and b"test-key" not in blob  # Telegram token and FRED key never exported


def test_doctor_offline(inst):
    from roj.obs.doctor import render, run_doctor

    checks = run(run_doctor(inst))
    text = render(checks)
    assert "[PASS] gateway" in text and "answers:" in text
    assert any(c.area == "models" and c.status == "WARN" and "no fallback" in c.detail for c in checks)
    assert not any(c.status == "FAIL" for c in checks), text
