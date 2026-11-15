"""Blueprint versions: lint -> propose -> owner approval (by control mode) -> activate + ADR.

Control modes (concept §3.5):
  manual / assist   every change waits for the owner (t2.blueprint_change)
  auto              applied directly, but only after the Leader earned it: at least 20
                    decided proposals with >= 90% approved; until then it behaves as assist
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

import yaml

from ..core.policy import Verdict
from ..prompting.linter import Issue, errors
from ..util import utcnow_iso
from .linter import lint
from .schema import Blueprint

if TYPE_CHECKING:
    from ..project import Project

AUTO_WINDOW, AUTO_RATE = 20, 0.9


class BlueprintInvalid(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in errors(issues)))


def parse(text: str) -> Blueprint:
    return Blueprint.model_validate(yaml.safe_load(text))


def dump(bp: Blueprint) -> str:
    return yaml.safe_dump(bp.model_dump(mode="json", exclude_defaults=False), sort_keys=False, allow_unicode=True)


def active(project: "Project") -> tuple[int, Blueprint] | None:
    row = project.store.one("SELECT version, ref FROM blueprints WHERE status = 'active' ORDER BY version DESC LIMIT 1")
    return (row["version"], Blueprint.model_validate(project.cas.get_json(row["ref"]))) if row else None


def auto_unlocked(project: "Project") -> tuple[bool, str]:
    rows = project.store.all("SELECT approved_without_edits FROM blueprints WHERE created_by != 'owner' AND approved_without_edits IS NOT NULL "
                             "ORDER BY version DESC LIMIT ?", (AUTO_WINDOW,))
    if len(rows) < AUTO_WINDOW:
        return False, f"{len(rows)}/{AUTO_WINDOW} decided proposals"
    rate = sum(r["approved_without_edits"] for r in rows) / len(rows)
    return rate >= AUTO_RATE, f"approval rate {rate:.0%} over the last {AUTO_WINDOW}"


def propose(project: "Project", bp: Blueprint, *, by: str = "owner") -> dict[str, Any]:
    issues = lint(bp, project)
    if errors(issues):
        raise BlueprintInvalid(issues)
    prev = active(project)
    diff = "".join(difflib.unified_diff(dump(prev[1]).splitlines(True) if prev else [], dump(bp).splitlines(True),
                                        f"v{prev[0]}" if prev else "empty", "proposed"))
    ref = project.cas.put_json(bp.model_dump(mode="json"))
    with project.store.tx() as c:
        v = c.execute("SELECT COALESCE(MAX(version), 0) + 1 v FROM blueprints").fetchone()["v"]
        c.execute("INSERT INTO blueprints(version, ref, status, created, created_by, diff) VALUES(?,?, 'proposed', ?,?,?)",
                  (v, ref, utcnow_iso(), by, diff))
        project.events.append("blueprint.proposed", {"version": v, "by": by, "warnings": [i.message for i in issues]}, actor=by)
    out: dict[str, Any] = {"version": v, "warnings": [f"{i.code}: {i.message}" for i in issues], "diff_lines": diff.count("\n")}
    unlocked, why = auto_unlocked(project)
    if bp.control_mode == "auto" and unlocked:
        activate(project, v, by="leader:auto", approval_required=False)
        out["status"] = "active"
        out["note"] = f"auto mode ({why})"
        return out
    verdict, aid = project.policy.check("t2.blueprint_change", {"version": v, "by": by, "teams": [t.name for t in bp.teams],
                                                                "diff": diff[-3000:]})
    out["status"] = "approved" if verdict == Verdict.ALLOW else "waiting_approval"
    out["approval_id"] = aid
    if bp.control_mode == "auto":
        out["note"] = f"auto not unlocked yet ({why}): owner approval required"
    return out


def activate(project: "Project", version: int, *, by: str = "owner", approval_required: bool = True) -> dict[str, Any]:
    row = project.store.one("SELECT * FROM blueprints WHERE version = ?", (version,))
    if not row:
        raise KeyError(f"no blueprint version {version}")
    if approval_required:
        bp = Blueprint.model_validate(project.cas.get_json(row["ref"]))
        verdict, _ = project.policy.check("t2.blueprint_change", {"version": version, "by": row["created_by"],
                                                                  "teams": [t.name for t in bp.teams], "diff": (row["diff"] or "")[-3000:]})
        if verdict != Verdict.ALLOW:
            raise ValueError(f"blueprint v{version} is waiting for owner approval (roj approvals list -p {project.name})")
    with project.store.tx() as c:
        c.execute("UPDATE blueprints SET status = 'superseded' WHERE status = 'active'")
        c.execute("UPDATE blueprints SET status = 'active', approved_without_edits = 1 WHERE version = ?", (version,))
        project.events.append("blueprint.activated", {"version": version}, actor=by)
    adr_dir = project.dir / "adr"
    adr_dir.mkdir(exist_ok=True)
    path = adr_dir / f"blueprint-v{version:03d}.md"
    path.write_text(f"# Blueprint v{version}\n\n- Status: active\n- Proposed by: {row['created_by']}\n- Activated by: {by}\n"
                    f"- Date: {utcnow_iso()}\n\n## Change\n\n```diff\n{row['diff'] or '(first version)'}\n```\n", encoding="utf-8")
    return {"version": version, "status": "active", "adr": str(path)}


def reject(project: "Project", version: int, *, by: str = "owner") -> None:
    project.store.run("UPDATE blueprints SET status = 'rejected', approved_without_edits = 0 WHERE version = ?", (version,))
    project.events.append("blueprint.rejected", {"version": version}, actor=by)
