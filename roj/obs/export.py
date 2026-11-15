"""`roj logs export`: one zip with everything needed to review a run together.

Contents (secrets masked, API keys never present because they live in the environment):
  logs/                      task logs, roj.log, doctor logs (last N days)
  meta/versions.json         roj, python, OS, installed packages
  meta/settings.json         installation and project settings
  projects/<p>/tasks.json    tasks of the period with step states
  projects/<p>/health.json   event-log verification, budget, skills, source health, approvals, ratings
  projects/<p>/reports/      Markdown reports of the period (briefs, research reports)
  prompts/<p>/<task>/        request and response of every model call (unless --no-prompts)
"""

from __future__ import annotations

import importlib.metadata as md
import json
import platform
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..config import ProjectSettings
from ..core.eventlog import mask_secrets

if TYPE_CHECKING:
    from ..project import Installation


def _j(obj: Any) -> str:
    return json.dumps(mask_secrets(obj), ensure_ascii=False, indent=2, default=str)


def export_bundle(inst: "Installation", *, days: int = 3, with_prompts: bool = True,
                  projects: list[str] | None = None) -> Path:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()
    out = inst.home / "exports" / f"roj-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    names = projects or inst.project_names()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        logs = inst.home / "logs"
        if logs.exists():
            for f in logs.rglob("*"):
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) >= since:
                    z.write(f, f"logs/{f.relative_to(logs).as_posix()}")
        pbd = inst.home / "playbook"
        if pbd.exists():
            for f in pbd.rglob("*"):
                if f.is_file() and not f.name.startswith("."):  # .holdout is the secret seed of the hold-out cases
                    z.write(f, f"playbook/{f.relative_to(pbd).as_posix()}")
        z.writestr("meta/versions.json", _j({
            "roj": __version__, "python": sys.version, "platform": platform.platform(),
            "packages": sorted(f"{d.metadata['Name']}=={d.version}" for d in md.distributions()),
            "exported_at": datetime.now().isoformat(), "days": days, "with_prompts": with_prompts,
        }))
        settings = {"installation": inst.settings.model_dump(mode="json"),
                    "projects": {n: ProjectSettings.load(inst.home / "projects" / n).model_dump(mode="json") for n in names}}
        z.writestr("meta/settings.json", _j(settings))
        for name in names:
            p = inst.project(name)
            try:
                _project(z, p, since_iso, with_prompts)
            finally:
                p.close()
    return out


def _project(z: zipfile.ZipFile, p, since_iso: str, with_prompts: bool) -> None:
    from ..skills.catalog import SKILLS
    from ..skills.registry import SpecRegistry

    base = f"projects/{p.name}"
    tasks = [p.kernel.get(t["id"]) for t in p.store.all("SELECT id FROM tasks WHERE updated >= ? ORDER BY created", (since_iso,))]
    z.writestr(f"{base}/tasks.json", _j(tasks))
    reg = SpecRegistry(p)
    skills = {}
    for k in SKILLS:
        active = reg.active(k)
        skills[k] = {"active_version": active[0] if active else None, "active_spec": active[1] if active else None,
                     "versions": reg.versions(k), "source_health": reg.health(k)}
    ratings = [dict(r) for r in p.store.all("SELECT * FROM ratings WHERE created >= ?", (since_iso,))] \
        if p.store.one("SELECT name FROM sqlite_master WHERE name = 'ratings'") else []
    z.writestr(f"{base}/health.json", _j({
        "event_log": p.events.verify().model_dump(), "budget_today": p.budget.report(),
        "approvals_pending": p.policy.pending(), "skills": skills, "ratings": ratings,
    }))
    reports = p.dir / "reports"
    if reports.exists():
        for f in [*reports.glob("*.md"), *reports.glob("*.patch")]:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat() >= since_iso:
                z.write(f, f"{base}/reports/{f.name}")
    if with_prompts:
        for t in tasks:
            for e in p.events.query(type="llm.call", task_id=t["id"]):
                stem = f"prompts/{p.name}/{t['id'][:8]}/{e.seq:06d}-{e.payload.get('agent', 'agent')}"
                for kind in ("request_ref", "response_ref"):
                    ref = e.payload.get(kind)
                    if ref and p.cas.exists(ref):
                        z.writestr(f"{stem}-{kind.split('_')[0]}.json", _j(p.cas.get_json(ref)))
