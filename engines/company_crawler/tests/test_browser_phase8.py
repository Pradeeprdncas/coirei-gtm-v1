"""Phase 8: render budgets, browser recycling and crash recovery, circuit breaker."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from gtm_crawler.config import FetchSettings, UrlSettings
from gtm_crawler.fetch.browser import BrowserFetcher, browser_trigger
from gtm_crawler.fetch.http import FetchError, FetchResult, HttpFetcher

from .helpers import IDENTITY, make_fetcher, response
from .test_browser import SETTINGS, SITE, browser_for, shell, thin_page
from .test_crawl import FakeSite, html, run, settings_for


def body_with(script: str) -> bytes:
    return f"<html><body><p>{'Plenty of text. ' * 40}</p>{script}</body></html>".encode()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (body_with("<noscript>Please enable JavaScript to use this app.</noscript>"
                   "<script src=/app.js></script>"), "noscript_notice"),
        (body_with("<noscript>This site requires JavaScript.</noscript><script>x()</script>"),
         "noscript_notice"),
        # No scripts: rendering could not add anything.
        (body_with("<noscript>Please enable JavaScript.</noscript>"), None),
        (body_with("<noscript><img src=/pixel.gif></noscript><script>x()</script>"), None),
    ],
)  # fmt: skip
def test_noscript_notice_triggers_a_render(body: bytes, expected: str | None) -> None:
    trigger = browser_trigger(thin_page("x" * 600), body, SETTINGS)
    assert (trigger.split(":")[0] if trigger else None) == expected


class Site:
    """Serves app.js (which requests /api/<n> for n < calls) and the API."""

    def __init__(self, calls: int = 10, api_body: bytes = b"{}") -> None:
        self.calls, self.api_body = calls, api_body
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if url == f"{SITE}/robots.txt":
            return response(200, b"User-agent: *\nAllow: /\n")
        if url == f"{SITE}/app.js":
            js = (
                f"for (let i = 0; i < {self.calls}; i++) fetch('/api/' + i).catch(() => {{}});"
                "document.getElementById('root').innerHTML = '<h1>Rendered</h1>';"
            )
            return response(200, js.encode(), **{"Content-Type": "application/javascript"})
        if url.startswith(f"{SITE}/api/"):
            return response(200, self.api_body, **{"Content-Type": "application/json"})
        return response(404)

    def api_calls(self) -> int:
        return sum(u.startswith(f"{SITE}/api/") for u in self.urls)


async def renders(
    fetcher: HttpFetcher, pages: list[FetchResult], **overrides: Any
) -> tuple[list[FetchResult], BrowserFetcher]:
    browser = browser_for(fetcher, **overrides)
    try:
        return [await browser.render(p) for p in pages], browser
    finally:
        await browser.aclose()
        await fetcher.aclose()


def test_request_budget_caps_what_a_render_may_fetch() -> None:
    site = Site(calls=10)
    [result], _ = asyncio.run(renders(make_fetcher(site), [shell()], max_requests_per_render=4))
    assert result.ok and b"Rendered" in (result.body or b"")
    assert site.api_calls() == 3  # app.js + 3 API calls
    assert result.render is not None
    assert (result.render["requests"], result.render["refused"]) == (4, 7)


def test_byte_budget_stops_further_requests() -> None:
    site = Site(calls=5, api_body=b"x" * 50_000)
    [result], _ = asyncio.run(renders(make_fetcher(site), [shell()], max_bytes_per_render=60_000))
    assert result.ok
    # Checked before each request: requests already in flight when the budget
    # is crossed still complete (at most per_host_concurrency of them).
    assert 2 <= site.api_calls() <= 3
    assert result.render is not None and result.render["refused"] == 5 - site.api_calls()


def test_oversized_rendered_page_is_refused_without_tripping_the_breaker() -> None:
    big = shell("<script>document.body.innerHTML += '<p>' + 'x'.repeat(200000) + '</p>';"
                "</script>")  # fmt: skip
    fetcher = make_fetcher(Site(calls=0), max_body_bytes=100_000)
    results, browser = asyncio.run(
        renders(fetcher, [big] * 3, circuit_failure_threshold=2)
    )  # fmt: skip
    assert all(r.error is FetchError.RENDER for r in results)
    assert all("too large" in (r.error_detail or "") for r in results)
    assert browser.breaker.state("chromium") == "closed"


def test_browser_is_recycled_after_n_renders() -> None:
    results, browser = asyncio.run(
        renders(make_fetcher(Site(calls=0)), [shell()] * 5, recycle_after_renders=2)
    )
    assert all(r.ok for r in results)
    assert browser.restarts["recycle"] == 2


def test_crashed_browser_is_relaunched_then_given_up() -> None:
    async def go(max_restarts: int) -> tuple[list[FetchResult], BrowserFetcher]:
        fetcher = make_fetcher(Site(calls=0))
        browser = browser_for(fetcher, max_restarts=max_restarts)
        out = []
        try:
            for _ in range(3):
                out.append(await browser.render(shell()))
                assert browser._browser is not None
                await browser._browser.close()  # simulate a crash between renders
            return out, browser
        finally:
            await browser.aclose()
            await fetcher.aclose()

    results, browser = asyncio.run(go(max_restarts=5))
    assert all(r.ok for r in results) and browser.restarts["crash"] == 2

    async def exhausted() -> tuple[FetchResult, BrowserFetcher]:
        fetcher = make_fetcher(Site(calls=0))
        browser = browser_for(fetcher, max_restarts=0)
        try:
            assert (await browser.render(shell())).ok
            assert browser._browser is not None
            await browser._browser.close()
            return await browser.render(shell()), browser
        finally:
            await browser.aclose()
            await fetcher.aclose()

    result, browser = asyncio.run(exhausted())
    assert result.error is FetchError.RENDER and "unavailable" in (result.error_detail or "")
    assert browser.unavailable == "browser crashed too often"


def test_repeated_browser_failures_open_the_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = HttpFetcher(FetchSettings(), IDENTITY, UrlSettings())
    browser = browser_for(fetcher, circuit_failure_threshold=2, circuit_cooldown_seconds=60)
    attempts: list[str] = []

    async def broken(fetched: FetchResult, result: FetchResult) -> FetchResult:
        attempts.append(fetched.url)
        result.error, result.error_detail = FetchError.RENDER, "TargetClosedError: crashed"
        return result

    monkeypatch.setattr(browser, "_render", broken)

    async def go() -> list[FetchResult]:
        try:
            return [await browser.render(shell()) for _ in range(4)]
        finally:
            await fetcher.aclose()

    results = asyncio.run(go())
    assert len(attempts) == 2  # then the circuit is open
    assert "circuit open" in (results[-1].error_detail or "")
    assert browser.breaker.state("chromium") == "open"


@pytest.mark.db
def test_render_cost_is_in_crawl_stats(engine: Engine, tmp_path: Path) -> None:
    routes: dict[str, Any] = {
        "https://acme.example/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        "https://acme.example/": ("text/html", html("Acme", "<p>Home of Acme.</p>")),
    }
    summary = run(engine, settings_for(tmp_path), FakeSite(routes))
    browser = summary.stats["browser"]
    for key in ("render_ms", "requests", "bytes", "requests_refused", "restarts", "circuit"):
        assert key in browser
