"""Permission tiers, owner-controlled flags and the approval queue.

Action names start with their tier: "t3.send_message", "t4.payment".
Defaults: T0/T1 auto, T2-T4 manual. T4 can never be plain "auto"; only
"auto_with_limits" (per-action and per-day amount caps plus an allow-list).
No answer before expiry means "no".
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, model_validator

from ..contracts import RiskTier
from ..util import canonical_json, day_key, sha256_hex, utcnow, utcnow_iso
from .eventlog import EventLog
from .store import Store


class Mode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    AUTO_WITH_LIMITS = "auto_with_limits"
    DENY = "deny"


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PolicyConfigError(ValueError):
    pass


class PolicyDenied(Exception):
    pass


class Rule(BaseModel):
    mode: Mode
    per_action: float | None = None
    per_day: float | None = None
    allow: list[str] | None = None  # glob patterns for targets (domains, recipients)

    @model_validator(mode="after")
    def _limits_present(self) -> "Rule":
        if self.mode == Mode.AUTO_WITH_LIMITS and self.per_action is None and self.per_day is None and not self.allow:
            raise ValueError("auto_with_limits needs per_action, per_day or allow")
        return self


class Decision(BaseModel):
    verdict: Verdict
    reason: str
    rule_key: str


TIER_DEFAULTS: dict[RiskTier, Mode] = {
    RiskTier.T0: Mode.AUTO,
    RiskTier.T1: Mode.AUTO,
    RiskTier.T2: Mode.MANUAL,
    RiskTier.T3: Mode.MANUAL,
    RiskTier.T4: Mode.MANUAL,
}


def tier_of(action: str) -> RiskTier:
    prefix = action.split(".", 1)[0].lower()
    try:
        return RiskTier(prefix)
    except ValueError as e:
        raise PolicyConfigError(f"action {action!r} must start with a tier: t0..t4") from e


def parse_rules(raw: dict[str, Any]) -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    for key, value in raw.items():
        tier = tier_of(key)
        rule = Rule(mode=Mode(value)) if isinstance(value, str) else Rule.model_validate(value)
        if tier == RiskTier.T4 and rule.mode == Mode.AUTO:
            raise PolicyConfigError(f"{key}: T4 cannot be plain 'auto'; use auto_with_limits with caps")
        if tier == RiskTier.T4 and rule.mode == Mode.AUTO_WITH_LIMITS and (rule.per_action is None or rule.per_day is None):
            raise PolicyConfigError(f"{key}: T4 auto_with_limits needs both per_action and per_day")
        rules[key] = rule
    return rules


def approval_key(task_id: str | None, action: str, details: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({"task": task_id, "action": action, "details": details}))


class Policy:
    def __init__(self, raw: dict[str, Any], store: Store, events: EventLog, tz: str, approval_ttl_s: float = 86400):
        self.rules = parse_rules(raw)
        self.store = store
        self.events = events
        self.tz = tz
        self.approval_ttl_s = approval_ttl_s

    def _rule_for(self, action: str) -> tuple[str, Rule]:
        if action in self.rules:
            return action, self.rules[action]
        tier = tier_of(action)
        candidates = [k for k in self.rules if any(c in k for c in "*?[") and fnmatch.fnmatchcase(action, k)]
        if candidates:
            k = max(candidates, key=len)
            return k, self.rules[k]
        if tier.value in self.rules:
            return tier.value, self.rules[tier.value]
        return f"default:{tier.value}", Rule(mode=TIER_DEFAULTS[tier])

    def evaluate(self, action: str, *, amount: float | None = None, target: str | None = None) -> Decision:
        key, rule = self._rule_for(action)
        if rule.mode == Mode.AUTO:
            return Decision(verdict=Verdict.ALLOW, reason="auto", rule_key=key)
        if rule.mode == Mode.MANUAL:
            return Decision(verdict=Verdict.ASK, reason="manual approval required", rule_key=key)
        if rule.mode == Mode.DENY:
            return Decision(verdict=Verdict.DENY, reason="denied by policy", rule_key=key)
        # auto_with_limits
        if rule.allow is not None:
            if target is None or not any(fnmatch.fnmatchcase(target, p) for p in rule.allow):
                return Decision(verdict=Verdict.ASK, reason=f"target {target!r} not in allow-list", rule_key=key)
        if rule.per_action is not None:
            if amount is None or amount > rule.per_action:
                return Decision(verdict=Verdict.ASK, reason=f"amount {amount} over per-action cap {rule.per_action}", rule_key=key)
        if rule.per_day is not None:
            spent = self._auto_spent_today(action)
            if amount is None or spent + amount > rule.per_day:
                return Decision(verdict=Verdict.ASK, reason=f"daily cap {rule.per_day} would be exceeded ({spent} used)", rule_key=key)
        return Decision(verdict=Verdict.ALLOW, reason="within limits", rule_key=key)

    def _auto_spent_today(self, action: str) -> float:
        today = day_key(self.tz)
        rows = self.store.all(
            "SELECT amount, created FROM approvals WHERE action = ? AND status = 'auto' AND amount IS NOT NULL", (action,)
        )
        from datetime import datetime

        return sum(r["amount"] for r in rows if day_key(self.tz, datetime.fromisoformat(r["created"])) == today)

    # --- approvals -----------------------------------------------------------
    def check(
        self, action: str, details: dict[str, Any], *, task_id: str | None = None,
        amount: float | None = None, target: str | None = None,
    ) -> tuple[Verdict, str | None]:
        """Evaluate and, when needed, look up or create an approval.

        Returns (ALLOW, approval_id|None), (ASK, approval_id) while pending, or raises PolicyDenied.
        """
        decision = self.evaluate(action, amount=amount, target=target)
        key = approval_key(task_id, action, details)
        if decision.verdict == Verdict.DENY:
            self.events.append("policy.denied", {"action": action, "rule": decision.rule_key}, task_id=task_id)
            raise PolicyDenied(f"{action}: {decision.reason}")
        if decision.verdict == Verdict.ALLOW:
            # Every automatic T2+ action is recorded (audit + daily caps); T0/T1 are not.
            if tier_of(action) not in (RiskTier.T0, RiskTier.T1):
                aid = uuid.uuid4().hex
                self.store.run(
                    "INSERT INTO approvals(id, key, action, tier, task_id, details, amount, status, created, decided, decided_by)"
                    " VALUES(?,?,?,?,?,?,?, 'auto', ?, ?, 'policy')",
                    (aid, key, action, tier_of(action).value, task_id, canonical_json(details), amount, utcnow_iso(), utcnow_iso()),
                )
                self.events.append("policy.auto_allowed", {"action": action, "rule": decision.rule_key, "amount": amount, "target": target}, task_id=task_id)
                return Verdict.ALLOW, aid
            return Verdict.ALLOW, None

        existing = self.store.one(
            "SELECT * FROM approvals WHERE key = ? AND status != 'auto' ORDER BY created DESC LIMIT 1", (key,)
        )
        if existing:
            status = self._effective_status(existing)
            if status == "approved":
                return Verdict.ALLOW, existing["id"]
            if status in ("rejected", "expired"):
                raise PolicyDenied(f"{action}: owner {status} ({existing['note'] or 'no note'})")
            return Verdict.ASK, existing["id"]
        aid = uuid.uuid4().hex
        expires = (utcnow() + timedelta(seconds=self.approval_ttl_s)).isoformat(timespec="milliseconds")
        self.store.run(
            "INSERT INTO approvals(id, key, action, tier, task_id, details, amount, status, created, expires)"
            " VALUES(?,?,?,?,?,?,?, 'pending', ?, ?)",
            (aid, key, action, tier_of(action).value, task_id, canonical_json(details), amount, utcnow_iso(), expires),
        )
        self.events.append("approval.requested", {"id": aid, "action": action, "reason": decision.reason, "details": details}, task_id=task_id)
        # notify the owner later (Telegram buttons, panel): outbox row, delivered by notify.outbox.flush
        self.store.run("INSERT INTO outbox(kind, payload, status, created) VALUES('approval', ?, 'pending', ?)",
                       (canonical_json({"id": aid, "action": action, "reason": decision.reason, "details": details,
                                        "task_id": task_id}), utcnow_iso()))
        return Verdict.ASK, aid

    def _effective_status(self, row) -> str:
        if row["status"] == "pending" and row["expires"] and row["expires"] < utcnow_iso():
            self.store.run("UPDATE approvals SET status = 'expired', decided = ? WHERE id = ?", (utcnow_iso(), row["id"]))
            self.events.append("approval.expired", {"id": row["id"], "action": row["action"]}, task_id=row["task_id"])
            return "expired"
        return row["status"]

    def decide(self, approval_id: str, approve: bool, *, by: str = "owner", note: str | None = None) -> None:
        row = self.store.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row:
            raise KeyError(approval_id)
        if self._effective_status(row) != "pending":
            raise ValueError(f"approval {approval_id} is {row['status']}, not pending")
        status = "approved" if approve else "rejected"
        self.store.run(
            "UPDATE approvals SET status = ?, decided = ?, decided_by = ?, note = ? WHERE id = ?",
            (status, utcnow_iso(), by, note, approval_id),
        )
        self.events.append(f"approval.{status}", {"id": approval_id, "action": row["action"], "note": note}, actor=by, task_id=row["task_id"])

    def pending(self) -> list[dict[str, Any]]:
        rows = self.store.all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created")
        out = []
        for r in rows:
            if self._effective_status(r) == "pending":
                out.append({"id": r["id"], "action": r["action"], "task_id": r["task_id"],
                            "details": json.loads(r["details"]), "amount": r["amount"], "expires": r["expires"]})
        return out
