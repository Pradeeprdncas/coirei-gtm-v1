"""Traction extraction: stated figures only, attributed only to the company itself."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.evidence import EvidenceStore, check_draft
from gtm_crawler.db.models import Observation, Organization, Relationship
from gtm_crawler.extract import extract_crawl
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.traction import extract_traction
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

from .test_crawl import SITE, FakeSite, html, run, settings_for
from .test_extract_company import ld

DOMAIN = "acme.example"


def page(body: str, head: str = "", path: str = "/news/x") -> ParsedPage:
    doc = f"<html><head><title>T</title>{head}</head><body>{body}</body></html>".encode()
    return parse_page(doc, f"{SITE}{path}", "text/html", ParseSettings(), UrlSettings())


def found(text: str) -> list[tuple[str, str, Any, Any]]:
    p = page(f"<p>{text}</p>")
    drafts = extract_traction(p, Context(DOMAIN, {"Acme"}))
    for d in drafts:
        check_draft(d, p)
    return [(d.field, d.value_verbatim, d.value_normalized,
             (d.related.type, d.related.name) if d.related else None) for d in drafts]  # fmt: skip


def usd(amount: float, qualifier: str = "eq", **extra: Any) -> dict[str, Any]:
    return {"qualifier": qualifier, "amount": amount, "currency": "USD", **extra}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Acme passed $10M in ARR in 2025.",
         [("arr", "$10M", usd(10e6, year=2025), None)]),
        ("Our annual recurring revenue exceeded $2.5 million.",
         [("arr", "$2.5 million", usd(2.5e6), None)]),
        ("We reached over $400K MRR.", [("mrr", "over $400K", usd(4e5, "gt"), None)]),
        ("Acme's annual revenue was €30 million in 2024.",
         [("annual_revenue", "€30 million",
           {"qualifier": "eq", "amount": 30e6, "currency": "EUR", "year": 2024}, None)]),
        ("We serve more than 500 customers.",
         [("customer_count", "more than 500 customers",
           {"qualifier": "gt", "value": 500}, None)]),
        ("Over 2,000+ companies use Acme.",
         [("customer_count", "Over 2,000+ companies", {"qualifier": "gte", "value": 2000}, None)]),
        ("Acme raised a $20M Series A led by Sequoia Capital with participation from Accel "
         "and Index Ventures.",
         [("funding_round", "$20M Series A", usd(20e6, round="series_a"), None),
          ("investor", "Sequoia Capital", None, ("investor", "Sequoia Capital")),
          ("investor", "Accel", None, ("investor", "Accel")),
          ("investor", "Index Ventures", None, ("investor", "Index Ventures"))]),
        ("We have raised $50M to date.",
         [("total_funding", "$50M to date", usd(50e6), None)]),
        ("Acme's total funding of $75 million makes it well capitalised.",
         [("total_funding", "$75 million", usd(75e6), None)]),
        ("Acme closed its seed round of $3M.",
         [("funding_round", "seed round of $3M", usd(3e6, round="seed"), None)]),
        ("Acme is now valued at $1.2 billion.",
         [("valuation", "$1.2 billion", usd(1.2e9, basis=None), None)]),
        ("Our largest customer accounted for 18% of total revenue in 2025.",
         [("customer_concentration",
           "Our largest customer accounted for 18% of total revenue", None, None)]),
        ("We are an AWS Advanced Technology Partner.",
         [("technology_partnership", "AWS", None, ("tech_partner", "AWS"))]),
        ("Acme announced a strategic partnership with Globex Corporation.",
         [("strategic_partnership", "Globex Corporation", None,
           ("strategic_partner", "Globex Corporation"))]),
        ("We partnered with Initech to reach new markets.",
         [("partnership", "Initech", {"qualifier": "eq", "type": "unspecified"}, None)]),
        ("We have sold more than 10,000 units since launch.",
         [("product_sales", "more than 10,000 units", {"qualifier": "gt", "value": 10000},
           None)]),
    ],
)  # fmt: skip
def test_stated_traction(text: str, expected: list[Any]) -> None:
    assert found(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # About someone else.
        "Globex raised $50M in its Series C.",
        "We congratulate our customer Globex on raising $50M.",
        "Stripe is valued at $50 billion.",
        "Globex has more than 1,000 customers.",
        # Funding is not valuation, and a round is not a total.
        "We raised $20M in our Series A.",
    ],
)
def test_not_attributed_or_not_inferred(text: str) -> None:
    got = found(text)
    assert not any(f in ("valuation", "total_funding") for f, _, _, _ in got)
    if "Globex" in text or "Stripe" in text:
        assert got == []


def test_round_is_never_total() -> None:
    assert [f for f, _, _, _ in found("We raised $20M in our Series A.")] == ["funding_round"]


def test_partner_page_sections() -> None:
    p = page("<h1>Partners</h1><h2>Technology partners</h2><ul><li>Salesforce</li>"
             "<li>HubSpot</li></ul><h2>Strategic partners</h2><ul><li>Globex</li></ul>"
             "<h2>Become a partner</h2><p>Apply now.</p>", path="/partners")  # fmt: skip
    drafts = extract_traction(p, Context(DOMAIN, {"Acme"}))
    assert [
        (d.field, d.value_verbatim, d.related.type if d.related else None) for d in drafts
    ] == [  # fmt: skip
        ("technology_partnership", "Salesforce", "tech_partner"),
        ("technology_partnership", "HubSpot", "tech_partner"),
        ("strategic_partnership", "Globex", "strategic_partner"),
    ]


def test_json_ld_funders() -> None:
    org = {"@type": "Organization", "name": "Acme",
           "funder": [{"@type": "Organization", "name": "Sequoia Capital"}]}  # fmt: skip
    p = page("", ld(org))
    drafts = extract_traction(p, Context(DOMAIN, {"Acme"}))
    assert [(d.field, d.value_verbatim) for d in drafts] == [("investor", "Sequoia Capital")]


# --- end to end ------------------------------------------------------------------

SITE_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", '<a href="/news/series-a">News</a><p>We are Acme.</p>',
        '<meta property="og:site_name" content="Acme">',
    )),
    f"{SITE}/news/series-a": ("text/html", html(
        "Series A", "<h1>Acme raises $20M</h1><p>Acme raised a $20M Series A led by Sequoia "
        "Capital.</p><p>We are backed by Sequoia Capital and Accel.</p>",
    )),
}  # fmt: skip


@pytest.mark.db
def test_extract_crawl_records_relationships(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    summary = run(engine, settings, FakeSite(SITE_ROUTES))
    stats = extract_crawl(engine, settings, summary.crawl_id)
    assert stats["rejected"] == 0

    with engine.connect() as conn:
        rels = conn.execute(
            select(Organization.name, Relationship.type, Relationship.observation_id)
            .join(Organization, Organization.id == Relationship.object_org_id)
            .join(Observation, Observation.id == Relationship.observation_id)
            .where(Observation.crawl_id == summary.crawl_id)
        ).all()
        obs = conn.execute(
            select(Observation.id, Observation.field, Observation.evidence_id).where(
                Observation.crawl_id == summary.crawl_id
            )
        ).all()
    # One organization per investor name; "Sequoia Capital" is stated twice (two
    # sentences) but is one statement for the crawl, so one relationship.
    assert sorted((r.name, r.type) for r in rels) == [
        ("Accel", "investor"),
        ("Sequoia Capital", "investor"),
    ]
    investor_obs = {o.id for o in obs if o.field == "investor"}
    assert {r.observation_id for r in rels} <= investor_obs
    store = EvidenceStore(engine, RawStore(settings.rawstore.root), settings.sources)
    assert all(store.verify(o.evidence_id) is None for o in obs)
