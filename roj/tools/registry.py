"""Tool registry: Python functions become agent tools with generated cards.

Cards are generated from the real signature, so they never drift from the code:
  L0  "name: summary"                               (~15 tokens, always in the prompt)
  L1  signature, parameters, tier, limits           (<= 150 tokens, for assigned tools)
  L2  full docstring                                (on demand: tool `tools.describe`)
Every call: argument validation, policy by tier (T2+ parks the task for the owner),
timeout, optional cache for pure tools, result in CAS, an event in the log.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, get_type_hints

from pydantic import ValidationError, create_model

from ..contracts import RiskTier
from ..util import canonical_json, sha256_hex, truncate

if TYPE_CHECKING:
    from ..core.kernel import StepContext


@dataclass
class ToolSpec:
    name: str
    summary: str
    fn: Callable[..., Any]
    tier: RiskTier
    timeout_s: float
    pure: bool
    side_effects: str

    def _params(self) -> list[tuple[str, Any, Any]]:
        hints = get_type_hints(self.fn)
        out = []
        for pname, prm in list(inspect.signature(self.fn).parameters.items())[1:]:  # first parameter is the StepContext
            out.append((pname, hints.get(pname, Any), prm.default))
        return out

    @property
    def params_model(self):
        fields = {n: (ann, ... if d is inspect.Parameter.empty else d) for n, ann, d in self._params()}
        return create_model(f"Args_{self.name.replace('.', '_')}", **fields)

    def card(self, level: int = 1) -> str:
        if level == 0:
            return f"{self.name}: {self.summary}"
        params = ", ".join(f"{n}: {getattr(a, '__name__', str(a))}" + ("" if d is inspect.Parameter.empty else f" = {d!r}")
                           for n, a, d in self._params())
        line = f"{self.name}({params}) [{self.tier.value}{', pure' if self.pure else ''}, timeout {self.timeout_s:g}s]: {self.summary}"
        if level == 1:
            return line
        return line + "\n" + inspect.cleandoc(self.fn.__doc__ or "")

    def openai_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.wire_name, "description": self.card(1)[:1000], "parameters": schema}}

    @property
    def wire_name(self) -> str:
        return self.name.replace(".", "__")


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, summary: str, *, tier: str = "t0", timeout_s: float = 30.0, pure: bool = False, side_effects: str = "none"):
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = ToolSpec(name, summary, fn, RiskTier(tier), timeout_s, pure, side_effects)
        return fn
    return deco


def get(name: str) -> ToolSpec:
    _load_builtins()
    key = name.replace("__", ".")
    if key not in REGISTRY:
        raise KeyError(f"unknown tool {name!r}; known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[key]


def all_tools() -> dict[str, ToolSpec]:
    _load_builtins()
    return dict(REGISTRY)


def _load_builtins() -> None:
    from . import builtin  # noqa: F401  (registers on import)


class ToolError(Exception):
    pass


async def call(ctx: "StepContext", name: str, args: dict[str, Any], *, agent: str) -> dict[str, Any]:
    spec = get(name)
    try:
        parsed = spec.params_model.model_validate(args or {})
    except ValidationError as e:
        return {"ok": False, "error": f"invalid arguments: {truncate(str(e), 600)}"}
    kwargs = parsed.model_dump()
    if spec.tier not in (RiskTier.T0, RiskTier.T1):
        ctx.require(f"{spec.tier.value}.tool.{spec.name}", {"tool": spec.name, "args": kwargs, "agent": agent})
    key = sha256_hex(canonical_json({"tool": spec.name, "args": kwargs}))
    if spec.pure:
        row = ctx.project.store.one("SELECT response FROM llm_cache WHERE key = ?", (key,))
        if row:
            import json

            ctx.log("tool.cache_hit", {"tool": spec.name, "agent": agent})
            return json.loads(row["response"])
    t0 = time.monotonic()
    try:
        if inspect.iscoroutinefunction(spec.fn):
            value = await asyncio.wait_for(spec.fn(ctx, **kwargs), spec.timeout_s)
        else:
            value = await asyncio.wait_for(asyncio.to_thread(spec.fn, ctx, **kwargs), spec.timeout_s)
        out = {"ok": True, "result": value}
    except asyncio.TimeoutError:
        out = {"ok": False, "error": f"timed out after {spec.timeout_s:g}s"}
    except (ToolError, ValueError, KeyError, FileNotFoundError, PermissionError) as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {truncate(str(e), 500)}"}
    ref = ctx.cas.put_json({"tool": spec.name, "args": kwargs, "out": out})
    ctx.log("tool.call", {"tool": spec.name, "agent": agent, "ok": out["ok"], "ref": ref, "ms": round((time.monotonic() - t0) * 1000),
                          "error": out.get("error")})
    if spec.pure and out["ok"]:
        ctx.project.store.run("INSERT OR IGNORE INTO llm_cache(key, model, response, created, hits) VALUES(?, ?, ?, ?, 0)",
                              (key, f"tool:{spec.name}", canonical_json(out), time.strftime("%Y-%m-%dT%H:%M:%S")))
    return out
