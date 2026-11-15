"""Deterministic linters for task specs and compiled prompts.

An error blocks the call. Warnings and infos are logged so the Meta team can
measure which ones matter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..contracts import TaskSpec

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


class PromptLintError(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in issues if i.severity == "error"))


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


# Words that make a criterion unmeasurable on their own (en + pl).
_VAGUE = re.compile(
    r"\b(good|nice|better|best|high[- ]quality|appropriate|properly|as (good|well) as possible|"
    r"user[- ]friendly|clean|robust|dobr\w*|ładn\w*|lepsz\w*|najlepsz\w*|odpowiedni\w*|porządn\w*|"
    r"jak najlepiej|wysokiej jakości|przyjazn\w*)\b",
    re.I,
)
# Markers that make a criterion checkable (numbers, quantifiers, verifiable verbs).
_MEASURABLE = re.compile(
    r"\d|[<>≤≥=]|\b(every|each|all|no|none|zero|exactly|at least|at most|contains?|includes?|passes?|matches?|"
    r"valid|schema|test|link|source|cites?|returns?|exists?|under|within|"
    r"każd\w*|wszystk\w*|żadn\w*|brak|zero|dokładnie|co najmniej|najwyżej|zawiera\w*|przechodzi|"
    r"zgodn\w*|schemat\w*|test\w*|link\w*|źródł\w*|cytat\w*|zwraca|istnieje|poniżej|w ciągu)\b",
    re.I,
)

_VOLATILE = [
    ("PR001", re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"), "timestamp"),
    ("PR001", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "UUID"),
    ("PR001", re.compile(r"\b1[6-9]\d{8}(\d{3})?\b"), "unix timestamp"),
]


def lint_taskspec(spec: TaskSpec) -> list[Issue]:
    issues: list[Issue] = []
    if len(spec.goal.strip()) < 10:
        issues.append(Issue("TS001", "error", "goal is missing or shorter than 10 characters"))
    if not spec.acceptance:
        issues.append(Issue("TS002", "error", "no acceptance criteria: a task without them never enters the queue"))
    for i, crit in enumerate(spec.acceptance, 1):
        measurable = bool(_MEASURABLE.search(crit))
        if _VAGUE.search(crit) and not measurable:
            issues.append(Issue("TS003", "error", f"criterion {i} is vague and not measurable: {crit!r}"))
        elif not measurable:
            issues.append(Issue("TS003", "warning", f"criterion {i} has no measurable marker: {crit!r}"))
    if not (spec.output_format or spec.output_schema or spec.deliverable.strip()):
        issues.append(Issue("TS004", "error", "no deliverable, output_format or output_schema"))
    if spec.budget is None or spec.budget.is_empty():
        issues.append(Issue("TS005", "warning", "no task budget: only team/project limits apply"))
    if len(spec.acceptance) > 12:
        issues.append(Issue("TS006", "warning", f"{len(spec.acceptance)} criteria: consider splitting the task"))
    return issues


def lint_prompt(
    *, role: str, stable: str, variable: str, stable_tokens: int, total_tokens: int,
    max_input_tokens: int, min_cacheable_prefix_tokens: int, required_clauses: list[str],
) -> list[Issue]:
    issues: list[Issue] = []
    if not role.strip():
        issues.append(Issue("PR005", "error", "empty role instructions"))
    for code, rx, what in _VOLATILE:
        m = rx.search(stable)
        if m:
            issues.append(Issue(code, "error", f"{what} {m.group(0)!r} in the stable prefix breaks provider caching; move it to the task part"))
    if total_tokens > max_input_tokens:
        issues.append(Issue("PR002", "error", f"estimated {total_tokens} input tokens > limit {max_input_tokens}"))
    if not variable.rstrip().endswith("</reminder>"):
        issues.append(Issue("PR003", "error", "goal and format are not repeated at the end"))
    for clause in required_clauses:
        if clause not in stable:
            issues.append(Issue("PR004", "error", f"required clause missing from rules: {clause[:40]!r}..."))
    if stable_tokens < min_cacheable_prefix_tokens:
        issues.append(Issue("PR006", "info", f"stable prefix ~{stable_tokens} tokens < {min_cacheable_prefix_tokens}: most providers will not cache it"))
    return issues
