"""Phase 10: customers, case studies, integrations, hiring, offices, technology."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.evidence import check_draft
from gtm_crawler.db.models import Observation
from gtm_crawler.extract import extract_crawl
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.signals import extract_signals
from gtm_crawler.parse import parse_page

from .test_crawl import SITE, FakeSite, html, run, settings_for


def signals(body: str, url: str = "https://acme.com/", first_party: bool = True,
            head: str = "") -> list[tuple[str, str]]:  # fmt: skip
    page = parse_page(
        f"<html><head>{head}</head><body>{body}</body></html>".encode(), url, "text/html",
        ParseSettings(), UrlSettings(),
    )  # fmt: skip
    ctx = Context(domain="acme.com", names={"Acme"}, first_party=first_party)
    drafts = extract_signals(page, ctx)
    for d in drafts:
        check_draft(d, page)  # every value is in its evidence
    return [(d.field, d.value_verbatim) for d in drafts]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<p>Our customers include Globex, Initech and Umbrella Corp.</p>",
         [("customer", "Globex"), ("customer", "Initech"), ("customer", "Umbrella Corp")]),
        ("<p>Trusted by Globex and Initech.</p>",
         [("customer", "Globex"), ("customer", "Initech")]),
        # Names no one.
        ("<p>Trusted by Fortune 500 companies.</p>", []),
        ("<h2>Trusted by Thousands of Students Across Chennai</h2>", []),
        ("<p>Trusted by 500+ Teams.</p>", []),
        ("<p>Trusted by 3M and 1Password.</p>", [("customer", "3M"), ("customer", "1Password")]),
        # The company is not the subject.
        ("<p>Globex's customers include Initech.</p>", []),
        ("<p>We congratulate our partner Globex, whose customers include Initech.</p>", []),
    ],
)  # fmt: skip
def test_customers(body: str, expected: list[tuple[str, str]]) -> None:
    assert signals(body) == expected


def test_third_party_pages_need_the_company_name() -> None:
    body = ("<p>Trusted by Globex.</p><p>Our customers include Initech.</p>"
            "<p>Acme customers include Hooli.</p>")  # fmt: skip
    assert signals(body, "https://news.example/x", first_party=False) == [("customer", "Hooli")]


def test_case_study_page() -> None:
    body = "<h1>How Globex cut shipping costs with Acme</h1><p>Story.</p>"
    assert signals(body, "https://acme.com/customers/globex") == [
        ("case_study", "How Globex cut shipping costs with Acme")
    ]
    assert signals(body, "https://acme.com/blog/globex") == []


@pytest.mark.parametrize(
    ("url", "body", "expected"),
    [
        ("https://acme.com/", "<p>Acme integrates with Salesforce, HubSpot and Slack.</p>",
         [("integration", "Salesforce"), ("integration", "HubSpot"), ("integration", "Slack")]),
        ("https://acme.com/integrations/salesforce", "<h1>Acme + Salesforce</h1>",
         [("integration", "Salesforce")]),
        ("https://acme.com/integrations/salesforce", "<h1>Salesforce integration</h1>",
         [("integration", "Salesforce")]),
        # Ambiguous heading: nothing.
        ("https://acme.com/integrations/crm", "<h1>Connect your CRM and ERP</h1>", []),
    ],
)  # fmt: skip
def test_integrations(url: str, body: str, expected: list[tuple[str, str]]) -> None:
    assert signals(body, url) == expected


def test_hiring_signals() -> None:
    posting = {"@context": "https://schema.org", "@type": "JobPosting",
               "title": "Senior Backend Engineer", "datePosted": "2026-09-01",
               "hiringOrganization": {"@type": "Organization", "name": "Acme",
                                      "sameAs": "https://acme.com"}}  # fmt: skip
    other = {
        **posting,
        "title": "Driver",
        "hiringOrganization": {"name": "Globex", "sameAs": "https://globex.com"},
    }
    head = "".join(f'<script type="application/ld+json">{json.dumps(p)}</script>'
                   for p in (posting, other))  # fmt: skip
    found = signals("<p>We have more than 40 open positions.</p>", head=head)
    assert ("open_roles", "more than 40 open positions") in found
    assert ("job_opening", "Senior Backend Engineer") in found
    assert ("job_opening", "Driver") not in found


def test_open_roles_keep_their_qualifier() -> None:
    page = parse_page(b"<p>We have 25+ open roles.</p>", "https://acme.com/careers",
                      "text/html", ParseSettings(), UrlSettings())  # fmt: skip
    [d] = extract_signals(page, Context(domain="acme.com", names={"Acme"}))
    assert d.value_normalized == {"qualifier": "gte", "value": 25}


def test_offices_and_generator() -> None:
    found = signals(
        "<p>We have offices in London, New York and Berlin.</p>",
        head='<meta name="generator" content="WordPress 6.4.2">',
    )
    assert found == [
        ("office_location", "London"), ("office_location", "New York"),
        ("office_location", "Berlin"), ("website_technology", "WordPress 6.4.2"),
    ]  # fmt: skip
    # A training institute's or clinic's branches are its offices too.
    assert signals("<p>We have branches in T Nagar, Velachery and Porur.</p>") == [
        ("office_location", "T Nagar"), ("office_location", "Velachery"),
        ("office_location", "Porur"),
    ]  # fmt: skip
    # Another site's generator says nothing about the company.
    assert signals("", "https://news.example/", first_party=False,
                   head='<meta name="generator" content="Ghost 5">') == []  # fmt: skip


@pytest.mark.db
def test_signals_are_stored_as_observations_with_evidence(engine: Engine, tmp_path: Path) -> None:
    routes: dict[str, Any] = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{SITE}/": ("text/html", html(
            "Acme", '<p>Our customers include Globex and Initech.</p>'
            '<a href="/customers/globex">Story</a>',
            '<meta property="og:site_name" content="Acme">')),
        f"{SITE}/customers/globex": ("text/html", html(
            "Globex", "<h1>How Globex ships faster with Acme</h1>")),
    }  # fmt: skip
    summary = run(engine, settings_for(tmp_path), FakeSite(routes))
    stats = extract_crawl(engine, settings_for(tmp_path), summary.crawl_id)
    assert stats["observations"]["customer"] == 2 and stats["observations"]["case_study"] == 1
    with engine.connect() as conn:
        rows = conn.execute(
            select(Observation.field, Observation.value_verbatim, Observation.extractor,
                   Observation.evidence_id)
            .where(Observation.extraction_id == stats["extraction_id"],
                   Observation.field.in_(("customer", "case_study")))
        ).all()  # fmt: skip
    assert {(r.field, r.value_verbatim) for r in rows} == {
        ("customer", "Globex"), ("customer", "Initech"),
        ("case_study", "How Globex ships faster with Acme"),
    }  # fmt: skip
    assert all(r.extractor == "signals" and r.evidence_id for r in rows)
