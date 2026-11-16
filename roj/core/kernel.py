"""Durable workflow kernel: every completed step is a checkpoint.

Guarantees
- A completed step is never executed again for the same task (exactly-once per
  completed step). Its result lives in CAS and is replayed into later steps.
- A step interrupted by a crash runs again on resume (at-least-once). Model calls
  at temperature 0 go through the exact cache, so a replay costs nothing.
- Budget exhaustion and API outages pause a task instead of failing it; approvals
  park it until the owner decides. The kernel reacts to API problems, it does not
  try to work around them.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import socket
import sys
import threading
import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..contracts import new_id
from ..util import canonical_json, truncate, utcnow_iso
from .budget import BudgetExceeded
from .eventlog import CURRENT_TASK
from .policy import PolicyDenied, Verdict

if TYPE_CHECKING:
    from ..project import Project


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED_BUDGET = "paused_budget"
    PAUSED_API = "paused_api"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"


log = logging.getLogger("roj.kernel")

_ACTIVE: set[tuple[str, str]] = set()
_ACTIVE_LOCK = threading.Lock()
RESUMABLE = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
PAUSED = {TaskStatus.PAUSED_BUDGET, TaskStatus.PAUSED_API}

StepFn = Callable[["StepContext"], Awaitable[Any]]


@dataclass(frozen=True)
class Step:
    name: str
    fn: StepFn


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    description: str = ""

    def __post_init__(self) -> None:
        names = [s.name for s in self.steps]
        if len(set(names)) != len(names):
            raise ValueError(f"workflow {self.name}: step names must be unique")
        if not names:
            raise ValueError(f"workflow {self.name}: needs at least one step")


class WaitForApproval(Exception):
    def __init__(self, approval_id: str, action: str):
        self.approval_id, self.action = approval_id, action
        super().__init__(f"waiting for owner approval {approval_id} ({action})")


class TaskLocked(Exception):
    pass


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    results: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def resolve_workflow(ref: str) -> Workflow:
    """'package.module:ATTR' -> Workflow. ATTR may be a Workflow or a zero-arg factory."""
    if ":" not in ref:
        raise ValueError(f"workflow ref must look like 'package.module:NAME', got {ref!r}")
    mod_name, attr = ref.split(":", 1)
    obj = getattr(importlib.import_module(mod_name), attr)
    wf = obj() if callable(obj) and not isinstance(obj, Workflow) else obj
    if not isinstance(wf, Workflow):
        raise TypeError(f"{ref} is not a Workflow")
    return wf


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StepContext:
    """What a step sees: inputs, earlier results, the model stack and the policy."""

    def __init__(self, project: "Project", task_id: str, trace_id: str, step: str, idx: int,
                 attempt: int, inputs: dict[str, Any], results: dict[str, Any]):
        self.project = project
        self.task_id = task_id
        self.trace_id = trace_id
        self.step = step
        self.idx = idx
        self.attempt = attempt
        self.inputs = inputs
        self.results = results

    @property
    def cas(self):
        return self.project.cas

    def log(self, type: str, payload: dict[str, Any] | None = None) -> None:
        self.project.events.append(type, payload or {}, actor=f"step:{self.step}", task_id=self.task_id, trace_id=self.trace_id)

    async def chat(self, *, binding, messages, agent: str = "worker", scope: str = "", **kw):
        return await self.project.caller.chat(
            binding=binding, messages=messages, agent=agent, scope=scope,
            task_id=self.task_id, trace_id=self.trace_id, **kw,
        )

    async def memo(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Checkpoint inside a step: a finished sub-unit (team, graph node, round) is never redone."""
        row = self.project.store.one("SELECT ref FROM task_memo WHERE task_id = ? AND key = ?", (self.task_id, key))
        if row:
            return self.project.cas.get_json(row["ref"])
        value = await fn()
        ref = self.project.cas.put_json(value)
        self.project.store.run("INSERT OR REPLACE INTO task_memo(task_id, key, ref, created) VALUES(?,?,?,?)",
                               (self.task_id, key, ref, utcnow_iso()))
        self.log("memo.saved", {"key": key, "ref": ref})
        return json.loads(canonical_json(value))

    def require(self, action: str, details: dict[str, Any], *, amount: float | None = None, target: str | None = None) -> str | None:
        """Gate an action with side effects. Parks the task until the owner decides."""
        verdict, aid = self.project.policy.check(action, details, task_id=self.task_id, amount=amount, target=target)
        if verdict == Verdict.ASK:
            raise WaitForApproval(aid or "", action)
        return aid


