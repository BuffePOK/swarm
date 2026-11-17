"""Main-content extraction: from a web page to clean text an agent can read cheaply.

Drops scripts, styles, navigation, footers, cookie banners, ads, share bars and forms; keeps
the main article (main / article / role=main, or the densest block) with headings, lists,
code blocks and tables in a light markdown form. Returns the links separately, so an agent
can follow them without the page text being cluttered. Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

DROP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "button", "nav", "footer", "aside", "template", "canvas",
             "select", "input", "textarea", "object", "embed", "dialog", "head"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
         "table", "tr", "dl", "dt", "dd", "figure", "figcaption", "header", "details", "summary", "hr", "br"}
JUNK = re.compile(r"(?:^|[\s_-])(?:cookie|consent|gdpr|banner|newsletter|subscribe|popup|modal|advert|ads?|sponsor|promo|share|"
                  r"social|breadcrumbs?|sidebar|related|comments?|footer|navbar|nav|menu|skip-link|toc-mobile|feedback|rating)(?:$|[\s_-])",
                  re.I)


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root
        self.meta: dict[str, str] = {}
        self.title = ""
        self.ld: list[str] = []
        self._in_title = self._in_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "title":
            self._in_title = True
        if tag == "script" and "ld+json" in a.get("type", ""):
            self._in_ld = True
            self.ld.append("")
        n = Node(tag, a, parent=self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_ld = False
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent  # type: ignore[assignment]
        if n is not self.root:
            self.cur = n.parent or self.root

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_ld:
            self.ld[-1] += data
        self.cur.children.append(data)


@dataclass
class Extracted:
    title: str
    text: str
    links: list[dict[str, str]]
    published: str | None = None
    description: str | None = None
    lang: str | None = None
    all_links: list[str] = field(default_factory=list)  # every link on the page, menus included (for crawling)

    @property
    def chars(self) -> int:
        return len(self.text)


def _junk(n: Node, inside_main: bool) -> bool:
    if n.tag in DROP_TAGS or (n.tag == "header" and not inside_main):
        return True
    a = n.attrs
    if a.get("hidden") is not None and "hidden" in a or a.get("aria-hidden") == "true":
        return True
    if re.search(r"display\s*:\s*none", a.get("style", ""), re.I):
        return True
    role = a.get("role", "")
    if role in ("navigation", "banner", "contentinfo", "complementary", "dialog", "search"):
        return True
    return bool(JUNK.search(" ".join([a.get("id", ""), a.get("class", "")]))) and n.tag not in ("main", "article", "body", "html")


def _text_len(n: Node, inside: bool = False) -> int:
    total = 0
    for c in n.children:
        if isinstance(c, str):
            total += len(c.strip())
        elif not _junk(c, inside):
            total += _text_len(c, inside or c.tag in ("main", "article"))
    return total


def _link_len(n: Node) -> int:
    total = 0
    for c in n.children:
        if isinstance(c, Node):
            total += _text_len(c) if c.tag == "a" else _link_len(c)
    return total


def _find(n: Node, pred) -> list[Node]:
    out = []
    for c in n.children:
        if isinstance(c, Node):
            if pred(c):
                out.append(c)
            out += _find(c, pred)
    return out


def _choose_root(root: Node) -> Node:
    for pred in (lambda x: x.tag == "main" or x.attrs.get("role") == "main", lambda x: x.tag == "article"):
        cands = [c for c in _find(root, pred) if _text_len(c, True) >= 200]
        if cands:
            return max(cands, key=lambda c: _text_len(c, True))
    blocks = [c for c in _find(root, lambda x: x.tag in ("div", "section", "td")) if _text_len(c) >= 300]
    if blocks:  # densest block: much text, few link characters
        best = max(blocks, key=lambda c: _text_len(c) - 2 * _link_len(c))
        if _text_len(best) - 2 * _link_len(best) > 200:
            return best
    body = _find(root, lambda x: x.tag == "body")
    return body[0] if body else root


class _Render:
    def __init__(self, base: str) -> None:
        self.base = base
        self.out: list[str] = []
        self.links: list[dict[str, str]] = []
        self._seen: set[str] = set()

    def link(self, href: str, text: str) -> None:
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            return
        url = urljoin(self.base, href).split("#")[0]
        if urlsplit(url).scheme in ("http", "https") and url not in self._seen:
            self._seen.add(url)
            self.links.append({"text": " ".join(text.split())[:120], "url": url})

    def inline(self, n: Node, inside: bool) -> str:
        parts = []
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            elif not _junk(c, inside):
                if c.tag == "br":
                    parts.append("\n")
                elif c.tag == "code" and n.tag != "pre":
                    parts.append(f"`{self.inline(c, inside).strip()}`")
                elif c.tag == "a":
                    t = self.inline(c, inside)
                    self.link(c.attrs.get("href", ""), t)
                    parts.append(t)
                elif c.tag == "img" and c.attrs.get("alt"):
                    parts.append(f"[image: {c.attrs['alt']}]")
                elif c.tag in BLOCK:
                    self.block(c, inside)
                else:
                    parts.append(self.inline(c, inside))
        return "".join(parts)

    def flush(self, text: str, prefix: str = "") -> None:
        t = " ".join(text.split())
        if t:
            self.out.append(prefix + t)

    def block(self, n: Node, inside: bool = False) -> None:
        inside = inside or n.tag in ("main", "article")
        if _junk(n, inside):
            return
        tag = n.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush(self.inline(n, inside), "#" * int(tag[1]) + " ")
        elif tag == "pre":
            code = "".join(c if isinstance(c, str) else self._raw(c) for c in n.children).strip("\n")
            if code.strip():
                self.out.append(f"```\n{code}\n```")
        elif tag == "li":
            self.flush(self.inline(n, inside), "- ")
        elif tag == "tr":
            cells = [" ".join(self.inline(c, inside).split()) for c in n.children if isinstance(c, Node) and c.tag in ("td", "th")]
            if any(cells):
                self.out.append("| " + " | ".join(cells) + " |")
        elif tag == "blockquote":
            self.flush(self.inline(n, inside), "> ")
        elif tag in ("ul", "ol", "table", "thead", "tbody", "div", "section", "article", "main", "body", "html", "#root",
                     "figure", "details", "dl", "header", "span") or any(isinstance(c, Node) and c.tag in BLOCK for c in n.children):
            buf: list[str] = []
            for c in n.children:
                if isinstance(c, str):
                    buf.append(c)
                elif _junk(c, inside):
                    continue
                elif c.tag in BLOCK or c.tag in ("thead", "tbody", "span") and any(isinstance(x, Node) and x.tag in BLOCK for x in c.children):
                    self.flush("".join(buf))
                    buf = []
                    self.block(c, inside)
                else:
                    buf.append(self.inline(Node("#span", {}, [c]), inside))
            self.flush("".join(buf))
        else:
            self.flush(self.inline(n, inside))

    def _raw(self, n: Node) -> str:
        return "".join(c if isinstance(c, str) else self._raw(c) for c in n.children)


def _published(t: _Tree, root: Node) -> str | None:
    for k in ("article:published_time", "datepublished", "date", "dc.date", "pubdate", "og:updated_time", "article:modified_time"):
        if t.meta.get(k):
            return t.meta[k][:25]
    for raw in t.ld:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict):
                for k in ("datePublished", "dateCreated", "dateModified"):
                    if d.get(k):
                        return str(d[k])[:25]
    times = _find(root, lambda x: x.tag == "time" and x.attrs.get("datetime"))
    return times[0].attrs["datetime"][:25] if times else None


def extract(html: str, base_url: str = "", limit: int = 20_000) -> Extracted:
    t = _Tree()
    try:
        t.feed(html)
        t.close()
    except Exception:  # noqa: BLE001 - malformed markup: plain text fallback
        text = " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())
        return Extracted(title="", text=text[:limit], links=[])
    main = _choose_root(t.root)
    r = _Render(base_url)
    r.block(main, main.tag in ("main", "article"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(r.out)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…[truncated]"
    every: list[str] = []
    seen: set[str] = set()
    for a in _find(t.root, lambda x: x.tag == "a" and x.attrs.get("href")):
        href = a.attrs["href"]
        if href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        u = urljoin(base_url, href).split("#")[0]
        if urlsplit(u).scheme in ("http", "https") and u not in seen:
            seen.add(u)
            every.append(u)
    htmls = _find(t.root, lambda x: x.tag == "html")
    title = (t.meta.get("og:title") or " ".join(t.title.split()) or "").strip()
    return Extracted(title=title[:300], text=text, links=r.links[:200], published=_published(t, t.root),
                     description=(t.meta.get("description") or t.meta.get("og:description") or "")[:400] or None,
                     lang=htmls[0].attrs.get("lang") if htmls else None, all_links=every[:1000])


def needs_browser(html: str, extracted: Extracted) -> bool:
    """A page whose content is built by JavaScript: little text but scripts or an app root."""
    if extracted.chars >= 600:
        return False
    low = html.lower()
    return ("<script" in low and (low.count("<script") >= 3 or 'id="root"' in low or 'id="app"' in low or "__next" in low
                                  or "enable javascript" in low or "requires javascript" in low)) or extracted.chars < 120
