"""Market data providers. Numbers in reports come only from here, never from a model.

Providers are declarative: an instrument names a provider, parameters and an optional
URL template. `{ENV_NAME}` placeholders (upper case) are filled from the environment,
so keys never live in specs or logs.

  frankfurter  central-bank FX reference rates, no key (api.frankfurter.dev)
  coingecko    crypto spot + 24h change, no key (public API, rate limited)
  fred         US daily series (indices, yields, oil, VIX), free key FRED_API_KEY
  json         any JSON endpoint: value_path / previous_path / date_path
  csv          any CSV endpoint: date_column / value_column, last two rows
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from .http import Fetcher

PRESET_URLS = {
    "frankfurter": "https://api.frankfurter.dev/v2/rates?from={from_date}&base={base}&quotes={quote}",
    "coingecko": "https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies={vs}&include_24hr_change=true",
    "fred": "https://api.stlouisfed.org/fred/series/observations?series_id={series}&api_key={FRED_API_KEY}"
            "&file_type=json&sort_order=desc&limit=10",
}


class InstrumentSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    label: str
    provider: str  # frankfurter | coingecko | fred | json | csv
    params: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None  # overrides the preset template
    unit: str = ""
    decimals: int = 2


class Quote(BaseModel):
    instrument_id: str
    label: str
    value: float
    previous: float | None = None
    change_pct: float | None = None
    as_of: str | None = None
    provider: str
    unit: str = ""
    decimals: int = 2

    def display_value(self) -> str:
        return f"{self.value:,.{self.decimals}f}"

    def display_change(self) -> str:
        return "n/a" if self.change_pct is None else f"{self.change_pct:+.2f}%"


class MarketError(Exception):
    pass


_ENV = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def build_url(spec: InstrumentSpec, today: date | None = None) -> str:
    template = spec.url or PRESET_URLS.get(spec.provider)
    if not template:
        raise MarketError(f"{spec.id}: provider {spec.provider!r} needs a url")
    missing = [n for n in _ENV.findall(template) if not os.environ.get(n)]
    if missing:
        raise MarketError(f"{spec.id}: missing environment variable(s) {', '.join(missing)}")
    values = {n: os.environ[n] for n in _ENV.findall(template)}
    today = today or date.today()
    values["from_date"] = (today - timedelta(days=10)).isoformat()
    values.update({k: str(v) for k, v in spec.params.items()})
    try:
        return template.format(**values)
    except KeyError as e:
        raise MarketError(f"{spec.id}: url template needs parameter {e}") from e


def _pct(value: float, prev: float | None) -> float | None:
    return None if prev in (None, 0) else (value / prev - 1.0) * 100.0


def _path(obj: Any, path: str) -> Any:
    for part in re.findall(r"[^.\[\]]+|\[-?\d+\]", path):
        if part.startswith("["):
            obj = obj[int(part[1:-1])]
        else:
            obj = obj[part]
    return obj


def parse_quote(spec: InstrumentSpec, body: bytes) -> Quote:
    p = spec.params
    base = dict(instrument_id=spec.id, label=spec.label, provider=spec.provider, unit=spec.unit, decimals=spec.decimals)
    try:
        if spec.provider == "frankfurter":
            rows = [r for r in json.loads(body) if r.get("quote") == str(p.get("quote"))]
            rows.sort(key=lambda r: r["date"])
            if not rows:
                raise MarketError("no rows for quote")
            v, prev = float(rows[-1]["rate"]), (float(rows[-2]["rate"]) if len(rows) > 1 else None)
            return Quote(value=v, previous=prev, change_pct=_pct(v, prev), as_of=rows[-1]["date"], **base)
        if spec.provider == "coingecko":
            d = json.loads(body)[str(p["coin"])]
            vs = str(p.get("vs", "usd"))
            v, chg = float(d[vs]), d.get(f"{vs}_24h_change")
            prev = v / (1 + float(chg) / 100) if chg is not None else None
            return Quote(value=v, previous=prev, change_pct=float(chg) if chg is not None else None, as_of="24h", **base)
        if spec.provider == "fred":
            obs = [o for o in json.loads(body)["observations"] if o.get("value") not in (".", "", None)]
            if not obs:
                raise MarketError("no observations")
            v, prev = float(obs[0]["value"]), (float(obs[1]["value"]) if len(obs) > 1 else None)
            return Quote(value=v, previous=prev, change_pct=_pct(v, prev), as_of=obs[0]["date"], **base)
        if spec.provider == "json":
            d = json.loads(body)
            v = float(_path(d, p["value_path"]))
            prev = float(_path(d, p["previous_path"])) if p.get("previous_path") else None
            as_of = str(_path(d, p["date_path"])) if p.get("date_path") else None
            return Quote(value=v, previous=prev, change_pct=_pct(v, prev), as_of=as_of, **base)
        if spec.provider == "csv":
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace")), delimiter=p.get("delimiter", ",")))
            col, dcol = p.get("value_column", "Close"), p.get("date_column", "Date")
            rows = [r for r in rows if r.get(col) not in (None, "", "null")]
            if not rows:
                raise MarketError("no rows")
            v, prev = float(rows[-1][col]), (float(rows[-2][col]) if len(rows) > 1 else None)
            return Quote(value=v, previous=prev, change_pct=_pct(v, prev), as_of=rows[-1].get(dcol), **base)
    except MarketError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise MarketError(f"{spec.id}: cannot parse {spec.provider} response: {type(e).__name__}: {e}") from e
    raise MarketError(f"{spec.id}: unknown provider {spec.provider!r}")


async def fetch_quote(fetcher: Fetcher, spec: InstrumentSpec) -> Quote:
    url = build_url(spec)
    r = await fetcher.get(url, purpose=f"market:{spec.id}", use_conditional=False)
    if not r.ok:
        raise MarketError(f"{spec.id}: {r.error}")
    return parse_quote(spec, r.content)
