"""Phase 6: adaptive rate control, circuit breakers, trap detection, budgets."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from protego import Protego
from sqlalchemy import Engine, select

from gtm_crawler.breaker import CircuitBreaker
from gtm_crawler.config import FrontierSettings
from gtm_crawler.db.models import CrawlEvent
from gtm_crawler.frontier import Frontier, FrontierEntry, Rejection
from gtm_crawler.robots import RobotsPolicy, RobotsStatus
from gtm_crawler.throttle import HostThrottle

from .test_crawl import SITE, FakeSite, crawl_urls, html, run, settings_for


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# --- adaptive rate control -------------------------------------------------


def test_overload_halves_concurrency_and_adds_delay_then_recovers() -> None:
    t = HostThrottle(4, 0.0, recover_after=3, max_extra_delay=8.0)
    t.observe("a.com", "overloaded")
    assert t.snapshot()["a.com"] == {"concurrency": 2, "extra_delay": 1.0, "slowdowns": 1}
    for _ in range(3):
        t.observe("a.com", "overloaded")
    assert t.snapshot()["a.com"]["concurrency"] == 1
    assert t.snapshot()["a.com"]["extra_delay"] == 8.0  # capped
    for _ in range(9):  # three rounds of recover_after fast successes
        t.observe("a.com", "ok", 0.1)
    assert t.snapshot()["a.com"]["concurrency"] == 4
    assert t.snapshot()["a.com"]["extra_delay"] == 1.0


def test_failures_and_slow_responses_lower_concurrency() -> None:
    t = HostThrottle(4, 0.0, slow_seconds=5.0)
    t.observe("a.com", "failed")
    assert t.snapshot()["a.com"]["concurrency"] == 2
    t.observe("a.com", "ok", 6.0)
    assert t.snapshot()["a.com"] == {"concurrency": 1, "extra_delay": 0.0, "slowdowns": 2}
    t.observe("b.com", "ok", 0.1)
    assert "b.com" not in t.snapshot()  # never slowed down


def test_adaptive_delay_adds_to_crawl_delay_and_never_shortens_it() -> None:
    clock = Clock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    t = HostThrottle(1, 0.5, clock=clock, sleep=sleep)
    t.observe("a.com", "overloaded")  # +1 s

    async def go() -> None:
        for _ in range(2):
            async with t.slot("a.com", crawl_delay=10):
                pass

    asyncio.run(go())
    assert sleeps == [11.0]


def test_lowered_limit_applies_to_new_requests() -> None:
    t = HostThrottle(3, 0.0)
    t.observe("a.com", "failed")  # 3 -> 1
    active = {"n": 0, "max": 0}

    async def request() -> None:
        async with t.slot("a.com"):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.01)
            active["n"] -= 1

    async def go() -> None:
        await asyncio.gather(*(request() for _ in range(4)))

    asyncio.run(go())
    assert active["max"] == 1


# --- circuit breaker -------------------------------------------------------


def test_circuit_opens_probes_and_closes() -> None:
    clock = Clock()
    b = CircuitBreaker("host", threshold=3, cooldown=60, max_cooldown=200, clock=clock)
    for _ in range(2):
        b.record("a.com", ok=False)
    assert b.check("a.com") == 0 and b.state("a.com") == "closed"
    b.record("a.com", ok=False)
    assert b.state("a.com") == "open" and b.check("a.com") == 60
    b.record("a.com", ok=False)  # a request that started before it opened
    clock.now = 30
    assert b.check("a.com") == 30  # cooldown not doubled by the straggler
    clock.now = 60
    assert b.check("a.com") == 0  # the probe
    assert 0 < b.check("a.com") <= 5  # everyone else waits for it
    b.record("a.com", ok=False)  # probe failed: twice as long
    assert b.check("a.com") == 120
    clock.now = 180
    assert b.check("a.com") == 0
    b.record("a.com", ok=True)
    assert b.state("a.com") == "closed" and b.check("a.com") == 0
    assert b.snapshot() == {"a.com": {"state": "closed", "trips": 2, "failures": 0}}


def test_lost_probe_is_replaced_after_a_cooldown() -> None:
    clock = Clock()
    b = CircuitBreaker("host", threshold=1, cooldown=10, max_cooldown=100, clock=clock)
    b.record("a.com", ok=False)
    clock.now = 10
    assert b.check("a.com") == 0  # probe granted, never reports back
    clock.now = 20
    assert b.check("a.com") == 0


def test_success_resets_the_failure_count() -> None:
    b = CircuitBreaker("host", threshold=2, cooldown=10, max_cooldown=100)
    for _ in range(5):
        b.record("a.com", ok=False)
        b.record("a.com", ok=True)
    assert b.state("a.com") == "closed"


# --- trap detection and budgets -------------------------------------------


def frontier(**overrides: Any) -> Frontier:
    return Frontier("acme.com", replace(FrontierSettings(), **overrides))


def add(f: Frontier, url: str) -> Rejection | None:
    return f.add(FrontierEntry(url, 1, 1.0, "link"))


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.com/pricing?sid=a81f9c2e77d04b1c9e",
        "https://acme.com/pricing?session_id=Zx81kq2ll0pq9qaa",
        "https://acme.com/(S(lit3py55t21z5v55vlm25s55))/pricing",
    ],
)
def test_session_ids_are_traps(url: str) -> None:
    assert add(frontier(), url) == Rejection.SESSION_ID


def test_short_or_non_session_parameters_are_fine() -> None:
    f = frontier()
    assert add(f, "https://acme.com/pricing?sid=eu") is None
    assert add(f, "https://acme.com/docs?page_id=a81f9c2e77d04b1c9e") is None


def test_sort_filter_combinations_are_capped_per_path() -> None:
    f = frontier(max_query_urls_per_path=3, max_urls_per_pattern=100)
    urls = [
        "https://acme.com/shop?sort=price",
        "https://acme.com/shop?color=red",
        "https://acme.com/shop?color=red&sort=price",
        "https://acme.com/shop?size=m&sort=name",
    ]
    assert [add(f, u) for u in urls] == [None, None, None, Rejection.QUERY_EXPLOSION]
    assert add(f, "https://acme.com/shop") is None  # the plain page is not a variant


def test_per_host_budget() -> None:
    f = frontier(max_urls_per_host=2)
    assert add(f, "https://acme.com/a") is None
    assert add(f, "https://acme.com/b") is None
    assert add(f, "https://acme.com/c") == Rejection.HOST_BUDGET
    assert add(f, "https://docs.acme.com/c") is None  # another host of the site


def test_low_yield_closes_a_shape_and_its_section() -> None:
    f = frontier(yield_min_fetches=4, min_unique_yield=0.5)
    f.record_yield("https://acme.com/tag/a", useful=True)
    for tag in "bcd":
        f.record_yield(f"https://acme.com/tag/{tag}", useful=False)
    assert f.low_yield("https://acme.com/tag/z")
    assert add(f, "https://acme.com/tag/z") == Rejection.LOW_YIELD
    assert not f.low_yield("https://acme.com/blog/post")


def test_useful_sections_stay_open() -> None:
    f = frontier(yield_min_fetches=4, min_unique_yield=0.2)
    for i in range(20):
        f.record_yield(f"https://acme.com/blog/post-{i}", useful=i % 3 == 0)
    assert not f.low_yield("https://acme.com/blog/next")


def test_translated_section_is_learned_from_a_page_in_its_language() -> None:
    f = frontier()
    f.learn_language("https://acme.com/", "en-US", seed=True)
    assert f.learn_language("https://acme.com/fr/", "fr-FR", seed=False) == "fr"
    assert f.learn_language("https://acme.com/de-de/x", "de", seed=False) == "de-de"
    assert f.other_language("https://www.acme.com/fr/pricing")
    assert f.other_language("https://support.acme.com/DE-DE/help")
    assert add(f, "https://acme.com/fr/blog/post") == Rejection.OTHER_LANGUAGE
    assert not f.other_language("https://acme.com/pricing")


def test_a_section_named_like_a_language_is_not_a_translation() -> None:
    f = frontier()
    f.learn_language("https://acme.com/", "en", seed=True)
    # English pages under /hr/ (human resources) or /it/: the segment is not their language.
    assert f.learn_language("https://acme.com/hr/payroll", "en", seed=False) is None
    assert f.learn_language("https://acme.com/en/", "en", seed=False) is None  # main language
    assert f.learn_language("https://acme.com/blog/fr", "fr", seed=False) is None  # not a prefix
    assert f.learn_language("https://acme.com/francais/", "fr", seed=False) is None
    assert not f.other_language("https://acme.com/hr/benefits")


def test_no_section_is_learned_while_the_main_language_is_unknown() -> None:
    f = frontier()
    assert f.learn_language("https://acme.com/fr/", "fr", seed=False) is None
    f.learn_language("https://acme.com/", None, seed=True)  # the seed states none
    assert f.learn_language("https://acme.com/fr/", "fr", seed=False) is None
    assert not f.other_language("https://acme.com/fr/a")


def test_huge_crawl_delay_is_flagged_not_shortened() -> None:
    policy = RobotsPolicy(
        "https://acme.com", RobotsStatus.PARSED, "GTMCrawler",
        Protego.parse("User-agent: *\nCrawl-delay: 3600\n"),
    )  # fmt: skip
    policy.max_crawl_delay = 60
    assert policy.crawl_delay == 3600 and policy.delay_too_long
    policy.max_crawl_delay = 7200
    assert not policy.delay_too_long


# --- in a crawl -------------------------------------------------------------


@pytest.mark.db
def test_huge_crawl_delay_defers_the_site_with_an_audited_reason(
    engine: Engine, tmp_path: Path
) -> None:
    routes = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nCrawl-delay: 86400\n"),
        f"{SITE}/": ("text/html", html("Acme", "<p>Home</p>")),
    }
    site = FakeSite(routes)
    summary = run(engine, settings_for(tmp_path), site)
    assert summary.status == "failed"
    assert crawl_urls(engine, summary.crawl_id)[f"{SITE}/"].skip_reason == "crawl_delay_too_long"
    assert site.requested == [f"{SITE}/robots.txt"]  # not even sitemaps
    with engine.connect() as conn:
        detail = conn.execute(
            select(CrawlEvent.detail).where(
                CrawlEvent.crawl_id == summary.crawl_id, CrawlEvent.reason == "crawl_delay_too_long"
            )
        ).scalar_one()
    assert detail["crawl_delay"] == 86400 and detail["max_crawl_delay"] == 60


@pytest.mark.db
def test_circuit_breaker_stops_requests_to_a_failing_host(engine: Engine, tmp_path: Path) -> None:
    links = "".join(f'<a href="/p{i}">Page {i}</a>' for i in range(8))
    routes: dict[str, Any] = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{SITE}/": ("text/html", html("Acme", links)),
        **{f"{SITE}/p{i}": 500 for i in range(8)},
    }
    site = FakeSite(routes)
    settings = settings_for(tmp_path, circuit_failure_threshold=2, circuit_cooldown_seconds=600,
                            max_duration_seconds=30)  # fmt: skip
    summary = run(engine, settings, site)
    assert len([u for u in site.requested if "/p" in u]) == 2
    assert summary.stats["stop_reason"] == "max_duration"  # the cooldown outlasts the budget
    assert summary.stats["circuits"]["acme.example"]["state"] == "open"
    assert summary.stats["circuit_deferrals"] == 6
    rows = crawl_urls(engine, summary.crawl_id)
    postponed = [r for r in rows.values() if r.skip_reason == "circuit_open"]
    assert len(postponed) == 6 and {r.state for r in postponed} == {"retry"}


@pytest.mark.db
def test_template_duplicates_close_their_section(engine: Engine, tmp_path: Path) -> None:
    links = "".join(f'<a href="/tag/t{i}">Tag {i}</a>' for i in range(12))
    empty = ("text/html", html("Tag", "<p>No posts yet.</p>"))
    routes: dict[str, Any] = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{SITE}/": ("text/html", html("Acme", links)),
        **{f"{SITE}/tag/t{i}": empty for i in range(12)},
    }
    site = FakeSite(routes)
    settings = settings_for(tmp_path)
    settings = replace(settings, frontier=replace(settings.frontier, yield_min_fetches=4,
                                                  min_unique_yield=0.5))  # fmt: skip
    summary = run(engine, settings, site)
    assert summary.status == "completed"
    assert len([u for u in site.requested if "/tag/" in u]) == 4
    rows = crawl_urls(engine, summary.crawl_id)
    trapped = [r for r in rows.values() if r.skip_reason == "trap:low_yield"]
    assert len(trapped) == 8 and {r.state for r in trapped} == {"trap"}
    assert summary.stats["low_yield_skipped"] == 8


def lang_page(lang: str, title: str, body: str) -> bytes:
    head = f"<head><title>{title}</title></head>"
    return f'<html lang="{lang}">{head}<body>{body}</body></html>'.encode()


@pytest.mark.db
def test_translated_copies_of_the_site_are_not_fetched(engine: Engine, tmp_path: Path) -> None:
    french = [f"{SITE}/fr/", f"{SITE}/fr/tarifs", f"{SITE}/fr/blog"]
    routes: dict[str, Any] = {
        f"{SITE}/robots.txt": ("text/plain",
                               f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n".encode()),
        f"{SITE}/sitemap.xml": ("application/xml", (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in [*french[1:], f"{SITE}/pricing"])
            + "</urlset>").encode()),
        f"{SITE}/": ("text/html", lang_page("en-US", "Acme", '<a href="/fr/">Français</a>'
                                            '<a href="/pricing">Pricing</a><p>Software.</p>')),
        f"{SITE}/pricing": ("text/html", lang_page("en", "Pricing", "<p>Starter $29.</p>")),
        **{u: ("text/html", lang_page("fr-FR", "Acme", f"<p>Logiciel {i}.</p>"))
           for i, u in enumerate(french)},
    }  # fmt: skip
    site = FakeSite(routes)
    summary = run(engine, settings_for(tmp_path), site)
    assert summary.status == "completed"
    assert f"{SITE}/pricing" in site.requested
    assert len([u for u in site.requested if u in french]) == 1  # the one that showed it
    rows = crawl_urls(engine, summary.crawl_id)
    skipped = [u for u, r in rows.items() if r.skip_reason == "other_language"]
    assert len(skipped) == 2 and {rows[u].state for u in skipped} == {"deferred"}
    assert summary.stats["other_language_skipped"] == 2
    assert summary.stats["languages"] == {"main": "en", "translated": ["fr"]}
