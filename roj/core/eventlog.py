"""Append-only event log with a hash chain.

Every event stores the hash of the previous one, so editing or deleting a past
event breaks the chain and `verify()` reports the first broken sequence number.
Secrets are masked before anything is written.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel

from ..util import ZERO_HASH, canonical_json, sha256_hex, utcnow_iso
from .store import Store

# Set by the kernel while a step runs: every event (HTTP, slots, model calls) is attributed
# to the task without threading task_id through every function.
CURRENT_TASK: ContextVar[str | None] = ContextVar("roj_current_task", default=None)

_SECRET_KEYS = re.compile(r"(api[_-]?key|authorization|password|passwd|secret|token(?!s)|cookie)", re.I)
_SECRET_VALUES = re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|AQ\.[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,})")


def mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if isinstance(k, str) and _SECRET_KEYS.search(k) and not isinstance(v, (int, float, bool)) else mask_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUES.sub("***", obj)
    return obj


class Event(BaseModel):
    seq: int
    ts: str
    type: str
    actor: str
    trace_id: str | None
    task_id: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str


class VerifyResult(BaseModel):
    ok: bool
    count: int
    first_bad_seq: int | None = None
    reason: str | None = None


def _event_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(body))


class EventLog:
    def __init__(self, store: Store):
        self.store = store

    def append(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "kernel",
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> Event:
        payload = mask_secrets(payload or {})
        task_id = task_id or CURRENT_TASK.get()
        trace_id = trace_id or task_id
        with self.store.tx() as c:
            last = c.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["hash"] if last else ZERO_HASH
            body = {
                "seq": seq,
                "ts": utcnow_iso(),
                "type": type,
                "actor": actor,
                "trace_id": trace_id,
                "task_id": task_id,
                "payload": payload,
            }
            h = _event_hash(prev, body)
            c.execute(
                "INSERT INTO events(seq, ts, type, actor, trace_id, task_id, payload, prev_hash, hash)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (seq, body["ts"], type, actor, trace_id, task_id, canonical_json(payload), prev, h),
            )
        return Event(prev_hash=prev, hash=h, **body)

    def verify(self) -> VerifyResult:
        prev = ZERO_HASH
        expected_seq = 1
        count = 0
        for row in self.store.all("SELECT * FROM events ORDER BY seq"):
            count += 1
            if row["seq"] != expected_seq:
                return VerifyResult(ok=False, count=count, first_bad_seq=expected_seq, reason="missing event")
            if row["prev_hash"] != prev:
                return VerifyResult(ok=False, count=count, first_bad_seq=row["seq"], reason="broken link")
            body = {
                "seq": row["seq"],
                "ts": row["ts"],
                "type": row["type"],
                "actor": row["actor"],
                "trace_id": row["trace_id"],
                "task_id": row["task_id"],
                "payload": json.loads(row["payload"]),
            }
            if _event_hash(prev, body) != row["hash"]:
                return VerifyResult(ok=False, count=count, first_bad_seq=row["seq"], reason="content changed")
            prev = row["hash"]
            expected_seq += 1
        return VerifyResult(ok=True, count=count)

    def head(self) -> str:
        row = self.store.one("SELECT hash FROM events ORDER BY seq DESC LIMIT 1")
        return row["hash"] if row else ZERO_HASH

    def query(
        self,
        *,
        type: str | None = None,
        type_prefix: str | None = None,
        task_id: str | None = None,
        since_seq: int = 0,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [since_seq]
        if type:
            sql += " AND type = ?"
            params.append(type)
        if type_prefix:
            sql += " AND substr(type, 1, ?) = ?"
            params += [len(type_prefix), type_prefix]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY seq " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            Event(
                seq=r["seq"], ts=r["ts"], type=r["type"], actor=r["actor"], trace_id=r["trace_id"],
                task_id=r["task_id"], payload=json.loads(r["payload"]), prev_hash=r["prev_hash"], hash=r["hash"],
            )
            for r in self.store.all(sql, tuple(params))
        ]
