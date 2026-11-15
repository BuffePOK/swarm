"""Compiled skill specs (per project) and the shared pool of generated code.

Spec versions:  candidate -> stable (owner activates) -> retired (kept for rollback)
Pool items:     draft -> candidate (passed automatic gates) -> stable (used successfully
                in >= 2 projects and >= POOL_STABLE_USES runs, or owner review) -> retired
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..util import canonical_json, sha256_hex, utcnow_iso

if TYPE_CHECKING:
    from ..project import Installation, Project

POOL_STABLE_USES = 5


class Status(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    STABLE = "stable"
    RETIRED = "retired"


class SpecRegistry:
    def __init__(self, project: "Project"):
        self.p = project
        self.store = project.store

    def add(self, skill: str, spec: dict[str, Any], *, created_by: str, task_id: str | None = None,
            notes: str = "", status: Status = Status.CANDIDATE) -> int:
        ref = self.p.cas.put_json(spec)
        with self.store.tx() as c:
            row = c.execute("SELECT COALESCE(MAX(version), 0) v FROM skill_specs WHERE skill = ?", (skill,)).fetchone()
            version = row["v"] + 1
            c.execute(
                "INSERT INTO skill_specs(skill, version, status, spec_ref, created, created_by, task_id, notes)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (skill, version, status, ref, utcnow_iso(), created_by, task_id, notes[:2000]),
            )
            self.p.events.append("skill.spec_added", {"skill": skill, "version": version, "status": status, "spec_ref": ref},
                                 actor=created_by, task_id=task_id)
        return version

    def get(self, skill: str, version: int) -> dict[str, Any]:
        row = self.store.one("SELECT spec_ref FROM skill_specs WHERE skill = ? AND version = ?", (skill, version))
        if not row:
            raise KeyError(f"{skill} v{version}")
        return self.p.cas.get_json(row["spec_ref"])

    def active(self, skill: str) -> tuple[int, dict[str, Any]] | None:
        row = self.store.one("SELECT active_version FROM skill_state WHERE skill = ?", (skill,))
        if not row or row["active_version"] is None:
            return None
        return row["active_version"], self.get(skill, row["active_version"])

    def activate(self, skill: str, version: int, *, by: str) -> None:
        self.get(skill, version)
        with self.store.tx() as c:
            c.execute("UPDATE skill_specs SET status = 'retired' WHERE skill = ? AND status = 'stable'", (skill,))
            c.execute("UPDATE skill_specs SET status = 'stable' WHERE skill = ? AND version = ?", (skill, version))
            c.execute(
                "INSERT INTO skill_state(skill, active_version, updated) VALUES(?,?,?)"
                " ON CONFLICT(skill) DO UPDATE SET active_version = excluded.active_version, updated = excluded.updated",
                (skill, version, utcnow_iso()),
            )
            self.p.events.append("skill.activated", {"skill": skill, "version": version}, actor=by)

    def versions(self, skill: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.all(
            "SELECT version, status, created, created_by, task_id, notes FROM skill_specs WHERE skill = ? ORDER BY version", (skill,))]

    def last_window_end(self, skill: str) -> str | None:
        row = self.store.one("SELECT last_window_end FROM skill_state WHERE skill = ?", (skill,))
        return row["last_window_end"] if row else None

    def mark_run(self, skill: str, task_id: str, window_end: str) -> None:
        self.store.run(
            "INSERT INTO skill_state(skill, last_run_task, last_window_end, updated) VALUES(?,?,?,?)"
            " ON CONFLICT(skill) DO UPDATE SET last_run_task = excluded.last_run_task,"
            " last_window_end = excluded.last_window_end, updated = excluded.updated",
            (skill, task_id, window_end, utcnow_iso()),
        )

    # --- per-source health -------------------------------------------------------
    def record_health(self, skill: str, source_id: str, ok: bool, error: str | None = None) -> int:
        now = utcnow_iso()
        with self.store.tx() as c:
            row = c.execute("SELECT consecutive_failures FROM source_health WHERE skill = ? AND source_id = ?",
                            (skill, source_id)).fetchone()
            fails = 0 if ok else ((row["consecutive_failures"] if row else 0) + 1)
            c.execute(
                "INSERT INTO source_health(skill, source_id, consecutive_failures, last_ok, last_error, updated)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(skill, source_id) DO UPDATE SET"
                " consecutive_failures = excluded.consecutive_failures,"
                " last_ok = COALESCE(excluded.last_ok, source_health.last_ok),"
                " last_error = excluded.last_error, updated = excluded.updated",
                (skill, source_id, fails, now if ok else None, None if ok else (error or "")[:300], now),
            )
        return fails

    def health(self, skill: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.all("SELECT * FROM source_health WHERE skill = ? ORDER BY source_id", (skill,))]


class Pool:
    """Installation-wide pool of generated algorithms (adapters, rule sets)."""

    def __init__(self, inst: "Installation"):
        self.inst = inst
        self.store = inst.store

    def submit(self, kind: str, name: str, code: str, *, card: dict[str, Any], tests: dict[str, Any],
               created_by: str, project: str, status: Status = Status.CANDIDATE) -> dict[str, Any]:
        sha = sha256_hex(code)
        existing = self.store.one("SELECT * FROM pool_items WHERE kind = ? AND sha256 = ?", (kind, sha))
        if existing:
            return dict(existing)
        with self.store.tx() as c:
            row = c.execute("SELECT COALESCE(MAX(version), 0) v FROM pool_items WHERE kind = ? AND name = ?", (kind, name)).fetchone()
            version = row["v"] + 1
            c.execute(
                "INSERT INTO pool_items(kind, name, version, status, sha256, code, card, tests, projects, created, created_by)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, name, version, status, sha, code, canonical_json(card), canonical_json(tests),
                 json.dumps([project]), utcnow_iso(), created_by),
            )
            self.inst.events.append("pool.submitted", {"kind": kind, "name": name, "version": version, "sha256": sha,
                                                       "status": status, "project": project}, actor=created_by)
        return dict(self.store.one("SELECT * FROM pool_items WHERE kind = ? AND name = ? AND version = ?", (kind, name, version)))

    def by_sha(self, sha: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT * FROM pool_items WHERE sha256 = ?", (sha,))
        return dict(row) if row else None

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT kind, name, version, status, sha256, uses, projects, created, created_by FROM pool_items"
        rows = self.store.all(q + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, name, version", (kind,) if kind else ())
        return [dict(r) for r in rows]

    def record_use(self, sha: str, project: str) -> None:
        row = self.by_sha(sha)
        if not row:
            return
        projects = sorted(set(json.loads(row["projects"])) | {project})
        status = row["status"]
        if status == Status.CANDIDATE and row["uses"] + 1 >= POOL_STABLE_USES and len(projects) >= 2:
            status = Status.STABLE
            self.inst.events.append("pool.promoted", {"name": row["name"], "version": row["version"], "reason": "uses"})
        self.store.run("UPDATE pool_items SET uses = uses + 1, projects = ?, status = ? WHERE sha256 = ?",
                       (json.dumps(projects), status, sha))

    def set_status(self, sha: str, status: Status, by: str = "owner") -> None:
        self.store.run("UPDATE pool_items SET status = ? WHERE sha256 = ?", (status, sha))
        self.inst.events.append("pool.status", {"sha256": sha, "status": status}, actor=by)
