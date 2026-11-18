"""Slots: the only places where a model works inside a skill.

A slot = role + TaskSpec + output model. The prompt is compiled (stable prefix,
linted), the answer must be JSON matching the output model, and an invalid answer
gets the validation errors back for a bounded number of repair attempts. Every
attempt is budgeted and traced like any other model call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from ..config import ModelBinding
from ..contracts import TaskSpec
from ..core.kernel import StepContext
from ..prompting.compiler import Document, KnowledgeBlock, RoleSpec, ToolCard, compile_prompt

REPAIR = {
    "pl": "Twoja odpowiedź nie przeszła walidacji:\n{errors}\nZwróć wyłącznie poprawiony JSON zgodny ze schematem, bez komentarzy.",
    "en": "Your answer failed validation:\n{errors}\nReturn only the corrected JSON matching the schema, no commentary.",
}
CUT = {
    "pl": "Twoja poprzednia odpowiedź została ucięta po {n} tokenach, zanim JSON był kompletny. Odpowiedz od razu obiektem JSON "
          "zgodnym ze schematem: bez wstępu, bez długiego rozumowania, zwięźle w polach tekstowych.",
    "en": "Your previous answer was cut off after {n} tokens, before the JSON was complete. Answer straight away with the JSON "
          "object matching the schema: no preamble, no long reasoning, keep the text fields concise.",
}
MAX_OUT = 32_000  # a slot never asks for more than this per answer, however often it is cut off


class SlotFailed(Exception):
    pass


@dataclass(frozen=True)
class Slot:
    name: str
    role: RoleSpec
    output: type[BaseModel]
    max_tokens: int = 4000
    temperature: float = 0.0
    repair_attempts: int = 2


@dataclass
class SlotResult:
    value: BaseModel
    attempts: int
    tokens: int
    cost_usd: float
    models: list[str]


def extract_json(text: str) -> Any:
    t = re.sub(r"(?is)<(think|thinking|reasoning)>.*?</\1>", "", text)
    t = re.sub(r"(?is)^\s*<(think|thinking|reasoning)>.*", "", t).strip()  # an unclosed reasoning block: nothing usable after it
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object in the answer")
    start = min(starts)
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= start:
        raise ValueError("no complete JSON object in the answer")
    return json.loads(t[start: end + 1])


def truncated(finish_reason: str | None, completion_tokens: int, max_tokens: int) -> bool:
    """The model ran out of room: the gateway says so, or it used (almost) every token it was allowed."""
    return finish_reason in ("length", "max_tokens") or completion_tokens >= 0.98 * max_tokens


async def run_slot(
    ctx: StepContext, slot: Slot, task: TaskSpec, *, binding: ModelBinding, scope: str,
    documents: Iterable[Document] = (), context: str | None = None,
    knowledge: Iterable[KnowledgeBlock] = (), tools: Iterable[ToolCard] = (),
    examples: Iterable[tuple[str, str]] = (),
) -> SlotResult:
    s = ctx.project.settings
    if task.output_schema is None:
        task = task.model_copy(update={"output_schema": slot.output.model_json_schema()})
    compiled = compile_prompt(slot.role, task, language=s.language, settings=s.prompt, documents=list(documents),
                              context=context, knowledge=list(knowledge), tools=list(tools),
                              profile=ctx.project.profile_for(binding.model), examples=list(examples))
    messages = list(compiled.messages)
    tokens, cost, models = 0, 0.0, []
    max_out = slot.max_tokens
    for attempt in range(1, slot.repair_attempts + 2):
        r = await ctx.chat(binding=binding, messages=messages, agent=slot.name, scope=scope,
                           params={"temperature": slot.temperature, "max_tokens": max_out},
                           prefix_hash=compiled.prefix_hash, purpose=f"slot:{slot.name}")
        tokens += r.usage.total
        cost += r.cost_usd
        models.append(r.model)
        cut = truncated(r.finish_reason, r.usage.completion_tokens, max_out)
        try:
            value = slot.output.model_validate(extract_json(r.text))
        except (ValueError, ValidationError) as e:
            err = str(e)[:1500]
            ctx.log("slot.invalid", {"slot": slot.name, "attempt": attempt, "model": r.model, "error": err,
                                     "finish_reason": r.finish_reason, "completion_tokens": r.usage.completion_tokens,
                                     "max_tokens": max_out, "truncated": cut, "chars": len(r.text), "head": r.text[:300],
                                     "response_ref": r.response_ref})
            if cut:  # not a wrong answer, an unfinished one: more room, and a cut text is no use as context
                max_out = min(MAX_OUT, max_out * 2)
                messages = list(compiled.messages) + [
                    {"role": "user", "content": CUT[s.language.prompt_language].format(n=r.usage.completion_tokens)}]
                continue
            messages = messages + [
                {"role": "assistant", "content": r.text[:6000]},
                {"role": "user", "content": REPAIR[s.language.prompt_language].format(errors=err)},
            ]
            continue
        ctx.log("slot.ok", {"slot": slot.name, "attempts": attempt, "tokens": tokens, "cost_usd": round(cost, 8), "model": r.model})
        return SlotResult(value, attempt, tokens, cost, models)
    raise SlotFailed(f"slot {slot.name}: no valid answer after {slot.repair_attempts + 1} attempts "
                     f"(last limit {max_out} output tokens; see slot.invalid in the task log)")
