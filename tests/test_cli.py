import json

from roj.cli import main
from roj.project import Installation


def j(capsys, *argv):
    code = main(["--json", *argv])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def test_cli_end_to_end(home, capsys):
    h = ["--home", str(home)]
    assert j(capsys, *h, "project", "create", "demo", "--usd", "1.5")[1]["usd_per_day"] == 1.5
    code, models = j(capsys, *h, "models", "--refresh")
    assert code == 0 and any(m["id"] == "fake-ok" for m in models)

    code, r = j(capsys, *h, "ask", "-p", "demo", "-m", "fake-blocked", "-f", "fake-ok", "hello")
    assert code == 0 and r["model"] == "fake-ok" and r["fallback_index"] == 1
    code, r2 = j(capsys, *h, "ask", "-p", "demo", "-m", "fake-ok", "hello")
    assert r2["exact_cache_hit"] is True and r2["cost_usd"] == 0

    code, t = j(capsys, *h, "task", "run", "-p", "demo", "roj.examples.hello:WORKFLOW",
                "-i", "topic=roj", "-i", "model=fake-ok", "-i", "publish=true")
    assert code == 0 and t["status"] == "waiting_approval"
    code, pending = j(capsys, *h, "approvals", "list", "-p", "demo")
    assert len(pending) == 1
    j(capsys, *h, "approvals", "approve", "-p", "demo", pending[0]["id"], "--note", "ok")
    code, resumed = j(capsys, *h, "task", "resume", "-p", "demo")
    assert resumed[0]["status"] == "done"

    code, st = j(capsys, *h, "stats", "-p", "demo")
    assert st["calls"] >= 2 and st["exact_cache_hits"] >= 1 and st["fallbacks"] >= 1 and st["alerts"] >= 1
    code, b = j(capsys, *h, "budget", "allocate", "-p", "demo", "team:demo", "--usd", "0.5")
    assert any(r["scope"] == "team:demo" for r in b)
    code, v = j(capsys, *h, "log", "verify", "-p", "demo")
    assert code == 0 and v["ok"]
    code, v2 = j(capsys, *h, "log", "verify")
    assert code == 0 and v2["ok"]


def test_projects_are_isolated(home, capsys):
    inst = Installation(home)
    a = inst.create_project("a")
    b = inst.create_project("b")
    a.events.append("secret.note", {"x": 1})
    a.cache.put("k", "fake-ok", {"text": "only for a"})
    assert b.events.query(type="secret.note") == []
    assert b.cache.get("k") is None
    assert a.dir != b.dir and a.store.path != b.store.path
    a.close(), b.close(), inst.close()


def test_missing_home_is_a_clear_error(tmp_path, capsys):
    code = main(["--home", str(tmp_path / "nope"), "project", "list"])
    assert code == 1 and "roj init" in capsys.readouterr().err
