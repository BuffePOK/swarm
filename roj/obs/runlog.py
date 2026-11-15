"""Human-readable task logs rendered from the event log (the single source of truth).

For every task roj writes, whenever the task stops (done, failed, parked, paused):
  <home>/logs/<project>/<submit-date>/<task8>-<workflow>.log     readable timeline + totals
  <home>/logs/<project>/<submit-date>/<task8>-<workflow>.jsonl   raw events (machine)
The files are rewritten with the full history on every resume, so one file = one task.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.eventlog import Event

if TYPE_CHECKING:
    from ..project import Project

WIDTH = 120


def _ts(e: Event) -> str:
    return e.ts.replace("T", " ")[:23]


def _dur(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def _short(obj: Any, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _line(e: Event) -> tuple[str, str]:
    p, t = e.payload, e.type
    if t == "llm.call":
        fb = f" (fallback #{p['fallback_index']} for {p['requested']})" if p.get("fallback_index") else ""
        est = " est." if p.get("usage_estimated") else ""
        return "LLM", (f"{p.get('agent')} -> {p['model']}{fb}  in {p['prompt_tokens']:,} (cached {p['cached_tokens']:,}) "
                       f"out {p['completion_tokens']:,}{est}  ${p['cost_usd']:.5f}  {p['latency_s']:.2f}s  "
                       f"try {p.get('attempt', 1)}  [{p.get('purpose', '')}]")
    if t == "llm.cache_hit":
        return "LLM", f"{p.get('agent')} -> {p['model']}  exact cache hit, saved {p.get('saved_tokens', 0):,} tokens"
    if t == "llm.error":
        return "WARN", f"{p.get('model')} {p.get('error')} status={p.get('status')} try {p.get('attempt')}: {_short(p.get('detail', ''), 140)}"
    if t == "llm.fallback":
        return "WARN", f"fallback {p.get('from')} -> {p.get('to')}"
    if t in ("llm.all_failed", "alert"):
        return "ALERT", _short(p, 220)
    if t == "llm.breaker_open":
        return "WARN", f"circuit open for {p.get('model')} ({p.get('cooldown_s')}s)"
    if t == "http.fetch":
        status = p.get("status") or "ERR"
        size = f"{p.get('bytes', 0) / 1024:.1f}KB"
        extra = " cache" if p.get("from_cache") else ""
        err = f"  {p['error']}" if p.get("error") else ""
        return "HTTP", f"{status} {p.get('url')} {size} {p.get('elapsed_s', 0):.2f}s{extra}{err}"
    if t.startswith("step."):
        return "STEP", f"{t[5:]:<8} {p.get('idx')}:{p.get('name', '')}" + (f" attempt {p['attempt']}" if "attempt" in p else "")
    if t.startswith("task."):
        rest = {k: v for k, v in p.items() if k not in ("steps",)}
        return "TASK", f"{t[5:]} {_short(rest, 180) if rest else ''}"
    if t.startswith("slot."):
        lvl = "WARN" if t == "slot.invalid" else "SLOT"
        return lvl, f"{t[5:]} {p.get('slot')} attempt {p.get('attempt', p.get('attempts'))} {_short(p.get('error', ''), 140)}"
    if t.startswith(("approval.", "policy.")):
        return "OWNER", f"{t} {p.get('action', '')} {_short({k: v for k, v in p.items() if k != 'action'}, 160)}"
    if t.startswith("budget."):
        return "BUDGET", f"{t} {_short(p, 160)}"
    if t == "quality.warning" or t.startswith("gate."):
        return "QUAL", _short(p, 200)
    return "EVENT", f"{t} {_short(p, 180)}"


def render(project: "Project", task_id: str) -> str:
    task = project.kernel.get(task_id)
    events = project.events.query(task_id=task_id)
    lines = [
        "=" * WIDTH,
        f"roj task log   project={project.name}   task={task_id}",
        f"workflow={task['workflow']}   status={task['status']}   created={task['created']}   updated={task['updated']}",
    ]
    if task.get("error"):
        lines.append(f"error: {_short(task['error'], 400)}")
    lines.append("=" * WIDTH)

    # steps with durations
    started: dict[int, str] = {}
    step_rows = []
    for e in events:
        if e.type == "step.started":
            started[e.payload["idx"]] = e.ts
        elif e.type in ("step.done",) and e.payload["idx"] in started:
            step_rows.append((e.payload["idx"], e.payload["name"], "done", _dur(started[e.payload["idx"]], e.ts)))
    for s in task["steps"]:
        if s["status"] != "done":
            step_rows.append((s["idx"], s["name"], s["status"], None))
    lines.append("STEPS")
    for idx, name, st, d in sorted(step_rows):
        lines.append(f"  {idx:>2} {name:<22} {st:<16} {'' if d is None else f'{d:8.2f}s'}")

    # model usage per agent and model
    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    http = defaultdict(int)
    errors = []
    for e in events:
        p = e.payload
        if e.type == "llm.call":
            a = agg[(p.get("agent", "?"), p["model"])]
            a["calls"] += 1
            a["in"] += p["prompt_tokens"]
            a["cached"] += p["cached_tokens"]
            a["out"] += p["completion_tokens"]
            a["usd"] += p["cost_usd"]
            a["s"] += p["latency_s"]
        elif e.type == "http.fetch":
            http["requests"] += 1
            http["errors"] += 1 if p.get("error") else 0
            http["bytes"] += p.get("bytes", 0)
        elif e.type in ("llm.error", "alert", "llm.all_failed", "task.failed", "slot.invalid", "adapter.rejected"):
            errors.append(f"{_ts(e)}  {e.type}: {_short(p, 200)}")
    lines.append("MODEL USAGE")
    tot = defaultdict(float)
    for (agent, model), a in sorted(agg.items()):
        lines.append(f"  {agent:<18} {model:<28} calls {int(a['calls']):>3}  in {int(a['in']):>8,}  cached {int(a['cached']):>8,}"
                     f"  out {int(a['out']):>7,}  ${a['usd']:.5f}  {a['s']:.1f}s")
        for k, v in a.items():
            tot[k] += v
    lines.append(f"  {'TOTAL':<47} calls {int(tot['calls']):>3}  in {int(tot['in']):>8,}  cached {int(tot['cached']):>8,}"
                 f"  out {int(tot['out']):>7,}  ${tot['usd']:.5f}")
    lines.append(f"HTTP  requests {http['requests']}  errors {http['errors']}  {http['bytes'] / 1024:.1f}KB")
    lines.append(f"PROBLEMS  {len(errors)}")
    lines += [f"  {x}" for x in errors[:50]]
    lines += ["-" * WIDTH, "TIMELINE"]
    for e in events:
        lvl, text = _line(e)
        lines.append(f"{_ts(e)} | {lvl:<6}| {text}")
    lines.append("=" * WIDTH)
    return "\n".join(lines) + "\n"


def task_log_path(project: "Project", task_id: str) -> Path:
    task = project.kernel.get(task_id)
    wf = task["workflow"].rsplit(":", 1)[-1].lower()
    mod = task["workflow"].split(":", 1)[0].rsplit(".", 2)
    name = f"{mod[-2]}-{wf}" if len(mod) >= 2 and mod[-1] == "workflows" else wf
    day = task["created"][:10]
    return project.inst.home / "logs" / project.name / day / f"{task_id[:8]}-{name}.log"


def write(project: "Project", task_id: str) -> Path:
    path = task_log_path(project, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(project, task_id), encoding="utf-8")
    with open(path.with_suffix(".jsonl"), "w", encoding="utf-8") as f:
        for e in project.events.query(task_id=task_id):
            f.write(json.dumps(e.model_dump(), ensure_ascii=False) + "\n")
    return path


def latest(project: "Project") -> Path | None:
    root = project.inst.home / "logs" / project.name
    files = sorted(root.glob("*/*.log"), key=lambda p: p.stat().st_mtime) if root.exists() else []
    return files[-1] if files else None
