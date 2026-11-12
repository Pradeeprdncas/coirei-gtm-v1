import asyncio

import pytest

from gtm_crawler.config import PrioritySettings
from gtm_crawler.priority import Prioritizer
from gtm_crawler.throttle import HostThrottle

P = Prioritizer(PrioritySettings())
BASE = "https://acme.example"


def test_gtm_pages_outrank_generic_and_low_value_pages() -> None:
    pricing = P.score(f"{BASE}/pricing", depth=1)
    case_studies = P.score(f"{BASE}/customers/case-studies", depth=2)
    blog_post = P.score(f"{BASE}/blog/why-inventory-matters", depth=2)
    generic = P.score(f"{BASE}/some-page", depth=1)
    tag_page = P.score(f"{BASE}/tag/inventory", depth=1)
    assert pricing > case_studies > blog_post > generic > tag_page


def test_anchor_text_and_context_raise_opaque_urls() -> None:
    plain = P.score(f"{BASE}/p/12", depth=1)
    anchored = P.score(f"{BASE}/p/12", depth=1, anchor_text="See our pricing")
    in_context = P.score(f"{BASE}/p/12", depth=1, context="Meet the leadership team")
    assert anchored > in_context > plain


def test_depth_and_sitemap_signals() -> None:
    assert P.score(f"{BASE}/x", depth=1) > P.score(f"{BASE}/x", depth=5)
    listed = P.score(f"{BASE}/x", depth=1, in_sitemap=True)
    listed_high = P.score(f"{BASE}/x", depth=1, in_sitemap=True, sitemap_priority=1.0)
    assert listed_high > listed > P.score(f"{BASE}/x", depth=1)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("/about", 1.0),
        ("/customers/case-studies/acme", 1.0),  # multi-token term
        ("/studies/case", 0.0),  # term tokens must appear in order
        ("/docs/api", 0.5),
        ("/canvas", 0.0),  # "vs" must be a whole token
        ("/compare/acme-vs-other", 1.0),
    ],
)
def test_term_matching_uses_whole_tokens(text: str, value: float) -> None:
    assert P.term_value(text) == value


def test_inlink_boost_diminishes() -> None:
    assert P.inlink_boost(1) > P.inlink_boost(2) > P.inlink_boost(10) > 0


def test_throttle_spaces_requests_and_honours_crawl_delay() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    throttle = HostThrottle(1, 0.5, clock=lambda: now[0], sleep=fake_sleep)

    async def run() -> None:
        for _ in range(3):
            async with throttle.slot("a.example"):
                pass
        async with throttle.slot("b.example"):  # other hosts are independent
            pass
        for _ in range(2):
            async with throttle.slot("c.example", crawl_delay=10):
                pass
        async with throttle.slot("d.example", crawl_delay=0.1):  # never below the minimum
            pass
        async with throttle.slot("d.example", crawl_delay=0.1):
            pass

    asyncio.run(run())
    assert sleeps == [0.5, 0.5, 10.0, 0.5]


def test_throttle_limits_concurrency_per_host() -> None:
    throttle = HostThrottle(2, 0.0)
    active = {"n": 0, "max": 0}

    async def request() -> None:
        async with throttle.slot("a.example"):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.01)
            active["n"] -= 1

    async def run() -> None:
        await asyncio.gather(*(request() for _ in range(6)))

    asyncio.run(run())
    assert active["max"] == 2


def test_signals_explain_the_score() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 29, tzinfo=UTC)
    signals = P.signals(f"{BASE}/pricing", 1, "Plans", in_sitemap=True, sitemap_priority=0.8,
                        lastmod="2026-09-29", parent_topic="Acme products", now=now)  # fmt: skip
    assert signals == {"path": 4.0, "anchor": 3.0, "parent": 1.0, "sitemap": 0.8,
                       "freshness": 1.0, "depth": 1.0}  # fmt: skip
    assert P.score(f"{BASE}/pricing", 1, "Plans", in_sitemap=True, sitemap_priority=0.8,
                   lastmod="2026-09-29", parent_topic="Acme products", now=now) == 10.8  # fmt: skip
    assert P.signals(f"{BASE}/tag/x", 1)["low_value"] == -4.0


@pytest.mark.parametrize(
    ("lastmod", "value"),
    [("2026-09-29T08:00:00Z", 1.0), ("2025-09-29", 0.5), ("2020-01-01", 0.0),
     ("2027-01-01", 1.0), ("not a date", 0.0), (None, 0.0)],
)  # fmt: skip
def test_freshness_from_sitemap_lastmod(lastmod: str | None, value: float) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 29, 8, tzinfo=UTC)
    assert P.freshness(lastmod, now) == pytest.approx(value, abs=0.01)


def test_links_inherit_the_relevance_of_the_page_they_are_on() -> None:
    on_products_page = P.score(f"{BASE}/p/12", 2, parent_topic="Our products | Acme")
    on_generic_page = P.score(f"{BASE}/p/12", 2, parent_topic="Hello world")
    assert on_products_page > on_generic_page