class Kernel:
    def __init__(self, project: "Project"):
        self.project = project
        self.store = project.store
        self.events = project.events
        self.host = socket.gethostname()

    # --- submission ----------------------------------------------------------
    def submit(self, workflow_ref: str, inputs: dict[str, Any] | None = None) -> str:
        wf = resolve_workflow(workflow_ref)  # fail fast on a bad ref
        inputs = inputs or {}
        canonical_json(inputs)  # must be JSON-serialisable
        task_id = new_id()
        now = utcnow_iso()
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO tasks(id, workflow, inputs, status, created, updated) VALUES(?,?,?,?,?,?)",
                (task_id, workflow_ref, canonical_json(inputs), TaskStatus.PENDING, now, now),
            )
            self.events.append("task.submitted", {"workflow": workflow_ref, "name": wf.name, "steps": [s.name for s in wf.steps]},
                               task_id=task_id, trace_id=task_id)
        return task_id

    def get(self, task_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise KeyError(task_id)
        steps = self.store.all("SELECT idx, name, status, attempts FROM steps WHERE task_id = ? ORDER BY idx", (task_id,))
        return {**dict(row), "steps": [dict(s) for s in steps]}

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.store.all("SELECT * FROM tasks WHERE status = ? ORDER BY created DESC LIMIT ?", (status, limit))
        else:
            rows = self.store.all("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def cancel(self, task_id: str) -> None:
        self._set_status(task_id, TaskStatus.CANCELLED)
        self.events.append("task.cancelled", {}, actor="owner", task_id=task_id)

    # --- execution -------------------------------------------------------------
    def _claim(self, task_id: str) -> dict[str, Any]:
        with self.store.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            status = TaskStatus(row["status"])
            if status in (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED):
                raise TaskLocked(f"task {task_id} is {status}")
            if status == TaskStatus.RUNNING and row["owner_pid"] and row["owner_pid"] != os.getpid():
                if row["owner_host"] == self.host and _pid_alive(row["owner_pid"]):
                    raise TaskLocked(f"task {task_id} is running in pid {row['owner_pid']}")
            c.execute(
                "UPDATE tasks SET status = ?, owner_host = ?, owner_pid = ?, updated = ?, error = NULL WHERE id = ?",
                (TaskStatus.RUNNING, self.host, os.getpid(), utcnow_iso(), task_id),
            )
            resumed = status != TaskStatus.PENDING
            self.events.append("task.resumed" if resumed else "task.started", {"previous_status": status.value, "pid": os.getpid()},
                               task_id=task_id, trace_id=task_id)
            return dict(row)

    def _set_status(self, task_id: str, status: TaskStatus, error: str | None = None, result_ref: str | None = None) -> None:
        self.store.run(
            "UPDATE tasks SET status = ?, error = ?, result_ref = COALESCE(?, result_ref), updated = ? WHERE id = ?",
            (status, error, result_ref, utcnow_iso(), task_id),
        )

    async def run(self, task_id: str) -> TaskResult:
        """Run or continue a task. A readable log is (re)written whenever it stops."""
        key = (str(self.project.dir), task_id)
        with _ACTIVE_LOCK:  # the database lock compares pids; threads of one process (serve + panel) need this one
            if key in _ACTIVE:
                raise TaskLocked(f"task {task_id} is already running in this process")
            _ACTIVE.add(key)
        token = CURRENT_TASK.set(task_id)
        try:
            return await self._run(task_id)
        finally:
            CURRENT_TASK.reset(token)
            with _ACTIVE_LOCK:
                _ACTIVE.discard(key)
            try:
                self.project.write_task_log(task_id)
            except Exception:  # noqa: BLE001 - logging must never break the kernel
                log.exception("could not write task log for %s", task_id)

    async def _run(self, task_id: str) -> TaskResult:
        from ..models.errors import AllModelsFailed  # local import: models depends on core

        row = self._claim(task_id)
        wf = resolve_workflow(row["workflow"])
        inputs = json.loads(row["inputs"])
        done = {
            r["idx"]: r for r in self.store.all(
                "SELECT idx, name, status, attempts, result_ref FROM steps WHERE task_id = ?", (task_id,)
            )
        }
        results: dict[str, Any] = {}
        for idx, step in enumerate(wf.steps):
            prev = done.get(idx)
            if prev and prev["status"] == "done":
                if prev["name"] != step.name:
                    return self._fail(task_id, f"workflow changed: step {idx} was {prev['name']!r}, now {step.name!r}")
                results[step.name] = self.project.cas.get_json(prev["result_ref"])
                continue
            attempt = (prev["attempts"] if prev else 0) + 1
            with self.store.tx() as c:
                c.execute(
                    "INSERT INTO steps(task_id, idx, name, status, attempts, started) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(task_id, idx) DO UPDATE SET status='running', attempts=excluded.attempts,"
                    " started=excluded.started, error=NULL",
                    (task_id, idx, step.name, "running", attempt, utcnow_iso()),
                )
                self.events.append("step.started", {"idx": idx, "name": step.name, "attempt": attempt}, task_id=task_id, trace_id=task_id)
            ctx = StepContext(self.project, task_id, task_id, step.name, idx, attempt, inputs, dict(results))
            try:
                value = await step.fn(ctx)
            except WaitForApproval as w:
                self._park(task_id, idx, TaskStatus.WAITING_APPROVAL, "waiting", {"approval_id": w.approval_id, "action": w.action})
                return TaskResult(task_id, TaskStatus.WAITING_APPROVAL, results, str(w))
            except BudgetExceeded as b:
                self._park(task_id, idx, TaskStatus.PAUSED_BUDGET, "paused", {"scope": b.scope, "kind": b.kind, "limit": b.limit})
                return TaskResult(task_id, TaskStatus.PAUSED_BUDGET, results, str(b))
            except AllModelsFailed as a:
                self._park(task_id, idx, TaskStatus.PAUSED_API, "paused", {"errors": a.summary()})
                self.events.append("alert", {"kind": "api_unavailable", "task_id": task_id, "step": step.name, "errors": a.summary()},
                                   task_id=task_id)
                return TaskResult(task_id, TaskStatus.PAUSED_API, results, str(a))
            except PolicyDenied as p:
                return self._fail(task_id, f"policy denied: {p}", idx)
            except Exception as e:  # noqa: BLE001 - a step bug must not kill the kernel
                log.exception("step %s of task %s failed", step.name, task_id)
                tb = truncate("".join(traceback.format_exception_only(type(e), e)).strip(), 2000)
                return self._fail(task_id, f"step {step.name} raised: {tb}", idx)
            ref = self.project.cas.put_json(value)
            with self.store.tx() as c:
                c.execute(
                    "UPDATE steps SET status='done', result_ref=?, finished=? WHERE task_id=? AND idx=?",
                    (ref, utcnow_iso(), task_id, idx),
                )
                self.events.append("step.done", {"idx": idx, "name": step.name, "result_ref": ref}, task_id=task_id, trace_id=task_id)
            results[step.name] = json.loads(canonical_json(value))
        result_ref = self.project.cas.put_json(results)
        with self.store.tx():
            self._set_status(task_id, TaskStatus.DONE, result_ref=result_ref)
            self.events.append("task.done", {"result_ref": result_ref}, task_id=task_id, trace_id=task_id)
        return TaskResult(task_id, TaskStatus.DONE, results)

    def _park(self, task_id: str, idx: int, status: TaskStatus, step_status: str, info: dict[str, Any]) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE steps SET status=? WHERE task_id=? AND idx=?", (step_status, task_id, idx))
            self._set_status(task_id, status, error=canonical_json(info))
            self.events.append(f"task.{status.value}", {"idx": idx, **info}, task_id=task_id, trace_id=task_id)

    def _fail(self, task_id: str, error: str, idx: int | None = None) -> TaskResult:
        with self.store.tx() as c:
            if idx is not None:
                c.execute("UPDATE steps SET status='failed', error=? WHERE task_id=? AND idx=?", (error, task_id, idx))
            self._set_status(task_id, TaskStatus.FAILED, error=error)
            self.events.append("task.failed", {"idx": idx, "error": error}, task_id=task_id, trace_id=task_id)
        return TaskResult(task_id, TaskStatus.FAILED, error=error)

    async def resume(self, task_id: str | None = None, *, include_paused: bool = False) -> list[TaskResult]:
        """Continue interrupted work. Paused tasks (budget/API) only with include_paused."""
        statuses = set(RESUMABLE) | (PAUSED if include_paused else set())
        if task_id:
            ids = [task_id]
        else:
            ids = [r["id"] for r in self.store.all("SELECT id, status FROM tasks ORDER BY created") if TaskStatus(r["status"]) in statuses]
        out = []
        for tid in ids:
            row = self.store.one("SELECT status FROM tasks WHERE id = ?", (tid,))
            if row and TaskStatus(row["status"]) == TaskStatus.WAITING_APPROVAL and not self._approval_decided(tid):
                continue
            try:
                out.append(await self.run(tid))
            except TaskLocked:
                continue
        return out

    def _approval_decided(self, task_id: str) -> bool:
        row = self.store.one("SELECT error FROM tasks WHERE id = ?", (task_id,))
        info = json.loads(row["error"] or "{}") if row else {}
        aid = info.get("approval_id")
        if not aid:
            return True
        a = self.store.one("SELECT status, expires FROM approvals WHERE id = ?", (aid,))
        if not a:
            return True
        if a["status"] == "pending" and a["expires"] and a["expires"] < utcnow_iso():
            return True  # expired counts as "no", the step will see PolicyDenied
        return a["status"] != "pending"
