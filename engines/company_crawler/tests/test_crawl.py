"""End-to-end crawl of a simulated company site into the test database."""

import asyncio
import itertools
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select

from gtm_crawler.config import (
    ConfigError,
    CrawlSettings,
    ExternalSettings,
    FetchSettings,
    FrontierSettings,
    IdentitySettings,
    RawStoreSettings,
    Settings,
)
from gtm_crawler.crawl import CrawlSummary, crawl
from gtm_crawler.db.models import Crawl, CrawlUrl, Fetch, Link, Page, RobotsSnapshot, Url
from gtm_crawler.domain import IN_FLIGHT_URL_STATES
from gtm_crawler.parse import PARSER_VERSION
from gtm_crawler.rawstore import RawStore

from .helpers import IDENTITY, response

pytestmark = pytest.mark.db

SITE = "https://acme.example"


def html(title: str, body: str, head: str = "") -> bytes:
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>".encode()


def a(href: str, text: str) -> str:
    return f'<a href="{href}">{text}</a>'


ABOUT = html("About", "<p>Acme Inc was founded in 2019 in Austin.</p>" + a("/", "Home"))

ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": (
        "text/plain",
        b"User-agent: *\nDisallow: /private/\nSitemap: https://acme.example/sitemap.xml\n",
    ),
    f"{SITE}/sitemap.xml": (
        "application/xml",
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://acme.example/pricing</loc><priority>0.9</priority></url>"
        b"<url><loc>https://acme.example/private/secret</loc></url>"
        b"<url><loc>https://other.example/x</loc></url></urlset>",
    ),
    f"{SITE}/": (
        "text/html",
        html(
            "Acme",
            "<nav>"
            + "".join(
                [
                    a("/about", "About us"),
                    a("/pricing", "Pricing"),
                    a("/blog/post-1", "A post"),
                    a("/products/stock", "Stock"),
                    a("/tag/inventory", "Inventory tag"),
                    a("/old", "Old page"),
                    a("/moved-away", "Moved"),
                    a("/missing", "Missing"),
                    a("/deck.pdf", "Deck"),
                    a("/about-copy", "About (copy)"),
                    a("https://www.linkedin.com/company/acme", "LinkedIn"),
                    a("https://news.example/acme-raises", "Coverage"),
                    a("mailto:hi@acme.example", "Mail"),
                    *(a(f"/events/2026/{m:02d}", f"Events {m}") for m in range(1, 6)),
                ]
            )
            + "</nav><p>Inventory software for retailers.</p>",
        ),
    ),  # fmt: skip
    f"{SITE}/about": ("text/html", ABOUT),
    f"{SITE}/about-copy": ("text/html", ABOUT),
    f"{SITE}/about-us": ("text/html", html("About us", "<p>Redirect target page text.</p>")),
    f"{SITE}/pricing": (
        "text/html",
        html(
            "Pricing",
            "<p>Starter $29 per month.</p>",
            '<link rel="canonical" href="https://acme.example/plans">',
        ),
    ),  # fmt: skip
    f"{SITE}/plans": ("text/html", html("Plans", "<p>Plans page with different text.</p>")),
    f"{SITE}/products/stock": ("text/html", html("Stock", "<p>Stock counts inventory.</p>")),
    f"{SITE}/blog/post-1": ("text/html", html("Post", "<p>A blog post about stock.</p>")),
    f"{SITE}/tag/inventory": ("text/html", html("Tag", "<p>Tag listing.</p>")),
    f"{SITE}/deck.pdf": ("application/pdf", b"%PDF-1.4"),
    **{
        f"{SITE}/events/2026/{m:02d}": ("text/html", html("Events", f"<p>Events month {m}.</p>"))
        for m in range(1, 6)
    },
}
REDIRECTS = {
    f"{SITE}/old": f"{SITE}/about-us",
    f"{SITE}/moved-away": "https://elsewhere.example/landing",
}


class FakeSite:
    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = ROUTES if routes is None else routes
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in REDIRECTS:
            return response(301, **{"Location": REDIRECTS[url]})
        route = self.routes.get(url)
        if route is None:
            return response(404)
        if isinstance(route, int):
            return response(route)
        content_type, body = route
        return response(200, body, **{"Content-Type": content_type})


