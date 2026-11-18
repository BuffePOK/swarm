"""Search for agents, free and within the rules of every source.

Sources (`[search] sources`, all free, all meant for programs):
  index          roj's own full-text index of sites agents have crawled (`web.crawl`): unlimited, local
  hn             Hacker News via Algolia: engineering blogs, launches, discussions
  github         GitHub repository search: projects, frameworks, their READMEs
  wikipedia      Wikipedia search: concepts and background
  stackexchange  Stack Overflow: practical problems and answers
Optional general web search (`[search] provider`): Tavily or Brave (key), or your own SearXNG.
Papers (`papers.search`): arXiv and Semantic Scholar.

Scraping search engines' result pages (Google, Bing, DuckDuckGo) is not done: their terms and
robots.txt forbid it, and evading their bot protection is not something roj does.
Searches are counted per day against `[search] daily_limit` and logged as events.
"""

from __future__ import annotations

import asyncio
import html as _html
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..util import day_key, utcnow_iso

if TYPE_CHECKING:
    from ..core.eventlog import EventLog
    from ..project import Installation, Project

ATOM = "{http://www.w3.org/2005/Atom}"
URLS = {
    "tavily": "https://api.tavily.com/search", "brave": "https://api.search.brave.com/res/v1/web/search",
    "hn": "https://hn.algolia.com/api/v1/search", "github": "https://api.github.com/search/repositories",
    "wikipedia": "https://en.wikipedia.org/w/api.php", "stackexchange": "https://api.stackexchange.com/2.3/search/advanced",
}
_last_arxiv = [0.0]  # arXiv asks for at most one request every 3 seconds


class SearchError(RuntimeError):
    pass


def _count(inst: "Installation", key_prefix: str = "search", limit: int | None = None, n: int = 1) -> None:
    store = inst.store
    key = f"{key_prefix}:{day_key(inst.settings.timezone)}"
    limit = limit if limit is not None else inst.settings.search.daily_limit
    row = store.one("SELECT value FROM kv WHERE key = ?", (key,))
    used = int(row["value"]) if row else 0
    if used + n > limit:
        raise SearchError(f"daily limit reached for {key_prefix} ({limit}); raise it in [search] or continue tomorrow")
    store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES(?,?,?)", (key, str(used + n), utcnow_iso()))


def _strip(s: Any) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", "", str(s or ""))).split())


