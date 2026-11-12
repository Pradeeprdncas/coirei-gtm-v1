import asyncio

import httpx
import pytest

from gtm_crawler.config import RobotsSettings
from gtm_crawler.fetch.http import FetchResult
from gtm_crawler.robots import RobotsCache, RobotsStatus

from .helpers import make_fetcher, response

ROBOTS = b"""
User-agent: *
Disallow: /private/
Allow: /private/press/
Crawl-delay: 2
Sitemap: https://example.com/sitemap_index.xml

User-agent: GTMCrawler
Disallow: /no-gtm/
Crawl-delay: 5

User-agent: OtherBot
Disallow: /
"""


def cache_for(handler: object, **settings: int) -> tuple[RobotsCache, list[str]]:
    requested: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)  # type: ignore[operator]

    fetcher = make_fetcher(recording, max_retries=0)
    return RobotsCache(fetcher, RobotsSettings(**settings), "GTMCrawler"), requested


def test_rules_for_our_user_agent_group() -> None:
    cache, _ = cache_for(lambda r: response(200, ROBOTS, **{"Content-Type": "text/plain"}))

    async def run() -> None:
        policy = await cache.policy_for("https://example.com/")
        assert policy.status is RobotsStatus.PARSED
        # Our own group applies instead of "*" (RFC 9309: the most specific group wins).
        assert not policy.can_fetch("https://example.com/no-gtm/page")
        assert policy.can_fetch("https://example.com/private/page")
        assert policy.crawl_delay == 5.0
        assert policy.sitemaps == ["https://example.com/sitemap_index.xml"]

    asyncio.run(run())


def test_wildcard_group_and_longest_match() -> None:
    body = b"User-agent: *\nDisallow: /private/\nAllow: /private/press/\nDisallow: /*.pdf$\n"
    cache, _ = cache_for(lambda r: response(200, body))

    async def run() -> None:
        assert not await cache.can_fetch("https://example.com/private/x")
        assert await cache.can_fetch("https://example.com/private/press/launch")
        assert not await cache.can_fetch("https://example.com/files/deck.pdf")
        assert await cache.can_fetch("https://example.com/about")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "expected", "allowed"),
    [
        (404, RobotsStatus.UNAVAILABLE, True),
        (403, RobotsStatus.UNAVAILABLE, True),
        (410, RobotsStatus.UNAVAILABLE, True),
        (429, RobotsStatus.UNREACHABLE, False),
        (500, RobotsStatus.UNREACHABLE, False),
        (503, RobotsStatus.UNREACHABLE, False),
    ],
)
def test_rfc9309_availability(status: int, expected: RobotsStatus, allowed: bool) -> None:
    cache, _ = cache_for(lambda r: response(status))

    async def run() -> None:
        policy = await cache.policy_for("https://example.com/")
        assert policy.status is expected
        assert policy.can_fetch("https://example.com/anything") is allowed

    asyncio.run(run())


def test_network_failure_disallows_everything() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cache, _ = cache_for(fail)

    async def run() -> None:
        policy = await cache.policy_for("https://example.com/")
        assert policy.status is RobotsStatus.UNREACHABLE
        assert not policy.can_fetch("https://example.com/")

    asyncio.run(run())


def test_cached_per_origin() -> None:
    cache, requested = cache_for(lambda r: response(200, b"User-agent: *\nDisallow: /x\n"))

    async def run() -> None:
        for path in ("/a", "/b", "/x"):
            await cache.can_fetch(f"https://example.com{path}")
        await cache.can_fetch("https://docs.example.com/a")
        await cache.can_fetch("http://example.com/a")

    asyncio.run(run())
    assert requested == [
        "https://example.com/robots.txt",
        "https://docs.example.com/robots.txt",
        "http://example.com/robots.txt",
    ]


def test_only_max_bytes_are_parsed() -> None:
    padding = b"# " + b"x" * 200 + b"\n"
    body = b"User-agent: *\n" + padding * 10 + b"Disallow: /late/\n"
    cache, _ = cache_for(lambda r: response(200, body), max_bytes=500)

    async def run() -> None:
        # The rule sits past the byte limit, so it is not applied.
        assert await cache.can_fetch("https://example.com/late/page")

    asyncio.run(run())


def test_concurrent_requests_load_an_origin_once() -> None:
    requested: list[str] = []
    loaded: list[tuple[str, RobotsStatus]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return response(200, ROBOTS)

    async def on_load(origin: str, status: RobotsStatus, result: FetchResult) -> None:
        loaded.append((origin, status))

    cache = RobotsCache(make_fetcher(handler), RobotsSettings(), "GTMCrawler", on_load)

    async def run() -> None:
        await asyncio.gather(*(cache.can_fetch(f"https://example.com/p{i}") for i in range(5)))

    asyncio.run(run())
    assert requested == ["https://example.com/robots.txt"]
    assert loaded == [("https://example.com", RobotsStatus.PARSED)]
