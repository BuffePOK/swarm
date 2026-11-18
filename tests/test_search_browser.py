import asyncio

import pytest

from roj.core.kernel import StepContext
from roj.hands.crawl import crawl
from roj.hands.extract import extract, needs_browser
from roj.hands.search import web_search
from roj.tools import registry


def run(c):
    return asyncio.run(c)


def call(project, tool, **args):
    return run(registry.call(StepContext(project, None, None, "t", 0, 1, {}, {}), tool, args, agent="t"))


def browser_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_extract_keeps_content_drops_clutter():
    html = ("<html lang='en'><head><title>T</title><meta property='article:published_time' content='2026-03-02'></head><body>"
            "<header><nav><a href='/'>Home</a></nav></header><div id='cookie-consent'>We use cookies</div>"
            "<main><h1>Agent loops</h1><p>" + "An agent loop calls a <a href='/tools'>tool</a> and observes. " * 5 + "</p>"
            "<pre><code>while True:\n    step()</code></pre><table><tr><th>M</th><th>P</th></tr><tr><td>A</td><td>1</td></tr></table>"
            "<div class='share-bar'>Share</div></main><footer>(c) 2026</footer></body></html>")
    e = extract(html, "https://d.test/x/y")
    assert e.text.startswith("# Agent loops") and "while True:\n    step()" in e.text and "| A | 1 |" in e.text
    assert all(w not in e.text for w in ("cookies", "Home", "Share", "(c) 2026"))
    assert e.links == [{"text": "tool", "url": "https://d.test/tools"}] and "https://d.test/" in e.all_links
    assert e.published == "2026-03-02" and e.lang == "en" and not needs_browser(html, e)
    shell = '<html><body><div id="root"></div><script src=a.js></script><script>b()</script><script>c()</script></body></html>'
    assert needs_browser(shell, extract(shell))


def test_free_sources_are_merged_and_failures_reported(project):
    r = run(web_search(project.inst, "agent harness", 4, project=project))
    sources = {x["source"] for x in r["results"]}
    assert {"hn", "github", "wikipedia", "searxng"} <= sources
    assert r["results"][0]["source"] != r["results"][1]["source"]  # round robin: the best of each source first
    assert any(e.startswith("stackexchange") for e in r["errors"])  # one source down does not stop the others
    wiki = next(x for x in r["results"] if x["source"] == "wikipedia")
    assert wiki["url"].endswith("/wiki/Multi-agent_system") and "<span>" not in wiki["snippet"]
    only = run(web_search(project.inst, "x", 3, project=project, sources=["github"]))
    assert {x["source"] for x in only["results"]} == {"github"}


def test_crawl_builds_a_free_local_index(project, web):
    _, url = web
    stats = run(crawl(project, f"{url}/docs-site/a", max_pages=10, render="never"))
    assert stats["pack"].startswith("web-") and stats["prefix"] == "/docs-site/"
    assert stats["indexed"] == 3 and stats["thin"] == 1  # a, b, c indexed; the JavaScript shell is thin without a browser
    docs = {r["title"] for r in project.store.all("SELECT title FROM kb_docs WHERE pack = ?", (stats["pack"],))}
    assert docs == {"Docs a", "Docs b", "Docs c"}  # /other/x (sitemap) and ?sort= variants stay out
    again = run(crawl(project, f"{url}/docs-site/a", max_pages=10, render="never"))
    assert again["indexed"] == 0 and again["unchanged"] == 3
    hits = run(web_search(project.inst, "compaction agent conversations", 3, project=project, sources=["index"]))
    assert hits["results"] and hits["results"][0]["source"] == "index" and "/docs-site/" in hits["results"][0]["url"]


def test_web_fetch_returns_clean_text_and_respects_robots(project, web):
    _, url = web
    r = call(project, "web.fetch", url=f"{url}/docs-site/b", render="never")
    assert r["ok"] and r["result"]["title"] == "Docs b" and "Accept cookies" not in r["result"]["text"]
    assert r["result"]["text"].startswith("# Page b") and r["result"]["rendered"] is False
    blocked = call(project, "web.fetch", url=f"{url}/private/x")
    assert not blocked["ok"] and "robots" in blocked["error"]


@pytest.mark.skipif(not browser_ok(), reason="Chromium not available")
def test_browser_renders_javascript_pages(project, web):
    _, url = web
    r = call(project, "web.fetch", url=f"{url}/docs-site/js")
    assert r["ok"] and r["result"]["rendered"] is True and "exists only after JavaScript" in r["result"]["text"], r
    stats = run(crawl(project, f"{url}/docs-site/js", max_pages=10))
    assert stats["rendered"] >= 1 and stats["indexed"] == 4
    ev = project.store.one("SELECT COUNT(*) n FROM events WHERE type = 'web.render'")["n"]
    assert ev >= 2


def test_crawl_follows_a_site_that_moved_and_survives_errors(project, web):
    _, url = web
    port = url.rsplit(":", 1)[-1]
    stats = run(crawl(project, f"http://127.0.0.1:{port}/moved-docs/start", max_pages=10, render="never"))
    assert stats["redirected_to"] == f"http://localhost:{port}/docs-site/a"
    assert stats["pack"] == f"web-localhost-{port}" and stats["prefix"] == "/docs-site/" and stats["indexed"] == 3
    bad = run(crawl(project, f"{url}/missing-docs/gone", max_pages=5, render="never"))
    assert bad["indexed"] == 0 and bad["errors"] and "404" in bad["errors"][0]
