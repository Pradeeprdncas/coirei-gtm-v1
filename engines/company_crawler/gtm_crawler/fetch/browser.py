"""Headless-browser fallback for JavaScript-rendered pages.

HTTP fetch -> parse -> useful content? If not, and the page runs scripts,
render it in Chromium (Playwright) and parse the resulting DOM.

Chromium never opens a network connection itself. Every request it makes is
intercepted and either refused or performed by ``HttpFetcher.request_once``,
so the SSRF guard, size limits, robots.txt and per-host throttle apply to a
render exactly as to a plain fetch. The main document is served from the body
already fetched over HTTP. As a backstop, Chromium is pointed at a dead proxy
(including for loopback), WebSockets are mocked, service workers are blocked
and WebRTC may not use non-proxied UDP. Chromium's own OS sandbox is on by
default (``browser.sandbox``; the container setup is in docs/security-review.md).

Only document, script, xhr and fetch requests are allowed: images, styles,
fonts and media cost bandwidth and add no text.

Budgets and containment (spec section 11): each render has a wall-clock
limit, a request budget and a byte budget, and its final HTML may not exceed
the fetcher's body limit. Each render gets its own browser context (no shared
cookies, storage or cache). Chromium runs with a capped V8 heap, is replaced
after ``recycle_after_renders`` renders (a retired browser closes once its
last render ends) and is relaunched after a crash, at most ``max_restarts``
times. Repeated render failures open a circuit breaker that stops rendering
for a cooldown. Every render reports its cost in ``FetchResult.render``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Playwright, Route, WebSocketRoute, async_playwright
from playwright.async_api import Error as PlaywrightError

from ..breaker import CircuitBreaker
from ..config import BrowserSettings, UrlSettings
from ..metrics import BROWSER_RESTARTS, RENDER_DURATION
from ..parse import ParsedPage
from ..robots import RobotsCache
from ..throttle import HostThrottle
from ..urls import InvalidUrl, normalize_url, url_host
from .http import FetchError, FetchResult, HttpFetcher

log = logging.getLogger(__name__)

ALLOWED_RESOURCES = frozenset({"document", "script", "xhr", "fetch"})
# Set by our client or meaningless once the body is decoded here.
_DROP_REQUEST_HEADERS = frozenset(
    {"host", "content-length", "accept-encoding", "connection", "keep-alive",
     "transfer-encoding", "upgrade"}
)  # fmt: skip
_DROP_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# Executable scripts only: JSON-LD and other JSON data blocks produce no content.
_EXECUTABLE_SCRIPT = re.compile(
    rb"<script\b(?![^>]*\btype\s*=\s*[\"']?application/(?:ld\+)?json)", re.IGNORECASE
)
# A page telling visitors without JavaScript that its content needs it.
_NOSCRIPT_NOTICE = re.compile(
    rb"<noscript\b[^>]*>[^<]{0,300}?(?:enable|requires?|turn on)\s+javascript",
    re.IGNORECASE,
)
# Nothing listens here; anything that escapes interception fails to connect.
_DEAD_PROXY = "http://127.0.0.1:9"
_CHROMIUM_ARGS = [
    "--proxy-bypass-list=<-loopback>",  # also proxy loopback, which Chromium bypasses by default
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
]


def needs_browser(page: ParsedPage, body: bytes, settings: BrowserSettings) -> bool:
    """The HTTP page has too little text to use, and scripts that may produce more."""
    return browser_trigger(page, body, settings) is not None


def browser_trigger(page: ParsedPage, body: bytes, settings: BrowserSettings) -> str | None:
    """Why the page should be rendered (recorded with the render), or None."""
    chars = len(page.cleaned_text)
    if _EXECUTABLE_SCRIPT.search(body) is None:
        return None
    if chars < settings.min_text_chars:
        return f"thin_text_with_scripts: {chars} < {settings.min_text_chars} chars"
    if _NOSCRIPT_NOTICE.search(body) is not None:
        return "noscript_notice: the page says its content needs JavaScript"
    return None


@dataclass
class _Budget:
    """What one render has spent; refusals once a limit is reached."""

    max_requests: int
    max_bytes: int
    requests: int = 0
    bytes: int = 0
    refused: int = 0

    def report(self) -> dict[str, int]:
        return {"requests": self.requests, "bytes": self.bytes, "refused": self.refused}


class BrowserFetcher:
    """One Chromium per crawl, launched on first use; one isolated context per render."""

    def __init__(
        self,
        settings: BrowserSettings,
        urls: UrlSettings,
        fetcher: HttpFetcher,
        robots: RobotsCache,
        throttle: HostThrottle,
    ) -> None:
        self.settings = settings
        self._urls = urls
        self._fetcher = fetcher
        self._robots = robots
        self._throttle = throttle
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._launch_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # Renders in progress per browser, so a retired one closes when idle.
        self._active: Counter[Browser] = Counter()
        self._uses = 0
        self.restarts: Counter[str] = Counter()
        self.breaker = CircuitBreaker(
            "browser", settings.circuit_failure_threshold, settings.circuit_cooldown_seconds,
            settings.circuit_cooldown_seconds * 8,
        )  # fmt: skip
        # Set when Chromium cannot start; later renders fail fast with it.
        self.unavailable: str | None = None

    async def aclose(self) -> None:
        for browser in [*self._active, *([self._browser] if self._browser else [])]:
            try:
                await browser.close()
            except PlaywrightError:
                pass  # already gone
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None
        self._active.clear()

    async def render(self, fetched: FetchResult) -> FetchResult:
        """Render ``fetched`` (a successful HTML fetch). Never raises for page or browser errors."""
        url = fetched.final_url
        result = FetchResult(url=url, final_url=url, attempts=1)
        wait = self.breaker.check("chromium")
        if wait:
            return _fail(result, f"browser circuit open ({wait:.0f}s left)")
        started = time.perf_counter()
        async with self._semaphore:
            result = await self._render(fetched, result)
        RENDER_DURATION.observe(time.perf_counter() - started)
        # Only browser failures count against the browser, not a page's own
        # budget or size limits.
        self.breaker.record("chromium", not _browser_failure(result))
        return result

    async def _render(self, fetched: FetchResult, result: FetchResult) -> FetchResult:
        url = fetched.final_url
        s = self.settings
        budget = _Budget(s.max_requests_per_render, s.max_bytes_per_render)
        browser = await self._acquire()
        if browser is None:
            return _fail(result, f"browser unavailable: {self.unavailable}")
        try:
            try:
                context = await browser.new_context(
                    user_agent=self._fetcher.user_agent,
                    service_workers="block",
                    accept_downloads=False,
                    locale="en-US",
                )
            except PlaywrightError as exc:  # the browser died between renders
                return _fail(result, f"{type(exc).__name__}: {exc}")
            main_served = False

            async def route(route: Route) -> None:
                nonlocal main_served
                if not main_served and route.request.is_navigation_request():
                    main_served = True
                    await _fulfill(route, fetched)
                else:
                    await self._forward(route, budget)

            try:
                await context.route("**/*", route)
                await context.route_web_socket("**/*", _mock_websocket)
                page = await context.new_page()
                async with asyncio.timeout(self.settings.render_timeout):
                    response = await page.goto(url, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=self.settings.settle_timeout * 1000
                        )
                    except PlaywrightError:
                        pass  # still busy: keep whatever has rendered so far
                    html = await page.content()
                result.final_url = normalize_url(page.url, self._urls)
                if result.final_url != url:
                    result.redirect_chain.append(url)
            except TimeoutError:
                return _fail(result, "render timed out", budget)
            except (PlaywrightError, InvalidUrl) as exc:
                return _fail(result, f"{type(exc).__name__}: {exc}", budget)
            finally:
                try:
                    await context.close()
                except PlaywrightError:
                    pass  # the browser crashed with the page
        finally:
            await self._release(browser)

        body = html.encode("utf-8")
        if len(body) > self._fetcher.settings.max_body_bytes:
            return _fail(result, f"rendered page too large: {len(body)} bytes", budget)
        result.status = response.status if response is not None else None
        result.content_type = "text/html"
        result.headers = {"content-type": "text/html; charset=utf-8"}
        result.body = body
        result.render = budget.report()
        log.info(
            "render.completed",
            extra={"url": url, "final_url": result.final_url, "bytes": len(body),
                   **result.render},
        )  # fmt: skip
        return result

    async def _acquire(self) -> Browser | None:
        """The browser for the next render: recycled after ``recycle_after_renders``,
        relaunched after a crash (at most ``max_restarts`` times)."""
        async with self._launch_lock:
            current = self._browser
            if current is not None:
                crashed = not current.is_connected()
                if crashed or self._uses >= self.settings.recycle_after_renders:
                    reason = "crash" if crashed else "recycle"
                    self._browser = None
                    if crashed and self.restarts["crash"] >= self.settings.max_restarts:
                        self.unavailable = "browser crashed too often"
                        log.warning("browser.unavailable", extra={"error": self.unavailable})
                    else:
                        self.restarts[reason] += 1
                        BROWSER_RESTARTS.labels(reason).inc()
                        log.info("browser.restart", extra={"reason": reason})
                    if not self._active[current]:
                        await _close_quietly(current)
            if self._browser is None and self.unavailable is None:
                try:
                    if self._playwright is None:
                        self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        args=[*_CHROMIUM_ARGS,
                              f"--js-flags=--max-old-space-size={self.settings.js_heap_mb}"],
                        proxy={"server": _DEAD_PROXY},
                        chromium_sandbox=self.settings.sandbox,
                    )  # fmt: skip
                    self._uses = 0
                except PlaywrightError as exc:
                    self.unavailable = str(exc).splitlines()[0]
                    log.warning("browser.unavailable", extra={"error": self.unavailable})
                    await self.aclose()
            if self._browser is not None:
                self._uses += 1
                self._active[self._browser] += 1
            return self._browser

    async def _release(self, browser: Browser) -> None:
        async with self._launch_lock:
            self._active[browser] -= 1
            if self._active[browser] <= 0:
                del self._active[browser]
                if browser is not self._browser:  # retired while this render ran
                    await _close_quietly(browser)

    async def _forward(self, route: Route, budget: _Budget) -> None:
        request = route.request
        url = request.url
        try:
            allowed = request.resource_type in ALLOWED_RESOURCES
            parts = urlsplit(url)
            # No credentials in URLs (spec 9), as for a plain fetch.
            if not allowed or parts.scheme not in ("http", "https") or "@" in parts.netloc:
                await route.abort("blockedbyclient")
                return
            if budget.requests >= budget.max_requests or budget.bytes >= budget.max_bytes:
                budget.refused += 1
                log.info("render.request_skipped", extra={"url": url, "reason": "budget"})
                await route.abort("blockedbyclient")
                return
            budget.requests += 1
            policy = await self._robots.policy_for(url)
            if not policy.can_fetch(url) or policy.delay_too_long:
                log.info("render.request_skipped", extra={"url": url, "reason": "robots"})
                await route.abort("blockedbyclient")
                return
            headers = {
                k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
            }
            async with self._throttle.slot(url_host(url), policy.crawl_delay):
                sub = await self._fetcher.request_once(
                    request.method, url, headers, request.post_data_buffer
                )
            budget.bytes += len(sub.body or b"")
            if sub.error is not None or sub.status is None:
                log.info("render.request_failed", extra={"url": url, "error": sub.error})
                blocked = sub.error is FetchError.BLOCKED
                await route.abort("blockedbyclient" if blocked else "failed")
                return
            await _fulfill(route, sub)
        except (PlaywrightError, InvalidUrl) as exc:
            # The page may have closed mid-request, or the URL has no host.
            log.debug("render.route_error", extra={"url": url, "error": str(exc)})
            try:
                await route.abort("failed")
            except PlaywrightError:
                pass


async def _fulfill(route: Route, fetched: FetchResult) -> None:
    headers = {k: v for k, v in fetched.headers.items() if k not in _DROP_RESPONSE_HEADERS}
    # A 304 carries the stored copy: serve it as the 200 it was, since Chromium
    # aborts a navigation answered with 304 (net::ERR_ABORTED).
    status = 200 if fetched.not_modified else fetched.status or 200
    await route.fulfill(status=status, headers=headers, body=fetched.body or b"")


def _mock_websocket(ws: WebSocketRoute) -> Any:
    # Not connecting to the server leaves the socket mocked: nothing leaves the process.
    return None


async def _close_quietly(browser: Browser) -> None:
    try:
        await browser.close()
    except PlaywrightError:
        pass  # already gone


# Failures of the page itself, not of the browser: they do not trip the breaker.
_PAGE_FAILURES = ("rendered page too large", "render timed out")


def _browser_failure(result: FetchResult) -> bool:
    detail = result.error_detail or ""
    return result.error is FetchError.RENDER and not detail.startswith(_PAGE_FAILURES)


def _fail(result: FetchResult, detail: str, budget: _Budget | None = None) -> FetchResult:
    result.error = FetchError.RENDER
    result.error_detail = detail
    result.body = None
    result.render = budget.report() if budget is not None else None
    log.info("render.failed", extra={"url": result.url, "error": detail})
    return result