def settings_for(tmp_path: Path, **crawl_overrides: Any) -> Settings:
    return Settings(
        identity=IDENTITY,
        fetch=FetchSettings(max_retries=0),
        frontier=FrontierSettings(max_urls_per_pattern=3),
        rawstore=RawStoreSettings(root=str(tmp_path / "raw")),
        crawl=replace(
            # One attempt per URL: retries are tested on their own.
            CrawlSettings(min_delay_seconds=0.0, concurrency=1, max_url_attempts=1),
            **crawl_overrides,
        ),
        # The site crawl is tested on its own; see test_external.py.
        external=ExternalSettings(enabled=False),
    )


def run(engine: Engine, settings: Settings, site: FakeSite, **kwargs: Any) -> CrawlSummary:
    return asyncio.run(
        crawl(f"{SITE}/", settings, engine, transport=httpx.MockTransport(site), **kwargs)
    )


def crawl_urls(engine: Engine, crawl_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(Url.url, CrawlUrl.state, CrawlUrl.skip_reason, CrawlUrl.discovered_via)
            .join(Url, Url.id == CrawlUrl.url_id)
            .where(CrawlUrl.crawl_id == crawl_id)
        ).all()
    return {row.url: row for row in rows}


@pytest.fixture(scope="module")
def full_crawl(
    engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> tuple[CrawlSummary, FakeSite, Path]:
    tmp = tmp_path_factory.mktemp("crawl")
    site = FakeSite()
    return run(engine, settings_for(tmp), site), site, tmp


def test_crawl_completes_and_respects_policies(
    full_crawl: tuple[CrawlSummary, FakeSite, Path],
) -> None:
    summary, site, _ = full_crawl
    assert summary.status == "completed" and summary.stats["stop_reason"] is None
    requested = site.requested
    assert f"{SITE}/private/secret" not in requested  # robots.txt
    assert not any(
        "other.example" in u or "linkedin" in u or "news.example" in u for u in requested
    )
    assert "https://elsewhere.example/landing" not in requested  # off-site redirect not followed
    # Seed first, then GTM-relevant pages before generic/low-value ones.
    pages = [u for u in requested if not u.endswith((".txt", ".xml"))]
    assert pages[0] == f"{SITE}/"
    assert pages.index(f"{SITE}/pricing") < pages.index(f"{SITE}/blog/post-1")
    assert pages.index(f"{SITE}/blog/post-1") < pages.index(f"{SITE}/tag/inventory")
    assert len(requested) == len(set(requested))  # nothing fetched twice


def test_crawl_url_states_record_what_happened(
    engine: Engine, full_crawl: tuple[CrawlSummary, FakeSite, Path]
) -> None:
    summary, _, _ = full_crawl
    rows = crawl_urls(engine, summary.crawl_id)
    assert rows[f"{SITE}/"].discovered_via == "seed"
    assert rows[f"{SITE}/private/secret"].skip_reason == "robots_disallowed"
    assert (
        rows[f"{SITE}/missing"].state == "failed"
        and rows[f"{SITE}/missing"].skip_reason == "http_404"
    )
    assert rows[f"{SITE}/deck.pdf"].skip_reason == "unsupported_content_type"
    assert rows[f"{SITE}/moved-away"].skip_reason == "redirect_off_site"
    assert (
        rows[f"{SITE}/plans"].discovered_via == "canonical"
        and rows[f"{SITE}/plans"].state == "processed"
    )
    assert rows[f"{SITE}/pricing"].discovered_via == "sitemap"
    events = {u: rows[u] for u in rows if "/events/" in u}
    # The pattern budget stopped the calendar; the refused months are kept as traps.
    assert sorted(r.state for r in events.values()) == ["processed"] * 3 + ["trap"] * 2
    assert {r.skip_reason for r in events.values() if r.state == "trap"} == {"trap:pattern_budget"}
    assert summary.stats["frontier_rejections"]["trap:pattern_budget"] == 2
    assert all(r.state != "eligible" for r in rows.values())
    # The duplicate About page is recorded as such; nothing is left leased.
    about_copy = rows[f"{SITE}/about-copy"]
    assert (about_copy.state, about_copy.skip_reason) == ("processed", "duplicate")
    assert not any(r.state in IN_FLIGHT_URL_STATES for r in rows.values())
    assert rows[f"{SITE}/private/secret"].state == "blocked"


def test_duplicates_links_and_raw_documents(
    engine: Engine, full_crawl: tuple[CrawlSummary, FakeSite, Path]
) -> None:
    summary, _, tmp = full_crawl
    with engine.connect() as conn:
        pages = conn.execute(
            select(Url.url, Page.id, Page.duplicate_of_page_id, Fetch.raw_sha256)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .join(Url, Url.id == func.coalesce(Fetch.final_url_id, Fetch.url_id))
            .where(Fetch.crawl_id == summary.crawl_id)
        ).all()
        by_url = {p.url: p for p in pages}
        links = conn.execute(
            select(Url.url, Link.is_external, Link.is_reference)
            .join(Url, Url.id == Link.target_url_id)
            .where(Link.page_id == by_url[f"{SITE}/"].id)
        ).all()
    assert by_url[f"{SITE}/about-copy"].duplicate_of_page_id == by_url[f"{SITE}/about"].id
    assert by_url[f"{SITE}/about"].duplicate_of_page_id is None
    assert f"{SITE}/about-us" in by_url  # in-site redirect followed and stored under final URL
    assert summary.stats["duplicates"] == 1
    assert summary.stats["external_links"] == 2  # linkedin and news.example, never fetched
    assert summary.stats["reference_links"] == 1

    link_flags = {link.url: (link.is_external, link.is_reference) for link in links}
    assert link_flags["https://www.linkedin.com/company/acme"] == (True, True)
    assert link_flags["https://news.example/acme-raises"] == (True, False)
    assert link_flags[f"{SITE}/pricing"] == (False, False)

    store = RawStore(tmp / "raw")
    assert all(store.get(p.raw_sha256) for p in pages)


def test_crawl_row_stats_and_config_snapshot(
    engine: Engine, full_crawl: tuple[CrawlSummary, FakeSite, Path]
) -> None:
    summary, _, _ = full_crawl
    with engine.connect() as conn:
        row = conn.execute(select(Crawl).where(Crawl.id == summary.crawl_id)).one()
        fetches = conn.execute(
            select(func.count()).select_from(Fetch).where(Fetch.crawl_id == summary.crawl_id)
        ).scalar_one()
    assert row.status == "completed" and row.finished_at is not None
    assert "database" not in row.config  # credentials are not recorded
    assert row.config["crawl"]["max_pages"] == 1000
    assert row.stats["fetches"] == fetches == summary.stats["fetches"]
    assert row.stats["robots_status"] == "parsed"


def test_robots_snapshot_is_kept(
    engine: Engine, full_crawl: tuple[CrawlSummary, FakeSite, Path]
) -> None:
    summary, _, tmp = full_crawl
    with engine.connect() as conn:
        rows = conn.execute(
            select(RobotsSnapshot).where(RobotsSnapshot.crawl_id == summary.crawl_id)
        ).all()
    assert [(r.origin, r.status, r.status_code) for r in rows] == [(SITE, "parsed", 200)]
    assert RawStore(tmp / "raw").get(rows[0].raw_sha256) == ROUTES[f"{SITE}/robots.txt"][1]


def test_unreachable_robots_snapshot_has_no_body(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite({f"{SITE}/robots.txt": 503}))
    with engine.connect() as conn:
        row = conn.execute(
            select(RobotsSnapshot).where(RobotsSnapshot.crawl_id == summary.crawl_id)
        ).one()
    assert (row.status, row.status_code, row.raw_sha256) == ("unreachable", 503, None)


def test_page_budget_gives_partial(engine: Engine, tmp_path: Path) -> None:
    site = FakeSite()
    summary = run(engine, settings_for(tmp_path, max_pages=3), site)
    assert summary.status == "partial" and summary.stats["stop_reason"] == "max_pages"
    assert summary.stats["fetches"] == 3


def test_time_budget_gives_partial(engine: Engine, tmp_path: Path) -> None:
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(10.0))
    summary = run(engine, settings_for(tmp_path, max_duration_seconds=5), FakeSite(),
                  clock=lambda: next(ticks))  # fmt: skip
    assert summary.status == "partial" and summary.stats["stop_reason"] == "max_duration"
    assert summary.stats["pages_parsed"] == 1


