"""Compaction of long agent conversations.

Two layers, cheapest first:
1. old tool results are cut to a short head (the full text stays in the CAS) - no model call
2. if that is not enough, old turns are replaced by progress notes written by a cheap model

The system prompt and the first user message are never touched, so the provider's cached
prefix survives; the most recent turns stay verbatim, and assistant tool calls are never
separated from their results.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import ModelBinding
from ..core.kernel import StepContext
from ..models.errors import AllModelsFailed
from ..util import estimate_tokens

NOTES = {
    "pl": ("Streszczasz dotychczasową pracę agenta, żeby mógł ją kontynuować bez starszej części rozmowy. Pisz zwięźle, w punktach, "
           "w sekcjach: ZROBIONE, FAKTY (z identyfikatorami źródeł i dokładnymi liczbami), DECYZJE, PLIKI I ARTEFAKTY, OTWARTE. "
           "Nie dodawaj niczego, czego nie ma w rozmowie. Zachowaj dosłownie nazwy, ścieżki, identyfikatory i komunikaty błędów."),
    "en": ("You summarise an agent's work so far so that it can continue without the older part of the conversation. Be concise, "
           "use bullet points in the sections: DONE, FACTS (with source ids and exact numbers), DECISIONS, FILES AND ARTIFACTS, OPEN. "
           "Add nothing that is not in the conversation. Keep names, paths, ids and error messages verbatim."),
}


def size(messages: list[dict[str, Any]]) -> int:
    return estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))


def split(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """-> (head, turns). head ends with the first user message; a turn starts at an assistant message."""
    first_user = next((i for i, m in enumerate(messages) if m.get("role") == "user"), len(messages) - 1)
    head, turns = messages[: first_user + 1], []
    for m in messages[first_user + 1:]:
        if m.get("role") == "assistant" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return head, turns


def _render(turns: list[list[dict[str, Any]]]) -> str:
    lines = []
    for t in turns:
        for m in t:
            if m.get("tool_calls"):
                calls = "; ".join(f"{c['function']['name']}({str(c['function'].get('arguments'))[:300]})" for c in m["tool_calls"])
                lines.append(f"assistant called: {calls}")
            if m.get("content"):
                lines.append(f"{m['role']}: {str(m['content'])[:1500]}")
    return "\n".join(lines)


async def maybe_compact(ctx: StepContext, messages: list[dict[str, Any]], *, binding: ModelBinding, agent: str,
                        scope: str = "") -> list[dict[str, Any]]:
    cfg = ctx.project.settings.context
    before = size(messages)
    if before <= cfg.compact_at_tokens:
        return messages
    head, turns = split(messages)
    if len(turns) <= cfg.keep_turns:
        return messages
    old, recent = turns[: -cfg.keep_turns], turns[-cfg.keep_turns:]
    trimmed: list[list[dict[str, Any]]] = []
    for t in old:
        tt = []
        for m in t:
            c = m.get("content")
            if m.get("role") == "tool" and isinstance(c, str) and len(c) > cfg.tool_result_chars:
                ref = ctx.cas.put_text(c)
                m = {**m, "content": c[: cfg.tool_result_chars] + f" …[{len(c) - cfg.tool_result_chars} chars elided, full text in CAS {ref[:12]}]"}
            tt.append(m)
        trimmed.append(tt)
    flat = lambda ts: [m for t in ts for m in t]  # noqa: E731
    layer1 = head + flat(trimmed) + flat(recent)
    if size(layer1) <= cfg.compact_at_tokens * 0.7:
        ctx.log("agent.compacted", {"agent": agent, "layer": 1, "before": before, "after": size(layer1)})
        return layer1
    try:
        summarizer = ctx.project.settings.binding_for("summarizer")
    except KeyError:
        summarizer = binding
    lang = ctx.project.settings.language.prompt_language
    try:
        r = await ctx.chat(binding=summarizer, agent=f"{agent}:summarizer", scope=scope, purpose="compaction",
                           messages=[{"role": "system", "content": f'<role name="summarizer">\n{NOTES[lang]}\n</role>'},
                                     {"role": "user", "content": _render(trimmed)[:60_000]}],
                           params={"temperature": 0, "max_tokens": cfg.summary_max_tokens})
    except AllModelsFailed:
        ctx.log("agent.compacted", {"agent": agent, "layer": 1, "before": before, "after": size(layer1), "summary_failed": True})
        return layer1
    notes = {"role": "user", "content": f"<progress_notes turns=\"{len(old)}\">\n{r.text.strip()}\n</progress_notes>"}
    out = head + [notes] + flat(recent)
    ctx.log("agent.compacted", {"agent": agent, "layer": 2, "before": before, "after": size(out), "turns_summarised": len(old),
                                "summary_tokens": r.usage.total})
    return out
