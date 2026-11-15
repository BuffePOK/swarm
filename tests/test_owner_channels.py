import asyncio
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from roj.core.kernel import TaskStatus
from roj.notify.outbox import flush, poll_once
from roj.panel.server import md_to_html, serve
from roj.skills.news_digest.workflows import BOOTSTRAP_REF, RUN_REF


def run(c):
    return asyncio.run(c)


def _bootstrap(project, url):
    tid = project.kernel.submit(BOOTSTRAP_REF, {"seeds": f"offline:{url}"})
    assert run(project.kernel.run(tid)).status == TaskStatus.WAITING_APPROVAL
    return tid


def _click(state, data, chat="42", uid=None):
    uid = uid or len(state.updates) + 1
    state.updates.append({"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data,
                                                               "message": {"message_id": 1, "chat": {"id": chat}}}})


def test_approval_via_telegram_button(project, web, inst):
    state, url = web
    tid = _bootstrap(project, url)
    assert run(flush(project))["sent"] == 1
    msg = state.telegram[-1]
    assert "Approval needed" in msg["text"] and msg["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve"
    approve = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    _click(state, approve.replace(":y", ":n"), chat="999")  # a stranger presses a forwarded button
    _click(state, approve)
    handled = run(poll_once(inst))
    assert len(handled) == 1 and handled[0]["decision"] == "Approved" and handled[0]["task_status"] == "done"
    assert project.kernel.get(tid)["status"] == "done"
    assert inst.events.query(type="telegram.ignored")
    assert any(m == "answerCallbackQuery" for m, _ in state.telegram_calls)
    assert run(poll_once(inst)) == []  # offset remembered: the click is not processed twice


def test_brief_rating_button(project, web, inst):
    state, url = web
    _bootstrap(project, url)
    for a in project.policy.pending():
        project.policy.decide(a["id"], True)
    run(project.kernel.resume())
    tid = project.kernel.submit(RUN_REF, {})
    assert run(project.kernel.run(tid)).status == TaskStatus.DONE
    kb = state.telegram[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["1", "2", "3", "4", "5"]
    _click(state, kb[3]["callback_data"])
    run(poll_once(inst))
    row = project.store.one("SELECT score, source FROM ratings WHERE task_id = ?", (tid,))
    assert row["score"] == 4 and row["source"] == "telegram"


def test_md_renderer_escapes():
    h = md_to_html("# T\n- <script>x</script> [a](https://e.example) **b**\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<script>" not in h and "&lt;script&gt;" in h and '<a href="https://e.example"' in h and "<table>" in h


def test_panel_auth_approval_and_reports(home, project, web):
    _, url = web
    tid = _bootstrap(project, url)
    server, panel, link = serve(home, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def get(path, cookie=None):
        req = urllib.request.Request(base + path, headers={"Cookie": cookie} if cookie else {})
        try:
            r = opener.open(req)
            return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def post(path, form, cookie):
        req = urllib.request.Request(base + path, data=urllib.parse.urlencode(form).encode(), headers={"Cookie": cookie})
        try:
            return opener.open(req).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert get("/")[0] == 401
        code, _, headers = get(f"/?token={panel.token}")
        assert code == 303 and "HttpOnly" in headers["Set-Cookie"]
        cookie = f"roj_panel={panel.token}"
        code, body, _ = get("/p/t", cookie)
        assert code == 200 and "t2.activate_skill" in body and "Approve" in body
        aid = project.policy.pending()[0]["id"]
        assert post(f"/p/t/approval/{aid}", {"decision": "approve"}, cookie) == 403  # no form token
        assert post(f"/p/t/approval/{aid}", {"decision": "approve", "token": panel.token}, cookie) == 303
        for _ in range(100):
            if project.kernel.get(tid)["status"] == "done":
                break
            time.sleep(0.05)
        assert project.kernel.get(tid)["status"] == "done"
        reports = sorted((project.dir / "reports").glob("research-*.md"))
        code, body, _ = get(f"/p/t/report/{reports[0].name}", cookie)
        assert code == 200 and "Topic rules learned" in body
        assert get("/p/t/report/..%2F..%2Froj.toml", cookie)[0] == 404
        assert get(f"/p/t/task/{tid}", cookie)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
