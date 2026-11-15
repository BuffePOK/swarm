"""Local web panel (stdlib only): approvals, tasks, logs, reports, budgets, gates, ratings.

Security
- binds to 127.0.0.1 by default; another host needs --allow-remote and still the token
- a random token (stored in <home>/panel_token) must be presented once via ?token=...;
  it becomes an HttpOnly, SameSite=Strict cookie; every POST also carries it as a form field
- no JavaScript, no external assets
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from ..notify.outbox import rate
from ..obs import runlog
from ..project import Installation
from ..skills.catalog import SKILLS
from ..skills.registry import SpecRegistry
from ..verify.ratchet import Ratchet

CSS = """
:root{--bg:#fafaf8;--fg:#1b1b1b;--mut:#666;--line:#ddd;--ok:#1d7f4e;--bad:#b3261e;--warn:#9a6700;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e8e8e6;--mut:#9a9a9a;--line:#33353a;--card:#1e2024}}
body{font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1100px;padding:16px}
a{color:inherit}h1{font-size:20px}h2{font-size:16px;margin-top:28px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600}
pre{background:var(--card);border:1px solid var(--line);padding:10px;overflow:auto;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
button{padding:4px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
input,select{padding:4px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
"""

STATUS_CLASS = {"done": "ok", "failed": "bad", "paused_api": "bad", "paused_budget": "warn", "waiting_approval": "warn", "running": "mut"}


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def md_to_html(md: str) -> str:
    """Tiny Markdown subset: headings, lists, tables, bold, italics, links, code. Input is escaped first."""
    out, in_list, table = [], False, []

    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noreferrer">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    def flush_table() -> None:
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            out.append("<table>" + "".join("<tr>" + "".join(f"<{'th' if i == 0 else 'td'}>{inline(c)}</{'th' if i == 0 else 'td'}>"
                                                         for c in row) + "</tr>" for i, row in enumerate(cells)) + "</table>")
            table.clear()

    for line in md.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        flush_table()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"(#{1,4}) (.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n + 1}>{inline(m.group(2))}</h{n + 1}>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush_table()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{esc(title)} · roj</title><style>{CSS}</style></head><body>"
            f"<p class='mut'><a href='/'>roj</a></p>{body}</body></html>").encode("utf-8")


def load_token(home: Path) -> str:
    path = home / "panel_token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


class Panel:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.token = load_token(self.home)

    # --- views ------------------------------------------------------------------------
    def index(self) -> bytes:
        inst = Installation(self.home)
        try:
            rows = []
            for name in inst.project_names():
                p = inst.project(name)
                try:
                    used = p.budget.report()[0]
                    rows.append(f"<tr><td><a href='/p/{quote(name)}'>{esc(name)}</a></td><td>{len(p.policy.pending())}</td>"
                                f"<td>{used['tokens_used']:,} / {used['tokens_limit']:,}</td><td>${used['usd_used']:.4f} / ${used['usd_limit']}</td></tr>")
                finally:
                    p.close()
            alerts = "".join(f"<li>{esc(e.ts[:19])} {esc(json.dumps(e.payload)[:200])}</li>" for e in inst.events.query(type="alert", limit=10, newest_first=True))
        finally:
            inst.close()
        body = ("<h1>Projects</h1><table><tr><th>Project</th><th>Approvals</th><th>Tokens today</th><th>USD today</th></tr>"
                + "".join(rows) + "</table>" + (f"<h2>Installation alerts</h2><ul>{alerts}</ul>" if alerts else ""))
        return page("projects", body)

    def project(self, name: str) -> bytes:
        inst = Installation(self.home)
        p = inst.project(name)
        try:
            t = self.token
            appr = "".join(
                f"<div class='card'><b>{esc(a['action'])}</b> <span class='mut'>expires {esc(a['expires'])}</span>"
                f"<pre>{esc(json.dumps(a['details'], ensure_ascii=False, indent=1)[:4000])}</pre>"
                f"<form method='post' action='/p/{quote(name)}/approval/{a['id']}'><input type='hidden' name='token' value='{t}'>"
                f"<input name='note' placeholder='note (optional)'> <button name='decision' value='approve'>Approve</button> "
                f"<button name='decision' value='reject'>Reject</button></form></div>" for a in p.policy.pending()) or "<p class='mut'>none</p>"
            tasks = "".join(
                f"<tr><td><a href='/p/{quote(name)}/task/{x['id']}'>{x['id'][:8]}</a></td><td>{esc(x['workflow'].rsplit(':', 1)[-1])}</td>"
                f"<td class='{STATUS_CLASS.get(x['status'], '')}'>{esc(x['status'])}</td><td>{esc(x['updated'][:19])}</td></tr>"
                for x in p.kernel.list(limit=30))
            budget = "".join(f"<tr><td>{esc(b['scope'])}</td><td>{b['tokens_used']:,} / {b['tokens_limit'] if b['tokens_limit'] is not None else '∞'}</td>"
                             f"<td>${b['usd_used']:.4f} / {b['usd_limit'] if b['usd_limit'] is not None else '∞'}</td></tr>" for b in p.budget.report())
            skills = []
            reg = SpecRegistry(p)
            for k in SKILLS:
                act = reg.active(k)
                health = "".join(f"<li>{esc(h['source_id'])}: <span class='{'bad' if h['consecutive_failures'] else 'ok'}'>"
                                 f"{h['consecutive_failures']} failures</span> {esc(h['last_error'] or '')}</li>" for h in reg.health(k))
                skills.append(f"<div class='card'><b>{esc(k)}</b> active version {esc(act[0] if act else '—')}<ul>{health}</ul></div>")
            gates = "".join(f"<tr><td>{esc(g['suite'])}</td><td>{esc(g['metric'])}</td><td>{g['threshold']:.3f}</td><td>{esc(g['last_value'])}</td>"
                            f"<td>{g['cases']} ({g['hidden']} hidden)</td></tr>" for g in Ratchet(p).report())
            rdir = p.dir / "reports"
            reports = sorted(rdir.glob("*.md"), reverse=True)[:20] if rdir.exists() else []
            rep = "".join(f"<li><a href='/p/{quote(name)}/report/{quote(f.name)}'>{esc(f.name)}</a></li>" for f in reports)
            runs = p.store.all("SELECT t.id, t.updated, r.score FROM tasks t LEFT JOIN ratings r ON r.task_id = t.id "
                               "WHERE t.workflow LIKE '%:RUN' AND t.status = 'done' ORDER BY t.updated DESC LIMIT 7")
            rate_rows = "".join(
                f"<tr><td>{esc(r['updated'][:16])}</td><td>{esc(r['score'] or '—')}</td><td><form method='post' action='/p/{quote(name)}/rate/{r['id']}'>"
                f"<input type='hidden' name='token' value='{t}'><select name='score'>{''.join(f'<option>{n}</option>' for n in range(5, 0, -1))}</select> "
                f"<input name='comment' placeholder='comment'> <button>Rate</button></form></td></tr>" for r in runs)
        finally:
            p.close()
            inst.close()
        body = (f"<h1>{esc(name)}</h1><h2>Approvals</h2>{appr}<h2>Tasks</h2><table><tr><th>Task</th><th>Workflow</th><th>Status</th>"
                f"<th>Updated</th></tr>{tasks}</table><h2>Skills</h2>{''.join(skills)}<h2>Briefs: rate them</h2><table>{rate_rows}</table>"
                f"<h2>Reports</h2><ul>{rep}</ul><h2>Budget today</h2><table>{budget}</table>"
                f"<h2>Quality gates</h2><table><tr><th>Suite</th><th>Metric</th><th>Threshold</th><th>Last</th><th>Cases</th></tr>{gates}</table>")
        return page(name, body)

    def task(self, name: str, task_id: str) -> bytes:
        inst = Installation(self.home)
        p = inst.project(name)
        try:
            return page(f"task {task_id[:8]}", f"<h1>Task {esc(task_id[:8])}</h1><pre>{esc(runlog.render(p, task_id))}</pre>")
        finally:
            p.close()
            inst.close()

    def report(self, name: str, file: str) -> bytes:
        path = (self.home / "projects" / name / "reports" / file).resolve()
        if path.parent != (self.home / "projects" / name / "reports").resolve() or not path.exists():
            raise FileNotFoundError(file)
        return page(file, md_to_html(path.read_text(encoding="utf-8")))

    # --- actions ------------------------------------------------------------------------
    def decide(self, name: str, approval_id: str, form: dict[str, str]) -> None:
        inst = Installation(self.home)
        p = inst.project(name)
        try:
            p.policy.decide(approval_id, form.get("decision") == "approve", by="owner:panel", note=form.get("note") or None)
            task = p.store.one("SELECT task_id FROM approvals WHERE id = ?", (approval_id,))["task_id"]
        finally:
            p.close()
            inst.close()
        if task:
            threading.Thread(target=self._resume, args=(name, task), daemon=True).start()

    def _resume(self, name: str, task_id: str) -> None:
        inst = Installation(self.home)
        p = inst.project(name)
        try:
            asyncio.run(p.kernel.resume(task_id))
        finally:
            p.close()
            inst.close()

    def rate(self, name: str, task_id: str, form: dict[str, str]) -> None:
        inst = Installation(self.home)
        p = inst.project(name)
        try:
            rate(p, task_id, int(form.get("score", "0")), comment=form.get("comment", ""), source="panel")
        finally:
            p.close()
            inst.close()


def make_handler(panel: Panel):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return

        def _authed(self) -> bool:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "roj_panel" in c and secrets.compare_digest(c["roj_panel"].value, panel.token)

        def _send(self, code: int, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", to)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if q.get("token") and secrets.compare_digest(q["token"], panel.token):
                return self._redirect(u.path or "/", {"Set-Cookie": f"roj_panel={panel.token}; HttpOnly; SameSite=Strict; Path=/"})
            if u.path == "/healthz":  # no secrets: liveness for the deploy script and monitoring
                hb = panel.home / "serve.heartbeat"
                return self._send(200, f"ok {hb.read_text() if hb.exists() else 'no-heartbeat'}".encode())
            if not self._authed():
                return self._send(401, page("locked", "<h1>Locked</h1><p>Open the URL printed by <code>roj panel</code> (it contains the token).</p>"))
            parts = [p for p in u.path.split("/") if p]
            try:
                if not parts:
                    return self._send(200, panel.index())
                if parts[0] == "p" and len(parts) == 2:
                    return self._send(200, panel.project(parts[1]))
                if parts[0] == "p" and len(parts) == 4 and parts[2] == "task":
                    return self._send(200, panel.task(parts[1], parts[3]))
                if parts[0] == "p" and len(parts) == 4 and parts[2] == "report":
                    return self._send(200, panel.report(parts[1], parts[3]))
            except (FileNotFoundError, KeyError) as e:
                return self._send(404, page("not found", f"<h1>Not found</h1><p>{esc(e)}</p>"))
            return self._send(404, page("not found", "<h1>Not found</h1>"))

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return self._send(401, page("locked", "<h1>Locked</h1>"))
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(n, 100_000)).decode()).items()}
            if not secrets.compare_digest(form.get("token", ""), panel.token):
                return self._send(403, page("forbidden", "<h1>Forbidden</h1>"))
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            try:
                if len(parts) == 4 and parts[0] == "p" and parts[2] == "approval":
                    panel.decide(parts[1], parts[3], form)
                elif len(parts) == 4 and parts[0] == "p" and parts[2] == "rate":
                    panel.rate(parts[1], parts[3], form)
                else:
                    return self._send(404, page("not found", "<h1>Not found</h1>"))
            except (ValueError, KeyError) as e:
                return self._send(400, page("error", f"<h1>Error</h1><p>{esc(e)}</p>"))
            return self._redirect(f"/p/{parts[1]}")

    return H


def serve(home: Path, host: str = "127.0.0.1", port: int = 8765, *, allow_remote: bool = False):
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise ValueError("the panel binds to localhost only; pass --allow-remote to expose it (token still required)")
    panel = Panel(home)
    server = ThreadingHTTPServer((host, port), make_handler(panel))
    return server, panel, f"http://{host}:{server.server_address[1]}/?token={panel.token}"
