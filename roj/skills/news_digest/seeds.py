"""Seed candidates for the bootstrap research. The probe decides what actually works.

Checked on 2026-11-07 with an external fetcher:
  verified    reachable, valid RSS 2.0, fresh items
  blocked     robots.txt disallows automated fetching: left out on purpose
  unverified  could not be checked from the build environment; the probe on
              the owner's machine will accept or reject them
Seeds are suggestions, not decisions: the planner may reject any of them and may
propose new candidates, which are probed before they can be selected.
"""

from __future__ import annotations

from typing import Any

SEED_GOAL = (
    "Every morning, tell the owner what important happened overnight worldwide and how it affects "
    "the world economy and markets. Cover central banks, macro data, energy and commodities, FX, "
    "equities, trade and geopolitics with economic impact; prefer primary sources and independent coverage."
)

SEED_SOURCES: list[dict[str, Any]] = [
    # primary: central banks and statistics offices
    {"id": "fed-press", "name": "Federal Reserve press releases", "url": "https://www.federalreserve.gov/feeds/press_all.xml",
     "tier": "primary", "region": "us", "topics": ["monetary-policy", "regulation"], "check": "verified"},
    {"id": "ecb-press", "name": "European Central Bank press", "url": "https://www.ecb.europa.eu/rss/press.html",
     "tier": "primary", "region": "eu", "topics": ["monetary-policy"], "check": "verified"},
    {"id": "boe-news", "name": "Bank of England news", "url": "https://www.bankofengland.co.uk/rss/news",
     "tier": "primary", "region": "uk", "topics": ["monetary-policy", "regulation"], "check": "verified"},
    {"id": "bea", "name": "US Bureau of Economic Analysis", "url": "https://apps.bea.gov/rss/rss.xml",
     "tier": "primary", "region": "us", "topics": ["macro-data"], "check": "unverified"},
    # secondary: newsrooms and wires
    {"id": "marketwatch-top", "name": "MarketWatch top stories", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
     "tier": "secondary", "region": "us", "topics": ["equities", "markets"], "check": "verified"},
    {"id": "wsj-markets", "name": "WSJ Markets", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "tier": "secondary", "region": "us", "topics": ["markets"], "check": "unverified", "independence": "dowjones"},
    {"id": "cnbc-top", "name": "CNBC top news", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
     "tier": "secondary", "region": "us", "topics": ["markets", "economy"], "check": "unverified"},
    {"id": "bbc-business", "name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
     "tier": "secondary", "region": "global", "topics": ["economy"], "check": "unverified"},
    {"id": "guardian-business", "name": "The Guardian Business", "url": "https://www.theguardian.com/business/rss",
     "tier": "secondary", "region": "uk", "topics": ["economy"], "check": "unverified"},
    {"id": "nyt-business", "name": "New York Times Business", "url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
     "tier": "secondary", "region": "us", "topics": ["economy"], "check": "unverified"},
    {"id": "ft-markets", "name": "Financial Times Markets (headlines)", "url": "https://www.ft.com/markets?format=rss",
     "tier": "secondary", "region": "global", "topics": ["markets"], "check": "unverified", "notes": "paywalled articles"},
    {"id": "economist-economics", "name": "The Economist: Economics", "url": "https://www.economist.com/sections/economics/rss.xml",
     "tier": "secondary", "region": "global", "topics": ["economy"], "check": "unverified", "notes": "paywalled articles"},
    {"id": "scmp-business", "name": "South China Morning Post", "url": "https://www.scmp.com/rss/91/feed",
     "tier": "secondary", "region": "asia", "topics": ["economy", "trade"], "check": "unverified"},
    {"id": "oilprice", "name": "OilPrice.com", "url": "https://oilprice.com/rss/main",
     "tier": "unknown", "region": "global", "topics": ["energy"], "check": "unverified"},
    {"id": "investing-news", "name": "Investing.com news", "url": "https://www.investing.com/rss/news.rss",
     "tier": "unknown", "region": "global", "topics": ["markets"], "check": "unverified"},
    {"id": "coindesk", "name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
     "tier": "unknown", "region": "global", "topics": ["crypto"], "check": "unverified"},
]
# Left out: Bloomberg markets RSS and Nikkei Asia RSS (robots.txt disallows automated access, 2026-11-07).

