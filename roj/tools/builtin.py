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


@tool("web.fetch", "Read a public web page as clean text (main content, headings, code, tables) plus its links; "
      "pages built by JavaScript are rendered in a headless browser. robots.txt is respected", timeout_s=90)
async def web_fetch(ctx: "StepContext", url: str, max_chars: int = 8000, render: str = "auto", links: int = 30) -> dict:
    """render: auto (only when the page needs JavaScript) | always | never"""
    from ..hands.extract import extract, needs_browser

    r = await ctx.project.fetcher.get(url, purpose="tool:web.fetch")
    html, final, rendered, note = "", url, False, None
    if r.ok:
        if looks_like_feed(r.content):
            try:
                title, items = parse_feed(r.content, base_url=url)
                return {"kind": "feed", "title": title, "items": [i.as_dict() for i in items[:40]]}
            except FeedError:
                pass
        html, final = r.text(), r.where
    elif render == "never" or (r.error and "robots" in r.error):
        raise ToolError(r.error or f"HTTP {r.status}")
    page = extract(html, final, limit=max(1000, min(max_chars, MAX_TEXT))) if html else None
    if render == "always" or (render == "auto" and (page is None or needs_browser(html, page))):
        from ..hands.browser import BrowserUnavailable, pool

        try:
            rr = await pool(ctx.project.inst).render(ctx.project, url)
            if rr.refused:
                note = rr.refused
            elif rr.html:
                html, final, rendered = rr.html, rr.url, True
                page = extract(html, final, limit=max(1000, min(max_chars, MAX_TEXT)))
        except BrowserUnavailable as e:
            note = f"not rendered: {e}"
    if page is None:
        raise ToolError(note or r.error or f"HTTP {r.status}")
    out = {"kind": "page", "url": final, "title": page.title, "published": page.published, "text": page.text,
           "links": page.links[: max(0, min(links, 100))], "rendered": rendered, "ref": r.ref}
    if note:
        out["note"] = note
    return out


@tool("web.search", "Search: roj's own index of crawled sites, Hacker News, GitHub, Wikipedia, Stack Overflow (and a general web "
      "search if configured). Returns title, url, snippet, date, source. Read results with web.fetch; index a whole site with web.crawl",
      timeout_s=60)
async def web_search_tool(ctx: "StepContext", query: str, max_results: int = 8, sources: str = "") -> dict:
    """sources: comma-separated subset of index, hn, github, wikipedia, stackexchange, web (empty = all configured)"""
    from ..hands.search import SearchError, web_search

    try:
        return await web_search(ctx.project.inst, query, max_results, events=ctx.project.events, project=ctx.project,
                                sources=[x.strip() for x in sources.split(",") if x.strip()] or None)
    except SearchError as e:
        raise ToolError(str(e)) from e


@tool("web.crawl", "Index a site section (e.g. a documentation) into roj's own search index so it can be searched "
      "for free with web.search source 'index'. Stays on one host under the URL's path; robots.txt respected",
      tier="t1", side_effects="adds pages to the project's knowledge base", timeout_s=1800)
async def web_crawl_tool(ctx: "StepContext", url: str, max_pages: int = 40, depth: int = 3, prefix: str = "") -> dict:
    from ..hands.crawl import crawl

    try:
        return await crawl(ctx.project, url, max_pages=max(1, min(max_pages, 300)), depth=max(0, min(depth, 6)), prefix=prefix or None)
    except ValueError as e:
        raise ToolError(str(e)) from e


@tool("papers.search", "Search research papers (arXiv and Semantic Scholar): title, url, abstract, year, citations",
      timeout_s=60)
async def papers_search_tool(ctx: "StepContext", query: str, max_results: int = 8, source: str = "all") -> dict:
    from ..hands.search import SearchError, paper_search

    try:
        return await paper_search(ctx.project.inst, query, max_results, source, events=ctx.project.events)
    except SearchError as e:
        raise ToolError(str(e)) from e


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
        r = subprocess.run(command, shell=True, cwd=_workspace(ctx), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=min(timeout_s, 110))
        return {"exit": r.returncode, "stdout": r.stdout[-8000:], "stderr": r.stderr[-4000:]}
    return await asyncio.to_thread(go)


@tool("tools.describe", "Full documentation (L2 card) of a tool", timeout_s=5)
def tools_describe(ctx: "StepContext", name: str) -> str:
    return all_tools()[name].card(2)
from . import repo  # noqa: E402,F401  (registers the repo.* tools)
