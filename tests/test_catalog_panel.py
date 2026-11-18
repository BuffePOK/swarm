import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request

from roj import mission as ms
from roj.config import ModelBinding
from roj.models.catalog import catalog_for, import_apigate_prices, is_chat, render, write_pricing
from roj.project import Installation


def run(c):
    return asyncio.run(c)


def test_only_architect_and_leader_are_needed_other_roles_stand_in(project):
    s = project.settings
    s.models = {"architect": ModelBinding(model="a"), "leader": ModelBinding(model="l"), "cheap": ModelBinding(model="c")}
    assert s.binding_for("planner").model == "a" and s.binding_for("coder").model == "a"
    assert s.binding_for("critic").model == "l" and s.binding_for("checker").model == "l"
    assert s.binding_for("summarizer").model == "c" and s.binding_for("examiner").model == "c"
    s.models = {"planner": ModelBinding(model="p"), "critic": ModelBinding(model="k")}  # older configs keep working
    assert s.binding_for("architect").model == "p" and s.binding_for("leader").model == "k" and s.binding_for("cheap").model == "p"


def test_catalog_keeps_chat_models_with_prices_and_cards(inst, tmp_path):
    run(inst.registry.refresh())
    assert is_chat("mistral/mistral-embed", {}) is False and is_chat("x/y", {"kind": "embedding"}) is False
    assert is_chat("mistral/voxtral-small-latest", {}) is False and is_chat("anthropic/claude-opus-5-5", {}) is True
    cat = catalog_for(inst, inst.settings.pricing)
    ids = [m["id"] for m in cat]
    assert "fake-smart" in ids and "fake-embed" not in ids
    smart = next(m for m in cat if m["id"] == "fake-smart")
    assert smart["price_known"] and smart["in"] == 3.0
    text = render(cat)
    assert "fake-smart | fake-smart | 3/15" in text and "no card yet" in text

    prices = ('[defaults]\nin = 5.0\nout = 30.0\n[[price]]\nprovider = "anthropic"\npattern = "*"\nin = 5.0\nout = 25.0\n'
              '[[price]]\nprovider = "anthropic"\npattern = "claude-haiku-5-5"\nin = 1.0\nout = 5.0\n')
    rows, default = import_apigate_prices(prices)
    assert rows["anthropic/claude-haiku-5-5"] == {"input": 1.0, "output": 5.0} and default == {"input": 5.0, "output": 30.0}
    write_pricing(inst.home, rows)
    again = Installation(inst.home)
    try:
        p = again.settings.pricing
        assert p.price_for("anthropic/claude-haiku-5-5")[0].input == 1.0  # exact row beats the provider glob
        assert p.price_for("anthropic/claude-opus-5-5")[0].output == 25.0  # provider-wide row
        assert p.price_for("fake-smart")[0].input == 3.0  # roj.toml entries still win / still present
    finally:
        again.close()


def test_architect_must_pick_real_models(home, tmp_path):
    from roj.core.kernel import StepContext

    inst = Installation(home)
    p = inst.create_project("lab")
    try:
        run(inst.registry.refresh())
        ctx = StepContext(p, None, None, "t", 0, 1, {}, {})
        member = dict(agent="w-reader", summary="reads things", instructions="Czytasz pliki projektu i opisujesz ich zawartość dokładnie.",
                      tools=["kb.search"], task="Opisz strukturę repozytorium w pakiecie project-map", acceptance=["Pakiet zawiera co najmniej 1 dokument"],
                      deliverable="Wpis w pakiecie")
        bad = ms.StagePlan(objective="Stage 1: describe the repository", acceptance=["1 document exists"],
                           team=[ms.Member(**member, model="no-such-model", fallbacks=["fake-embed"])])
        issues = ms.lint_stage(ctx, bad)
        assert any("no-such-model" in i for i in issues) and any("fake-embed" in i for i in issues)
        good = ms.StagePlan(objective="Stage 1: describe the repository", acceptance=["1 document exists"],
                            team=[ms.Member(**member, model="fake-smart", fallbacks=["fake-smart-b"])])
        assert ms.lint_stage(ctx, good) == []
    finally:
        p.close()
        inst.close()


