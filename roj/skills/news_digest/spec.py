"""DigestSpec: what bootstrap compiles and every daily run executes.

Everything here is data. The engine that executes it is deterministic code;
only adapters (for sources without a feed) are generated code, referenced by
sha256 from the shared pool.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...hands.market import InstrumentSpec

Tier = Literal["primary", "secondary", "unknown", "low_trust"]
ID = r"^[a-z0-9][a-z0-9_\-]{0,40}$"


class SourceSpec(BaseModel):
    id: str = Field(pattern=ID)
    name: str
    url: str
    kind: Literal["feed", "adapter"] = "feed"
    adapter_sha: str | None = None
    tier: Tier = "unknown"
    region: str = "global"
    topics: list[str] = Field(default_factory=list)
    weight: float = Field(1.0, ge=0.0, le=3.0)
    independence_key: str | None = None  # same newsroom / wire = same key
    fixture_ref: str | None = None  # raw sample captured at bootstrap (tests and repair)
    quality: int | None = None  # 1-5 from the assessor at bootstrap
    notes: str = ""


class TopicRule(BaseModel):
    name: str
    keywords: list[str] = Field(min_length=1)
    weight: float = Field(1.0, ge=0.0, le=3.0)


class RankingSpec(BaseModel):
    tier_weights: dict[str, float] = Field(default_factory=lambda: {
        "primary": 1.0, "secondary": 0.7, "unknown": 0.4, "low_trust": 0.2})
    coverage_weight: float = 1.0
    topic_weight: float = 1.0
    recency_weight: float = 0.5
    continuing_penalty: float = 0.5
    cluster_threshold: float = 0.34


class WindowSpec(BaseModel):
    fallback_hours: float = 12.0  # first run, or last run too long ago
    max_hours: float = 36.0


class LimitsSpec(BaseModel):
    max_items_per_source: int = 40
    candidate_events: int = 12
    analysed_events: int = 6
    min_events: int = 1
    telegram_events: int = 5
    repair_after_failures: int = 2


class DigestSpec(BaseModel):
    skill: Literal["news_digest"] = "news_digest"
    goal: str
    sources: list[SourceSpec] = Field(min_length=1)
    instruments: list[InstrumentSpec] = Field(default_factory=list)
    topics: list[TopicRule] = Field(min_length=1)
    ranking: RankingSpec = RankingSpec()
    window: WindowSpec = WindowSpec()
    limits: LimitsSpec = LimitsSpec()
    output_language: str = "en"
    plan_notes: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    research: dict = Field(default_factory=dict)  # bootstrap metrics: rules F1, NDCG, coverage, costs

    def source(self, source_id: str) -> SourceSpec:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)
