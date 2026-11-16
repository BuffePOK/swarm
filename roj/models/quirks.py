"""Per-model request quirks, learned from the provider's own 400 errors.

Providers change what they accept: a model stops taking `temperature`, another requires
`top_p = 1` for greedy sampling, a reasoning model wants `max_completion_tokens`. Writing
these down by hand never ends. Instead the client reads the error, applies one of a few
safe fixes, retries, and remembers the fix for that model (installation-wide). Only sampling
and limit parameters are ever touched; the messages, tools and schema never are.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..util import utcnow_iso

if TYPE_CHECKING:
    from ..core.eventlog import EventLog
    from ..core.store import Store

DROPPABLE = {"temperature", "top_p", "top_k", "seed", "presence_penalty", "frequency_penalty", "parallel_tool_calls",
             "logprobs", "top_logprobs", "n", "user", "stream_options", "reasoning_effort", "stop"}
_P = r"[`'\"]?([a-z_]+)[`'\"]?"
RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"top_p must be 1"), "set:top_p=1"),
    (re.compile(r"temperature.{0,40}(?:and|with).{0,10}top_p.{0,40}(?:cannot|can't|not).{0,20}both"), "drop:top_p"),
    (re.compile(r"max_tokens.{0,80}max_completion_tokens"), "rename:max_tokens=max_completion_tokens"),
    (re.compile(_P + r" is deprecated"), "drop:{0}"),
    (re.compile(r"unsupported (?:parameter|value):? " + _P), "drop:{0}"),
    (re.compile(r"unrecognized request argument supplied:? " + _P), "drop:{0}"),
    (re.compile(r"unknown (?:parameter|field|argument):? " + _P), "drop:{0}"),
    (re.compile(_P + r" (?:is|are) not (?:supported|allowed|permitted)"), "drop:{0}"),
    (re.compile(r"does not support (?:the )?(?:parameter )?" + _P), "drop:{0}"),
    (re.compile(_P + r".{0,40}only the default \(1\) value is supported"), "drop:{0}"),
]


def diagnose(error_text: str) -> dict[str, Any] | None:
    """-> {"drop": [...]} | {"set": {...}} | {"rename": {...}} for a 400 error we know how to fix, else None."""
    low = error_text.lower()
    for rx, action in RULES:
        m = rx.search(low)
        if not m:
            continue
        kind, arg = action.split(":", 1)
        arg = arg.format(*m.groups()) if m.groups() else arg
        if kind == "drop" and arg in DROPPABLE:
            return {"drop": [arg]}
        if kind == "set":
            k, v = arg.split("=")
            return {"set": {k: float(v) if "." in v else int(v)}}
        if kind == "rename":
            a, b = arg.split("=")
            return {"rename": {a: b}}
    return None


def merge(q: dict[str, Any], fix: dict[str, Any]) -> dict[str, Any]:
    out = {"drop": sorted(set(q.get("drop", [])) | set(fix.get("drop", []))), "set": {**q.get("set", {}), **fix.get("set", {})},
           "rename": {**q.get("rename", {}), **fix.get("rename", {})}}
    return {k: v for k, v in out.items() if v}


def apply(params: dict[str, Any], q: dict[str, Any] | None) -> dict[str, Any]:
    if not q:
        return params
    out = {k: v for k, v in params.items() if k not in set(q.get("drop", []))}
    for a, b in q.get("rename", {}).items():
        if a in out:
            out[b] = out.pop(a)
    out.update(q.get("set", {}))
    return out


class QuirkStore:
    """Installation-wide memory of quirks, in the kv table (key quirk:<model>)."""

    def __init__(self, store: "Store", events: "EventLog | None" = None):
        self.store, self.events = store, events
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, model: str) -> dict[str, Any]:
        if model not in self._cache:
            row = self.store.one("SELECT value FROM kv WHERE key = ?", (f"quirk:{model}",))
            self._cache[model] = json.loads(row["value"]) if row else {}
        return self._cache[model]

    def learn(self, model: str, fix: dict[str, Any], error: str) -> dict[str, Any]:
        q = merge(self.get(model), fix)
        self.store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES(?,?,?)", (f"quirk:{model}", json.dumps(q), utcnow_iso()))
        self._cache[model] = q
        if self.events:
            self.events.append("llm.quirk_learned", {"model": model, "fix": fix, "now": q, "error": error[:300]}, actor="client")
        return q

    def all(self) -> dict[str, dict[str, Any]]:
        return {r["key"][6:]: json.loads(r["value"]) for r in self.store.all("SELECT key, value FROM kv WHERE key LIKE 'quirk:%' ORDER BY key")}

    def forget(self, model: str | None = None) -> int:
        self._cache.clear()
        if model:
            return self.store.run("DELETE FROM kv WHERE key = ?", (f"quirk:{model}",)).rowcount
        return self.store.run("DELETE FROM kv WHERE key LIKE 'quirk:%'").rowcount