def test_unreachable_robots_fetches_nothing_else(engine: Engine, tmp_path: Path) -> None:
    site = FakeSite({f"{SITE}/robots.txt": 503})
    summary = run(engine, settings_for(tmp_path), site)
    assert summary.status == "failed" and summary.stats["stop_reason"] == "robots_unreachable"
    assert site.requested == [f"{SITE}/robots.txt"]


def test_unfetchable_seed_fails(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite({f"{SITE}/": 500}))
    assert summary.status == "failed" and summary.stats["stop_reason"] == "no_pages"


def test_missing_contact_refuses_before_writing(engine: Engine, tmp_path: Path) -> None:
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(Crawl)).scalar_one()
    settings = replace(settings_for(tmp_path), identity=IdentitySettings())
    with pytest.raises(ConfigError):
        run(engine, settings, FakeSite())
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Crawl)).scalar_one() == before


def test_concurrent_crawl_matches_sequential(engine: Engine, tmp_path: Path) -> None:
    site = FakeSite()
    summary = run(engine, settings_for(tmp_path, concurrency=8), site)
    assert summary.status == "completed"
    assert summary.stats["duplicates"] == 1  # twins fetched concurrently still resolve
    assert len(site.requested) == len(set(site.requested))


SPA_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": (
        "text/html",
        html("Acme", '<div id="root"></div><script src="/app.js"></script>'),
    ),
    f"{SITE}/app.js": (
        "application/javascript",
        b"document.getElementById('root').innerHTML = '<p>Acme builds warehouse robots"
        b' for mid-size retailers across North America.</p><a href="/about">About</a>\';',
    ),
    f"{SITE}/about": ("text/html", ABOUT),
}


