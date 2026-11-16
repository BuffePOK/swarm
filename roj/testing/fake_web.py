"""A synthetic world for offline tests and demos: news sites, market data, Telegram.

Everything is invented (Nordland, Atlantica, Pacifica, Zentra), timestamps are
relative to the current time, and failure modes can be switched on:

  state.moved_wire = True     /wire/atom.xml 404, homepage advertises /wire/v2/atom.xml
  state.energy_v2 = True      Energy Desk changes its HTML markup (old adapter breaks)

Run:  python -m roj.testing.fake_web --port 8046
Seeds: roj skill bootstrap -p demo news_digest --seeds offline:http://127.0.0.1:8046
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape


class FakeWebState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits: Counter[str] = Counter()
        self.telegram: list[dict[str, Any]] = []
        self.telegram_calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []  # inject owner clicks for tests
        self.pulls: list[dict[str, Any]] = []
        self.moved_wire = False
        self.energy_v2 = False


def _ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


CB = [
    ("Central Bank of Nordland holds policy rate at 4.25%",
     "The Central Bank of Nordland kept its main policy rate at 4.25% and said inflation risks remain tilted to the upside.", 3),
    ("Nordland regulators propose higher capital buffers for large banks",
     "Bank regulators in Nordland proposed raising the countercyclical capital buffer to 2% from 1.5%.", 5),
    ("Central Bank of Nordland publishes financial stability report",
     "The report flags elevated commercial property risks for lenders.", 8),
]
WIRE = [
    ("Nordland central bank keeps rate at 4.25%, signals patience",
     "Policymakers at the Central Bank of Nordland held the policy rate at 4.25% for a third meeting.", 2.5),
    ("Atlantica inflation slows to 2.9% in October",
     "Consumer prices in Atlantica rose 2.9% from a year earlier, down from 3.1% in September.", 4),
    ("Brent crude jumps 3% after Zentra exporters cut supply",
     "Brent crude rose 3% to 84.10 dollars a barrel after exporters in Zentra announced a supply cut of 400,000 barrels a day.", 6),
    ("Pacifica factory output falls 1.8% as exports weaken",
     "Industrial production in Pacifica dropped 1.8% in September as overseas demand cooled.", 7),
    ("Atlantica and Pacifica to resume tariff talks on steel",
     "Trade negotiators from Atlantica and Pacifica will meet again to discuss tariffs on steel imports.", 9),
    ("Weekly wrap: markets end mixed", "A look back at the week.", 30),
]
MARKETS = [
    ("Atlantica inflation cools to 2.9%, boosting rate-cut bets",
     "<p>Annual inflation in Atlantica slowed to <b>2.9%</b> in October, raising expectations of a rate cut.</p>", 3.5),
    ("Tech shares lead overnight rally on the Atlantica 500 index",
     "<p>The Atlantica 500 index rose 1.2% as technology shares gained.</p>", 5),
    ("Pacifica currency weakens past 155 per dollar",
     "<p>The Pacifica dollar weakened past 155 per US dollar, its lowest level since spring.</p>", 6),
    ("Weekend lifestyle: best coffee shops in Nordland", "<p>Our guide to the cafes worth a visit.</p>", 4),
    ("Oil climbs 3% as Zentra trims output", "<p>Oil prices climbed 3% after Zentra cut production.</p>", 5.5),
]
ASIA = [
    ("Pacifica industrial production drops 1.8% in September", "Factory output in Pacifica fell 1.8% from August.", 6.5),
    ("Pacifica currency slides past 155 per dollar", "The currency hit 155 per dollar in early trade.", 5.5),
    ("Pacifica central bank says it watches currency moves closely", "Officials signalled readiness to act on volatility.", 4.5),
]
LATAM = [
    ("Andesia copper exports rise 4% on strong Pacifica demand", "Copper shipments from Andesia grew 4% in October.", 5),
    ("Andesia central bank cuts policy rate to 6.5%", "The central bank of Andesia lowered its policy rate by 25 basis points to 6.5%.", 3.2),
    ("Andesia currency firms after rate decision", "The currency gained against the dollar after the decision.", 2.8),
    ("Regional lenders in Andesia report record profits", "Banks benefited from higher lending margins.", 8),
]
ENERGY = [
    ("Zentra exporters cut supply by 400,000 barrels a day", "The cut lifted Brent crude by 3%.", 6),
    ("Refiners in Atlantica brace for higher input costs", "Margins are expected to narrow this quarter.", 7),
    ("Nordland wind output hits seasonal record", "Strong winds pushed power prices lower.", 8),
    ("Gas storage in Atlantica stays above average", "Inventories remain comfortable before winter.", 10),
    ("Zentra oil minister defends production cut", "He said the market was oversupplied.", 6.2),
]


def rss(title: str, entries, *, z_dates: bool = False, html_desc: bool = False) -> bytes:
    items = []
    for i, (t, d, h) in enumerate(entries):
        date = format_datetime(_ago(h), usegmt=True)
        if z_dates:
            date = date.replace(" GMT", " Z")
        desc = f"<![CDATA[{d}]]>" if html_desc else escape(d)
        items.append(f"<item><title>{escape(t)}</title><link>https://example.invalid/{hashlib.sha256(t.encode()).hexdigest()[:10]}</link>"
                     f"<guid>{escape(t)}</guid><pubDate>{date}</pubDate><description>{desc}</description></item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(title)}</title>'
            f"<link>https://example.invalid/</link><description>synthetic</description>{''.join(items)}</channel></rss>").encode()


def atom(title: str, entries) -> bytes:
    es = "".join(
        f"<entry><title>{escape(t)}</title><link rel=\"alternate\" href=\"https://wire.example.invalid/{i}\"/>"
        f"<id>urn:wire:{i}</id><updated>{_ago(h).isoformat(timespec='seconds')}</updated><summary>{escape(d)}</summary></entry>"
        for i, (t, d, h) in enumerate(entries))
    return f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{escape(title)}</title>{es}</feed>'.encode()


def rdf(title: str, entries) -> bytes:
    es = "".join(
        f'<item rdf:about="https://pacifica.example.invalid/{i}"><title>{escape(t)}</title>'
        f"<link>https://pacifica.example.invalid/{i}</link><description>{escape(d)}</description>"
        f"<dc:date>{_ago(h).isoformat(timespec='seconds')}</dc:date></item>" for i, (t, d, h) in enumerate(entries))
    return ('<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<channel><title>{escape(title)}</title></channel>{es}</rdf:RDF>").encode()


def energy_page(v2: bool) -> bytes:
    nav = "".join(f'<a href="/energy/section/{i}">Section {i}</a>' for i in range(4))
    if v2:
        cards = "".join(
            f'<div class="card"><h3><a href="/energy/story/{i}">{escape(t)}</a></h3><span data-ts="{_ago(h).isoformat(timespec="seconds")}"></span><p>{escape(d)}</p></div>'
            for i, (t, d, h) in enumerate(ENERGY))
        body = f"<main>{cards}</main>"
    else:
        lis = "".join(
            f'<li class="story"><a href="/energy/story/{i}">{escape(t)}</a><time datetime="{_ago(h).isoformat(timespec="seconds")}">{h}h ago</time><p class="dek">{escape(d)}</p></li>'
            for i, (t, d, h) in enumerate(ENERGY))
        body = f'<ul class="news">{lis}</ul>'
    return (f"<!doctype html><html><head><title>Energy Desk</title><script>var tracking=1;</script></head>"
            f"<body><nav>{nav}</nav><h1>Energy Desk</h1>{body}<footer>synthetic</footer></body></html>").encode()


def _handler(state: FakeWebState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str = "application/xml") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path
            with state.lock:
                state.hits[path] += 1
            today = datetime.now(timezone.utc).date()
            if path.startswith("/docs/prompting-"):
                fam = path.rsplit("-", 1)[-1]
                body = ("<html><body><h1>Prompting guide for " + fam + "</h1>" + "".join(
                    f"<p>Tip {i}: separate instructions from data with clear structure such as XML tags, give examples of the "
                    f"expected format, ask for reasoning before the answer on multi-step tasks and place long documents "
                    f"before the question. Section {i} of the {fam} guide.</p>" for i in range(1, 9)) + "</body></html>")
                return self._send(200, body.encode(), "text/html")
            if path == "/robots.txt":
                return self._send(200, b"User-agent: *\nDisallow: /private/\n", "text/plain")
            if path == "/cb/rss.xml":
                return self._send(200, rss("Central Bank of Nordland", CB, z_dates=True))
            if path == "/wire/atom.xml":
                return self._send(404, b"moved") if state.moved_wire else self._send(200, atom("Global Wire", WIRE))
            if path == "/wire/v2/atom.xml":
                return self._send(200, atom("Global Wire", WIRE))
            if path == "/wire/":
                href = "/wire/v2/atom.xml" if state.moved_wire else "/wire/atom.xml"
                return self._send(200, f'<html><head><link rel="alternate" type="application/atom+xml" href="{href}"></head><body>Wire</body></html>'.encode(), "text/html")
            if path == "/markets/rss.xml":
                return self._send(200, rss("Market Daily", MARKETS, html_desc=True))
            if path == "/asia/":
                return self._send(200, b'<html><head><link rel="alternate" type="application/rss+xml" href="/asia/feed.xml"></head><body>Pacifica Post</body></html>', "text/html")
            if path == "/asia/feed.xml":
                return self._send(200, rdf("Pacifica Post", ASIA))
            if path == "/latam/rss.xml":
                return self._send(200, rss("Andes Business Journal", LATAM))
            if path == "/energy/":
                return self._send(200, energy_page(state.energy_v2), "text/html")
            if path == "/private/feed.xml":
                return self._send(200, rss("Private", CB))
            if path == "/broken/feed.xml":
                return self._send(200, b"<rss><channel><item><title>oops")
            if path == "/stale/feed.xml":
                return self._send(200, rss("Stale", [(t, d, h + 240) for t, d, h in CB]))
            if path == "/fx/rates":
                rows = [{"date": (today - timedelta(days=1)).isoformat(), "base": q.get("base"), "quote": q.get("quotes"), "rate": 1.1219},
                        {"date": today.isoformat(), "base": q.get("base"), "quote": q.get("quotes"), "rate": 1.1215}]
                return self._send(200, json.dumps(rows).encode(), "application/json")
            if path == "/cg/simple/price":
                return self._send(200, json.dumps({q.get("ids", "bitcoin"): {"usd": 68250.0, "usd_24h_change": -1.234}}).encode(), "application/json")
            if path == "/fred/series/observations":
                if not q.get("api_key"):
                    return self._send(400, b'{"error_message": "api_key is required"}', "application/json")
                obs = [{"date": today.isoformat(), "value": "5432.10"}, {"date": (today - timedelta(days=1)).isoformat(), "value": "5398.20"}]
                return self._send(200, json.dumps({"observations": obs}).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/repos/") and self.path.endswith("/pulls"):
                n = int(self.headers.get("Content-Length") or 0)
                state.pulls.append(json.loads(self.rfile.read(n) or b"{}"))
                return self._send(201, json.dumps({"number": len(state.pulls), "html_url": f"https://example.test/pull/{len(state.pulls)}"}).encode(),
                                  "application/json")
            if not self.path.startswith("/bot"):
                return self._send(404, b"{}", "application/json")
            method = self.path.rsplit("/", 1)[-1]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            ok = lambda result: self._send(200, json.dumps({"ok": True, "result": result}).encode(), "application/json")  # noqa: E731
            with state.lock:
                state.telegram_calls.append((method, body))
                if method == "sendMessage":
                    state.telegram.append(body)
                    return ok({"message_id": len(state.telegram), "chat": {"id": body.get("chat_id")}})
                if method == "getUpdates":
                    off = int(body.get("offset") or 0)
                    return ok([u for u in state.updates if u["update_id"] >= off])
                if method == "getChat":
                    if str(body.get("chat_id")) == "123456789":
                        return self._send(400, b'{"ok": false, "description": "Bad Request: chat not found"}', "application/json")
                    return ok({"id": body.get("chat_id"), "type": "private"})
                if method == "getMe":
                    return ok({"id": 1, "is_bot": True, "username": "roj_test_bot"})
                if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
                    return ok(True)
            return self._send(404, b'{"ok": false, "description": "unknown method"}', "application/json")

    return H


def start_fake_web(host: str = "127.0.0.1", port: int = 0):
    state = FakeWebState()
    server = ThreadingHTTPServer((host, port), _handler(state))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, state, f"http://{host}:{server.server_address[1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description="roj synthetic web for offline demos")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8046)
    a = ap.parse_args()
    server = ThreadingHTTPServer((a.host, a.port), _handler(FakeWebState()))
    print(f"fake web on http://{a.host}:{a.port}  (seeds: offline:http://{a.host}:{a.port})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
