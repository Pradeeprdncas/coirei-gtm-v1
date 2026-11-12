"""Phase 4: the policy engine, robots.txt snapshots and expiry, auditable
refusals, redirect hops, and sitemap fetch records."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import RobotsSettings, SitemapSettings, UrlSettings
from gtm_crawler.db.models import CrawlEvent, RobotsSnapshot, SitemapFetch, Url
from gtm_crawler.domain import PolicyDecision
from gtm_crawler.policy import PolicyEngine
from gtm_crawler.robots import RobotsCache
from gtm_crawler.sitemaps import SitemapFetchRecord, discover_sitemaps
from gtm_crawler.throttle import HostThrottle

from .helpers import make_fetcher, response
from .test_crawl import SITE, FakeSite, crawl_urls, html, run, settings_for

ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 3\n"


def engine_for(routes: dict[str, Any], clock: Any = None) -> tuple[PolicyEngine, list[str]]:
    requested: list[str] = []
    loads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        route = routes.get(str(request.url), 404)
        return response(route) if isinstance(route, int) else response(200, route)

    async def on_load(origin: str, status: Any, result: Any) -> int:
        loads.append(origin)
        return len(loads)  # stands in for the stored snapshot's id

    cache = RobotsCache(make_fetcher(handler, max_retries=0), RobotsSettings(max_age_seconds=60),
                        "GTMCrawler", on_load, **({"clock": clock} if clock else {}))  # fmt: skip
    return PolicyEngine(cache, lambda url: url.startswith("https://acme.com")), requested


def test_decisions_are_explicit_and_explained() -> None:
    policy, _ = engine_for({"https://acme.com/robots.txt": ROBOTS,
                            "https://down.com/robots.txt": 503})  # fmt: skip

    async def go() -> None:
        allow = await policy.check("https://acme.com/pricing")
        assert allow.allowed and allow.crawl_delay == 3.0
        blocked = await policy.check("https://acme.com/private/x")
        assert (blocked.decision, blocked.reason) == (PolicyDecision.BLOCK, "robots_disallowed")
        assert blocked.detail["robots_snapshot_id"] == 1
        assert blocked.detail["robots_origin"] == "https://acme.com"
        off_site = await policy.check("https://other.com/")
        assert (off_site.decision, off_site.reason) == (PolicyDecision.REJECT, "out_of_scope")
        # An external page skips the scope rule, never robots.txt.
        down = await policy.check("https://down.com/page", external=True)
        assert (down.decision, down.reason) == (PolicyDecision.BLOCK, "robots_unreachable")

    asyncio.run(go())


def test_robots_rules_expire_and_reload_as_a_new_snapshot() -> None:
    now = [0.0]
    policy, requested = engine_for({"https://acme.com/robots.txt": ROBOTS}, clock=lambda: now[0])

    async def go() -> None:
        first = await policy.check("https://acme.com/private/x")
        await policy.check("https://acme.com/private/y")
        now[0] = 61  # past max_age_seconds
        again = await policy.check("https://acme.com/private/x")
        assert first.detail["robots_snapshot_id"] == 1
        assert again.detail["robots_snapshot_id"] == 2

    asyncio.run(go())
    assert requested.count("https://acme.com/robots.txt") == 2


# --- crawl integration -------------------------------------------------------------


class RedirectingSite(FakeSite):
    def __init__(self, routes: dict[str, Any], redirects: dict[str, str]) -> None:
        super().__init__(routes)
        self.redirects = redirects

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in self.redirects:
            self.requested.append(url)
            return response(301, **{"Location": self.redirects[url]})
        return super().__call__(request)


ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /private/\n"
                                         b"Sitemap: https://acme.example/sitemap-private/a.xml\n"),
    f"{SITE}/": ("text/html", html("Acme", '<a href="/go">Go</a><a href="/private/x">X</a>'
                                           '<a href="/fine">Fine</a>')),
    f"{SITE}/fine": ("text/html", html("Fine", "<p>Fine.</p>")),
    f"{SITE}/private/secret": ("text/html", html("Secret", "<p>Secret.</p>")),
    f"{SITE}/sitemap.xml": ("application/xml",
                            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                            b"<url><loc>https://acme.example/fine</loc></url>"
                            b"<url><loc>https://elsewhere.example/x</loc></url></urlset>"),
}  # fmt: skip


def refusals(engine: Engine, crawl_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(Url.url, CrawlEvent.to_state, CrawlEvent.reason, CrawlEvent.detail)
            .join(Url, Url.id == CrawlEvent.url_id)
            .where(CrawlEvent.crawl_id == crawl_id, CrawlEvent.event == "policy")
        ).all()
    return {r.url: r for r in rows}


@pytest.mark.db
def test_refusals_are_recorded_with_their_evidence(engine: Engine, tmp_path: Path) -> None:
    site = RedirectingSite(ROUTES, {f"{SITE}/go": f"{SITE}/private/secret"})
    summary = run(engine, settings_for(tmp_path), site)
    # The in-site redirect into a disallowed path is not followed.
    assert f"{SITE}/private/secret" not in site.requested
    rows = crawl_urls(engine, summary.crawl_id)
    assert (rows[f"{SITE}/go"].state, rows[f"{SITE}/go"].skip_reason) == (
        "blocked", "redirect_robots_disallowed")  # fmt: skip
    assert (rows[f"{SITE}/private/x"].state, rows[f"{SITE}/private/x"].skip_reason) == (
        "blocked", "robots_disallowed")  # fmt: skip

    events = refusals(engine, summary.crawl_id)
    direct, redirect = events[f"{SITE}/private/x"], events[f"{SITE}/go"]
    assert (direct.to_state, direct.reason) == ("block", "robots_disallowed")
    assert redirect.detail["target"] == f"{SITE}/private/secret"
    with engine.connect() as conn:
        snapshot = conn.execute(
            select(RobotsSnapshot).where(RobotsSnapshot.id == direct.detail["robots_snapshot_id"])
        ).one()  # fmt: skip
    assert snapshot.crawl_id == summary.crawl_id and snapshot.origin == SITE
    assert summary.stats["policy_decisions"] == {
        "block:robots_disallowed": 1, "block:redirect_robots_disallowed": 1}  # fmt: skip


@pytest.mark.db
def test_sitemap_fetches_are_recorded(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                Url.url,
                SitemapFetch.status_code,
                SitemapFetch.error,
                SitemapFetch.raw_sha256,
                SitemapFetch.url_count,
                SitemapFetch.dropped,
                SitemapFetch.duration_ms,
                SitemapFetch.depth,
            )
            .join(Url, Url.id == SitemapFetch.url_id)
            .where(SitemapFetch.crawl_id == summary.crawl_id)
        ).all()
    by_url = {r.url: r for r in rows}
    missing = by_url[f"{SITE}/sitemap-private/a.xml"]  # declared in robots.txt, absent
    assert (missing.status_code, missing.error, missing.raw_sha256) == (404, "http_404", None)
    ok = by_url[f"{SITE}/sitemap.xml"]
    assert (ok.status_code, ok.url_count, ok.dropped, ok.error) == (200, 1, 1, None)
    assert ok.raw_sha256 and ok.duration_ms is not None and ok.depth == 0


def test_sitemap_requests_obey_robots_and_the_throttle() -> None:
    sleeps: list[float] = []
    now = [0.0]

    async def sleep(seconds: float) -> None:  # the throttle re-checks its clock after sleeping
        sleeps.append(seconds)
        now[0] += seconds

    routes = {
        "https://example.com/robots.txt": b"User-agent: *\nDisallow: /hidden/\nCrawl-delay: 4\n"
        b"Sitemap: https://example.com/hidden/map.xml\nSitemap: https://example.com/a.xml\n",
        "https://example.com/a.xml": b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/p</loc></url></urlset>",
    }
    records: list[SitemapFetchRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url))
        if route is None:
            return response(404)
        return response(200, route, **{"Content-Type": "application/xml"})

    async def on_fetch(record: SitemapFetchRecord) -> None:
        records.append(record)

    async def go() -> None:
        async with make_fetcher(handler, max_retries=0) as fetcher:
            robots = RobotsCache(fetcher, RobotsSettings(), "GTMCrawler")
            throttle = HostThrottle(1, 0.0, clock=lambda: now[0], sleep=sleep)
            await discover_sitemaps(
                "https://example.com/", fetcher, robots, SitemapSettings(), UrlSettings(),
                throttle=throttle, on_fetch=on_fetch,
            )  # fmt: skip

    asyncio.run(go())
    by_url = {r.url: r for r in records}
    assert by_url["https://example.com/hidden/map.xml"].error == "robots_disallowed"
    assert by_url["https://example.com/hidden/map.xml"].result is None
    assert by_url["https://example.com/a.xml"].url_count == 1
    assert by_url["https://example.com/sitemap.xml"].error == "http_404"
    assert sleeps and all(s == pytest.approx(4, abs=0.1) for s in sleeps)  # crawl-delay honoured
