"""The bounded external phase: reference profiles and search results, fetched by us."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text

from gtm_crawler.config import ExternalSettings, SearchSettings, Secret, Settings
from gtm_crawler.crawl import CrawlSummary, crawl
from gtm_crawler.db.identity import resolve_profiles
from gtm_crawler.db.models import (
    Crawl,
    IdentityDecision,
    Observation,
    Organization,
    Relationship,
    SearchQuery,
)
from gtm_crawler.dossier import build_dossier, clean_dossier
from gtm_crawler.extract import extract_crawl
from gtm_crawler.search import competitor_queries, rank_candidates

from .test_crawl import SITE, FakeSite, crawl_urls, html, settings_for

pytestmark = pytest.mark.db

KEY = "exa-test-key-do-not-store"
ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", '<a href="https://www.linkedin.com/company/acme">LinkedIn</a><p>We are Acme.</p>',
        '<meta property="og:site_name" content="Acme">',
    )),
    "https://www.linkedin.com/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /\n"),
    "https://www.crunchbase.com/organization/acme": ("text/html", html(
        "Acme - Crunchbase", "<p>Acme raised $12M in total funding.</p>"
        f'<a href="{SITE}/">Website</a>')),
    "https://reviews.example/acme-alternatives": ("text/html", html(
        "Acme alternatives", "<h1>Top Acme Alternatives</h1><h2>1. Globex</h2><h2>2. Initech</h2>"
        '<a href="https://globex.com/">Globex</a>'
        f'<a href="{SITE}/">Acme</a>')),
    "https://news.example/a": ("text/html", html("A", "<p>News.</p>")),
    "https://news.example/b": ("text/html", html("B", "<p>News.</p>")),
}  # fmt: skip
# Competitor queries with no first-party values: the company's name only.
NAME_QUERIES = [q for _, q in competitor_queries(["Acme"])]
GAP_QUERIES = ['"Acme" funding', '"Acme" revenue customers', '"Acme"']


class FakeSearch:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[tuple[dict[str, Any], str | None]] = []
        self.contents: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.status != 200:
            self.requests.append((body, request.headers.get("x-api-key")))
            return httpx.Response(self.status, text="quota exceeded")
        if request.url.path == "/contents":  # the provider's copy of a LinkedIn page
            self.contents += body["urls"]
            return httpx.Response(200, json={"results": [{"url": u, "title": "Acme", "text": (
                "# Acme\n- Homepage: acme.example\n- Company Size: 51-200 employees"
            )} for u in body["urls"]]})  # fmt: skip
        self.requests.append((body, request.headers.get("x-api-key")))
        query = body["query"]
        if body.get("includeDomains"):
            urls = ["https://www.crunchbase.com/organization/acme",
                    "https://www.crunchbase.com/organization/globex"]  # fmt: skip
        elif query.startswith("best "):
            urls = ["https://blog.example/best-crm"]
        elif "alternatives" in query:
            urls = ["https://reviews.example/acme-alternatives", f"{SITE}/",
                    "https://news.example/a", "https://news.example/b"]  # fmt: skip
        else:
            urls = ["https://reviews.example/acme-alternatives"]
        return httpx.Response(200, json={"results": [{"url": u, "title": "t"} for u in urls]})

    def queries(self) -> list[str]:
        return [b["query"] for b, _ in self.requests]


def run(
    engine: Engine,
    settings: Settings,
    search: FakeSearch | None,
    gaps: Callable[[int, int], set[str]] | None = None,
    routes: dict[str, Any] | None = None,
    known: Callable[[int, int], dict[str, list[str]]] | None = None,
) -> tuple[CrawlSummary, FakeSite]:
    site = FakeSite(routes or ROUTES)
    summary = asyncio.run(crawl(
        f"{SITE}/", settings, engine, transport=httpx.MockTransport(site),
        search_transport=httpx.MockTransport(search) if search else None, gaps=gaps,
        known=known,
    ))  # fmt: skip
    return summary, site


def external(tmp_path: Path, key: str = KEY, **overrides: Any) -> Settings:
    # Fast retries: backoff is tested on its own (test_search.py).
    search = SearchSettings(api_key=Secret(key), retry_backoff_seconds=0.001)
    return replace(settings_for(tmp_path), search=search,
                   external=ExternalSettings(**overrides))  # fmt: skip


def test_external_phase(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    summary, site = run(engine, external(tmp_path), search)
    ext = summary.stats["external"]
    assert ext["subject"] == "Acme" and ext["search_error"] is None
    # Without a first-party pass every field is open, so every gap query runs;
    # then competitor discovery, which runs every time.
    assert ext["gaps"] == "all"
    assert search.queries() == GAP_QUERIES + NAME_QUERIES
    assert all(key == KEY for _, key in search.requests)
    assert "linkedin.com" in search.requests[2][0]["includeDomains"]
    # Competitor queries keep result slots for other sites.
    assert all(b["excludeDomains"] == ["acme.example"] for b, _ in search.requests[3:])
    assert ext["competitors"] == {"queries": 11, "queries_over_budget": 0,
                                  "search_pages": 2, "second_hop": []}  # fmt: skip

    requested = site.requested
    # The LinkedIn page the site links to is never fetched from linkedin.com
    # (its robots.txt refuses crawlers): the search provider's copy is taken.
    assert not any("linkedin.com" in u for u in requested)
    assert search.contents == ["https://www.linkedin.com/company/acme"]
    # A profile found by search is fetched only if it is this company's.
    assert "https://www.crunchbase.com/organization/acme" in requested
    assert "https://www.crunchbase.com/organization/globex" not in requested
    # Search results: fetched once each, never our own site again, links not followed.
    assert requested.count("https://reviews.example/acme-alternatives") == 1
    assert "https://globex.com/" not in requested
    assert requested.count(f"{SITE}/") == 1
    states = crawl_urls(engine, summary.crawl_id)
    assert states["https://www.linkedin.com/company/acme"].state == "processed"
    assert states["https://reviews.example/acme-alternatives"].discovered_via == "search"

    # The key is a credential: it is never stored with the crawl.
    with engine.connect() as conn:
        row = conn.execute(select(Crawl).where(Crawl.id == summary.crawl_id)).one()
    assert KEY not in json.dumps(row.config) and KEY not in json.dumps(row.stats, default=str)

    # Third-party evidence: competitors from the alternatives list, funding from the
    # profile, which is taken as Acme's because it links to Acme's site. The
    # LinkedIn copy names acme.example as its homepage: a link both ways.
    with engine.begin() as conn:
        assert resolve_profiles(conn, summary.crawl_id) == {"match": 1, "probable_match": 1}
    stats = extract_crawl(engine, external(tmp_path), summary.crawl_id)
    # The two news pages name nobody's site: they are kept and not read.
    assert stats["rejected"] == 0 and stats["identity_unverified_pages"] == 2
    with engine.connect() as conn:
        obs = conn.execute(
            select(Observation.field, Observation.value_verbatim, Observation.source_type,
                   Observation.is_first_party)
            .where(Observation.crawl_id == summary.crawl_id,
                   Observation.field.in_(["competitor", "total_funding", "company_size"]))
        ).all()  # fmt: skip
        rels = conn.execute(
            select(Organization.name, Relationship.basis)
            .join(Organization, Organization.id == Relationship.object_org_id)
            .join(Observation, Observation.id == Relationship.observation_id)
            .where(Observation.crawl_id == summary.crawl_id, Relationship.type == "competitor")
        ).all()  # fmt: skip
    assert sorted((o.field, o.value_verbatim, o.source_type) for o in obs) == [
        ("company_size", "51-200 employees", "professional_profile"),
        ("competitor", "Globex", "unverified_third_party"),
        ("competitor", "Initech", "unverified_third_party"),
        ("total_funding", "$12M in total", "business_database"),
    ]
    assert not any(o.is_first_party for o in obs)
    assert sorted(rels) == [("Globex", "third_party_alternatives_list"),
                            ("Initech", "third_party_alternatives_list")]  # fmt: skip


def test_a_profile_with_the_same_name_and_no_link_says_nothing_about_the_company(
    engine: Engine, tmp_path: Path
) -> None:
    # Some other Acme: its profile and an article about it name it, and nothing
    # links either to acme.example.
    routes = {**ROUTES,
              "https://www.crunchbase.com/organization/acme": ("text/html", html(
                  "Acme - Crunchbase", "<p>Acme raised $12M in total funding.</p>")),
              "https://news.example/a": ("text/html", html(
                  "Acme raises", "<p>Acme raised $95M in total funding.</p>"
                  '<a href="https://acme-surplus.example/">Acme</a>'))}  # fmt: skip
    with engine.begin() as conn:  # what earlier tests decided about this profile
        conn.execute(text("TRUNCATE identity_link, identity_decision, external_identity CASCADE"))
    summary, site = run(engine, external(tmp_path), FakeSearch(), routes=routes)
    assert "https://www.crunchbase.com/organization/acme" in site.requested
    for _ in range(2):  # the same answer, and no second decision, on a rerun
        with engine.begin() as conn:
            assert resolve_profiles(conn, summary.crawl_id) == {"match": 1, "unknown": 1}
        stats = extract_crawl(engine, external(tmp_path), summary.crawl_id)
        assert stats["identity_unverified_pages"] == 3  # the profile, both articles
    with engine.connect() as conn:
        funding = conn.execute(
            select(Observation.id)
            .where(Observation.crawl_id == summary.crawl_id, Observation.field == "total_funding")
        ).all()  # fmt: skip
        unknown = conn.execute(
            select(IdentityDecision.signals, IdentityDecision.organization_id)
            .where(IdentityDecision.crawl_id == summary.crawl_id,
                   IdentityDecision.outcome == "unknown")
        ).all()  # fmt: skip
    assert funding == []
    assert [tuple(u) for u in unknown] == [([], summary.organization_id)]


def test_a_same_name_linkedin_page_gives_no_size(engine: Engine, tmp_path: Path) -> None:
    # The site links to no LinkedIn page; search finds one for another Acme,
    # whose copy names another homepage. Its size is not Acme's.
    class OtherAcme(FakeSearch):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path == "/contents":
                self.contents += body["urls"]
                return httpx.Response(200, json={"results": [{"url": body["urls"][0], "text": (
                    "# Acme\n- Homepage: acme-surplus.example\n- Company Size: 2-10 employees"
                )}]})  # fmt: skip
            if body.get("includeDomains"):
                self.requests.append((body, None))
                return httpx.Response(
                    200,
                    json={"results": [{"url": "https://www.linkedin.com/company/acme-surplus"}]},
                )
            return super().__call__(request)

    routes = {
        **ROUTES,
        f"{SITE}/": (
            "text/html",
            html("Acme", "<p>We are Acme.</p>", '<meta property="og:site_name" content="Acme">'),
        ),
    }
    search = OtherAcme()
    summary, _ = run(engine, external(tmp_path), search, routes=routes)
    assert search.contents == ["https://www.linkedin.com/company/acme-surplus"]
    with engine.begin() as conn:
        resolve_profiles(conn, summary.crawl_id)
    extract_crawl(engine, external(tmp_path), summary.crawl_id)
    with engine.connect() as conn:
        sizes = conn.execute(
            select(Observation.id).where(Observation.crawl_id == summary.crawl_id,
                                         Observation.field == "company_size")
        ).all()  # fmt: skip
    assert sizes == []


def test_search_targets_only_open_fields(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    calls: list[tuple[int, int]] = []

    def gaps(crawl_id: int, organization_id: int) -> set[str]:
        calls.append((crawl_id, organization_id))
        return {"competitor", "company_description"}

    summary, site = run(engine, external(tmp_path), search, gaps)
    # Once: without ``known`` there is no second round to extract for.
    assert calls == [(summary.crawl_id, summary.organization_id)]
    assert summary.stats["external"]["gaps"] == ["company_description", "competitor"]
    # No gap query can fill these; competitor discovery runs regardless.
    assert search.queries() == NAME_QUERIES
    # No profile field is open: profiles are neither searched nor fetched.
    assert not any(u.startswith("https://www.crunchbase.com") for u in site.requested)
    assert "https://www.linkedin.com/robots.txt" not in site.requested


def test_no_gap_search_when_first_party_evidence_suffices(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    summary, site = run(engine, external(tmp_path), search, lambda crawl_id, org_id: set())
    ext = summary.stats["external"]
    assert ext["gaps"] == [] and ext["search_pages"] == ext["reference_pages"] == 0
    assert search.queries() == NAME_QUERIES  # competitors only
    assert not any("crunchbase" in u for u in site.requested)
    # With competitor discovery off too, nothing leaves the site.
    search = FakeSearch()
    summary, site = run(engine, external(tmp_path, max_competitor_queries=0), search,
                        lambda crawl_id, org_id: set())  # fmt: skip
    assert search.requests == [] and all(u.startswith(SITE) for u in site.requested)


def test_search_page_caps(engine: Engine, tmp_path: Path) -> None:
    settings = external(tmp_path, max_search_pages=0, max_competitor_search_pages=1)
    summary, site = run(engine, settings, FakeSearch())
    ext = summary.stats["external"]
    assert ext["search_pages"] == 0 and ext["competitors"]["search_pages"] == 1
    # The page most queries returned is the one fetched.
    assert "https://reviews.example/acme-alternatives" in site.requested
    assert "https://news.example/a" not in site.requested


def test_competitor_query_budget_is_recorded(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    summary, _ = run(engine, external(tmp_path, max_competitor_queries=2), search,
                     lambda crawl_id, org_id: set())  # fmt: skip
    assert search.queries() == NAME_QUERIES[:2]
    assert summary.stats["external"]["competitors"]["queries_over_budget"] == 9
    with engine.connect() as conn:
        rows = conn.execute(
            select(SearchQuery.template, SearchQuery.query, SearchQuery.status)
            .where(SearchQuery.crawl_id == summary.crawl_id).order_by(SearchQuery.id)
        ).all()  # fmt: skip
    assert [r.status for r in rows] == ["ok"] * 2 + ["over_budget"] * 9
    assert rows[0].template == 'name:"{n}" competitors'


def test_known_values_seed_queries_and_a_second_round(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    gap_calls: list[int] = []
    answers = [
        {"product_name": ["Acme Flow"], "product_category": ["CRM"], "competitor": ["Globex"]},
        # After round one's pages are extracted.
        {"competitor": ["Globex", "Initech", "Hooli"]},
    ]

    def gaps(crawl_id: int, organization_id: int) -> set[str]:
        gap_calls.append(crawl_id)
        return set()

    summary, _ = run(engine, external(tmp_path, second_hop_competitors=2), search, gaps,
                     known=lambda crawl_id, org_id: answers.pop(0))  # fmt: skip
    queries = search.queries()
    assert '"Acme Flow" alternatives' in queries and "best CRM software" in queries
    assert queries[-4:] == ['"Globex" vs "Acme"', '"Initech" vs "Acme"',
                            '"Globex" alternatives', '"Initech" alternatives']  # fmt: skip
    assert summary.stats["external"]["competitors"]["second_hop"] == ["Globex", "Initech"]
    assert len(gap_calls) == 2  # before the external phase, and before round two


def test_category_list_gives_candidates_that_are_never_competitors(
    engine: Engine, tmp_path: Path
) -> None:
    best = html("Best CRM", "<h1>The 10 Best CRM Software</h1><h2>1. Globex</h2>"
                f'<h2>2. Acme</h2><h2>3. Hooli</h2><a href="{SITE}/">Acme</a>')  # fmt: skip
    routes = {**ROUTES, "https://blog.example/best-crm": ("text/html", best)}
    summary, site = run(engine, external(tmp_path), FakeSearch(), routes=routes,
                        known=lambda crawl_id, org_id: {"product_category": ["CRM"]})  # fmt: skip
    assert "https://blog.example/best-crm" in site.requested
    with engine.begin() as conn:
        resolve_profiles(conn, summary.crawl_id)
    extract_crawl(engine, external(tmp_path), summary.crawl_id)
    with engine.connect() as conn:
        rels = conn.execute(
            select(Organization.name, Relationship.type, Relationship.basis)
            .join(Organization, Organization.id == Relationship.object_org_id)
            .join(Observation, Observation.id == Relationship.observation_id)
            .where(Observation.crawl_id == summary.crawl_id)
        ).all()  # fmt: skip
    assert ("Hooli", "candidate_competitor", "category_list") in rels
    assert ("Globex", "candidate_competitor", "category_list") in rels
    # Candidates are not competitors: no competitor relationship, which is all
    # resolve_competitor_domains (the sites to crawl) reads...
    assert not any(name == "Hooli" and kind == "competitor" for name, kind, _ in rels)
    # ...and the dossier keeps them apart, a name found both ways being a competitor.
    dossier = build_dossier(engine, summary.organization_id)
    candidates = [c["name"] for c in dossier["candidate_competitors"]]
    assert "Hooli" in candidates and "Globex" not in candidates
    assert "Globex" in [c["name"] for c in dossier["competitors"]]
    # The clean (GTM Parameters V1.0) view has competitors only.
    assert "Hooli" not in [c["name"] for c in clean_dossier(dossier)["competitor_analysis"]]


def test_search_failure_does_not_stop_the_crawl(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch(status=429)
    summary, _ = run(engine, external(tmp_path), search)
    assert summary.status == "completed"
    # Three queries, each tried three times (two retries); then the provider's
    # circuit is open and the other 11 queries (gap and competitor) are not sent.
    assert len(search.requests) == 9
    with engine.connect() as conn:
        rows = conn.execute(
            select(SearchQuery.status, SearchQuery.error)
            .where(SearchQuery.crawl_id == summary.crawl_id).order_by(SearchQuery.id)
        ).all()  # fmt: skip
    assert [r.status for r in rows] == ["failed"] * 14
    assert all("HTTP 429" in (r.error or "") for r in rows[:3])
    assert all("circuit open" in (r.error or "") for r in rows[3:])


def test_without_a_key_only_linked_profiles_are_tried(engine: Engine, tmp_path: Path) -> None:
    summary, site = run(engine, external(tmp_path, key=""), None)
    assert summary.stats["external"]["search_error"] == "disabled"
    assert "https://www.linkedin.com/robots.txt" in site.requested
    assert not any("reviews.example" in u or "crunchbase" in u for u in site.requested)


def test_external_phase_can_be_disabled(engine: Engine, tmp_path: Path) -> None:
    search = FakeSearch()
    summary, site = run(engine, external(tmp_path, enabled=False), search)
    assert "external" not in summary.stats and search.requests == []
    assert not any("linkedin" in u for u in site.requested)


def test_same_statement_on_another_site_is_corroboration(engine: Engine, tmp_path: Path) -> None:
    founded = "<p>Acme was founded in 2019.</p>"
    routes = {**ROUTES,
              f"{SITE}/": ("text/html", html("Acme", founded + founded,
                                            '<meta property="og:site_name" content="Acme">')),
              "https://news.example/a": ("text/html", html(
                  "A", founded + f'<a href="{SITE}/">Acme</a>'))}  # fmt: skip
    summary, _ = run(engine, external(tmp_path), FakeSearch(), routes=routes)
    extract_crawl(engine, external(tmp_path), summary.crawl_id)
    with engine.connect() as conn:
        obs = conn.execute(
            select(Observation.is_first_party)
            .where(Observation.crawl_id == summary.crawl_id, Observation.field == "founded_year")
        ).scalars().all()  # fmt: skip
    # Repeated on the site: once. Repeated elsewhere: kept.
    assert sorted(obs) == [False, True]


def test_candidates_found_by_more_queries_rank_first() -> None:
    hits = [("https://a.com/", "q1", 0), ("https://b.com/", "q1", 1),
            ("https://c.com/", "q2", 0), ("https://b.com/", "q2", 3)]  # fmt: skip
    assert rank_candidates(hits) == [
        ("https://b.com/", "q1"),  # found by both queries
        ("https://a.com/", "q1"),  # then best position, then discovery order
        ("https://c.com/", "q2"),
    ]
