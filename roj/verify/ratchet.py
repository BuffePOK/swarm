"""Quality ratchet: eval suites whose thresholds only go up on their own (concept §14).

- The first measurement sets the baseline (value - margin).
- After `streak_to_raise` consecutive results at least `margin` above the threshold,
  the threshold rises to (latest value - margin). Never down by itself.
- Lowering is an owner action with a mandatory note, logged as an event.
- Cases are append-only. A deterministic ~20% of them are hidden: code evaluates on
  them, but they are never shown to agents (protection against learning the test).
- Escaped defects are added as new cases (`add_cases(..., source="escaped")`).
This package is owner-only: agents may propose cases and stricter gates, never edit it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..util import canonical_json, sha256_hex, utcnow_iso

if TYPE_CHECKING:
    from ..project import Project


@dataclass
class GateResult:
    suite: str
    metric: str
    value: float
    threshold: float | None
    passed: bool
    raised_to: float | None = None


class Ratchet:
    def __init__(self, project: "Project", *, margin: float = 0.02, streak_to_raise: int = 3, hidden_share: float = 0.2):
        self.p = project
        self.store = project.store
        self.margin = margin
        self.streak_to_raise = streak_to_raise
        self.hidden_share = hidden_share

    # --- cases ---------------------------------------------------------------------
    def add_cases(self, suite: str, cases: list[dict[str, Any]], *, source: str) -> int:
        added = 0
        with self.store.tx() as c:
            for data in cases:
                cid = data.get("case_id") or sha256_hex(canonical_json(data))[:16]
                hidden = int(sha256_hex(f"{suite}|{cid}")[:4], 16) < int(0x10000 * self.hidden_share)
                cur = c.execute("INSERT OR IGNORE INTO eval_cases(suite, case_id, data, hidden, source, created) VALUES(?,?,?,?,?,?)",
                                (suite, cid, canonical_json(data), int(hidden), source, utcnow_iso()))
                added += cur.rowcount
            if added:
                self.p.events.append("gate.cases_added", {"suite": suite, "added": added, "source": source})
        return added

    def cases(self, suite: str, *, visible_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT case_id, data, hidden FROM eval_cases WHERE suite = ?" + (" AND hidden = 0" if visible_only else "")
        return [{**json.loads(r["data"]), "case_id": r["case_id"], "hidden": bool(r["hidden"])}
                for r in self.store.all(q + " ORDER BY case_id", (suite,))]

    # --- thresholds --------------------------------------------------------------------
    def threshold(self, suite: str, metric: str) -> float | None:
        row = self.store.one("SELECT threshold FROM gates WHERE suite = ? AND metric = ?", (suite, metric))
        return row["threshold"] if row else None

    def check(self, suite: str, metric: str, value: float) -> bool:
        th = self.threshold(suite, metric)
        return th is None or value >= th - 1e-9

    def evaluate(self, suite: str, metric: str, value: float, *, ref: str | None = None) -> GateResult:
        with self.store.tx() as c:
            row = c.execute("SELECT threshold, streak FROM gates WHERE suite = ? AND metric = ?", (suite, metric)).fetchone()
            raised = None
            if row is None:
                th = max(0.0, value - self.margin)
                c.execute("INSERT INTO gates(suite, metric, threshold, streak, updated) VALUES(?,?,?,?,?)",
                          (suite, metric, th, 0, utcnow_iso()))
                passed, threshold = True, th
            else:
                threshold = row["threshold"]
                passed = value >= threshold - 1e-9
                streak = row["streak"] + 1 if passed and value >= threshold + self.margin else 0
                if streak >= self.streak_to_raise:
                    raised = round(value - self.margin, 6)
                    threshold, streak = max(threshold, raised), 0
                c.execute("UPDATE gates SET threshold = ?, streak = ?, updated = ? WHERE suite = ? AND metric = ?",
                          (threshold, streak, utcnow_iso(), suite, metric))
            c.execute("INSERT INTO gate_history(suite, metric, value, threshold, passed, ref, created) VALUES(?,?,?,?,?,?,?)",
                      (suite, metric, value, threshold, int(passed), ref, utcnow_iso()))
            self.p.events.append("gate.evaluated", {"suite": suite, "metric": metric, "value": round(value, 4),
                                                    "threshold": round(threshold, 4), "passed": passed})
            if raised is not None:
                self.p.events.append("gate.raised", {"suite": suite, "metric": metric, "threshold": raised})
        return GateResult(suite, metric, value, threshold, passed, raised)

    def lower(self, suite: str, metric: str, new_threshold: float, *, note: str, by: str = "owner") -> None:
        if not note.strip():
            raise ValueError("lowering a gate requires a note (it becomes part of the audit trail)")
        current = self.threshold(suite, metric)
        if current is not None and new_threshold >= current:
            raise ValueError("lower() only lowers; thresholds rise on their own")
        self.store.run("INSERT INTO gates(suite, metric, threshold, streak, updated) VALUES(?,?,?,0,?)"
                       " ON CONFLICT(suite, metric) DO UPDATE SET threshold = excluded.threshold, streak = 0, updated = excluded.updated",
                       (suite, metric, new_threshold, utcnow_iso()))
        self.p.events.append("gate.lowered", {"suite": suite, "metric": metric, "from": current, "to": new_threshold, "note": note}, actor=by)

    def report(self) -> list[dict[str, Any]]:
        rows = self.store.all("SELECT suite, metric, threshold, streak, updated FROM gates ORDER BY suite, metric")
        out = []
        for r in rows:
            last = self.store.one("SELECT value, passed FROM gate_history WHERE suite = ? AND metric = ? ORDER BY id DESC LIMIT 1",
                                  (r["suite"], r["metric"]))
            n = self.store.one("SELECT COUNT(*) n, SUM(hidden) h FROM eval_cases WHERE suite = ?", (r["suite"],))
            out.append({**dict(r), "last_value": last["value"] if last else None, "last_passed": bool(last["passed"]) if last else None,
                        "cases": n["n"], "hidden": n["h"] or 0})
        return out
