"""Blueprint schema (concept v2 §3.4): the system of one project as data, versioned.

project goal and acceptance -> teams (mode, members with concrete models and fallbacks,
autonomy, tools, knowledge packs, budget, dependencies) -> integration guardian.
"""

from __future__ import annotations

import ast
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..contracts import Autonomy

Mode = Literal["strict", "graph", "orchestrator"]


class MemberSpec(BaseModel):
    name: str
    role: str
    model: str
    fallbacks: list[str] = Field(default_factory=list)
    autonomy: Autonomy | None = None
    tools: list[str] | None = None  # None: the role's defaults
    knowledge: list[str] = Field(default_factory=list)  # knowledge packs this member may search


class NodeSpec(BaseModel):
    id: str
    kind: Literal["agent", "tool"] = "agent"
    member: str | None = None  # agent nodes
    task: str = ""  # what this node must do; {state.x} placeholders are filled from earlier nodes
    tool: str | None = None  # tool nodes
    args: dict[str, Any] = Field(default_factory=dict)


class EdgeSpec(BaseModel):
    source: str
    target: str
    when: str | None = None  # safe expression over `state`, e.g. "state['verify']['status'] != 'done'"


class GraphSpec(BaseModel):
    start: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec] = Field(default_factory=list)
    max_visits: int = Field(3, ge=1, le=20)


class TeamBudget(BaseModel):
    tokens: int | None = None
    usd: float | None = None


class TeamSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,40}$")
    goal: str
    acceptance: list[str] = Field(default_factory=list)
    mode: Mode = "strict"
    members: list[MemberSpec] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    budget: TeamBudget = TeamBudget()
    graph: GraphSpec | None = None  # graph mode; strict mode may list nodes in order
    max_rounds: int = Field(8, ge=1, le=30)  # orchestrator mode
    output_format: str = "A concise result with sources"


class Integration(BaseModel):
    guardian: MemberSpec | None = None


class Blueprint(BaseModel):
    project: str
    goal: str
    acceptance: list[str] = Field(default_factory=list)
    control_mode: Literal["auto", "assist", "manual"] = "assist"
    teams: list[TeamSpec] = Field(min_length=1)
    integration: Integration = Integration()

    def team(self, name: str) -> TeamSpec:
        for t in self.teams:
            if t.name == name:
                return t
        raise KeyError(name)


# --- safe edge conditions ------------------------------------------------------------------

_ALLOWED = (ast.Expression, ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Not, ast.And, ast.Or, ast.Name, ast.Load, ast.Subscript,
            ast.Constant, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.List, ast.Tuple)


def check_condition(expr: str) -> ast.Expression:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"condition uses {type(node).__name__}: only comparisons on state[...] are allowed")
        if isinstance(node, ast.Name) and node.id != "state":
            raise ValueError(f"condition may only reference 'state', not {node.id!r}")
    return tree


def eval_condition(expr: str, state: dict[str, Any]) -> bool:
    tree = check_condition(expr)

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return state
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Subscript):
            base = ev(n.value)
            key = ev(n.slice)
            try:
                return base[key]
            except (KeyError, IndexError, TypeError):
                return None
        if isinstance(n, ast.UnaryOp):
            return not ev(n.operand)
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right_node in zip(n.ops, n.comparators):
                right = ev(right_node)
                ok = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
                      ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
                      ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b, ast.Is: lambda a, b: a is b,
                      ast.IsNot: lambda a, b: a is not b}[type(op)]
                try:
                    if not ok(left, right):
                        return False
                except TypeError:
                    return False
                left = right
            return True
        raise ValueError(type(n).__name__)

    return bool(ev(tree))
