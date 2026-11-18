"""roj's own search index: crawl a site (documentation, a blog, a project's pages) into a local
full-text knowledge pack `web-<host>`. Searching it afterwards is free, unlimited and offline
(`web.search` source `index`).

Polite by construction: robots.txt and per-host pacing come from the fetcher, the crawl stays
on one host under one path prefix, sitemaps are preferred over link discovery, query-string
variants are skipped, and pages are counted against `[search] crawl_daily_pages`. Unchanged
pages are not re-indexed (content hash). BM25 only, so indexing costs no tokens.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..util import sha256_hex
from .extract import extract, needs_browser
from .search import SearchError, _count

if TYPE_CHECKING:
    from ..project import Project

SKIP_EXT = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|pdf|zip|gz|tar|mp4|mp3|wav|woff2?|ttf|css|js|json|xml|rss|atom)$", re.I)


def pack_name(host: str) -> str:
    return ("web-" + re.sub(r"[^a-z0-9.-]+", "-", host.lower()).strip("-"))[:60]


def _norm(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path or '/'}"


def _robots_sitemaps(project: "Project", origin: str) -> list[str]:
    row = project.store.one("SELECT body FROM robots_cache WHERE origin = ?", (origin,))
    return [m.group(1).strip() for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", (row["body"] or "") if row else "")]


async def _sitemap(project: "Project", origin: str, host: str, prefix: str, limit: int) -> list[str]:
    out: list[str] = []
    todo, seen = deque(dict.fromkeys(_robots_sitemaps(project, origin) + [origin + "/sitemap.xml"])), set()
    while todo and len(seen) < 6 and len(out) < limit:
        sm = todo.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        r = await project.fetcher.get(sm, purpose="crawl:sitemap")
        if not r.ok:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for el in root.iter():
            if el.tag.endswith("loc") and el.text:
                loc = el.text.strip()
                if el.text and any(p.tag.endswith("sitemap") for p in root) and loc.endswith(".xml"):
                    todo.append(loc)
                elif urlsplit(loc).netloc == host and urlsplit(loc).path.startswith(prefix) and "?" not in loc:
                    out.append(_norm(loc))
    return out[:limit]


async def crawl(project: "Project", start: str, *, max_pages: int = 50, depth: int = 3, prefix: str | None = None,
                render: str = "auto") -> dict[str, Any]:
    from ..knowledge.packs import KnowledgeBase

    u = urlsplit(start)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("start must be an http(s) URL")
    host, origin = u.netloc, f"{u.scheme}://{u.netloc}"
    explicit_prefix = prefix
    prefix = prefix if prefix is not None else (u.path.rsplit("/", 1)[0] + "/" if "/" in u.path else "/")
    stats: dict[str, Any] = {"pack": "", "prefix": prefix, "indexed": 0, "unchanged": 0, "thin": 0, "rendered": 0,
                             "refused": 0, "errors": []}
    first = await project.fetcher.get(start, purpose="crawl:start")
    if first.ok and first.final_url and _norm(first.final_url) != _norm(start):
        fu = urlsplit(first.final_url)  # moved (e.g. docs.claude.com -> platform.claude.com/docs): follow the site, not the old address
        stats["redirected_to"] = first.final_url
        if fu.netloc != host or not fu.path.startswith(prefix):
            host, origin = fu.netloc, f"{fu.scheme}://{fu.netloc}"
            if explicit_prefix is None:
                prefix = fu.path.rsplit("/", 1)[0] + "/" if "/" in fu.path else "/"
        start = first.final_url
    stats["prefix"] = prefix
    kb = KnowledgeBase(project)
    pack = pack_name(host)
    stats["pack"] = pack
    if pack not in {p["pack"] for p in kb.packs()}:
        kb.create(pack, f"Pages of {host} indexed by roj's crawler (full text, sources are the page URLs)")
    queue: deque[tuple[str, int]] = deque([(_norm(start), 0)])
    for loc in await _sitemap(project, origin, host, prefix, max_pages * 3):
        queue.append((loc, 1))
    seen: set[str] = set()
    browser = None
    while queue and stats["indexed"] + stats["unchanged"] < max_pages:
        url, d = queue.popleft()
        if url in seen or SKIP_EXT.search(urlsplit(url).path):
            continue
        seen.add(url)
        try:
            _count(project.inst, "crawl", project.inst.settings.search.crawl_daily_pages)
        except SearchError as e:
            stats["errors"].append(str(e))
            break
        r = await project.fetcher.get(url, purpose="crawl")
        if not r.ok:
            if r.error and "robots" in r.error:
                stats["refused"] += 1
            else:
                stats["errors"].append(f"{url}: {r.error or r.status}")
            continue
        html, final = r.text(), r.where
        page = extract(html, final)
        if render == "always" or (render == "auto" and needs_browser(html, page)):
            from .browser import BrowserUnavailable, pool

            try:
                browser = browser or pool(project.inst)
                rr = await browser.render(project, url)
                if rr.html and not rr.refused:
                    html, final, page = rr.html, rr.url, extract(rr.html, rr.url)
                    stats["rendered"] += 1
            except BrowserUnavailable as e:
                render = "never"
                stats["errors"].append(f"browser: {e}")
        if page.chars < 200:
            stats["thin"] += 1
        else:
            header = f"Source: {final}\n" + (f"Published: {page.published}\n" if page.published else "")
            added = await kb.add_text(pack, sha256_hex(_norm(final))[:16], page.title or final, header + "\n" + page.text, source=final)
            stats["indexed" if added else "unchanged"] += 1
        if d < depth:
            for link in page.all_links:
                lu = urlsplit(link)
                if lu.netloc == host and lu.path.startswith(prefix) and not lu.query and _norm(link) not in seen:
                    queue.append((_norm(link), d + 1))
    stats["errors"] = stats["errors"][:10]
    project.events.append("web.crawl", {"start": start[:300], **{k: v for k, v in stats.items() if k != "errors"},
                                        "errors": len(stats["errors"])})
    return stats
