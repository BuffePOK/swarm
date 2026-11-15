"""Built-in tools. Hands are deterministic; agents decide when to use them."""

from __future__ import annotations

import re

import ast
import asyncio
import operator
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..core.kernel import StepContext  # noqa: F401  (resolves the annotations of tool signatures)

from ..hands.feeds import FeedError, html_to_text, looks_like_feed, parse_feed
from .registry import ToolError, all_tools, tool

if TYPE_CHECKING:
    from ..core.kernel import StepContext

MAX_TEXT = 20_000


@tool("web.fetch", "Fetch a public URL (robots.txt respected) and return readable text, truncated")
async def web_fetch(ctx: "StepContext", url: str, max_chars: int = 8000) -> dict:
    r = await ctx.project.fetcher.get(url, purpose="tool:web.fetch")
    if not r.ok:
        raise ToolError(r.error or f"HTTP {r.status}")
    if looks_like_feed(r.content):
        try:
            title, items = parse_feed(r.content, base_url=url)
            return {"kind": "feed", "title": title, "items": [i.as_dict() for i in items[:40]]}
        except FeedError:
            pass
    text = html_to_text(r.text(), limit=min(max_chars, MAX_TEXT))
    return {"kind": "page", "url": r.url, "text": text, "ref": r.ref}


@tool("kb.search", "Search a knowledge pack of this project (hybrid BM25 + vectors); returns cited chunks")
async def kb_search(ctx: "StepContext", pack: str, query: str, k: int = 5) -> list[dict]:
    from ..knowledge.packs import KnowledgeBase

    hits = await KnowledgeBase(ctx.project).search(pack, query, k=min(k, 10))
    return [{"chunk": f"{h.doc_id}#{h.chunk_id}", "title": h.title, "section": h.section, "text": h.text[:1200]} for h in hits]


@tool("kb.retrieve", "Find knowledge relevant to a question across the packs your role may use; returns nothing if nothing fits")
async def kb_retrieve(ctx: "StepContext", query: str) -> list[dict]:
    from ..knowledge.router import retrieve

    r = await retrieve(ctx.project, query, role=getattr(ctx, "agent_role", "*"))
    return [{"chunk": f"{h.doc_id}#{h.chunk_id}", "title": h.title, "section": h.section, "text": h.text[:1200]} for h in r.hits]


@tool("kb.note", "Save a piece of knowledge (title + text) into a knowledge pack of this project so other agents can retrieve it",
      tier="t1", side_effects="writes to the project's knowledge base")
async def kb_note(ctx: "StepContext", pack: str, title: str, text: str) -> dict:
    from ..knowledge.packs import KnowledgeBase

    kb = KnowledgeBase(ctx.project)
    if pack not in {p["pack"] for p in kb.packs()}:
        kb.create(pack, f"Notes written by agents: {pack}")
    doc_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"
    chunks = await kb.add_text(pack, doc_id, title, text[:40_000], source=f"agent:{getattr(ctx, 'agent_role', 'agent')}")
    return {"pack": pack, "doc_id": doc_id, "chunks": chunks}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ToolError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("only numbers and + - * / ** % // are allowed")


@tool("calc", "Evaluate arithmetic exactly (never compute numbers in your head)", pure=True, timeout_s=5)
def calc(ctx: "StepContext", expression: str) -> float:
    if len(expression) > 300:
        raise ToolError("expression too long")
    return _eval(ast.parse(expression, mode="eval"))


@tool("time.now", "Current date and time in a timezone (IANA name)", timeout_s=5)
def time_now(ctx: "StepContext", timezone: str = "Europe/Warsaw") -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def _workspace(ctx: "StepContext") -> Path:
    ws = (ctx.project.dir / "workspace").resolve()
    ws.mkdir(exist_ok=True)
    return ws


def _inside(ctx: "StepContext", path: str) -> Path:
    ws = _workspace(ctx)
    p = (ws / path).resolve()
    if p != ws and ws not in p.parents:
        raise PermissionError("path is outside the project workspace")
    return p


@tool("files.read", "Read a text file from the project workspace", timeout_s=10)
def files_read(ctx: "StepContext", path: str, max_chars: int = 20000) -> str:
    return _inside(ctx, path).read_text(encoding="utf-8", errors="replace")[: min(max_chars, MAX_TEXT)]


@tool("files.write", "Write a text file inside the project workspace", tier="t1", side_effects="writes a workspace file", timeout_s=10)
def files_write(ctx: "StepContext", path: str, text: str) -> dict:
    p = _inside(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"path": str(p.relative_to(_workspace(ctx))), "bytes": len(text.encode())}


@tool("files.list", "List files in the project workspace", timeout_s=10)
def files_list(ctx: "StepContext", path: str = ".") -> list[str]:
    base = _inside(ctx, path)
    ws = _workspace(ctx)
    return sorted(str(f.relative_to(ws)) for f in base.rglob("*") if f.is_file())[:500]


@tool("shell.run", "Run a shell command in the project workspace (owner approval by default)", tier="t2",
      side_effects="runs a process", timeout_s=120)
async def shell_run(ctx: "StepContext", command: str, timeout_s: int = 60) -> dict:
    def go() -> dict:
        r = subprocess.run(command, shell=True, cwd=_workspace(ctx), capture_output=True, text=True, timeout=min(timeout_s, 110))
        return {"exit": r.returncode, "stdout": r.stdout[-8000:], "stderr": r.stderr[-4000:]}
    return await asyncio.to_thread(go)


@tool("tools.describe", "Full documentation (L2 card) of a tool", timeout_s=5)
def tools_describe(ctx: "StepContext", name: str) -> str:
    return all_tools()[name].card(2)
from . import repo  # noqa: E402,F401  (registers the repo.* tools)
