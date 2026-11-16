"""Prompt profiles: the knobs of the compiler that the Lab team measures per model family.

A profile never changes *what* is asked, only how the same TaskSpec is laid out. The default
profile is the layout used since v0.1; every deviation must come from the Playbook, i.e. from
an A/B experiment that the owner approved (roj/lab).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

KNOBS: dict[str, list[Any]] = {
    "language": ["pl", "en"],  # language of the scaffolding and of role instructions
    "structure": ["xml", "markdown"],  # <tags> or ## HEADINGS
    "documents_first": [True, False],  # long material above or below the task
    "reminder": [True, False],  # goal and format repeated at the very end
    "reasoning": ["none", "brief"],  # ask for a short <thinking> block before the answer
    "examples": [2, 0],  # few-shot examples included when the caller has any
}


@dataclass(frozen=True)
class PromptProfile:
    language: str | None = None  # None: the project's [language] prompt_language
    structure: str = "xml"
    documents_first: bool = True
    reminder: bool = True
    reasoning: str = "none"
    examples: int = 2

    def with_knob(self, knob: str, value: Any) -> "PromptProfile":
        if knob not in KNOBS:
            raise KeyError(f"unknown prompt knob {knob!r}; known: {', '.join(KNOBS)}")
        if value not in KNOBS[knob]:
            raise ValueError(f"knob {knob}: {value!r} is not one of {KNOBS[knob]}")
        return replace(self, **{knob: value})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PromptProfile":
        p = cls()
        for k, v in (raw or {}).items():
            if k == "language" and v is None:
                continue
            p = p.with_knob(k, v)
        return p


DEFAULT = PromptProfile()


_MISTRAL = ("mistral", "ministral", "magistral", "codestral", "devstral", "voxtral", "pixtral", "leanstral", "labs-leanstral")
_PREFIXES = ("claude", "gemini", "gemma", "gpt", "o1", "o3", "o4", "deepseek", "llama", "qwen", "grok", "glm", "zai-glm", "kimi",
             "minimax", "fake-smart-b", "fake-smart")


def family(model: str) -> str:
    """Who made the model, from an id such as 'qwen/deepseek-v4-pro' -> 'deepseek' (the gateway's provider prefix is
    ignored: the same model served by two providers is one family, and errors are shared within a family)."""
    m = model.lower().split("/")[-1]
    if m.startswith(_MISTRAL):
        return "mistral"
    for fam in _PREFIXES:
        if m.startswith(fam):
            return {"zai-glm": "glm", "o1": "gpt", "o3": "gpt", "o4": "gpt"}.get(fam, fam)
    return m.split("-")[0]
