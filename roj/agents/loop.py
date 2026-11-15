"""The tool-calling agent: plan -> act (tools) -> observe -> submit.

- tools are offered as OpenAI function schemas generated from the registry; models without
  native tool calling may answer with {"tool": ..., "args": ...} JSON instead
- the agent ends by calling `submit_result` (validated AgentOutput); a plain JSON answer
  with a "status" field is accepted too
- autonomy: L0 only the listed tools and no proposals, L1 listed tools, L2 may also
  `propose_change` to its lead (an Envelope on the bus), L3 may additionally request subtasks
- guards: max steps, repeated identical tool calls, budget (via the model caller)
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..config import ModelBinding
from ..contracts import Autonomy, Envelope, MessageType, TaskSpec
from ..core.kernel import StepContext
from ..prompting.compiler import Document, ToolCard, compile_prompt
from ..skills.slots import extract_json
from ..tools import registry
from .compaction import maybe_compact
from .roles import RoleDef


class AgentOutput(BaseModel):
    status: Literal["done", "question", "failed"]
    summary: str = Field(min_length=1, max_length=4000)
    output: dict[str, Any] = Field(default_factory=dict)
    question: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)


SUBMIT = {"type": "function", "function": {"name": "submit_result",
                                           "description": "Finish: report the result (or a blocking question) for this task.",
                                           "parameters": AgentOutput.model_json_schema()}}
PROPOSE = {"type": "function", "function": {"name": "propose_change", "description": "Propose a change to your part of the plan to the lead.",
                                            "parameters": {"type": "object", "properties": {"proposal": {"type": "string"},
                                                                                            "reason": {"type": "string"}},
                                                           "required": ["proposal", "reason"]}}}


class AgentRun(BaseModel):
    output: AgentOutput
    steps: int
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


async def run_agent(ctx: StepContext, *, role: RoleDef, task: TaskSpec, binding: ModelBinding, agent_name: str,
                    tools: list[str] | None = None, autonomy: Autonomy | None = None, scope: str = "",
                    documents: list[Document] | None = None, lead: str | None = None, max_steps: int = 12) -> AgentRun:
    autonomy = autonomy or role.autonomy
    names = list(tools if tools is not None else role.tools)
    specs = [registry.get(n) for n in names]
    s = ctx.project.settings
    profile = ctx.project.profile_for(binding.model)
    ctx.agent_role = role.name  # tools such as kb.retrieve route by the caller's role
    compiled = compile_prompt(role.spec(profile.language or s.language.prompt_language), task, language=s.language, settings=s.prompt,
                              profile=profile,
                              tools=[ToolCard(t.name, t.summary, t.card(1).split(" [", 1)[0][len(t.name):]) for t in specs],
                              documents=documents or [])
    messages = list(compiled.messages)
    schemas = [t.openai_schema() for t in specs] + [SUBMIT] + ([PROPOSE] if autonomy in (Autonomy.L2, Autonomy.L3) else [])
    seen: dict[str, int] = {}
    calls: list[dict[str, Any]] = []
    for step in range(1, max_steps + 1):
        messages = await maybe_compact(ctx, messages, binding=binding, agent=agent_name, scope=scope)
        r = await ctx.chat(binding=binding, messages=messages, agent=agent_name, scope=scope, prefix_hash=compiled.prefix_hash,
                           purpose=f"agent:{role.name}", params={"temperature": 0, "max_tokens": 2000, "tools": schemas,
                                                                 "tool_choice": "auto"})
        tcs = [{"id": tc.get("id") or f"call{step}_{k}", "name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or "{}"}
               for k, tc in enumerate(r.tool_calls or [])]
        if not tcs:
            try:
                data = extract_json(r.text)
            except ValueError:
                data = None
            if isinstance(data, dict) and "tool" in data:
                tcs = [{"id": f"json{step}", "name": data["tool"], "arguments": json.dumps(data.get("args", {}))}]
            elif isinstance(data, dict) and "status" in data:
                tcs = [{"id": f"json{step}", "name": "submit_result", "arguments": json.dumps(data)}]
            else:
                out = AgentOutput(status="done", summary=r.text.strip()[:4000] or "(empty answer)", confidence=0.3)
                ctx.log("agent.done", {"agent": agent_name, "steps": step, "status": out.status, "unstructured": True})
                return AgentRun(output=out, steps=step, tool_calls=calls)
        messages.append({"role": "assistant", "content": r.text or None,
                         "tool_calls": [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["arguments"]}}
                                        for t in tcs]})
        for t in tcs:
            try:
                args = json.loads(t["arguments"]) if isinstance(t["arguments"], str) else t["arguments"]
            except json.JSONDecodeError:
                args = {}
            if t["name"] == "submit_result":
                try:
                    out = AgentOutput.model_validate(args)
                except ValidationError as e:
                    messages.append({"role": "tool", "tool_call_id": t["id"], "content": json.dumps({"ok": False, "error": str(e)[:800]})})
                    continue
                ctx.log("agent.done", {"agent": agent_name, "steps": step, "status": out.status, "confidence": out.confidence})
                return AgentRun(output=out, steps=step, tool_calls=calls)
            if t["name"] == "propose_change":
                if lead:
                    ctx.project.bus.send(Envelope(sender=agent_name, recipient=lead, type=MessageType.PROPOSE, payload=args,
                                                  task_id=ctx.task_id))
                content = {"ok": True, "result": "proposal recorded for the lead"}
            else:
                name = t["name"].replace("__", ".")
                if name not in names:
                    content = {"ok": False, "error": f"tool {name} is not available to you"}
                else:
                    sig = f"{name}|{json.dumps(args, sort_keys=True)}"
                    seen[sig] = seen.get(sig, 0) + 1
                    if seen[sig] > 2:
                        ctx.log("agent.loop_detected", {"agent": agent_name, "tool": name})
                        out = AgentOutput(status="failed", summary=f"stopped: repeated the same call to {name} three times")
                        return AgentRun(output=out, steps=step, tool_calls=calls)
                    content = await registry.call(ctx, name, args, agent=agent_name)
                    calls.append({"tool": name, "args": args, "ok": content.get("ok")})
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": json.dumps(content, ensure_ascii=False, default=str)[:12000]})
    ctx.log("agent.max_steps", {"agent": agent_name, "steps": max_steps})
    return AgentRun(output=AgentOutput(status="failed", summary=f"no result after {max_steps} steps"), steps=max_steps, tool_calls=calls)
