"""The live view of a crawl: the pages its job fetched most recently, as screenshots.

A screenshot shows our stored copy of the page (the raw store), the bytes the crawler
read, rendered in headless Chromium with JavaScript off: the site is not fetched again
for the page, and nothing on it runs. Its stylesheets, images and fonts are loaded so it
looks like the site, each through the network guard (public addresses and allowed ports
only, as for any fetch) within a request and byte budget; every other request is refused.
Chromium itself has no route out (the dead proxy of ``fetch.browser``).

One browser per process, started on first use; one render at a time (a page render
costs about as much memory as a crawl worker); the last screenshots kept in memory.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

import httpx
from playwright.async_api import Browser, Playwright, Route, ViewportSize, async_playwright
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import Engine, select

from .config import Settings
from .db.models import Crawl, Fetch, Url
from .fetch.browser import _CHROMIUM_ARGS, _DEAD_PROXY
from .netguard import NetGuard, guarded_transport, ip_policy
from .rawstore import RawStoreBackend

log = logging.getLogger(__name__)

VIEWPORT: ViewportSize = {"width": 1280, "height": 900}
RENDER_TIMEOUT = 15.0  # seconds; then the page as it stands is shot
MAX_REQUESTS = 60  # stylesheets, images and fonts per render
MAX_BYTES = 8 * 1024 * 1024
LOOKS = frozenset({"stylesheet", "image", "font"})
CACHED = 32  # screenshots kept, by fetch


def recent_pages(engine: Engine, job_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """The HTML pages the job fetched most recently, newest first, from every site it
    crawled (the company's, then its competitors')."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(Fetch.id, Url.url, Crawl.seed_url, Fetch.status_code, Fetch.fetched_at)
            .join(Crawl, Crawl.id == Fetch.crawl_id)
            .join(Url, Url.id == Fetch.url_id)
            .where(Crawl.job_id == job_id, Fetch.raw_sha256.is_not(None),
                   Fetch.status_code.between(200, 299),
                   Fetch.content_type.like("text/html%"))
            .order_by(Fetch.id.desc()).limit(limit)
        ).all()  # fmt: skip
    return [{"fetch_id": r.id, "url": r.url, "site": r.seed_url, "status_code": r.status_code,
             "fetched_at": r.fetched_at.isoformat()} for r in rows]  # fmt: skip


def stored_page(engine: Engine, job_id: int, fetch_id: int) -> tuple[str, str, str] | None:
    """(URL, content type, raw-store digest) of a page the job fetched; None if it did not."""
    with engine.connect() as conn:
        row = conn.execute(
            select(Url.url, Fetch.content_type, Fetch.raw_sha256)
            .join(Crawl, Crawl.id == Fetch.crawl_id)
            .join(Url, Url.id == Fetch.url_id)
            .where(Fetch.id == fetch_id, Crawl.job_id == job_id,
                   Fetch.raw_sha256.is_not(None), Fetch.content_type.like("text/html%"))
        ).first()  # fmt: skip
    return (row.url, row.content_type, row.raw_sha256) if row else None


class Screenshots:
    """PNG screenshots of stored pages, rendered one at a time in a shared Chromium."""

    def __init__(self, settings: Settings, rawstore: RawStoreBackend) -> None:
        self.settings = settings
        self.rawstore = rawstore
        f = settings.fetch
        self._guard = NetGuard(
            f.allowed_ports,
            ip_allowed=ip_policy(f.allowed_networks),
            cache_size=f.dns_cache_size,
            cache_seconds=f.dns_cache_seconds,
        )
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def png(self, fetch_id: int, url: str, content_type: str, digest: str) -> bytes:
        if fetch_id in self._cache:
            self._cache.move_to_end(fetch_id)
            return self._cache[fetch_id]
        body = await asyncio.to_thread(self.rawstore.get, digest)
        async with self._lock:
            shot = await self._render(url, content_type, body)
        self._cache[fetch_id] = shot
        while len(self._cache) > CACHED:
            self._cache.popitem(last=False)
        return shot

    async def _launch(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                args=_CHROMIUM_ARGS, proxy={"server": _DEAD_PROXY},
                chromium_sandbox=self.settings.browser.sandbox,
            )  # fmt: skip
        return self._browser

    async def _render(self, url: str, content_type: str, body: bytes) -> bytes:
        browser = await self._launch()
        context = await browser.new_context(java_script_enabled=False, viewport=VIEWPORT,
                                            service_workers="block")  # fmt: skip
        budget = {"requests": MAX_REQUESTS, "bytes": MAX_BYTES}
        client = httpx.AsyncClient(transport=guarded_transport(self._guard), timeout=10.0,
                                   follow_redirects=True, trust_env=False)  # fmt: skip

        async def route(r: Route) -> None:
            request = r.request
            if request.resource_type == "document" and request.url.split("#")[0] == url:
                await r.fulfill(status=200, body=body, content_type=content_type)
                return
            if request.resource_type not in LOOKS or budget["requests"] <= 0:
                await r.abort()
                return
            budget["requests"] -= 1
            try:
                got = await client.get(request.url)
            except (httpx.HTTPError, OSError) as exc:  # refused by the guard, or failed
                log.debug("live.resource_refused", extra={"url": request.url, "error": str(exc)})
                await r.abort()
                return
            if got.status_code != 200 or len(got.content) > budget["bytes"]:
                await r.abort()
                return
            budget["bytes"] -= len(got.content)
            await r.fulfill(
                status=200, body=got.content, content_type=got.headers.get("content-type")
            )

        try:
            await context.route("**/*", route)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="load", timeout=RENDER_TIMEOUT * 1000)
            except PlaywrightError as exc:  # slow assets: shoot what is there
                log.info("live.render_partial", extra={"url": url, "error": str(exc)[:200]})
            return await page.screenshot(type="png", timeout=RENDER_TIMEOUT * 1000)
        finally:
            await context.close()
            await client.aclose()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None
