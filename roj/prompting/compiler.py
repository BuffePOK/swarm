"""Prompt compiler: TaskSpec + role + tools + knowledge -> messages.

Layout (ADR concept §7.2, §11.1):
  system  = [role][rules][tools][knowledge]                 stable, byte-identical
  user    = [documents][context][task][format][reminder]    variable, question repeated last

Agents never hand-write prompts: they fill a TaskSpec. The compiler makes the
stable part deterministic (sorted tools, canonical JSON, no timestamps), so all
agents of one role share a provider cache prefix, and runs the linter before
anything leaves the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

from ..config import LanguageSettings, PromptSettings
from ..contracts import TaskSpec
from ..util import canonical_json, estimate_tokens, sha256_hex
from .profiles import DEFAULT, PromptProfile
from .linter import Issue, PromptLintError, errors, lint_prompt, lint_taskspec
from .templates import TEMPLATE_VERSION, TEMPLATES


@dataclass(frozen=True)
class ToolCard:
    """L0/L1 tool card: ~15-150 tokens. Full docs (L2) are fetched on demand."""

    name: str
    summary: str
    signature: str = ""


@dataclass(frozen=True)
class KnowledgeBlock:
    id: str
    content: str


@dataclass(frozen=True)
class Document:
    id: str
    content: str
    source: str = ""


@dataclass(frozen=True)
class RoleSpec:
    name: str
    instructions: str
    rules: tuple[str, ...] = ()
    version: str = "1"


@dataclass
class CompiledPrompt:
    messages: list[dict[str, Any]]
    prefix_hash: str
    stable_tokens: int
    total_tokens: int
    issues: list[Issue] = field(default_factory=list)


def _sec(xml: bool, name: str, body: str, attrs: str = "") -> str:
    if xml:
        return f"<{name}{(' ' + attrs) if attrs else ''}>\n{body}\n</{name}>"
    return f"## {name.upper()}{(' (' + attrs + ')') if attrs else ''}\n{body}"


def _rule_texts(role: RoleSpec, t: dict[str, str], profile: PromptProfile) -> list[str]:
    rules = [*role.rules, t["unknown"], t["ask"], t["data"], t["facts"]]
    if profile.reasoning == "brief":
        rules.append(t["think"])
    if profile.structure != "xml":
        rules = [r.replace("<documents>", "DOCUMENTS").replace("<context>", "CONTEXT") for r in rules]
    return rules


def _stable_part(role: RoleSpec, tools: Iterable[ToolCard], knowledge: Iterable[KnowledgeBlock], t: dict[str, str],
                 profile: PromptProfile = DEFAULT) -> str:
    xml = profile.structure == "xml"
    enc = escape if xml else (lambda x: x)
    attrs = f"name={quoteattr(role.name)} version={quoteattr(role.version)} templates={quoteattr(TEMPLATE_VERSION)}"
    parts = [_sec(xml, "role", enc(role.instructions.strip()), attrs),
             _sec(xml, "rules", "\n".join(f"- {enc(r.strip())}" for r in _rule_texts(role, t, profile)))]
    tool_list = sorted(tools, key=lambda c: c.name)
    if tool_list:
        parts.append(_sec(xml, "tools", "\n".join(
            f"- {enc(c.name)}{enc((' ' + c.signature) if c.signature else '')}: {enc(c.summary.strip())}" for c in tool_list)))
    for k in knowledge:
        parts.append(_sec(xml, "knowledge", enc(k.content.strip()), f"id={quoteattr(k.id)}"))
    return "\n".join(parts)


def _variable_part(spec: TaskSpec, documents: Iterable[Document], context: str | None, t: dict[str, str], out_lang: str,
                   profile: PromptProfile = DEFAULT, examples: Iterable[tuple[str, str]] = ()) -> str:
    xml = profile.structure == "xml"
    enc = escape if xml else (lambda x: x)
    doc_parts: list[str] = []
    docs = list(documents)
    if docs:
        inner = [_sec(xml, "document", enc(d.content.strip()),
                      f"id={quoteattr(d.id)}" + (f" source={quoteattr(d.source)}" if d.source else "")) for d in docs]
        doc_parts.append(_sec(xml, "documents", "\n".join(inner)))
    parts: list[str] = [] if not profile.documents_first else list(doc_parts)
    if context:
        parts.append(_sec(xml, "context", enc(context.strip())))
    shots = list(examples)[: profile.examples]
    if shots:
        parts.append(_sec(xml, "examples", "\n".join(
            _sec(xml, "example", _sec(xml, "input", enc(i.strip())) + "\n" + _sec(xml, "output", enc(o.strip()))) for i, o in shots)))
    task = [f"{t['goal']}: {spec.goal.strip()}"]
    if spec.deliverable:
        task.append(f"{t['deliverable']}: {spec.deliverable.strip()}")
    if spec.acceptance:
        task.append(f"{t['acceptance']}:")
        task += [f"{i}. {c.strip()}" for i, c in enumerate(spec.acceptance, 1)]
    if spec.inputs:
        task.append(f"{t['inputs']}: {canonical_json(spec.inputs)}")
    if spec.open_questions:
        task.append(f"{t['questions']}:")
        task += [f"- {q.strip()}" for q in spec.open_questions]
    parts.append(_sec(xml, "task", enc("\n".join(task))))
    if not profile.documents_first:
        parts += doc_parts
    fmt: list[str] = []
    if spec.output_format:
        fmt.append(spec.output_format.strip())
    if spec.output_schema:
        fmt.append(f"{t['schema']}: {canonical_json(spec.output_schema)}")
    fmt.append(t["language"].format(lang=out_lang))
    parts.append(_sec(xml, "format", enc("\n".join(fmt))))
    if profile.reminder:
        short_format = spec.output_format.strip().splitlines()[0] if spec.output_format else ("JSON" if spec.output_schema else spec.deliverable.strip())
        line = f"{enc(t['reminder'])}: {enc(spec.goal.strip())} | {enc(t['format'])}: {enc(short_format)}"
        parts.append(f"<reminder>{line}</reminder>" if xml else f"## REMINDER\n{line}")
    return "\n".join(parts)


def compile_prompt(
    role: RoleSpec,
    spec: TaskSpec,
    *,
    language: LanguageSettings,
    settings: PromptSettings,
    tools: Iterable[ToolCard] = (),
    knowledge: Iterable[KnowledgeBlock] = (),
    documents: Iterable[Document] = (),
    context: str | None = None,
    strict: bool = True,
    profile: PromptProfile | None = None,
    examples: Iterable[tuple[str, str]] = (),
) -> CompiledPrompt:
    profile = profile or DEFAULT
    t = TEMPLATES[profile.language or language.prompt_language]
    enc = escape if profile.structure == "xml" else (lambda x: x)
    out_lang = spec.output_language or language.output_language
    stable = _stable_part(role, tools, knowledge, t, profile)
    variable = _variable_part(spec, documents, context, t, out_lang, profile, examples)
    stable_tokens = estimate_tokens(stable)
    total_tokens = stable_tokens + estimate_tokens(variable)
    rules = _rule_texts(role, t, profile)
    issues = lint_taskspec(spec) + lint_prompt(
        role=role.instructions, stable=stable, variable=variable, stable_tokens=stable_tokens,
        total_tokens=total_tokens, max_input_tokens=settings.max_input_tokens,
        min_cacheable_prefix_tokens=settings.min_cacheable_prefix_tokens,
        required_clauses=[enc(rules[len(role.rules)]), enc(rules[len(role.rules) + 2])],
    )
    if not (profile.reminder and profile.structure == "xml"):  # PR003 describes the default layout only
        issues = [i for i in issues if i.code != "PR003"]
    if strict and errors(issues):
        raise PromptLintError(issues)
    messages = [{"role": "system", "content": stable}, {"role": "user", "content": variable}]
    return CompiledPrompt(messages, sha256_hex(stable), stable_tokens, total_tokens, issues)
