import asyncio
import json

import pytest

from roj.hands.feeds import FeedError, discover_feeds, parse_date, parse_feed
from roj.hands.http import mask_url
from roj.hands.market import InstrumentSpec, MarketError, build_url, parse_quote
from roj.testing import fake_web


def test_rss_with_z_dates_and_html():
    title, items = parse_feed(fake_web.rss("CB", fake_web.CB, z_dates=True))
    assert title == "CB" and len(items) == 3
    assert all(i.published and i.published.endswith("+00:00") for i in items)
    _, md = parse_feed(fake_web.rss("M", fake_web.MARKETS, html_desc=True))
    assert "<p>" not in md[0].summary and "2.9%" in md[0].summary


def test_atom_and_rdf():
    _, a = parse_feed(fake_web.atom("W", fake_web.WIRE))
    assert len(a) == 6 and a[0].link.startswith("https://wire.example.invalid/")
    _, r = parse_feed(fake_web.rdf("P", fake_web.ASIA))
    assert len(r) == 3 and r[0].published


def test_entity_bomb_and_garbage_rejected():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
    with pytest.raises(FeedError):
        parse_feed(bomb)
    with pytest.raises(FeedError):
        parse_feed(b"<rss><channel><item><title>oops")
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>not a feed</body></html>")


def test_dates():
    assert parse_date("Thu, 05 Nov 2026 12:00:00 Z").isoformat() == "2026-11-05T12:00:00+00:00"
    assert parse_date("2026-11-05T13:00:00+01:00").isoformat() == "2026-11-05T12:00:00+00:00"
    assert parse_date("not a date") is None


def test_discovery():
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'
    assert discover_feeds(html, "https://site.example/news/") == ["https://site.example/feed.xml"]


def test_url_masking():
    assert mask_url("https://x.example/a?series_id=SP500&api_key=SECRET") == "https://x.example/a?series_id=SP500&api_key=***"


def test_market_parsers(monkeypatch):
    fx = InstrumentSpec(id="eurusd", label="EUR/USD", provider="frankfurter", params={"base": "EUR", "quote": "USD"}, decimals=4)
    q = parse_quote(fx, json.dumps([{"date": "2026-11-06", "quote": "USD", "rate": 1.12}, {"date": "2026-11-07", "quote": "USD", "rate": 1.1}]).encode())
    assert q.value == 1.1 and round(q.change_pct, 2) == -1.79 and q.as_of == "2026-11-07"
    fred = InstrumentSpec(id="sp500", label="S&P", provider="fred", params={"series": "SP500"})
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(MarketError, match="FRED_API_KEY"):
        build_url(fred)
    monkeypatch.setenv("FRED_API_KEY", "k")
    assert "series_id=SP500" in build_url(fred)
    q2 = parse_quote(fred, json.dumps({"observations": [{"date": "d2", "value": "."}, {"date": "d1", "value": "100"}, {"date": "d0", "value": "50"}]}).encode())
    assert q2.value == 100 and q2.change_pct == 100.0
    generic = InstrumentSpec(id="x", label="X", provider="json", url="https://x/{id}", params={"id": 1, "value_path": "data[-1].v"})
    assert parse_quote(generic, b'{"data": [{"v": 1}, {"v": 2.5}]}').value == 2.5


def test_fetcher_robots_cache_and_masking(project, web):
    state, url = web
    f = project.fetcher
    r1 = asyncio.run(f.get(f"{url}/cb/rss.xml"))
    assert r1.ok and r1.ref
    blocked = asyncio.run(f.get(f"{url}/private/feed.xml"))
    assert not blocked.ok and "robots" in blocked.error
    assert state.hits["/private/feed.xml"] == 0  # never requested
    missing = asyncio.run(f.get(f"{url}/nope?api_key=SECRET"))
    assert missing.status == 404 and not missing.ok
    logged = json.dumps([e.payload for e in project.events.query(type="http.fetch")])
    assert "SECRET" not in logged and "api_key=***" in logged