@pytest.mark.parametrize("enabled", [True, False])
def test_javascript_page_is_rendered(engine: Engine, tmp_path: Path, enabled: bool) -> None:
    settings = settings_for(tmp_path)
    settings = replace(settings, browser=replace(settings.browser, min_text_chars=50,
                                                 enabled=enabled))  # fmt: skip
    summary = run(engine, settings, FakeSite(SPA_ROUTES))
    with engine.connect() as conn:
        rows = conn.execute(
            select(Fetch.method, Fetch.trigger, Fetch.duration_ms, Page.cleaned_text,
                   Page.parser_version)
            .join(Url, Url.id == Fetch.url_id)
            .outerjoin(Page, Page.fetch_id == Fetch.id)
            .where(Fetch.crawl_id == summary.crawl_id, Url.url == f"{SITE}/")
        ).all()  # fmt: skip
    urls = crawl_urls(engine, summary.crawl_id)
    assert all(r.duration_ms is not None and r.duration_ms >= 0 for r in rows)
    assert {r.parser_version for r in rows if r.cleaned_text is not None} == {PARSER_VERSION}
    if enabled:
        # Both fetches are evidence; the page comes from the render, with its reason.
        assert sorted(r.method for r in rows) == ["browser", "http"]
        triggers = {r.method: r.trigger for r in rows}
        assert triggers["http"] is None
        assert triggers["browser"].startswith("thin_text_with_scripts: ")
        page_text = {r.method: r.cleaned_text for r in rows}
        assert page_text["http"] is None and "warehouse robots" in page_text["browser"]
        assert urls[f"{SITE}/about"].state == "processed"  # link only exists in the rendered DOM
        browser = summary.stats["browser"]
        assert {k: browser[k] for k in ("renders", "used", "errors", "over_budget",
                                        "unavailable", "restarts", "circuit")} == {
            "renders": 1, "used": 1, "errors": {}, "over_budget": 0, "unavailable": None,
            "restarts": {}, "circuit": {},
        }  # fmt: skip
        # Its cost: time, and the script it requested.
        assert browser["render_ms"] > 0 and browser["requests"] >= 1 and browser["bytes"] > 0
    else:
        assert [r.method for r in rows] == ["http"]
        assert f"{SITE}/about" not in urls
        assert summary.stats["browser"]["renders"] == 0


def test_priority_breakdown_is_stored_and_sums_to_the_score(
    engine: Engine, full_crawl: tuple[CrawlSummary, FakeSite, Path]
) -> None:
    summary, _, _ = full_crawl
    with engine.connect() as conn:
        rows = conn.execute(
            select(Url.url, CrawlUrl.score, CrawlUrl.priority_signals)
            .join(Url, Url.id == CrawlUrl.url_id)
            .where(CrawlUrl.crawl_id == summary.crawl_id, CrawlUrl.is_external.is_(False))
        ).all()
    assert all(r.priority_signals for r in rows)
    for r in rows:
        assert sum(r.priority_signals.values()) == pytest.approx(r.score, abs=1e-5), r.url
    by_url = {r.url: r.priority_signals for r in rows}
    assert by_url[f"{SITE}/"] == {"seed": 1000.0}
    assert {"path", "sitemap"} <= set(by_url[f"{SITE}/pricing"])