SEED_INSTRUMENTS: list[dict[str, Any]] = [
    {"id": "eurusd", "label": "EUR/USD (central bank ref.)", "provider": "frankfurter", "params": {"base": "EUR", "quote": "USD"}, "decimals": 4},
    {"id": "usdjpy", "label": "USD/JPY (central bank ref.)", "provider": "frankfurter", "params": {"base": "USD", "quote": "JPY"}, "decimals": 2},
    {"id": "eurpln", "label": "EUR/PLN (central bank ref.)", "provider": "frankfurter", "params": {"base": "EUR", "quote": "PLN"}, "decimals": 4},
    {"id": "usdcny", "label": "USD/CNY (central bank ref.)", "provider": "frankfurter", "params": {"base": "USD", "quote": "CNY"}, "decimals": 4},
    {"id": "btcusd", "label": "Bitcoin (USD, 24h)", "provider": "coingecko", "params": {"coin": "bitcoin", "vs": "usd"}, "decimals": 0},
    # FRED: free key, set FRED_API_KEY. Daily closes of the previous US session.
    {"id": "sp500", "label": "S&P 500", "provider": "fred", "params": {"series": "SP500"}, "decimals": 2},
    {"id": "nasdaq", "label": "Nasdaq Composite", "provider": "fred", "params": {"series": "NASDAQCOM"}, "decimals": 2},
    {"id": "ust10y", "label": "US 10y yield (%)", "provider": "fred", "params": {"series": "DGS10"}, "decimals": 2, "unit": "%"},
    {"id": "brent", "label": "Brent (USD/bbl)", "provider": "fred", "params": {"series": "DCOILBRENTEU"}, "decimals": 2},
    {"id": "vix", "label": "VIX", "provider": "fred", "params": {"series": "VIXCLS"}, "decimals": 2},
]


def offline_seeds(base_url: str) -> dict[str, Any]:
    """Seeds pointing at roj.testing.fake_web: a synthetic world for tests and demos."""
    b = base_url.rstrip("/")
    return {
        "goal": SEED_GOAL,
        "sources": [
            {"id": "nordland-cb", "name": "Central Bank of Nordland", "url": f"{b}/cb/rss.xml", "tier": "primary", "region": "europe"},
            {"id": "global-wire", "name": "Global Wire", "url": f"{b}/wire/atom.xml", "tier": "secondary", "region": "global"},
            {"id": "market-daily", "name": "Market Daily", "url": f"{b}/markets/rss.xml", "tier": "secondary", "region": "us"},
            {"id": "pacifica-post", "name": "Pacifica Post", "url": f"{b}/asia/", "tier": "secondary", "region": "asia"},
            {"id": "energy-desk", "name": "Energy Desk", "url": f"{b}/energy/", "tier": "unknown", "region": "global"},
            {"id": "private-letter", "name": "Private Letter", "url": f"{b}/private/feed.xml", "tier": "unknown"},
            {"id": "broken-feed", "name": "Broken Feed", "url": f"{b}/broken/feed.xml", "tier": "unknown"},
            {"id": "stale-feed", "name": "Stale Feed", "url": f"{b}/stale/feed.xml", "tier": "unknown"},
        ],
        "instruments": [
            {"id": "eurusd", "label": "EUR/USD", "provider": "frankfurter", "url": f"{b}/fx/rates?from={{from_date}}&base={{base}}&quotes={{quote}}",
             "params": {"base": "EUR", "quote": "USD"}, "decimals": 4},
            {"id": "btcusd", "label": "Bitcoin (USD, 24h)", "provider": "coingecko",
             "url": f"{b}/cg/simple/price?ids={{coin}}&vs_currencies={{vs}}&include_24hr_change=true",
             "params": {"coin": "bitcoin", "vs": "usd"}, "decimals": 0},
            {"id": "sp500", "label": "S&P 500", "provider": "fred",
             "url": f"{b}/fred/series/observations?series_id={{series}}&api_key={{FRED_API_KEY}}&file_type=json&sort_order=desc&limit=10",
             "params": {"series": "SP500"}, "decimals": 2},
        ],
    }


def default_seeds() -> dict[str, Any]:
    return {"goal": SEED_GOAL, "sources": SEED_SOURCES, "instruments": SEED_INSTRUMENTS}
