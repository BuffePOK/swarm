"""roj command line.

  roj init                                   create a home (ROJ_HOME or ~/.roj)
  roj project create NAME [--tokens --usd --timezone]
  roj project list
  roj models [--refresh] [--unblock MODEL]
  roj ask -p P -m MODEL [-f FALLBACK ...] "prompt"
  roj task run -p P module:WORKFLOW [-i key=value ...]
  roj task resume -p P [TASK_ID] [--include-paused]
  roj task list|show -p P [TASK_ID]
  roj approvals list|approve|reject -p P [ID] [--note]
  roj budget show|allocate -p P [SCOPE --tokens N --usd X]
  roj log verify|tail -p P [-n N]
  roj stats -p P [--task TASK_ID]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import ModelBinding, resolve_home
from .core.budget import BudgetExceeded
from .models.errors import AllModelsFailed, ModelError
from .project import Installation
from .stats import llm_stats


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(obj, list):
        if not obj:
            print("(none)")
            return
        if isinstance(obj[0], dict):
            keys = list(obj[0].keys())
            widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in obj)) for k in keys}
            widths = {k: min(w, 60) for k, w in widths.items()}
            print("  ".join(k.ljust(widths[k]) for k in keys))
            for r in obj:
                print("  ".join(str(r.get(k, ""))[:60].ljust(widths[k]) for k in keys))
            return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False, default=str)
            print(f"{k}: {v}")
        return
    print(obj)


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--input expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="roj", description="roj multi-agent kernel")
    ap.add_argument("--home", help="roj home directory (default: $ROJ_HOME or ~/.roj)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=f"roj {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create a roj home")

    pr = sub.add_parser("project", help="manage projects").add_subparsers(dest="action", required=True)
    pc = pr.add_parser("create")
    pc.add_argument("name")
    pc.add_argument("--tokens", type=int)
    pc.add_argument("--usd", type=float)
    pc.add_argument("--timezone")
    pr.add_parser("list")

    cr = sub.add_parser("crawl", help="index a site section into roj's own search index (web.search source 'index')")
    cr.add_argument("-p", "--project", required=True)
    cr.add_argument("url")
    cr.add_argument("--max-pages", type=int, default=100)
    cr.add_argument("--depth", type=int, default=3)
    cr.add_argument("--prefix", help="path prefix to stay under (default: the URL's directory)")
    cr.add_argument("--render", choices=["auto", "always", "never"], default="auto")

    se = sub.add_parser("search", help="try web.search / papers.search from the command line")
    se.add_argument("-p", "--project", required=True)
    se.add_argument("query")
    se.add_argument("--sources", default="", help="comma-separated: index,hn,github,wikipedia,stackexchange,web,papers")
    se.add_argument("-n", type=int, default=5)

    br = sub.add_parser("browser", help="headless Chromium: check it, or open a profile so you can log in").add_subparsers(dest="action", required=True)
    br.add_parser("check", help="start Chromium and render a test page (no network)")
    bro = br.add_parser("open", help="open a persistent profile with DevTools on 127.0.0.1:<port> (attach through an SSH tunnel)")
    bro.add_argument("--profile", default="research")
    bro.add_argument("--port", type=int, default=9222)
    bro.add_argument("--minutes", type=float, default=15)
    bro.add_argument("url", nargs="?")
    brf = br.add_parser("fetch", help="read one page the way agents do (clean text, rendered if needed)")
    brf.add_argument("-p", "--project", required=True)
    brf.add_argument("url")
    brf.add_argument("--render", choices=["auto", "always", "never"], default="auto")

    pz = sub.add_parser("pricing", help="model prices used by budgets").add_subparsers(dest="action", required=True)
    pzi = pz.add_parser("import", help="import the gateway's price list (apigate presets/prices.toml format)")
    pzi.add_argument("file")
    pz.add_parser("show", help="chat models on the gateway with the price roj budgets them at")

    m = sub.add_parser("models", help="list models known to the gateway")
    m.add_argument("--refresh", action="store_true")
    m.add_argument("--unblock")
    m.add_argument("--quirks", action="store_true", help="request fixes learned from providers' 400 errors, per model")
    m.add_argument("--forget-quirks", nargs="?", const="*", metavar="MODEL", help="forget learned fixes (all, or one model)")

    a = sub.add_parser("ask", help="one model call through the full stack (budget, governor, log)")
    a.add_argument("-p", "--project", required=True)
    a.add_argument("-m", "--model", required=True)
    a.add_argument("-f", "--fallback", action="append", default=[])
    a.add_argument("--system", default="")
    a.add_argument("--temperature", type=float, default=0.0)
    a.add_argument("--max-tokens", type=int)
    a.add_argument("prompt")

    t = sub.add_parser("task", help="run and inspect workflows").add_subparsers(dest="action", required=True)
    tr = t.add_parser("run")
    tr.add_argument("-p", "--project", required=True)
    tr.add_argument("workflow", help="module:WORKFLOW")
    tr.add_argument("-i", "--input", action="append", default=[])
    tres = t.add_parser("resume")
    tres.add_argument("-p", "--project", required=True)
    tres.add_argument("task_id", nargs="?")
    tres.add_argument("--include-paused", action="store_true")
    tl = t.add_parser("list")
    tl.add_argument("-p", "--project", required=True)
    tl.add_argument("--status")
    ts = t.add_parser("show")
    ts.add_argument("-p", "--project", required=True)
    ts.add_argument("task_id")
    tc = t.add_parser("cancel")
    tc.add_argument("-p", "--project", required=True)
    tc.add_argument("task_id")

    ap_ = sub.add_parser("approvals", help="owner approvals").add_subparsers(dest="action", required=True)
    al = ap_.add_parser("list")
    al.add_argument("-p", "--project", required=True)
    for name in ("approve", "reject"):
        x = ap_.add_parser(name)
        x.add_argument("-p", "--project", required=True)
        x.add_argument("id")
        x.add_argument("--note")

    b = sub.add_parser("budget", help="budgets").add_subparsers(dest="action", required=True)
    bs = b.add_parser("show")
    bs.add_argument("-p", "--project", required=True)
    ba = b.add_parser("allocate")
    ba.add_argument("-p", "--project", required=True)
    ba.add_argument("scope", help="e.g. team:analysis or team:analysis/agent:writer")
    ba.add_argument("--tokens", type=int)
    ba.add_argument("--usd", type=float)

    lg = sub.add_parser("log", help="event log").add_subparsers(dest="action", required=True)
    lv = lg.add_parser("verify")
    lv.add_argument("-p", "--project")
    lt = lg.add_parser("tail")
    lt.add_argument("-p", "--project", required=True)
    lt.add_argument("-n", type=int, default=20)
    lt.add_argument("--type")

    st = sub.add_parser("stats", help="token, cost and cache statistics")
    st.add_argument("-p", "--project", required=True)
    st.add_argument("--task")

    sk = sub.add_parser("skill", help="skills: bootstrap once, run cheaply, repair").add_subparsers(dest="action", required=True)
    sb = sk.add_parser("bootstrap", help="research and compile the skill (expensive, once)")
    sb.add_argument("-p", "--project", required=True)
    sb.add_argument("skill")
    sb.add_argument("--seeds", default="default", help="default | offline:<fake web url> | path to YAML")
    sb.add_argument("--model", help="use this model for every role (testing)")
    sr = sk.add_parser("run", help="run the active spec now")
    sr.add_argument("-p", "--project", required=True)
    sr.add_argument("skill")
    sr.add_argument("--no-send", action="store_true", help="write the report, do not send it")
    sr.add_argument("--deliver-at", help="HH:MM: wait until this local time before sending")
    sr.add_argument("--model", help="use this model for every role (testing)")
    sp = sk.add_parser("repair", help="re-probe and fix sources")
    sp.add_argument("-p", "--project", required=True)
    sp.add_argument("skill")
    sp.add_argument("--source", action="append", required=True)
    ss = sk.add_parser("status")
    ss.add_argument("-p", "--project", required=True)
    ss.add_argument("skill")
    sl = sk.add_parser("list")
    sl.add_argument("-p", "--project")

    sc = sub.add_parser("schedule", help="daily schedules of a project").add_subparsers(dest="action", required=True)
    sa = sc.add_parser("add")
    sa.add_argument("-p", "--project", required=True)
    sa.add_argument("skill")
    sa.add_argument("--at", default="08:00")
    sa.add_argument("--lead", type=int, default=20, help="minutes to start before delivery")
    sa.add_argument("--grace", type=float, default=4.0, help="hours after 'at' when a missed run still happens")
    sls = sc.add_parser("list")
    sls.add_argument("-p", "--project", required=True)

    sch = sub.add_parser("scheduler", help="drive schedules of all projects").add_subparsers(dest="action", required=True)
    sch.add_parser("tick", help="one pass: resume interrupted work, start due runs")
    srun = sch.add_parser("run", help="loop forever (servers)")
    srun.add_argument("--interval", type=float, default=60.0)
    sw = sch.add_parser("windows-task", help="print Windows Task Scheduler commands")
    sw.add_argument("--start", default="07:40")

    pl = sub.add_parser("pool", help="shared pool of generated algorithms").add_subparsers(dest="action", required=True)
    pl.add_parser("list")

    lo = sub.add_parser("logs", help="readable task logs and diagnostic bundles").add_subparsers(dest="action", required=True)
    los = lo.add_parser("show", help="print a task log (latest if no id)")
    los.add_argument("-p", "--project", required=True)
    los.add_argument("task_id", nargs="?")
    lol = lo.add_parser("list")
    lol.add_argument("-p", "--project", required=True)
    loe = lo.add_parser("export", help="zip logs, settings, health and prompts for review")
    loe.add_argument("-p", "--project", action="append")
    loe.add_argument("--days", type=int, default=3)
    loe.add_argument("--no-prompts", action="store_true")

    pa = sub.add_parser("panel", help="local web panel (approvals, tasks, logs, reports)")
    pa.add_argument("--host", default="127.0.0.1")
    pa.add_argument("--port", type=int, default=8765)
    pa.add_argument("--allow-remote", action="store_true")

    tg = sub.add_parser("telegram", help="owner notifications and buttons").add_subparsers(dest="action", required=True)
    tgp = tg.add_parser("poll", help="process button clicks and deliver queued notifications")
    tgp.add_argument("--forever", action="store_true")
    tg.add_parser("test", help="send a test message")

    ra = sub.add_parser("rate", help="rate a brief 1-5")
    ra.add_argument("-p", "--project", required=True)
    ra.add_argument("task_id")
    ra.add_argument("score", type=int)
    ra.add_argument("--comment", default="")

    kb = sub.add_parser("kb", help="knowledge packs (RAG)").add_subparsers(dest="action", required=True)
    kbc = kb.add_parser("create")
    kbc.add_argument("-p", "--project", required=True)
    kbc.add_argument("pack")
    kbc.add_argument("--description", required=True)
    kbc.add_argument("--embed-model")
    kbc.add_argument("--audience", help="comma-separated roles that share this pack (default: every role)")
    kbc.add_argument("--level", default="project", choices=["global", "project", "team", "task", "personal"])
    kba = kb.add_parser("add")
    kba.add_argument("-p", "--project", required=True)
    kba.add_argument("pack")
    kba.add_argument("files", nargs="+")
    kba.add_argument("--contextualize", action="store_true", help="a cheap model summarises each document once")
    kba.add_argument("--novelty", action="store_true", help="closed-book exam first: skip documents the model already knows")
    kbs = kb.add_parser("search")
    kbs.add_argument("-p", "--project", required=True)
    kbs.add_argument("pack")
    kbs.add_argument("query")
    kbs.add_argument("-k", type=int, default=5)
    kbs.add_argument("--rewrite", action="store_true")
    kbs.add_argument("--rerank", action="store_true")
    kbq = kb.add_parser("question", help="add a test question with the doc ids that must be found")
    kbq.add_argument("-p", "--project", required=True)
    kbq.add_argument("pack")
    kbq.add_argument("question")
    kbq.add_argument("--expect", action="append", required=True)
    kbe = kb.add_parser("eval")
    kbe.add_argument("-p", "--project", required=True)
    kbe.add_argument("pack")
    kbe.add_argument("-k", type=int, default=5)
    kbl = kb.add_parser("list")
    kbl.add_argument("-p", "--project", required=True)

    ga = sub.add_parser("gates", help="quality gates (ratchet)").add_subparsers(dest="action", required=True)
    gas = ga.add_parser("show")
    gas.add_argument("-p", "--project", required=True)
    gal = ga.add_parser("lower", help="owner only: lower a threshold, with a note")
    gal.add_argument("-p", "--project", required=True)
    gal.add_argument("suite")
    gal.add_argument("metric")
    gal.add_argument("threshold", type=float)
    gal.add_argument("--note", required=True)

    bpp = sub.add_parser("blueprint", help="the project's system as data").add_subparsers(dest="action", required=True)
    bl = bpp.add_parser("lint")
    bl.add_argument("-p", "--project")
    bl.add_argument("file")
    bpr = bpp.add_parser("propose")
    bpr.add_argument("-p", "--project", required=True)
    bpr.add_argument("file")
    bpa = bpp.add_parser("activate")
    bpa.add_argument("-p", "--project", required=True)
    bpa.add_argument("version", type=int)
    bps = bpp.add_parser("show")
    bps.add_argument("-p", "--project", required=True)
    bpn = bpp.add_parser("run")
    bpn.add_argument("-p", "--project", required=True)

    ro = sub.add_parser("roles", help="role registry").add_subparsers(dest="action", required=True)
    ro.add_parser("list")
    roa = ro.add_parser("add", help="add roles from a YAML file to the shared pool (as candidates)")
    roa.add_argument("file")
    to = sub.add_parser("tools", help="tool registry").add_subparsers(dest="action", required=True)
    tol = to.add_parser("list")
    tol.add_argument("--level", type=int, default=1, choices=[0, 1, 2])

    me = sub.add_parser("lab", help="Lab team: study models and prompts, Playbook").add_subparsers(dest="action", required=True)
    ms = me.add_parser("study", help="measure models and prompt knobs, propose a Playbook (owner approves)")
    ms.add_argument("-p", "--project", required=True)
    ms.add_argument("--models", help="comma-separated model ids (default: every model bound to a role)")
    ms.add_argument("--per-kind", type=int, default=2, help="eval cases per task kind and stage")
    ms.add_argument("--budget-tokens", type=int, default=400_000)
    ms.add_argument("--knobs", help="comma-separated subset of prompt knobs")
    ms.add_argument("--no-research", action="store_true", help="skip reading the providers' prompting guides")
    me.add_parser("playbook", help="show the active Playbook")
    me.add_parser("cards", help="show model cards from the last study")

    mi = sub.add_parser("mission", help="a goal for the Architect and the Leader, worked on in stages").add_subparsers(dest="action", required=True)
    mis = mi.add_parser("start", help="state the goal; the Architect reads the project and asks what is missing")
    mis.add_argument("-p", "--project", required=True)
    mis.add_argument("goal")
    mis.add_argument("--control", choices=["leads", "partial", "full"], default="leads")
    for name, hlp in (("show", "state, questions, stages"), ("next", "plan and run the next stage (stops for your approvals)"),
                      ("answer", "answer the open questions: id=text ... (no pairs: asks one by one; Enter = default)"),
                      ("control", "switch the control mode: leads | partial | full")):
        x = mi.add_parser(name, help=hlp)
        x.add_argument("-p", "--project", required=True)
        x.add_argument("-m", "--mission", help="mission id prefix (default: the latest)")
        if name == "answer":
            x.add_argument("pairs", nargs="*")
        if name == "control":
            x.add_argument("mode", choices=["leads", "partial", "full"])
    mil = mi.add_parser("list")
    mil.add_argument("-p", "--project", required=True)

    ch = sub.add_parser("chat", help="one message to the architect, the leader or (by control mode) a mission agent")
    ch.add_argument("-p", "--project", required=True)
    ch.add_argument("-m", "--mission")
    ch.add_argument("--to", default="leader")
    ch.add_argument("text")

    sd = sub.add_parser("selfdev", help="roj improves its own repository (one gated pull request)").add_subparsers(dest="action", required=True)
    sdr = sd.add_parser("run")
    sdr.add_argument("-p", "--project", required=True)
    sdr.add_argument("--goal", default="", help="what to improve (default: the planner picks from backlog and log signals)")

    sv = sub.add_parser("serve", help="server mode: scheduler, Telegram, panel and daily backup in one process")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--interval", type=float, default=30.0)
    sv.add_argument("--no-panel", action="store_true")
    sv.add_argument("--allow-remote", action="store_true")

    st = sub.add_parser("selftest", help="the whole chain offline in a throwaway home: no tokens, no network")
    st.add_argument("--keep", action="store_true", help="keep the temporary home for inspection")

    hc = sub.add_parser("healthcheck", help="exit 0 if `roj serve` wrote its heartbeat recently (container health)")
    hc.add_argument("--max-age", type=int, default=7200, help="seconds; a tick may run a long task")

    bk = sub.add_parser("backup", help="snapshot databases, settings, Playbook and reports to [storage] backup_dir")
    bk.add_argument("--with-cas", action="store_true", help="include the content store (prompts, answers, fetched pages)")

    dr = sub.add_parser("doctor", help="first-run checklist (writes logs/doctor-*.log)")
    dr.add_argument("--live", action="store_true", help="also send a few tiny requests (models, Telegram, one feed)")
    return ap


async def _amain(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    if args.cmd == "init":
        created = Installation.init(home)
        _print({"home": str(home), "created": created,
                "next": "edit roj.toml ([gateway] base_url, prices), set ROJ_API_KEY, then `roj project create NAME`"}, args.json)
        return 0

    if args.cmd == "selftest":
        from .obs.selftest import run_selftest, render

        results, root = await run_selftest(keep=args.keep)
        print(render(results), end="")
        if args.keep:
            print(f"kept: {root}")
        return 0 if results and all(r.ok for r in results) else 1
    if args.cmd == "healthcheck":
        from datetime import datetime, timezone

        hb = home / "serve.heartbeat"
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb.read_text("utf-8").strip())).total_seconds() if hb.exists() else None
        print(f"heartbeat {'missing' if age is None else f'{age:.0f}s old'}")
        return 0 if age is not None and age <= args.max_age else 1
    if args.cmd == "serve" and Installation.init(home):  # first start of a container: create the home
        print(f"initialised {home}: edit roj.toml, then restart", flush=True)
    inst = Installation(home)
    try:
        if args.cmd == "project":
            if args.action == "create":
                p = inst.create_project(args.name, tokens=args.tokens, usd=args.usd, timezone=args.timezone)
                _print({"project": p.name, "dir": str(p.dir), "tokens_per_day": p.settings.budget.tokens_per_day,
                        "usd_per_day": p.settings.budget.usd_per_day, "timezone": p.settings.timezone}, args.json)
                p.close()
            else:
                _print([{"project": n} for n in inst.project_names()], args.json)
            return 0

        if args.cmd == "browser" and args.action in ("check", "open"):
            from .hands import browser as hb

            if args.action == "open":
                await hb.open_for_owner(inst, args.profile, args.port, args.url, args.minutes)
                return 0
            ok, why = hb.available()
            if not ok:
                print(why, file=sys.stderr)
                return 1
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                b = await pw.chromium.launch(headless=True, args=hb.launch_args())
                page = await b.new_page()
                await page.set_content("<main><h1 id=t></h1></main><script>document.getElementById('t').textContent='rendered by JavaScript'</script>")
                text = await page.inner_text("h1")
                version = b.version
                await b.close()
            print(f"chromium {version}: {text}")
            return 0 if text == "rendered by JavaScript" else 1

        if args.cmd == "pricing":
            from .models.catalog import catalog_for, import_apigate_prices, write_pricing

            if args.action == "import":
                rows, default = import_apigate_prices(Path(args.file).read_text("utf-8"))
                path = write_pricing(home, rows)
                inst.close()
                inst = Installation(home)  # reload settings with the imported prices
                missing = [m["id"] for m in catalog_for(inst, inst.settings.pricing) if not m["price_known"]]
                _print({"imported": len(rows), "file": str(path), "gateway_default_for_unlisted": default,
                        "chat_models_still_without_price": missing,
                        "next": "add [pricing.models.\"<id>\"] in roj.toml for the models above, or accept the expensive default"}, True)
            else:
                cat = catalog_for(inst, inst.settings.pricing)
                _print([{"id": m["id"], "family": m["family"], "in": m["in"], "out": m["out"], "known": m["price_known"],
                         "context": m["context"]} for m in cat], args.json)
            return 0

        if args.cmd == "models" and (args.quirks or args.forget_quirks):
            qs = inst.client.quirks
            if args.forget_quirks:
                n = qs.forget(None if args.forget_quirks == "*" else args.forget_quirks)
                print(f"forgot {n} model(s)")
            _print(qs.all(), True)
            return 0

        if args.cmd == "models":
            if args.unblock:
                inst.registry.unblock(args.unblock)
            if args.refresh:
                diff = await inst.registry.refresh()
                if not args.json:
                    print(f"refreshed: {diff['total']} models, +{len(diff['added'])} -{len(diff['removed'])}")
            _print(inst.registry.list(), args.json)
            return 0

        if args.cmd == "log" and args.action == "verify" and not args.project:
            r = inst.events.verify()
            _print({"scope": "installation", **r.model_dump()}, args.json)
            return 0 if r.ok else 2

        if args.cmd == "scheduler":
            from .scheduler import run_forever, tick, windows_task_commands

            if args.action == "tick":
                _print(await tick(inst), args.json)
            elif args.action == "run":
                await run_forever(inst, args.interval)
            else:
                exe = shutil.which("roj") or "roj"
                for line in windows_task_commands(exe, str(home), args.start):
                    print(line)
            return 0

        if args.cmd == "panel":
            from .panel.server import serve

            server, _, url = serve(home, args.host, args.port, allow_remote=args.allow_remote)
            print(f"roj panel: {url}\n(open it once; the token is then kept in a cookie). Ctrl+C to stop.")
            try:
                await asyncio.to_thread(server.serve_forever)
            finally:
                server.server_close()
            return 0

        if args.cmd == "telegram":
            from .notify.outbox import flush_all, poll_once
            from .notify.telegram import send_telegram

            if args.action == "test":
                ids = await send_telegram(inst.settings.notify, "<b>roj</b>: test message, Telegram works.")
                _print({"sent": ids}, args.json)
                return 0
            while True:
                handled = await poll_once(inst, timeout=30 if args.forever else 0)
                sent = await flush_all(inst)
                if not args.forever:
                    _print({"handled": handled, "outbox": sent}, args.json)
                    return 0

        if args.cmd == "roles":
            from .agents.roles import RoleRegistry

            reg = RoleRegistry(inst)
            if args.action == "add":
                import yaml as _yaml

                added = [reg.add(r).name for r in _yaml.safe_load(open(args.file, encoding="utf-8")) or []]
                _print({"added": added, "status": "candidate"}, args.json)
            else:
                _print(reg.list(), args.json)
            return 0

        if args.cmd == "tools":
            from .tools.registry import all_tools

            _print([{"tool": n, "tier": s.tier.value, "card": s.card(args.level)} for n, s in sorted(all_tools().items())], args.json)
            return 0

        if args.cmd == "blueprint" and args.action == "lint" and not args.project:
            from .blueprint import manage
            from .blueprint.linter import lint

            issues = lint(manage.parse(open(args.file, encoding="utf-8").read()))
            _print([i.__dict__ for i in issues], args.json)
            return 1 if any(i.severity == "error" for i in issues) else 0

        if args.cmd == "doctor":
            from .obs import doctor

            checks = await doctor.run_doctor(inst, live=args.live)
            path = doctor.write(inst, checks)
            if args.json:
                _print([c.__dict__ for c in checks], True)
            else:
                print(doctor.render(checks), end="")
                print(f"written to {path}")
            await inst.client.aclose()
            return 1 if any(c.status == "FAIL" for c in checks) else 0

        if args.cmd == "logs" and args.action == "export":
            from .obs.export import export_bundle

            path = export_bundle(inst, days=args.days, with_prompts=not args.no_prompts, projects=args.project)
            _print({"bundle": str(path), "size_kb": round(path.stat().st_size / 1024, 1),
                    "next": "send this zip for review; API keys are not included"}, args.json)
            return 0

        if args.cmd == "serve":
            from .server import serve_forever

            try:
                await serve_forever(inst, host=args.host, port=args.port, panel=not args.no_panel, interval_s=args.interval,
                                    allow_remote=args.allow_remote)
            finally:
                await inst.client.aclose()
            return 0

        if args.cmd == "backup":
            from .server import backup

            path = backup(inst, with_cas=args.with_cas)
            _print({"backup": str(path), "kb": round(path.stat().st_size / 1024, 1)}, args.json)
            return 0

        if args.cmd == "lab" and args.action in ("playbook", "cards"):
            from .lab.playbook import load_active, pb_dir

            if args.action == "playbook":
                pb = load_active(home)
                _print({"version": pb.version, "study_task": pb.study_task, "profiles": pb.profiles,
                        "rules": [{"id": r.id, "status": r.status, "effect": r.effect, "tokens": r.tokens_delta, "n": r.n} for r in pb.rules]}, True)
            else:
                _print([json.loads(f.read_text("utf-8")) for f in sorted((pb_dir(home) / "models").glob("*.json"))], True)
            return 0

        if args.cmd == "pool":
            from .skills.registry import Pool

            _print(Pool(inst).list(), args.json)
            return 0

        if args.cmd == "skill" and args.action == "list" and not args.project:
            from .skills.catalog import SKILLS

            _print([{"skill": k, "description": v["description"]} for k, v in SKILLS.items()], args.json)
            return 0

        p = inst.project(args.project)
        try:
            code = await _project_cmd(args, p)
            if args.cmd in ("skill", "task", "approvals", "blueprint", "lab", "selfdev", "mission", "chat", "crawl", "search") \
            or (args.cmd == "browser" and args.action == "fetch"):
                from .notify.outbox import flush

                try:
                    await flush(p)
                except Exception:  # noqa: BLE001 - notifications are best effort
                    logging.getLogger("roj.cli").exception("outbox flush failed")
            return code
        finally:
            await inst.client.aclose()
            p.close()
    finally:
        inst.close()


async def _run_workflow(args: argparse.Namespace, p, ref: str, inputs: dict[str, Any], show: tuple[str, ...]) -> int:
    tid = p.kernel.submit(ref, inputs)
    res = await p.kernel.run(tid)
    out: dict[str, Any] = {"task_id": tid, "status": res.status.value, "error": res.error}
    for step in show:
        if step in res.results:
            out[step] = res.results[step]
    if res.status.value == "waiting_approval":
        out["next"] = f"review the report, then approve (Telegram, panel or `roj approvals approve -p {p.name} <id>`); `roj serve` resumes it"
    _print(out, True)
    return 0 if res.status.value in ("done", "waiting_approval") else 4


async def _mission_cmd(args: argparse.Namespace, p) -> int:
    from . import mission as ms

    def view(m: dict[str, Any]) -> dict[str, Any]:
        return {"mission": m["id"], "status": m["status"], "control": m["control"], "goal": m["goal"], "understanding": m["understanding"],
                "open_questions": [{k: q[k] for k in ("id", "question", "why", "options", "default", "from")} for q in ms.open_questions(m)],
                "directives": [d["text"] for d in m["directives"]], "roadmap": m["roadmap"],
                "stages": [{"n": st["n"], "objective": st["plan"]["objective"], "team": [t["agent"] for t in st["plan"]["team"]],
                            "verdict": st["review"]["verdict"], "change_requests": st["change_requests"]} for st in m["stages"]],
                "next": {"needs_answers": "roj mission answer", "ready": "roj mission next", "stage_done": "read the stage report, then roj mission next",
                         "new": "roj mission start", "complete": "nothing: the Leader reports the mission complete"}.get(m["status"], "")}

    if args.cmd == "chat":
        m = ms.load(p, args.mission)
        try:
            ms.may_talk(m, args.to)  # refuse before spending tokens
        except PermissionError as e:
            print(str(e), file=sys.stderr)
            return 3
        res = await p.kernel.run(p.kernel.submit(ms.CHAT_REF, {"mission": m["id"], "to": args.to, "text": args.text}))
        _print(res.results.get("chat") or {"status": res.status.value, "error": res.error}, True)
        return 0 if res.status.value == "done" else 4
    if args.action == "list":
        _print([{"mission": r["id"][:8], "status": r["status"], "control": r["control"], "goal": r["goal"][:80], "updated": r["updated"]}
                for r in p.store.all("SELECT * FROM missions ORDER BY created DESC")], args.json)
        return 0
    if args.action == "start":
        mid = ms.create(p, args.goal, args.control)
        res = await p.kernel.run(p.kernel.submit(ms.INTAKE_REF, {"mission": mid}))
        if res.status.value != "done":
            _print({"mission": mid, "status": res.status.value, "error": res.error}, True)
            return 4
        _print(view(ms.load(p, mid)), True)
        return 0
    m = ms.load(p, args.mission)
    if args.action == "control":
        ms.set_control(p, m["id"], args.mode)
    elif args.action == "answer":
        answers = dict(pair.split("=", 1) for pair in args.pairs)
        if not answers and sys.stdin.isatty():
            for q in ms.open_questions(m):
                print(f"\n[{q['id']}] {q['question']}\n  why: {q['why']}" + (f"\n  options: {' | '.join(q['options'])}" if q["options"] else "")
                      + f"\n  default: {q['default'] or '(none)'}")
                typed = input("> ").strip()
                if typed:
                    answers[q["id"]] = typed
        ms.answer(p, m["id"], answers)
    elif args.action == "next":
        if ms.open_questions(m):
            print("answer the open questions first: roj mission answer", file=sys.stderr)
            _print(view(m), True)
            return 2
        return await _run_workflow(args, p, ms.STAGE_REF, {"mission": m["id"]}, ("close",))
    _print(view(ms.load(p, m["id"])), True)
    return 0


async def _project_cmd(args: argparse.Namespace, p) -> int:
    if args.cmd == "lab":
        from .lab.study import STUDY_REF

        inputs: dict[str, Any] = {"per_kind": args.per_kind, "budget_tokens": args.budget_tokens, "research": not args.no_research}
        if args.models:
            inputs["models"] = args.models
        if args.knobs:
            inputs["knobs"] = args.knobs
        return await _run_workflow(args, p, STUDY_REF, inputs, ("report",))
    if args.cmd in ("mission", "chat"):
        return await _mission_cmd(args, p)
    if args.cmd == "crawl":
        from .hands.crawl import crawl

        _print(await crawl(p, args.url, max_pages=args.max_pages, depth=args.depth, prefix=args.prefix, render=args.render), True)
        return 0
    if args.cmd == "search":
        from .hands.search import paper_search, web_search

        srcs = [x.strip() for x in args.sources.split(",") if x.strip()]
        out: dict[str, Any] = {}
        if not srcs or set(srcs) - {"papers"}:
            out["web"] = await web_search(p.inst, args.query, args.n, events=p.events, project=p,
                                          sources=[x for x in srcs if x != "papers"] or None)
        if not srcs or "papers" in srcs:
            out["papers"] = await paper_search(p.inst, args.query, args.n, events=p.events)
        _print(out, True)
        return 0
    if args.cmd == "browser":
        from .core.kernel import StepContext
        from .tools import registry

        r = await registry.call(StepContext(p, None, None, "cli", 0, 1, {}, {}), "web.fetch", {"url": args.url, "render": args.render},
                                agent="owner")
        _print(r, True)
        return 0 if r.get("ok") else 4
    if args.cmd == "selfdev":
        from .lab.selfdev import SELFDEV_REF

        return await _run_workflow(args, p, SELFDEV_REF, {"goal": args.goal}, ("propose", "publish"))
    if args.cmd == "ask":
        messages = ([{"role": "system", "content": args.system}] if args.system else []) + [{"role": "user", "content": args.prompt}]
        params: dict[str, Any] = {"temperature": args.temperature}
        if args.max_tokens:
            params["max_tokens"] = args.max_tokens
        try:
            r = await p.caller.chat(binding=ModelBinding(model=args.model, fallbacks=args.fallback), messages=messages,
                                    agent="owner-cli", purpose="ask", params=params)
        except (AllModelsFailed, BudgetExceeded, ModelError) as e:
            _print({"error": type(e).__name__, "detail": str(e)}, args.json)
            return 3
        if args.json:
            _print({"text": r.text, "model": r.model, "fallback_index": r.fallback_index, "usage": r.usage.__dict__,
                    "cost_usd": r.cost_usd, "exact_cache_hit": r.exact_cache_hit}, True)
        else:
            print(r.text)
            print(f"\n-- {r.model} | in {r.usage.prompt_tokens} (cached {r.usage.cached_tokens}) out {r.usage.completion_tokens}"
                  f" | ${r.cost_usd:.6f}{' | exact cache hit' if r.exact_cache_hit else ''}"
                  f"{' | fallback #' + str(r.fallback_index) if r.fallback_index else ''}", file=sys.stderr)
        return 0

    if args.cmd == "task":
        k = p.kernel
        if args.action == "run":
            tid = k.submit(args.workflow, _parse_inputs(args.input))
            res = await k.run(tid)
            results: Any = res.results if args.json else {
                name: _short(value) for name, value in res.results.items()}
            _print({"task_id": tid, "status": res.status.value, "error": res.error, "results": results}, args.json)
            return 0 if res.status.value in ("done", "waiting_approval") else 4
        if args.action == "resume":
            results = await k.resume(args.task_id, include_paused=args.include_paused)
            _print([{"task_id": r.task_id, "status": r.status.value, "error": r.error or ""} for r in results], args.json)
            return 0
        if args.action == "list":
            _print([{"id": t["id"], "workflow": t["workflow"], "status": t["status"], "updated": t["updated"]}
                    for t in k.list(args.status)], args.json)
            return 0
        if args.action == "show":
            _print(k.get(args.task_id), args.json)
            return 0
        if args.action == "cancel":
            k.cancel(args.task_id)
            _print({"task_id": args.task_id, "status": "cancelled"}, args.json)
            return 0

    if args.cmd == "approvals":
        if args.action == "list":
            _print(p.policy.pending(), args.json)
        else:
            p.policy.decide(args.id, args.action == "approve", note=args.note)
            _print({"id": args.id, "decision": args.action, "next": "roj task resume -p " + p.name}, args.json)
        return 0

    if args.cmd == "budget":
        if args.action == "allocate":
            warnings = p.budget.allocate(args.scope, args.tokens, args.usd)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
        _print(p.budget.report(), args.json)
        return 0

    if args.cmd == "log":
        if args.action == "verify":
            r = p.events.verify()
            _print({"scope": f"project:{p.name}", **r.model_dump()}, args.json)
            return 0 if r.ok else 2
        evs = p.events.query(type=args.type, limit=args.n, newest_first=True)
        rows = [{"seq": e.seq, "ts": e.ts, "type": e.type, "actor": e.actor, "task": (e.task_id or "")[:8],
                 "payload": json.dumps(e.payload, ensure_ascii=False)[:120]} for e in reversed(evs)]
        _print(rows if not args.json else [e.model_dump() for e in reversed(evs)], args.json)
        return 0

    if args.cmd == "stats":
        _print(llm_stats(p.events, task_id=args.task), args.json)
        return 0

    if args.cmd == "skill":
        return await _skill_cmd(args, p)

    if args.cmd == "kb":
        from pathlib import Path as _P

        from .knowledge.packs import KnowledgeBase

        kbase = KnowledgeBase(p)
        if args.action == "create":
            _print(kbase.create(args.pack, args.description, level=args.level, embed_model=args.embed_model,
                                audience=args.audience.split(",") if args.audience else None), args.json)
        elif args.action == "add":
            out = [{"file": f, "chunks": await kbase.add_file(args.pack, _P(f), contextualize=args.contextualize, novelty=args.novelty)} for f in args.files]
            _print(out, args.json)
        elif args.action == "search":
            hits = await kbase.search(args.pack, args.query, k=args.k, rewrite=args.rewrite, rerank=args.rerank)
            _print([{"doc": h.doc_id, "section": h.section, "score": h.score, "via": ",".join(h.via), "text": h.text[:200]}
                    for h in hits], args.json)
        elif args.action == "question":
            kbase.add_question(args.pack, args.question, args.expect)
            _print({"pack": args.pack, "questions": len(kbase.manifest(args.pack)["questions"])}, args.json)
        elif args.action == "eval":
            _print(await kbase.evaluate(args.pack, k=args.k), args.json)
        else:
            _print(kbase.packs(), args.json)
        return 0

    if args.cmd == "gates":
        from .verify.ratchet import Ratchet

        rat = Ratchet(p)
        if args.action == "lower":
            rat.lower(args.suite, args.metric, args.threshold, note=args.note)
        _print(rat.report(), args.json)
        return 0

    if args.cmd == "blueprint":
        from .blueprint import manage
        from .blueprint.linter import lint
        from .blueprint.runner import RUN_REF

        if args.action == "lint":
            issues = lint(manage.parse(open(args.file, encoding="utf-8").read()), p)
            _print([i.__dict__ for i in issues], args.json)
            return 1 if any(i.severity == "error" for i in issues) else 0
        if args.action == "propose":
            try:
                _print(manage.propose(p, manage.parse(open(args.file, encoding="utf-8").read())), args.json)
            except manage.BlueprintInvalid as e:
                _print([i.__dict__ for i in e.issues], args.json)
                return 1
            return 0
        if args.action == "activate":
            _print(manage.activate(p, args.version), args.json)
            return 0
        if args.action == "show":
            act = manage.active(p)
            versions = [dict(r) for r in p.store.all("SELECT version, status, created, created_by FROM blueprints ORDER BY version")]
            _print({"active": act[0] if act else None, "versions": versions,
                    "blueprint": act[1].model_dump(mode="json") if act else None}, args.json)
            return 0
        tid = p.kernel.submit(RUN_REF, {})
        res = await p.kernel.run(tid)
        _print({"task_id": tid, "status": res.status.value, "error": res.error, **(res.results.get("report") or {})}, args.json)
        return 0 if res.status.value in ("done", "waiting_approval") else 4

    if args.cmd == "rate":
        from .notify.outbox import rate

        matches = [t["id"] for t in p.kernel.list(limit=1000) if t["id"].startswith(args.task_id)]
        if not matches:
            raise KeyError(f"no task starting with {args.task_id}")
        rate(p, matches[0], args.score, comment=args.comment, source="cli")
        _print({"task_id": matches[0], "score": args.score}, args.json)
        return 0

    if args.cmd == "logs":
        from .obs import runlog

        if args.action == "list":
            root = p.inst.home / "logs" / p.name
            files = sorted(root.glob("*/*.log"), key=lambda f: f.stat().st_mtime, reverse=True) if root.exists() else []
            _print([{"log": str(f.relative_to(p.inst.home)), "kb": round(f.stat().st_size / 1024, 1)} for f in files[:50]], args.json)
            return 0
        if args.task_id:
            matches = [t["id"] for t in p.kernel.list(limit=1000) if t["id"].startswith(args.task_id)]
            if not matches:
                raise KeyError(f"no task starting with {args.task_id}")
            print(runlog.render(p, matches[0]), end="")
        else:
            path = runlog.latest(p)
            print(path.read_text(encoding="utf-8") if path else "(no task logs yet)", end="")
        return 0

    if args.cmd == "schedule":
        if args.action == "add":
            if any(s.skill == args.skill for s in p.settings.schedules):
                raise ValueError(f"{args.skill} already has a schedule; edit {p.dir / 'project.toml'}")
            from .config import ScheduleSpec

            sch = ScheduleSpec(skill=args.skill, at=args.at, lead_minutes=args.lead, grace_hours=args.grace)
            with open(p.dir / "project.toml", "a", encoding="utf-8") as f:
                f.write(f'\n[[schedules]]\nskill = "{sch.skill}"\nat = "{sch.at}"\nlead_minutes = {sch.lead_minutes}\n'
                        f"grace_hours = {sch.grace_hours}\n")
            _print({"skill": sch.skill, "at": sch.at, "starts": f"{sch.lead_minutes} min earlier",
                    "next": "run `roj scheduler tick` from Task Scheduler/cron, or `roj scheduler run`"}, args.json)
        else:
            _print([s.model_dump() for s in p.settings.schedules], args.json)
        return 0
    return 1


async def _skill_cmd(args: argparse.Namespace, p) -> int:
    from .skills.catalog import SKILLS, ref
    from .skills.registry import Pool, SpecRegistry

    if args.action == "list":
        reg = SpecRegistry(p)
        _print([{"skill": k, "active_version": (reg.active(k) or (None,))[0], "description": v["description"]}
                for k, v in SKILLS.items()], args.json)
        return 0
    models = {"*": args.model} if getattr(args, "model", None) else {}
    k = p.kernel
    if args.action == "bootstrap":
        tid = k.submit(ref(args.skill, "bootstrap"), {"seeds": args.seeds, "models": models})
        res = await k.run(tid)
        r = res.results
        out: dict[str, Any] = {"task_id": tid, "status": res.status.value, "error": res.error}
        if "probe" in r:
            out["probe"] = r["probe"]["summary"]
        if "compile" in r:
            spec = r["compile"]["spec"]
            out["sources"] = [f"{s['id']} ({s['kind']}, {s['tier']})" for s in spec["sources"]]
            out["instruments"] = [i["id"] for i in spec["instruments"]]
            out["validation"] = r["compile"]["validation"]
            out["notes"] = spec["plan_notes"]
        out["cost"] = llm_stats(p.events, task_id=tid)["cost_usd"]
        if res.status.value == "waiting_approval":
            out["next"] = f"review, then `roj approvals approve -p {p.name} <id>` and `roj task resume -p {p.name}`"
        _print(out, args.json)
        return 0 if res.status.value in ("done", "waiting_approval") else 4
    if args.action == "run":
        inputs: dict[str, Any] = {"models": models, "send": not args.no_send}
        if args.deliver_at:
            inputs["deliver_at"] = args.deliver_at
        tid = k.submit(ref(args.skill, "run"), inputs)
        res = await k.run(tid)
        out = {"task_id": tid, "status": res.status.value, "error": res.error}
        if "publish" in res.results:
            out.update(res.results["publish"])
            out["notes"] = res.results["gate"]["report"]["notes"]
        _print(out, args.json)
        return 0 if res.status.value == "done" else 4
    if args.action == "repair":
        tid = k.submit(ref(args.skill, "repair"), {"source_ids": args.source})
        res = await k.run(tid)
        _print({"task_id": tid, "status": res.status.value, "error": res.error,
                "fixes": res.results.get("fix", {}).get("fixes"), "unfixed": res.results.get("fix", {}).get("unfixed")}, args.json)
        return 0 if res.status.value in ("done", "waiting_approval") else 4
    if args.action == "status":
        reg = SpecRegistry(p)
        active = reg.active(args.skill)
        last = p.store.one("SELECT last_run_task, last_window_end FROM skill_state WHERE skill = ?", (args.skill,))
        last_cost = llm_stats(p.events, task_id=last["last_run_task"]) if last and last["last_run_task"] else None
        _print({"active_version": active[0] if active else None, "versions": reg.versions(args.skill),
                "source_health": reg.health(args.skill),
                "last_run": {"task_id": last["last_run_task"], "window_end": last["last_window_end"],
                             "tokens": last_cost["prompt_tokens"] + last_cost["completion_tokens"],
                             "cost_usd": last_cost["cost_usd"]} if last_cost else None,
                "adapters": [a for a in Pool(p.inst).list("adapter")]}, args.json)
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = resolve_home(args.home)
    if (home / "roj.toml").exists():
        from .obs import setup_process_log

        setup_process_log(home)
        logging.getLogger("roj.cli").info("command: %s", " ".join(sys.argv[1:] if argv is None else argv))
    try:
        return asyncio.run(_amain(args))
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, KeyError) as e:
        logging.getLogger("roj.cli").warning("error: %s", e)
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
