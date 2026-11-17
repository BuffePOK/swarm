"""Headless Chromium (Playwright) for pages whose content is built by JavaScript.

- Same manners as the plain fetcher: robots.txt is checked first and requests to a host are
  paced. The user agent says that it is roj. Images, media and fonts are not loaded.
- Persistent profiles under <home>/browser/<profile>: cookies and logins survive restarts.
  The owner can attach to a profile (`roj browser open`, DevTools over an SSH tunnel) and log
  in to sites where he has an account; agents then read those sites as him. Agents never get
  the owner's own desktop browser.
- No CAPTCHA solving, no fingerprint spoofing, no evasion of bot protection: a page that
  refuses automated readers is reported as refused.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..util import day_key, utcnow_iso

if TYPE_CHECKING:
    from ..project import Installation, Project

CHALLENGE = re.compile(r"verify you are (?:a )?human|are you a robot|captcha|attention required|unusual traffic|access denied|"
                       r"checking your browser", re.I)


class BrowserUnavailable(RuntimeError):
    pass


def available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Playwright is not installed (pip install 'roj[browser]' and `playwright install chromium`)"
    return True, "ok"


@dataclass
class Rendered:
    url: str
    status: int | None
    html: str
    seconds: float
    refused: str | None = None


def launch_args() -> list[str]:
    args = ["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run", "--mute-audio"]
    if os.environ.get("ROJ_BROWSER_NO_SANDBOX") == "1":  # inside the container the container is the sandbox
        args.append("--no-sandbox")
    return args


class BrowserPool:
    """One persistent Chromium context per (event loop, profile); closed after idle_close_s."""

    def __init__(self, inst: "Installation"):
        self.inst = inst
        self._ctx: dict[tuple[int, str], Any] = {}
        self._pw: dict[int, Any] = {}
        self._sem: dict[int, asyncio.Semaphore] = {}
        self._last_use: dict[tuple[int, str], float] = {}
        self._lock: dict[int, asyncio.Lock] = {}

    def profile_dir(self, profile: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", profile):
            raise ValueError("profile names: lowercase letters, digits, - and _")
        d = self.inst.home / "browser" / profile
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _context(self, profile: str) -> Any:
        ok, why = available()
        if not ok or not self.inst.settings.browser.enabled:
            raise BrowserUnavailable(why if not ok else "[browser] enabled = false")
        lid = id(asyncio.get_running_loop())
        key = (lid, profile)
        async with self._lock.setdefault(lid, asyncio.Lock()):
            if key in self._ctx:
                return self._ctx[key]
            from playwright.async_api import async_playwright

            if lid not in self._pw:
                self._pw[lid] = await async_playwright().start()
            cfg = self.inst.settings.browser
            try:
                ctx = await self._pw[lid].chromium.launch_persistent_context(
                    str(self.profile_dir(profile)), headless=True, args=launch_args(), java_script_enabled=True,
                    user_agent=None, locale="en-US", viewport={"width": 1280, "height": 900})
            except Exception as e:  # noqa: BLE001 - e.g. the profile is open in `roj browser open`
                raise BrowserUnavailable(f"cannot start Chromium for profile {profile}: {str(e).splitlines()[0][:200]}") from e
            ua = await ctx.pages[0].evaluate("navigator.userAgent") if ctx.pages else ""
            await ctx.set_extra_http_headers({"User-Agent": f"{ua} {self.inst.settings.web.user_agent}".strip()})
            blocked = set(cfg.block)
            if blocked:
                async def _filter(route: Any) -> None:
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()
                await ctx.route("**/*", _filter)
            self._ctx[key] = ctx
            asyncio.get_running_loop().create_task(self._idle_closer(key))
            return ctx

    async def _idle_closer(self, key: tuple[int, str]) -> None:
        idle = self.inst.settings.browser.idle_close_s
        while key in self._ctx:
            await asyncio.sleep(max(1.0, idle / 4))
            if time.monotonic() - self._last_use.get(key, time.monotonic()) > idle:
                await self.close_one(key)

    async def close_one(self, key: tuple[int, str]) -> None:
        ctx = self._ctx.pop(key, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        lid = id(asyncio.get_running_loop())
        for key in [k for k in self._ctx if k[0] == lid]:
            await self.close_one(key)
        pw = self._pw.pop(lid, None)
        if pw is not None:
            await pw.stop()

    def _count(self) -> None:
        cfg, store = self.inst.settings.browser, self.inst.store
        key = f"browser:{day_key(self.inst.settings.timezone)}"
        row = store.one("SELECT value FROM kv WHERE key = ?", (key,))
        n = int(row["value"]) if row else 0
        if n >= cfg.daily_limit:
            raise BrowserUnavailable(f"daily rendered-page limit reached ({cfg.daily_limit})")
        store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES(?,?,?)", (key, str(n + 1), utcnow_iso()))

    async def render(self, project: "Project", url: str, *, profile: str | None = None) -> Rendered:
        cfg = self.inst.settings.browser
        profile = profile or cfg.profile
        fetcher = project.fetcher
        async with fetcher._client() as client:  # same robots.txt rules as plain fetching
            ok, why = await fetcher.allowed(url, client)
        if not ok:
            return Rendered(url, None, "", 0.0, refused=why)
        self._count()
        await fetcher._polite(urlsplit(url).netloc)
        lid = id(asyncio.get_running_loop())
        sem = self._sem.setdefault(lid, asyncio.Semaphore(cfg.max_pages))
        t0 = time.monotonic()
        async with sem:
            ctx = await self._context(profile)
            self._last_use[(lid, profile)] = time.monotonic()
            page = await ctx.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_s * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(10_000, cfg.nav_timeout_s * 1000))
                except Exception:  # noqa: BLE001 - pages that poll forever never go idle; take what is there
                    pass
                await page.wait_for_timeout(cfg.settle_ms)
                html = await page.content()
                final, status = page.url, (resp.status if resp else None)
            finally:
                await page.close()
                self._last_use[(lid, profile)] = time.monotonic()
        refused = None
        if (status and status in (401, 403, 429)) or (len(html) < 20_000 and CHALLENGE.search(html)):
            refused = f"the site refused an automated reader (HTTP {status})"
        project.events.append("web.render", {"url": final[:300], "status": status, "chars": len(html), "profile": profile,
                                             "seconds": round(time.monotonic() - t0, 2), "refused": refused})
        return Rendered(final, status, html, time.monotonic() - t0, refused)


def pool(inst: "Installation") -> BrowserPool:
    p = getattr(inst, "_browser_pool", None)
    if p is None:
        p = BrowserPool(inst)
        inst._browser_pool = p  # type: ignore[attr-defined]
    return p


async def open_for_owner(inst: "Installation", profile: str, port: int, url: str | None, minutes: float) -> None:
    """Start the profile with DevTools on 127.0.0.1:<port> so the owner can log in through an SSH tunnel."""
    ok, why = available()
    if not ok:
        raise BrowserUnavailable(why)
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(pool(inst).profile_dir(profile)), headless=True,
            args=launch_args() + [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        print(f"profile '{profile}' is open with DevTools on 127.0.0.1:{port} for {minutes:g} min.\n"
              f"From your computer: ssh -L {port}:127.0.0.1:{port} <admin>@<server>\n"
              f"then in Chrome open chrome://inspect -> Configure -> add localhost:{port} -> 'inspect' the page.\n"
              f"You can click and type in the live view; log in where you need to. Cookies stay in the profile.\n"
              f"Ctrl+C ends the session early.", flush=True)
        try:
            await asyncio.sleep(minutes * 60)
        finally:
            await ctx.close()
