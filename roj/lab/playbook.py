"""The Playbook: prompt rules with their evidence, and the prompt profile each model family gets.

Files (YAML, readable and diffable by the owner):
  <home>/playbook/active.yaml            what the compiler uses now
  <home>/playbook/versions/v<N>.yaml     every activated version (rollback = activate an old one)
  <home>/playbook/proposed-<task>.yaml   a study's proposal waiting for the owner

Only rules with status "supported" and a knob change prompts. Advice from reading
(status "hypothesis") is kept, labelled and never applied (ADR-0002).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..prompting.profiles import DEFAULT, PromptProfile, family
from ..util import utcnow_iso


class Rule(BaseModel):
    id: str
    scope: str  # model family, exact model id, or "*"
    knob: str | None = None  # None: advice in prose, not machine-applied
    value: Any = None
    baseline: Any = None
    status: Literal["supported", "refuted", "inconclusive", "hypothesis"]
    claim: str
    effect: float | None = None  # mean score difference variant - baseline (0..1 scale)
    ci_low: float | None = None
    ci_high: float | None = None
    n: int = 0  # paired cases
    tokens_delta: float | None = None  # relative change of tokens per case
    source: str = "roj experiment"
    decided: str = Field(default_factory=utcnow_iso)


class Playbook(BaseModel):
    version: int = 0
    created: str = Field(default_factory=utcnow_iso)
    study_task: str | None = None
    rules: list[Rule] = Field(default_factory=list)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)  # scope -> knob values that differ from the default

    def profile_for(self, model: str) -> PromptProfile:
        merged: dict[str, Any] = {}
        for scope in ("*", family(model), model):
            merged.update(self.profiles.get(scope, {}))
        return PromptProfile.from_dict(merged) if merged else DEFAULT


def pb_dir(home: Path) -> Path:
    d = Path(home) / "playbook"
    (d / "versions").mkdir(parents=True, exist_ok=True)
    return d


_cache: dict[str, tuple[float, Playbook]] = {}


def load_active(home: Path) -> Playbook:
    path = Path(home) / "playbook" / "active.yaml"
    if not path.exists():
        return Playbook()
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    pb = Playbook.model_validate(yaml.safe_load(path.read_text("utf-8")) or {})
    _cache[str(path)] = (mtime, pb)
    return pb


def save_proposal(home: Path, pb: Playbook, task_id: str) -> Path:
    path = pb_dir(home) / f"proposed-{task_id[:8]}.yaml"
    path.write_text(yaml.safe_dump(pb.model_dump(mode="json"), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def activate(home: Path, proposal: Path) -> Playbook:
    d = pb_dir(home)
    pb = Playbook.model_validate(yaml.safe_load(Path(proposal).read_text("utf-8")))
    pb.version = max([int(p.stem[1:]) for p in (d / "versions").glob("v*.yaml")] + [0]) + 1
    text = yaml.safe_dump(pb.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    (d / "versions" / f"v{pb.version}.yaml").write_text(text, encoding="utf-8")
    (d / "active.yaml").write_text(text, encoding="utf-8")
    return pb
