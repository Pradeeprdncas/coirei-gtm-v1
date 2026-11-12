"""Browser fallback: when to render, what a render may request, and containment."""

import asyncio
import socket
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from gtm_crawler.config import BrowserSettings, FetchSettings, RobotsSettings, UrlSettings
from gtm_crawler.fetch.browser import BrowserFetcher, needs_browser
from gtm_crawler.fetch.http import FetchError, FetchResult, HttpFetcher
from gtm_crawler.netguard import NetGuard
from gtm_crawler.parse import ParsedPage, TextBlock
from gtm_crawler.robots import RobotsCache
from gtm_crawler.throttle import HostThrottle

from .helpers import IDENTITY, make_fetcher, response

SITE = "https://spa.example"
SETTINGS = BrowserSettings(render_timeout=15.0, settle_timeout=2.0)


def thin_page(text: str = "Loading") -> ParsedPage:
    return ParsedPage(url=f"{SITE}/", blocks=[TextBlock(xpath="/p", tag="p", text=text)])


@pytest.mark.parametrize(
    ("text", "body", "expected"),
    [
        ("Loading", b'<div id="root"></div><script src="/app.js"></script>', True),
        ("Loading", b"<SCRIPT>render()</SCRIPT>", True),
        ("Loading", b'<script type="application/ld+json">{}</script>', False),
        ("Loading", b"<script type='application/json'>{}</script>", False),
        ("Loading", b"<p>Loading</p>", False),
        ("x" * 300, b"<script src=/app.js></script>", False),
    ],
)
def test_needs_browser(text: str, body: bytes, expected: bool) -> None:
    assert needs_browser(thin_page(text), body, SETTINGS) is expected


def shell(extra: str = "") -> FetchResult:
    body = (
        '<html><head><title>SPA</title></head><body><div id="root"></div>'
        '<img src="/logo.png"><link rel="stylesheet" href="/style.css">'
        f'<script src="/app.js"></script>{extra}</body></html>'
    ).encode()
    return FetchResult(
        url=f"{SITE}/", final_url=f"{SITE}/", status=200, content_type="text/html",
        headers={"content-type": "text/html; charset=utf-8"}, body=body,
    )  # fmt: skip


APP_JS = b"""
fetch('/api/company', {method: 'POST', body: '{"q":1}'})
  .then(r => r.json())
  .then(d => {
    document.getElementById('root').innerHTML =
      '<h1>' + d.name + '</h1><p>' + d.about + '</p><a href="/pricing">Pricing</a>';
  });
fetch('/private/secret').catch(() => {});
"""


class Site:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url, request.read()))
        if url == f"{SITE}/robots.txt":
            return response(200, b"User-agent: *\nDisallow: /private/\n")
        if url == f"{SITE}/app.js":
            return response(200, APP_JS, **{"Content-Type": "application/javascript"})
        if url == f"{SITE}/api/company":
            body = b'{"name": "Acme Robotics", "about": "Acme builds warehouse robots."}'
            return response(200, body, **{"Content-Type": "application/json"})
        return response(404)

    @property
    def urls(self) -> list[str]:
        return [u for _, u, _ in self.requests]


def browser_for(fetcher: HttpFetcher, **overrides: Any) -> BrowserFetcher:
    settings = BrowserSettings(**{**SETTINGS.__dict__, **overrides})
    robots = RobotsCache(fetcher, RobotsSettings(), IDENTITY.product_token)
    return BrowserFetcher(settings, UrlSettings(), fetcher, robots, HostThrottle(2, 0.0))


async def render(fetcher: HttpFetcher, fetched: FetchResult, **overrides: Any) -> FetchResult:
    browser = browser_for(fetcher, **overrides)
    try:
        return await browser.render(fetched)
    finally:
        await browser.aclose()
        await fetcher.aclose()


def test_render_runs_scripts_through_the_fetcher() -> None:
    site = Site()
    result = asyncio.run(render(make_fetcher(site), shell()))

    assert result.ok, result.error_detail
    html = (result.body or b"").decode()
    assert "Acme builds warehouse robots." in html
    assert 'href="/pricing"' in html
    assert result.final_url == f"{SITE}/"
    # The shell came from the HTTP fetch; only allowed resource types were requested.
    assert f"{SITE}/" not in site.urls
    assert f"{SITE}/logo.png" not in site.urls
    assert f"{SITE}/style.css" not in site.urls
    # robots.txt applies to what the page requests.
    assert f"{SITE}/private/secret" not in site.urls
    assert ("POST", f"{SITE}/api/company", b'{"q":1}') in site.requests


def test_a_not_modified_page_renders_from_its_stored_copy() -> None:
    # A re-crawl's 304 carries the stored body; Chromium aborts a 304 navigation.
    cached = shell()
    cached.status, cached.not_modified = 304, True
    result = asyncio.run(render(make_fetcher(Site()), cached))

    assert result.ok, result.error_detail
    assert "Acme builds warehouse robots." in (result.body or b"").decode()


def test_render_timeout_is_an_error() -> None:
    hang = shell("<script>while (true) {}</script>")
    result = asyncio.run(render(make_fetcher(Site()), hang, render_timeout=3.0))
    assert result.error is FetchError.RENDER
    assert result.body is None


@pytest.fixture
def listener() -> Iterator[tuple[int, list[bytes]]]:
    """A local TCP server that records every connection it receives."""
    received: list[bytes] = []
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(1)
                try:
                    received.append(conn.recv(1024))
                except OSError:
                    received.append(b"")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1], received
    stop.set()
    thread.join()
    server.close()


def test_browser_cannot_reach_private_addresses(listener: tuple[int, list[bytes]]) -> None:
    port, received = listener
    target = f"127.0.0.1:{port}"
    page = shell(
        f'<script src="http://{target}/a.js"></script>'
        f'<iframe src="http://{target}/frame"></iframe>'
        f"<script>fetch('http://{target}/xhr').catch(() => {{}});"
        f"try {{ new WebSocket('ws://{target}/ws'); }} catch (e) {{}}"
        f"location.href = 'http://{target}/nav';</script>"
    )
    # Real guarded transport; the port is allowed so the address check is what refuses.
    fetcher = HttpFetcher(
        FetchSettings(max_retries=0), IDENTITY, UrlSettings(), guard=NetGuard([80, 443, port])
    )
    asyncio.run(render(fetcher, page, settle_timeout=3.0))
    assert received == []
