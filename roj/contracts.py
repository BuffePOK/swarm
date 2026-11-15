"""Typed contracts: how agents, teams and the kernel talk to each other.

Agents never chat free-form; every message is an Envelope with a type, and every
piece of work is a TaskSpec with measurable acceptance criteria.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .util import utcnow

HEX64 = r"^[0-9a-f]{64}$"


def new_id() -> str:
    return uuid.uuid4().hex


class MessageType(StrEnum):
    TASK_ASSIGN = "task.assign"
    TASK_PROGRESS = "task.progress"
    TASK_RESULT = "task.result"
    QUESTION = "question"
    DECISION_REQUEST = "decision.request"
    DECISION_RECORD = "decision.record"
    ESCALATION = "escalation"
    CONFLICT_OPEN = "conflict.open"
    CONFLICT_RESOLVED = "conflict.resolved"
    ALERT = "alert"
    # interaction protocols (FIPA-like), used by protocol / Contract Net modes
    REQUEST = "request"
    PROPOSE = "propose"
    ACCEPT = "accept"
    REJECT = "reject"
    INFORM = "inform"
    QUERY = "query"
    CRITIQUE = "critique"
    CFP = "cfp"


class Priority(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3


class RiskTier(StrEnum):
    T0 = "t0"  # read
    T1 = "t1"  # write inside the workspace
    T2 = "t2"  # local irreversible
    T3 = "t3"  # external effects (send, post, publish, submit)
    T4 = "t4"  # money, credentials, accounts, security settings


class Autonomy(StrEnum):
    L0 = "L0"  # strict: follow the script, deviation is an error
    L1 = "L1"  # limited: chooses how inside its step, may ask
    L2 = "L2"  # collaborative: may propose changes to its part of the plan
    L3 = "L3"  # owner: end-to-end module ownership within budget


class Envelope(BaseModel):
    id: str = Field(default_factory=new_id)
    trace_id: str = Field(default_factory=new_id)
    task_id: str | None = None
    sender: str
    recipient: str
    type: MessageType
    priority: Priority = Priority.P2
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)  # CAS hashes, not content
    budget_left: dict[str, float] | None = None
    ts: datetime = Field(default_factory=utcnow)

    @field_validator("refs")
    @classmethod
    def _refs_are_hashes(cls, v: list[str]) -> list[str]:
        import re

        bad = [r for r in v if not re.fullmatch(HEX64, r)]
        if bad:
            raise ValueError(f"refs must be sha256 hex digests, got {bad[:3]}")
        return v


class BudgetSpec(BaseModel):
    tokens: int | None = None
    usd: float | None = None
    steps: int | None = None
    wall_s: float | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in (self.tokens, self.usd, self.steps, self.wall_s))


class ResearchSpec(BaseModel):
    """ADR-0002: how deep to dig and whether to look at weak signals."""

    depth: Literal["shallow", "standard", "deep"] = "standard"
    include_low_trust: bool = False
    min_independent_sources: int = 1
    explore_budget_share: float = Field(0.0, ge=0.0, le=0.5)


class TaskSpec(BaseModel):
    goal: str
    deliverable: str = ""
    acceptance: list[str] = Field(default_factory=list)
    output_format: str | None = None
    output_schema: dict[str, Any] | None = None
    context_refs: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetSpec | None = None
    deadline: datetime | None = None
    risk_tier: RiskTier = RiskTier.T1
    allowed_tools: list[str] = Field(default_factory=list)
    parent: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    research: ResearchSpec | None = None
    output_language: str | None = None


class Question(BaseModel):
    question: str
    why_it_matters: str
    options: list[str] = Field(min_length=2, max_length=4)
    default_if_no_answer: str
    blocking: bool = False

    @model_validator(mode="after")
    def _default_is_an_option(self) -> "Question":
        if self.default_if_no_answer not in self.options:
            raise ValueError("default_if_no_answer must be one of options")
        return self


class RejectedOption(BaseModel):
    option: str
    reason: str


class DecisionRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    decision: str
    rationale: str
    scope: str  # e.g. "project", "team:analysis", "agent:coder-1/module:parser"
    author: str  # "owner" has the highest authority
    rejected: list[RejectedOption] = Field(default_factory=list)
    fallback: str | None = None
    reversible: bool = True
    owner_approval_needed: bool = False
    refs: list[str] = Field(default_factory=list)
    ts: datetime = Field(default_factory=utcnow)


class ConflictOpen(BaseModel):
    claim_a: str
    claim_b: str
    sources: list[str] = Field(default_factory=list)
    scope: str


class SourceTier(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"
    LOW_TRUST = "low_trust"


class Source(BaseModel):
    id: str = Field(default_factory=new_id)
    ref: str  # URL, file path, dataset id
    content_sha256: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    origin: str | None = None  # publisher / author
    tier: SourceTier = SourceTier.UNKNOWN
    independence_key: str | None = None  # syndicated copies share one key


class ClaimStatus(StrEnum):
    FACT = "fact"
    SUPPORTED = "supported"
    HYPOTHESIS = "hypothesis"
    CONTESTED = "contested"
    REFUTED = "refuted"


class Evidence(BaseModel):
    source_id: str
    quote: str | None = None
    quote_verified: bool = False  # set only by code, never by a model
    supports: bool = True


class Claim(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    status: ClaimStatus = ClaimStatus.HYPOTHESIS
    evidence: list[Evidence] = Field(default_factory=list)
    test_to_confirm: str | None = None  # for hypotheses: what would settle it

    def independent_supporting_sources(self, sources: dict[str, Source]) -> int:
        keys = set()
        for e in self.evidence:
            if e.supports and e.source_id in sources:
                s = sources[e.source_id]
                keys.add(s.independence_key or s.origin or s.ref)
        return len(keys)