def _clean(items: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    out, seen = [], set()
    for it in items:
        url = str(it.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({k: (v[:600] if isinstance(v, str) else v) for k, v in it.items() if v not in (None, "")})
        if len(out) >= n:
            break
    return out


def _url(cfg: Any, name: str) -> str:
    return cfg.endpoints.get(name) or URLS[name]


async def _hn(c: httpx.AsyncClient, cfg: Any, q: str, n: int) -> list[dict[str, Any]]:
    r = await c.get(_url(cfg, "hn"), params={"query": q, "tags": "story", "hitsPerPage": n})
    r.raise_for_status()
    return [{"title": h.get("title"), "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
             "snippet": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments on Hacker News",
             "published": str(h.get("created_at") or "")[:10], "discussion": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
             "source": "hn"} for h in r.json().get("hits", [])]


async def _github(c: httpx.AsyncClient, cfg: Any, q: str, n: int) -> list[dict[str, Any]]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(cfg.github_token_env, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await c.get(_url(cfg, "github"), params={"q": q, "per_page": n}, headers=headers)
    r.raise_for_status()
    return [{"title": x.get("full_name"), "url": x.get("html_url"), "snippet": x.get("description"),
             "published": str(x.get("pushed_at") or "")[:10], "stars": x.get("stargazers_count"), "source": "github"}
            for x in r.json().get("items", [])]


async def _wikipedia(c: httpx.AsyncClient, cfg: Any, q: str, n: int) -> list[dict[str, Any]]:
    r = await c.get(_url(cfg, "wikipedia"), params={"action": "query", "list": "search", "srsearch": q, "srlimit": n,
                                                    "format": "json", "utf8": 1})
    r.raise_for_status()
    base = _url(cfg, "wikipedia").rsplit("/w/api.php", 1)[0]
    return [{"title": x.get("title"), "url": f"{base}/wiki/{quote(str(x.get('title', '')).replace(' ', '_'))}",
             "snippet": _strip(x.get("snippet")), "published": str(x.get("timestamp") or "")[:10], "source": "wikipedia"}
            for x in (r.json().get("query") or {}).get("search", [])]


async def _stackexchange(c: httpx.AsyncClient, cfg: Any, q: str, n: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"order": "desc", "sort": "relevance", "q": q, "site": "stackoverflow", "pagesize": n}
    key = os.environ.get(cfg.stackexchange_key_env, "")
    if key:
        params["key"] = key
    r = await c.get(_url(cfg, "stackexchange"), params=params)
    r.raise_for_status()
    return [{"title": _strip(x.get("title")), "url": x.get("link"),
             "snippet": f"score {x.get('score', 0)}, {'answered' if x.get('is_answered') else 'unanswered'}",
             "published": datetime.fromtimestamp(int(x.get("creation_date") or 0), timezone.utc).date().isoformat(),
             "source": "stackexchange"} for x in r.json().get("items", [])]


async def _provider(c: httpx.AsyncClient, cfg: Any, q: str, n: int) -> list[dict[str, Any]]:
    if cfg.provider == "tavily":
        key = cfg.api_key()
        if not key:
            raise SearchError(f"Tavily needs a key in {cfg.key_env}")
        r = await c.post(cfg.api_url or URLS["tavily"], headers={"Authorization": f"Bearer {key}"},
                         json={"query": q, "max_results": n, "search_depth": "basic"})
        r.raise_for_status()
        items = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"), "published": x.get("published_date")}
                 for x in r.json().get("results", [])]
    elif cfg.provider == "brave":
        key = cfg.api_key()
        if not key:
            raise SearchError(f"Brave needs a key in {cfg.key_env}")
        r = await c.get(cfg.api_url or URLS["brave"], params={"q": q, "count": n}, headers={"X-Subscription-Token": key})
        r.raise_for_status()
        items = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description"), "published": x.get("age")}
                 for x in (r.json().get("web") or {}).get("results", [])]
    elif cfg.provider == "searxng":
        base = cfg.api_url or cfg.searxng_url
        if not base:
            raise SearchError("SearXNG needs [search] searxng_url")
        r = await c.get(base.rstrip("/") + "/search", params={"q": q, "format": "json"})
        r.raise_for_status()
        items = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"), "published": x.get("publishedDate")}
                 for x in r.json().get("results", [])]
    else:
        raise SearchError(f"unknown [search] provider {cfg.provider!r}")
    return [{**x, "source": cfg.provider} for x in items]


async def _index(project: "Project", q: str, n: int) -> list[dict[str, Any]]:
    from ..knowledge.packs import KnowledgeBase

    kb = KnowledgeBase(project)
    out = []
    for p in kb.packs():
        if not p["pack"].startswith("web-"):
            continue
        for h in await kb.search(p["pack"], q, k=n):
            out.append({"title": h.title, "url": h.source, "snippet": h.text[:400], "score": h.score, "source": "index"})
    return sorted(out, key=lambda x: -float(x.get("score") or 0))[:n]


FREE = {"hn": _hn, "github": _github, "wikipedia": _wikipedia, "stackexchange": _stackexchange}