JSONLD_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", "<p>Inventory software.</p>",
        '<script type="application/ld+json">{"@context": "https://schema.org",'
        ' "@type": "Organization", "url": "https://acme.example/company",'
        ' "sameAs": ["https://www.linkedin.com/company/acme", "https://acme.example/team"]}'
        "</script>",
    )),
    f"{SITE}/company": ("text/html", html("Company", "<p>About the company.</p>")),
    f"{SITE}/team": ("text/html", html("Team", "<p>The team.</p>")),
}  # fmt: skip


def test_structured_data_urls_are_discovered(engine: Engine, tmp_path: Path) -> None:
    site = FakeSite(JSONLD_ROUTES)
    summary = run(engine, settings_for(tmp_path), site)
    rows = crawl_urls(engine, summary.crawl_id)
    for path in ("/company", "/team"):
        assert rows[f"{SITE}{path}"].discovered_via == "structured_data"
        assert rows[f"{SITE}{path}"].state == "processed"
    with engine.connect() as conn:
        refs = conn.execute(
            select(Url.url, Link.rel, Link.is_reference)
            .join(Url, Url.id == Link.target_url_id)
            .join(Page, Page.id == Link.page_id)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .where(Fetch.crawl_id == summary.crawl_id, Link.is_external.is_(True))
        ).all()  # fmt: skip
    assert ("https://www.linkedin.com/company/acme", "structured-data", True) in refs


def test_stored_refusals_are_bounded_and_counted(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings = replace(settings, frontier=replace(settings.frontier, max_recorded_rejections=1))
    summary = run(engine, settings, FakeSite())
    traps = [r for r in crawl_urls(engine, summary.crawl_id).values() if r.state == "trap"]
    assert len(traps) == 1
    assert summary.stats["frontier_rejections"]["trap:pattern_budget"] == 2


class ValidatingSite(FakeSite):
    """Serves an ETag per page and answers 304 when the request carries it."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        out = super().__call__(request)
        if (
            out.status_code != 200
            or not str(request.url).startswith(SITE)
            or str(request.url).endswith((".txt", ".xml"))
        ):
            return out
        etag = f'"{zlib.crc32(str(request.url).encode()):x}"'
        if request.headers.get("if-none-match") == etag:
            self.revalidated.append(str(request.url))
            return response(304, **{"ETag": etag})
        out.headers["ETag"] = etag
        return out

    def __init__(self) -> None:
        super().__init__()
        self.revalidated: list[str] = []


def test_recrawl_revalidates_and_reuses_stored_pages(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    first = run(engine, settings, ValidatingSite())
    # The second crawl must not reuse the first crawl's fingerprints: a new crawl.
    site = ValidatingSite()
    second = run(engine, settings, site)
    assert site.revalidated and second.stats["not_modified"] == len(site.revalidated)
    assert second.stats["pages_parsed"] == first.stats["pages_parsed"]
    with engine.connect() as conn:
        rows = conn.execute(
            select(Fetch.status_code, Fetch.raw_sha256, Page.id)
            .outerjoin(Page, Page.fetch_id == Fetch.id)
            .where(Fetch.crawl_id == second.crawl_id, Fetch.status_code == 304)
        ).all()
    assert rows and all(r.raw_sha256 and r.id for r in rows)  # 304s are stored as pages

    # A pruned stored copy is fetched whole instead of revalidated.
    raw = RawStore(tmp_path / "raw")
    for path in (tmp_path / "raw").rglob("*.gz"):
        path.unlink()
    site = ValidatingSite()
    third = run(engine, settings, site)
    # Only a page whose identical content this crawl stored again is revalidated
    # (the store is content-addressed); every reuse succeeds.
    assert len(site.revalidated) < second.stats["not_modified"]
    assert third.stats["not_modified"] == len(site.revalidated)
    assert third.stats["pages_parsed"] == first.stats["pages_parsed"]
    assert not third.stats["fetch_errors"].get("decode_error")
    assert raw.root.exists()
