"""Feed parsing (RSS 2.0, RSS 1.0/RDF, Atom) and discovery, stdlib only.

Items keep only what a digest needs: title, link, date and a short plain-text
summary. Full articles are never stored.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

MAX_FEED_BYTES = 5_000_000
SUMMARY_CHARS = 500


class FeedError(ValueError):
    pass


@dataclass
class FeedItem:
    title: str
    link: str
    published: str | None  # ISO 8601 UTC
    summary: str
    guid: str

    def as_dict(self) -> dict:
        return asdict(self)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str | None, limit: int | None = SUMMARY_CHARS) -> str:
    if not s:
        return ""
    if "<" in s:
        p = _TextExtractor()
        try:
            p.feed(s)
            p.close()
            s = "".join(p.parts)
        except Exception:  # noqa: BLE001 - malformed markup: fall back to regex strip
            s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(re.sub(r"\s+Z$", " +0000", s))
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_feed(data: bytes, base_url: str = "") -> tuple[str, list[FeedItem]]:
    """Return (feed title, items). Raises FeedError for anything that is not a feed."""
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("feed too large")
    head = data[:65536]
    if b"<!ENTITY" in head or b"<!DOCTYPE" in head and b"[" in head.split(b"<!DOCTYPE", 1)[1][:2000]:
        raise FeedError("feeds with DTD entity declarations are rejected")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"not well-formed XML: {e}") from e
    kind = _local(root.tag)
    items: list[FeedItem] = []
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("rss without channel")
        title = _text(_child(channel, "title"))
        entries = [c for c in channel if _local(c.tag) == "item"]
    elif kind == "rdf":
        channel = _child(root, "channel")
        title = _text(_child(channel, "title")) if channel is not None else ""
        entries = [c for c in root if _local(c.tag) == "item"]
    elif kind == "feed":
        title = _text(_child(root, "title"))
        entries = [c for c in root if _local(c.tag) == "entry"]
    else:
        raise FeedError(f"unknown feed root <{kind}>")
    for e in entries:
        t = html_to_text(_text(_child(e, "title")), limit=300)
        link = ""
        if kind == "feed":
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                    link = c.get("href", "")
                    break
        else:
            link = _text(_child(e, "link"))
        link = urljoin(base_url, link) if link and base_url else link
        date_el = _child(e, "pubdate", "published", "updated", "date", "issued")
        published = _iso(parse_date(_text(date_el)))
        summ_el = _child(e, "description", "summary", "content", "encoded")
        summary = html_to_text(_text(summ_el) if summ_el is not None and len(summ_el) == 0 else
                               (ET.tostring(summ_el, encoding="unicode", method="html") if summ_el is not None else ""))
        guid = _text(_child(e, "guid", "id")) or link or t
        if t or link:
            items.append(FeedItem(title=t, link=link, published=published, summary=summary, guid=guid))
    return title, items


class _LinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in (
            "application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json",
        ) and a.get("href"):
            self.feeds.append(a["href"])


def discover_feeds(page: bytes | str, base_url: str) -> list[str]:
    """Feed URLs advertised by an HTML page via <link rel="alternate">."""
    text = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
    f = _LinkFinder()
    try:
        f.feed(text[:500_000])
    except Exception:  # noqa: BLE001
        pass
    seen: list[str] = []
    for href in f.feeds:
        u = urljoin(base_url, href)
        if u not in seen:
            seen.append(u)
    return seen


def looks_like_feed(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head