async def web_search(inst: "Installation", query: str, n: int | None = None, events: "EventLog | None" = None,
                     project: "Project | None" = None, sources: list[str] | None = None) -> dict[str, Any]:
    cfg = inst.settings.search
    n = max(1, min(n or cfg.max_results, 20))
    use = [s for s in (sources or cfg.sources) if s in FREE or s == "index"]
    if cfg.provider and (sources is None or cfg.provider in sources or "web" in sources):
        use.append("provider")
    if not use:
        raise SearchError("no search sources configured ([search] sources / provider)")
    _count(inst)
    per: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=cfg.timeout_s, headers={"User-Agent": inst.settings.web.user_agent,
                                                                  "Accept": "application/json"}) as c:
        async def one(name: str) -> None:
            try:
                if name == "index":
                    per[name] = await _index(project, query, n) if project is not None else []
                elif name == "provider":
                    per[cfg.provider] = await _provider(c, cfg, query, n)
                else:
                    per[name] = await FREE[name](c, cfg, query, n)
            except (httpx.HTTPError, ValueError, KeyError, SearchError) as e:
                errors.append(f"{cfg.provider if name == 'provider' else name}: {type(e).__name__}: {str(e)[:150]}")
        await asyncio.gather(*(one(s) for s in use))
    merged: list[dict[str, Any]] = []  # round robin: the best of each source first
    lists = [per[k] for k in sorted(per, key=lambda k: (k != "index", k))]
    for i in range(max((len(x) for x in lists), default=0)):
        merged += [x[i] for x in lists if i < len(x)]
    out = _clean(merged, n * 2)
    (events or inst.events).append("web.search", {"query": query[:300], "sources": sorted(per), "results": len(out), "errors": errors})
    return {"results": out, "errors": errors, "sources": sorted(per)}


async def _arxiv(c: httpx.AsyncClient, url: str, query: str, n: int) -> list[dict[str, Any]]:
    wait = 3.0 - (time.monotonic() - _last_arxiv[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_arxiv[0] = time.monotonic()
    r = await c.get(url, params={"search_query": f"all:{query}", "start": 0, "max_results": n, "sortBy": "relevance"})
    r.raise_for_status()
    out = []
    for e in ET.fromstring(r.content).findall(f"{ATOM}entry"):
        text = lambda tag: " ".join((e.findtext(f"{ATOM}{tag}") or "").split())  # noqa: E731
        out.append({"title": text("title"), "url": text("id"), "snippet": text("summary"), "published": text("published")[:10],
                    "authors": [" ".join((a.findtext(f"{ATOM}name") or "").split()) for a in e.findall(f"{ATOM}author")][:6],
                    "source": "arxiv"})
    return out


async def _semantic_scholar(c: httpx.AsyncClient, url: str, query: str, n: int, key_env: str = "") -> list[dict[str, Any]]:
    key = os.environ.get(key_env, "") if key_env else ""
    params = {"query": query, "limit": n, "fields": "title,url,abstract,year,citationCount,venue"}
    r = await c.get(url, params=params, headers={"x-api-key": key} if key else None)
    if r.status_code == 429:  # the keyless pool is shared by everyone: wait once and retry
        await asyncio.sleep(3.0)
        r = await c.get(url, params=params, headers={"x-api-key": key} if key else None)
    r.raise_for_status()
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("abstract"), "published": x.get("year"),
             "citations": x.get("citationCount"), "venue": x.get("venue"), "source": "semantic_scholar"} for x in r.json().get("data") or []]


async def paper_search(inst: "Installation", query: str, n: int = 8, source: str = "all",
                       events: "EventLog | None" = None) -> dict[str, Any]:
    cfg = inst.settings.search
    n = max(1, min(n, 20))
    _count(inst)
    results, errors = [], []
    async with httpx.AsyncClient(timeout=cfg.timeout_s, headers={"User-Agent": inst.settings.web.user_agent}) as c:
        for name, url in (("arxiv", cfg.arxiv_url), ("semantic_scholar", cfg.semantic_scholar_url)):
            if source not in ("all", name):
                continue
            try:
                results += await (_arxiv(c, url, query, n) if name == "arxiv" else _semantic_scholar(c, url, query, n, cfg.semantic_scholar_key_env))
            except (httpx.HTTPError, ET.ParseError, ValueError) as e:
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")  # e.g. Semantic Scholar's shared rate limit
    out = _clean(results, 2 * n)
    (events or inst.events).append("papers.search", {"query": query[:300], "results": len(out), "errors": errors})
    return {"results": out, "errors": errors}
