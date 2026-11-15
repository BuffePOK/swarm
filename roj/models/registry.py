"""Model registry: discovery via GET /models, availability, persistent blocks.

Shared by all projects of an installation (lives in the installation store).
Probes and evals (quality, latency, cache support per model) arrive in Phase 1;
the Architect assigns concrete models and fallbacks using this data.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from ..core.eventlog import EventLog
from ..core.store import Store
from ..util import canonical_json, utcnow, utcnow_iso
from .client import GatewayClient


class CircuitBreaker:
    """In-process breaker: N consecutive failures open the model for a cooldown."""

    def __init__(self, threshold: int, cooldown_s: float):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def allow(self, model: str) -> bool:
        return time.monotonic() >= self._open_until.get(model, 0.0)

    def success(self, model: str) -> None:
        self._fails[model] = 0
        self._open_until.pop(model, None)

    def failure(self, model: str) -> bool:
        self._fails[model] = self._fails.get(model, 0) + 1
        if self._fails[model] >= self.threshold:
            self._open_until[model] = time.monotonic() + self.cooldown_s
            return True
        return False


class ModelRegistry:
    def __init__(self, store: Store, events: EventLog, client: GatewayClient):
        self.store = store
        self.events = events
        self.client = client

    async def refresh(self) -> dict[str, Any]:
        items = await self.client.list_models()
        ids = sorted({str(m.get("id")) for m in items if m.get("id")})
        now = utcnow_iso()
        known = {r["id"]: r for r in self.store.all("SELECT id, status FROM models")}
        added, returned, removed = [], [], []
        with self.store.tx() as c:
            for mid in ids:
                meta = canonical_json(next((m for m in items if m.get("id") == mid), {}))
                if mid not in known:
                    c.execute("INSERT INTO models(id, status, first_seen, last_seen, meta) VALUES(?, 'available', ?, ?, ?)",
                              (mid, now, now, meta))
                    added.append(mid)
                else:
                    if known[mid]["status"] == "gone":
                        returned.append(mid)
                    c.execute("UPDATE models SET last_seen = ?, meta = ?, status = CASE WHEN status = 'gone' THEN 'available' ELSE status END"
                              " WHERE id = ?", (now, meta, mid))
            for mid, row in known.items():
                if mid not in ids and row["status"] != "gone":
                    c.execute("UPDATE models SET status = 'gone' WHERE id = ?", (mid,))
                    removed.append(mid)
            self.events.append("models.refreshed", {"total": len(ids), "added": added, "removed": removed, "returned": returned})
        return {"added": added, "removed": removed, "returned": returned, "total": len(ids)}

    def list(self) -> list[dict]:
        rows = self.store.all("SELECT id, status, first_seen, last_seen, blocked_until, block_reason FROM models ORDER BY id")
        out = []
        for r in rows:
            d = dict(r)
            if d["blocked_until"] and d["blocked_until"] < utcnow_iso():
                d["blocked_until"], d["block_reason"] = None, None
            out.append(d)
        return out

    def known(self, model: str) -> bool:
        return self.store.one("SELECT 1 FROM models WHERE id = ? AND status != 'gone'", (model,)) is not None

    def block_info(self, model: str) -> tuple[bool, str | None, str | None]:
        row = self.store.one("SELECT blocked_until, block_reason FROM models WHERE id = ?", (model,))
        if row and row["blocked_until"] and row["blocked_until"] > utcnow_iso():
            return True, row["block_reason"], row["blocked_until"]
        return False, None, None

    def block(self, model: str, seconds: float, reason: str) -> None:
        until = (utcnow() + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
        now = utcnow_iso()
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO models(id, status, first_seen, last_seen, blocked_until, block_reason) VALUES(?, 'available', ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET blocked_until = excluded.blocked_until, block_reason = excluded.block_reason",
                (model, now, now, until, reason[:300]),
            )
            self.events.append("models.blocked", {"model": model, "until": until, "reason": reason[:300]})

    def unblock(self, model: str, by: str = "owner") -> None:
        with self.store.tx():
            self.store.run("UPDATE models SET blocked_until = NULL, block_reason = NULL WHERE id = ?", (model,))
            self.events.append("models.unblocked", {"model": model}, actor=by)

    def mark_gone(self, model: str) -> None:
        with self.store.tx():
            self.store.run("UPDATE models SET status = 'gone' WHERE id = ?", (model,))
            self.events.append("models.gone", {"model": model})

