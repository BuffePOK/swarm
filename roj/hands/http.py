"""Polite HTTP fetcher: the agents' read-only hand for the web.

- respects robots.txt (RFC 9309: 4xx = allowed, 5xx/unreachable = disallowed)
- conditional GET (ETag / Last-Modified) with bodies stored in CAS
- one request per host per `per_host_interval_s`, bounded global concurrency
- size and time limits; never raises on network problems (returns `error`)
- API keys in query strings are masked in logs and in the cache index
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import WebSettings
from ..util import sha256_hex, utcnow_iso

if TYPE_CHECKING:
    from ..core.cas import CAS
    from ..core.eventlog import EventLog
    from ..core.store import Store

_SECRET_PARAM = re.compile(r"(api[_-]?key|apikey|key|token|secret|access[_-]?token|auth)", re.I)


def mask_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, "***" if _SECRET_PARAM.fullmatch(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, safe="*"), parts.fragment))


@dataclass
class FetchResult:
    url: str  # masked
    status: int | None
    content: bytes
    content_type: str
    ref: str | None
    from_cache: bool
    error: str | None
    elapsed_s: float
    final_url: str | None = None  # after redirects (unmasked: it is what the server sent back)

    @property
    def where(self) -> str:
        return self.final_url or self.url

    @property
    def ok(self) -> bool:
        return self.error is None and (self.from_cache or (self.status is not None and 200 <= self.status < 300))

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, store: "Store", cas: "CAS", events: "EventLog", settings: WebSettings,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.cas, self.events, self.settings = store, cas, events, settings
        self.transport = transport
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._sem: dict[int, asyncio.Semaphore] = {}

    def _semaphore(self) -> asyncio.Semaphore:
        lid = id(asyncio.get_running_loop())
        if lid not in self._sem:
            self._sem[lid] = asyncio.Semaphore(self.settings.max_concurrency)
            self._host_locks = {}
        return self._sem[lid]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.timeout_s, connect=min(10.0, self.settings.timeout_s)),
            headers={"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True, transport=self.transport,
        )

    async def _polite(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0.0) + self.settings.per_host_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def allowed(self, url: str, client: httpx.AsyncClient) -> tuple[bool, str]:
        if not self.settings.respect_robots:
            return True, "robots disabled"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        row = self.store.one("SELECT body, status, fetched_at FROM robots_cache WHERE origin = ?", (origin,))
        fresh = row is not None and (time.time() - _ts(row["fetched_at"])) < 86400
        if not fresh:
            body, status = "", None
            try:
                r = await client.get(origin + "/robots.txt")
                status, body = r.status_code, (r.text if r.status_code < 400 else "")
            except httpx.HTTPError:
                status = None
            self.store.run(
                "INSERT OR REPLACE INTO robots_cache(origin, body, status, fetched_at) VALUES(?,?,?,?)",
                (origin, body, status, utcnow_iso()),
            )
        else:
            body, status = row["body"] or "", row["status"]
        if status is None or status >= 500:
            return False, "robots.txt unreachable (treated as disallow)"
        if status >= 400:
            return True, "no robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        agent = self.settings.user_agent.split("/")[0]
        ok = rp.can_fetch(agent, url)
        return ok, "allowed by robots.txt" if ok else "disallowed by robots.txt"

    async def get(self, url: str, *, purpose: str = "", use_conditional: bool = True) -> FetchResult:
        masked = mask_url(url)
        key = sha256_hex(url)
        t0 = time.monotonic()
        async with self._semaphore():
            async with self._client() as client:
                ok, why = await self.allowed(url, client)
                if not ok:
                    res = FetchResult(masked, None, b"", "", None, False, why, time.monotonic() - t0)
                    self._log(res, purpose)
                    return res
                await self._polite(urlsplit(url).netloc)
                cached = self.store.one("SELECT * FROM http_cache WHERE key = ?", (key,))
                headers = {}
                if use_conditional and cached and cached["ref"]:
                    if cached["etag"]:
                        headers["If-None-Match"] = cached["etag"]
                    if cached["last_modified"]:
                        headers["If-Modified-Since"] = cached["last_modified"]
                try:
                    async with client.stream("GET", url, headers=headers) as r:
                        chunks, size = [], 0
                        async for chunk in r.aiter_bytes():
                            size += len(chunk)
                            if size > self.settings.max_bytes:
                                raise _TooLarge()
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        status, ctype = r.status_code, r.headers.get("content-type", "")
                        etag, lm = r.headers.get("etag"), r.headers.get("last-modified")
                        final = str(r.url)
                    if urlsplit(final).netloc != urlsplit(url).netloc:  # redirected to another site: its robots.txt applies
                        ok2, why2 = await self.allowed(final, client)
                        if not ok2:
                            res = FetchResult(masked, None, b"", "", None, False, f"{why2} (after redirect to {mask_url(final)})",
                                              time.monotonic() - t0)
                            self._log(res, purpose)
                            return res
                except _TooLarge:
                    res = FetchResult(masked, None, b"", "", None, False, f"larger than {self.settings.max_bytes} bytes", time.monotonic() - t0)
                    self._log(res, purpose)
                    return res
                except httpx.HTTPError as e:
                    res = FetchResult(masked, None, b"", "", None, False, f"{type(e).__name__}: {e}"[:300], time.monotonic() - t0)
                    self._log(res, purpose)
                    return res
        elapsed = time.monotonic() - t0
        if status == 304 and cached and cached["ref"]:
            res = FetchResult(masked, 304, self.cas.get_bytes(cached["ref"]), cached["content_type"] or "", cached["ref"], True, None, elapsed,
                              final)
        elif 200 <= status < 300:
            ref = self.cas.put_bytes(body)
            self.store.run(
                "INSERT OR REPLACE INTO http_cache(key, url_masked, etag, last_modified, ref, status, content_type, fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (key, masked, etag, lm, ref, status, ctype, utcnow_iso()),
            )
            res = FetchResult(masked, status, body, ctype, ref, False, None, elapsed, final)
        else:
            res = FetchResult(masked, status, body[:2000], ctype, None, False, f"HTTP {status}", elapsed, final)
        self._log(res, purpose)
        return res

    def _log(self, r: FetchResult, purpose: str) -> None:
        self.events.append("http.fetch", {"url": r.url, "status": r.status, "bytes": len(r.content), "ref": r.ref,
                                          "from_cache": r.from_cache, "error": r.error, "elapsed_s": round(r.elapsed_s, 3),
                                          "purpose": purpose}, actor="hand:http")


class _TooLarge(Exception):
    pass


def _ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()
