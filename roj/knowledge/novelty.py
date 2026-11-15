"""Novelty gate: do not store what the model already knows.

A closed-book exam instead of a guess: an examiner writes short-answer questions from the
document, the consuming model answers them without the document, code grades the answers.
The share it fails is the document's novelty. Two model calls per document, once, at ingestion.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..contracts import BudgetSpec, TaskSpec
from ..prompting.compiler import Document
from ..util import canonical_json

if TYPE_CHECKING:
    from ..project import Project


class Probe(BaseModel):
    question: str = Field(min_length=8)
    answer: str = Field(min_length=1, max_length=80)


class Probes(BaseModel):
    items: list[Probe] = Field(min_length=1, max_length=12)


class ClosedBook(BaseModel):
    answers: list[str]


def _norm(s: str) -> str:
    return re.sub(r"[^\w.]+", " ", str(s).lower()).strip()


def same(expected: str, got: str) -> bool:
    e, g = _norm(expected), _norm(got)
    if not g or not e:
        return False
    nums_e, nums_g = re.findall(r"\d+(?:\.\d+)?", e), re.findall(r"\d+(?:\.\d+)?", g)
    if nums_e:
        return all(n in nums_g for n in nums_e)
    return e in g or (len(g) > 3 and g in e)


async def measure_novelty(project: "Project", title: str, text: str, *, audience: list[str] | None = None) -> dict[str, Any]:
    from .packs import _role_binding, _slot

    n = project.settings.knowledge.novelty_questions
    task = TaskSpec(goal=f"Write {n} exam questions that test the specific facts of the document '{title}'",
                    deliverable="Probes JSON", output_format="JSON only",
                    acceptance=[f"Exactly {n} questions", "Each question is understandable without seeing the document",
                                "Each answer is a number, a name or a term of at most 6 words taken from the document"],
                    budget=BudgetSpec(tokens=8000))
    probes = (await _slot(project, "examiner", Probes, task, [Document("doc", text[:8000], title)],
                          _role_binding(project, "examiner", "librarian", "classifier", "analyst"))).items
    consumer = _role_binding(project, *[r for r in (audience or []) if r != "*"], "analyst", "classifier")
    task2 = TaskSpec(goal="Answer each question from your own knowledge, without any document",
                     deliverable="ClosedBook JSON", output_format="JSON only",
                     acceptance=[f"Exactly {len(probes)} answers in the order of the questions",
                                 "An answer you do not know is the word unknown, zero guessing"],
                     inputs={"questions": [p.question for p in probes]}, budget=BudgetSpec(tokens=4000))
    answers = (await _slot(project, "closed-book", ClosedBook, task2, [], consumer)).answers
    failed = [p.question for i, p in enumerate(probes) if i >= len(answers) or not same(p.answer, answers[i])]
    out = {"score": round(len(failed) / len(probes), 3), "asked": len(probes), "failed": len(failed), "consumer": consumer.model}
    project.events.append("kb.novelty", {"title": title, **out, "failed_questions": failed[:6], "ref": project.cas.put_json(
        {"probes": [p.model_dump() for p in probes], "answers": answers})})
    return out
