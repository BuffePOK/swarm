"""Smallest end-to-end workflow: compile -> call -> gate -> approval-gated publish.

Inputs: {"topic": str, "model": str, "fallbacks": [str], "publish": bool}
Run:    roj task run -p demo roj.examples.hello:WORKFLOW -i topic="..." -i model=fake-ok
"""

from __future__ import annotations

from roj.config import ModelBinding
from roj.contracts import BudgetSpec, TaskSpec
from roj.core.kernel import Step, StepContext, Workflow
from roj.prompting.compiler import RoleSpec, compile_prompt

ROLE = RoleSpec(
    name="summariser",
    instructions="You write short, factual summaries of a topic for a busy reader.",
    rules=("Maximum 3 sentences.",),
)


async def prepare(ctx: StepContext) -> dict:
    spec = TaskSpec(
        goal=f"Summarise the topic: {ctx.inputs.get('topic', 'roj')}",
        deliverable="A plain-text summary",
        acceptance=["At most 3 sentences", "Contains no claims without a source id, or says 'no data'"],
        output_format="Plain text, at most 3 sentences.",
        budget=BudgetSpec(tokens=5000),
    )
    s = ctx.project.settings
    compiled = compile_prompt(ROLE, spec, language=s.language, settings=s.prompt)
    return {"messages": compiled.messages, "prefix_hash": compiled.prefix_hash,
            "warnings": [f"{i.code}: {i.message}" for i in compiled.issues if i.severity != "error"]}


async def ask(ctx: StepContext) -> dict:
    prep = ctx.results["prepare"]
    binding = ModelBinding(model=ctx.inputs.get("model", "fake-ok"), fallbacks=list(ctx.inputs.get("fallbacks", [])))
    r = await ctx.chat(binding=binding, messages=prep["messages"], agent="summariser",
                       scope="team:demo/agent:summariser", params={"temperature": 0, "max_tokens": 300},
                       prefix_hash=prep["prefix_hash"], purpose="summary")
    return {"text": r.text, "model": r.model, "cost_usd": r.cost_usd, "cached_tokens": r.usage.cached_tokens,
            "exact_cache_hit": r.exact_cache_hit, "fallback_index": r.fallback_index}


async def gate(ctx: StepContext) -> dict:
    text = ctx.results["ask"]["text"].strip()
    sentences = [s for s in text.replace("!", ".").replace("?", ".").split(".") if s.strip()]
    passed = bool(text) and len(sentences) <= 3 and len(text) <= 1500
    if not passed:
        raise ValueError(f"gate failed: {len(sentences)} sentences, {len(text)} chars")
    return {"passed": True, "sentences": len(sentences)}


async def publish(ctx: StepContext) -> dict:
    if not ctx.inputs.get("publish", False):
        return {"published": False, "reason": "publish=false"}
    text = ctx.results["ask"]["text"]
    approval = ctx.require("t3.publish_note", {"text_ref": ctx.cas.put_text(text), "preview": text[:200]})
    return {"published": True, "approval_id": approval}


WORKFLOW = Workflow(
    name="hello",
    steps=(Step("prepare", prepare), Step("ask", ask), Step("gate", gate), Step("publish", publish)),
    description="compile -> call -> gate -> approval-gated publish",
)