def test_panel_runs_a_mission_end_to_end(home):
    from roj.panel.server import serve

    inst = Installation(home)
    inst.create_project("lab").close()
    inst.close()
    server, panel, url = serve(home, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(f"{base}/?token={panel.token}", timeout=10)

    def post(path, **form):
        data = urllib.parse.urlencode({"token": panel.token, **form}).encode()
        return opener.open(base + path, data=data, timeout=10)

    def mission_state():
        i = Installation(home)
        p = i.project("lab")
        try:
            return ms.load(p)
        finally:
            p.close()
            i.close()

    def wait(cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            m = mission_state()
            if cond(m):
                return m
            time.sleep(0.1)
        raise AssertionError(f"timed out; mission status {mission_state()['status']}")

    def pending():
        i = Installation(home)
        p = i.project("lab")
        try:
            return p.policy.pending()
        finally:
            p.close()
            i.close()

    try:
        r = post("/p/lab/mission", goal="Improve roj with roj, step by step", control="full")
        mid = r.geturl().rsplit("/", 1)[-1]
        m = wait(lambda m: m["status"] == "needs_answers")
        page = opener.open(f"{base}/p/lab/mission/{mid}", timeout=10).read().decode()
        assert "Questions" in page and "q_focus" in page and "Talk" in page
        post(f"/p/lab/mission/{mid}/answer", q_focus="token cost")
        assert mission_state()["status"] == "ready"
        post(f"/p/lab/mission/{mid}/next")
        end = time.time() + 20
        while not pending() and time.time() < end:
            time.sleep(0.1)
        page = opener.open(f"{base}/p/lab/mission/{mid}", timeout=10).read().decode()
        assert "Waiting for you: t2.mission_stage" in page and "fake-smart" in page
        for _ in range(3):  # stage, then the agent's own task (control full)
            for a in pending():
                if a["action"].startswith("t2.mission"):
                    post(f"/p/lab/approval/{a['id']}", decision="approve", back=f"/p/lab/mission/{mid}")
            time.sleep(0.5)
            if mission_state()["stages"]:
                break
        m = wait(lambda m: bool(m["stages"]))
        assert m["stages"][0]["plan"]["team"][0]["model"] == "fake-smart"
        page = opener.open(f"{base}/p/lab/mission/{mid}", timeout=10).read().decode()
        assert "Stages" in page and "report" in page and "w-code-analyst" in page
        post(f"/p/lab/mission/{mid}/chat", to="w-code-analyst", text="What did you find?")
        m = wait(lambda m: "w-code-analyst" in m["threads"])
        assert m["threads"]["w-code-analyst"][1]["from"] == "w-code-analyst"
    finally:
        server.shutdown()
        server.server_close()


def test_panel_reruns_an_unfinished_intake(home):
    """A mission whose intake failed stays 'new': the page offers the intake again, not a stage plan."""
    from roj import mission as ms
    from roj.panel.server import serve

    inst = Installation(home)
    p = inst.create_project("lab")
    mid = ms.create(p, "Improve roj with roj, step by step", "leads")
    p.close()
    inst.close()
    server, panel, url = serve(home, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(f"{base}/?token={panel.token}", timeout=10)

    def status():
        i = Installation(home)
        q = i.project("lab")
        try:
            return ms.load(q, mid)["status"]
        finally:
            q.close()
            i.close()

    try:
        page = opener.open(f"{base}/p/lab/mission/{mid}", timeout=10).read().decode()
        assert "Run the intake again" in page and "Plan the next stage" not in page
        opener.open(f"{base}/p/lab/mission/{mid}/intake", data=urllib.parse.urlencode({"token": panel.token}).encode(), timeout=10)
        end = time.time() + 20
        while status() != "needs_answers" and time.time() < end:
            time.sleep(0.1)
        assert status() == "needs_answers"
    finally:
        server.shutdown()
        server.server_close()
