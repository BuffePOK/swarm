"""`roj doctor`: first-run checklist for a new machine. Writes logs/doctor-<ts>.log.

Without --live it only reads configuration and asks the gateway for its model list.
With --live it also sends one tiny request per bound model (a few tokens each),
tests provider prompt caching, Telegram getMe and one public feed.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from .. import __version__
from ..config import ModelBinding, ProjectSettings

if TYPE_CHECKING:
    from ..project import Installation

PAIRS_DIFFERENT_FAMILY = [("planner", "critic"), ("architect", "critic"), ("analyst", "checker"), ("coder", "critic")]


@dataclass
class Check:
    area: str
    status: str  # PASS | WARN | FAIL | INFO
    detail: str
    hint: str = ""


def family(model: str) -> str:
    m = model.lower().split("/")[-1]
    for fam in ("claude", "gemini", "gemma", "gpt", "deepseek", "llama", "qwen", "mistral", "grok", "fake-smart-b", "fake-smart"):
        if m.startswith(fam):
            return fam
    return m.split("-")[0]


def _bindings(inst: "Installation") -> dict[str, dict[str, ModelBinding]]:
    out: dict[str, dict[str, ModelBinding]] = {"(installation)": dict(inst.settings.models)}
    for name in inst.project_names():
        ps = ProjectSettings.load(inst.home / "projects" / name)
        b = dict(ps.models)  # only what the project overrides; inherited bindings are checked once above
        for skill, cfg in ps.skills.items():
            for role, raw in (cfg.get("models") or {}).items():
                b[f"{skill}.{role}"] = ModelBinding.model_validate(raw)
        if b:
            out[name] = b
    return out


async def run_doctor(inst: "Installation", *, live: bool = False) -> list[Check]:
    c: list[Check] = []
    s = inst.settings
    c.append(Check("system", "PASS" if sys.version_info >= (3, 11) else "FAIL",
                   f"python {platform.python_version()} on {platform.platform()}; roj {__version__}"))
    free = shutil.disk_usage(inst.home).free / 1e9
    c.append(Check("system", "PASS" if free > 2 else "WARN", f"home {inst.home} ({free:.1f} GB free)",
                   "" if free > 2 else "logs, CAS and reports grow over time; move ROJ_HOME to a larger drive"))
    try:
        ZoneInfo(s.timezone)
        c.append(Check("config", "PASS", f"timezone {s.timezone}"))
    except Exception:  # noqa: BLE001
        c.append(Check("config", "FAIL", f"unknown timezone {s.timezone}", "use an IANA name such as Europe/Warsaw"))
        if sys.platform == "win32":
            c[-1].hint += "; on Windows also `pip install tzdata`"
    key = s.gateway.api_key()
    c.append(Check("gateway", "PASS" if key else "WARN", f"API key env {s.gateway.api_key_env} {'set' if key else 'not set'}",
                   "" if key else "fine if your gateway needs no key"))
    available: set[str] = set()
    try:
        models = await inst.client.list_models()
        available = {str(m.get("id")) for m in models}
        c.append(Check("gateway", "PASS", f"{s.gateway.base_url} answers: {len(available)} models"))
    except Exception as e:  # noqa: BLE001
        c.append(Check("gateway", "FAIL", f"{s.gateway.base_url}: {type(e).__name__}: {e}"[:300],
                       "start the gateway or fix [gateway] base_url (include /v1 if needed)"))

    distinct: set[str] = set()
    for scope, roles in _bindings(inst).items():
        if not roles:
            c.append(Check("models", "WARN", f"{scope}: no role bindings",
                           "add [models.planner] ... [models.checker] to roj.toml (see README)"))
            continue
        for role, b in sorted(roles.items()):
            cands = b.candidates()
            distinct.update(cands)
            if available:
                missing = [m for m in cands if m not in available]
                if b.model in missing and len(missing) == len(cands):
                    c.append(Check("models", "FAIL", f"{scope} {role}: none of {cands} on the gateway"))
                elif missing:
                    c.append(Check("models", "WARN", f"{scope} {role}: not on the gateway: {missing}"))
                else:
                    c.append(Check("models", "PASS", f"{scope} {role}: {b.model}" + (f" (+{len(b.fallbacks)} fallbacks)" if b.fallbacks else "")))
            if not b.fallbacks:
                c.append(Check("models", "WARN", f"{scope} {role}: no fallback", "add fallbacks = [...] so a 403/404 does not stop the run"))
        for a, bb in PAIRS_DIFFERENT_FAMILY:
            ra = next((v for k, v in roles.items() if k.endswith(a)), None)
            rb = next((v for k, v in roles.items() if k.endswith(bb)), None)
            if ra and rb and family(ra.model) == family(rb.model):
                c.append(Check("models", "WARN", f"{scope}: {a} and {bb} are both {family(ra.model)}",
                               "use another model family for the reviewer so errors are not shared"))
    for m in sorted(distinct):
        _, known = s.pricing.price_for(m)
        if not known:
            c.append(Check("pricing", "WARN", f"no price for {m}: budgets use the expensive default",
                           "add [pricing.models.\"<id or glob>\"] input/cached_input/output (USD per 1M tokens)"))

    tg_token = os.environ.get(s.notify.telegram_token_env, "")
    if tg_token and s.notify.telegram_chat_id:
        c.append(Check("telegram", "PASS", f"token in {s.notify.telegram_token_env}, chat {s.notify.telegram_chat_id}"))
    else:
        c.append(Check("telegram", "WARN", "Telegram not configured: briefs are only written to reports/",
                       f"set {s.notify.telegram_token_env} and [notify] telegram_chat_id"))
    c.append(Check("market", "PASS" if os.environ.get("FRED_API_KEY") else "INFO",
                   "FRED_API_KEY " + ("set" if os.environ.get("FRED_API_KEY") else "not set: indices, yields, Brent and VIX will be missing")))

    st, sd = s.storage, s.selfdev
    if st.backup_dir:
        ok = Path(st.backup_dir).expanduser().is_dir() and os.access(Path(st.backup_dir).expanduser(), os.W_OK)
        c.append(Check("storage", "PASS" if ok else "FAIL", f"backup_dir {st.backup_dir} " + ("writable" if ok else "missing or not writable"),
                       "" if ok else "mount the bucket and add the path to ReadWritePaths in roj.service"))
    else:
        c.append(Check("storage", "WARN", "no [storage] backup_dir: backups stay on the same disk as the data",
                       "point it at a mounted bucket; `roj serve` then makes one snapshot per day"))
    if sd.repo:
        is_git = (Path(sd.repo).expanduser() / ".git").exists()
        c.append(Check("selfdev", "PASS" if is_git else "FAIL", f"repo {sd.repo}" + ("" if is_git else " is not a git clone")))
        token = bool(os.environ.get(sd.github_token_env))
        c.append(Check("selfdev", "PASS" if sd.github_repo and token else "WARN",
                       f"pull requests to {sd.github_repo or '(no github_repo)'}: token {sd.github_token_env} {'set' if token else 'not set'}",
                       "" if sd.github_repo and token else "without them selfdev leaves a local branch and a patch file"))
    else:
        c.append(Check("selfdev", "INFO", "[selfdev] repo not set: self-improvement is off"))
    from ..lab.playbook import load_active

    pb = load_active(inst.home)
    c.append(Check("playbook", "INFO", f"version {pb.version}, profiles for: {', '.join(pb.profiles) or 'none (default prompt layout everywhere)'}"))
    hb = inst.home / "serve.heartbeat"
    if hb.exists():
        c.append(Check("serve", "INFO", f"last heartbeat {hb.read_text('utf-8')}"))

    for name in inst.project_names():
        p = inst.project(name)
        try:
            v = p.events.verify()
            c.append(Check(f"project:{name}", "PASS" if v.ok else "FAIL", f"event log {v.count} events, chain {'ok' if v.ok else 'BROKEN at ' + str(v.first_bad_seq)}"))
            pend = len(p.policy.pending())
            paused = p.store.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('paused_api','paused_budget','failed')")["n"]
            c.append(Check(f"project:{name}", "INFO", f"{pend} approvals pending, {paused} paused/failed tasks, "
                           f"budget {p.settings.budget.tokens_per_day:,} tokens / ${p.settings.budget.usd_per_day}/day, "
                           f"schedules: {', '.join(f'{x.skill}@{x.at}' for x in p.settings.schedules) or 'none'}"))
        finally:
            p.close()

    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Query", "/TN", "roj scheduler"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        c.append(Check("scheduler", "PASS" if r.returncode == 0 else "WARN", "Windows task 'roj scheduler' " + ("found" if r.returncode == 0 else "missing"),
                       "" if r.returncode == 0 else "run `roj scheduler windows-task` and execute the printed commands"))

    if live:
        c += await _live(inst, sorted(m for m in distinct if not available or m in available))
    return c


async def _live(inst: "Installation", models: list[str]) -> list[Check]:
    c: list[Check] = []
    prefix = "You are a connectivity probe. " * 400  # ~2.4k tokens: long enough for provider caches
    for m in models:
        try:
            msgs = [{"role": "system", "content": prefix}, {"role": "user", "content": "Reply with OK."}]
            r1 = await inst.client.chat(m, msgs, {"max_tokens": 5, "temperature": 0})
            r2 = await inst.client.chat(m, msgs, {"max_tokens": 5, "temperature": 0})
            cache = "yes" if r2.usage.cached_tokens > 0 else "not reported"
            c.append(Check("live", "PASS", f"{m}: {r1.latency_s:.2f}s, usage {'estimated' if r1.usage.estimated else 'reported'}, "
                           f"prompt cache {cache} ({r2.usage.cached_tokens} cached)"))
        except Exception as e:  # noqa: BLE001
            c.append(Check("live", "FAIL", f"{m}: {type(e).__name__}: {e}"[:300]))
    s = inst.settings
    token = os.environ.get(s.notify.telegram_token_env, "")
    if token:
        try:
            async with httpx.AsyncClient(timeout=15) as cl:
                r = await cl.get(f"{s.notify.telegram_api.rstrip('/')}/bot{token}/getMe")
            ok = r.status_code == 200 and r.json().get("ok")
            c.append(Check("live", "PASS" if ok else "FAIL", f"Telegram getMe: {'bot ' + r.json()['result'].get('username', '') if ok else r.text[:150]}"))
        except Exception as e:  # noqa: BLE001
            c.append(Check("live", "FAIL", f"Telegram: {type(e).__name__}: {e}"[:200]))
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": s.web.user_agent}) as cl:
            r = await cl.get("https://www.federalreserve.gov/feeds/press_all.xml")
        c.append(Check("live", "PASS" if r.status_code == 200 else "WARN", f"public feed (federalreserve.gov): HTTP {r.status_code}"))
    except Exception as e:  # noqa: BLE001
        c.append(Check("live", "FAIL", f"internet/feeds: {type(e).__name__}: {e}"[:200], "check proxy or firewall"))
    return c


def render(checks: list[Check]) -> str:
    counts = {k: sum(1 for x in checks if x.status == k) for k in ("PASS", "WARN", "FAIL", "INFO")}
    out = [f"roj doctor  {datetime.now().isoformat(timespec='seconds')}   "
           + "  ".join(f"{k} {v}" for k, v in counts.items())]
    for x in checks:
        out.append(f"[{x.status:<4}] {x.area:<18} {x.detail}")
        if x.hint and x.status in ("WARN", "FAIL"):
            out.append(f"{'':7}{'':18} -> {x.hint}")
    return "\n".join(out) + "\n"


def write(inst: "Installation", checks: list[Check]) -> Path:
    path = inst.home / "logs" / f"doctor-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(checks), encoding="utf-8")
    return path
