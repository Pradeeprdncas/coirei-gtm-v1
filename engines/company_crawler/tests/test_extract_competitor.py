"""Competitor discovery: explicit evidence only, never co-mentions."""

from typing import Any

import pytest

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.evidence import check_draft
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.company import extract_company
from gtm_crawler.extract.competitor import extract_competitors
from gtm_crawler.extract.product import extract_products, identify_products
from gtm_crawler.extract.traction import extract_traction
from gtm_crawler.parse import ParsedPage, parse_page

from .test_extract_company import ld

SITE = "https://acme.example"


def page(path: str, body: str, head: str = "", site: str = SITE) -> ParsedPage:
    doc = f"<html><head><title>T</title>{head}</head><body>{body}</body></html>".encode()
    return parse_page(doc, f"{site}{path}", "text/html", ParseSettings(), UrlSettings())


def found(p: ParsedPage, first_party: bool = True) -> list[tuple[str, str]]:
    drafts = extract_competitors(p, Context("acme.example", {"Acme"}, first_party=first_party))
    for d in drafts:
        check_draft(d, p)
    return [(d.value_verbatim, d.related.basis) for d in drafts if d.related]


@pytest.mark.parametrize(
    ("path", "h1", "expected"),
    [
        ("/vs/globex", "Acme vs Globex", [("Globex", "comparison_page")]),
        ("/compare/globex", "Compare Acme and Globex", [("Globex", "comparison_page")]),
        ("/acme-vs-initech", "Acme vs. Initech: which is right for you?",
         [("Initech", "comparison_page")]),
        ("/globex-alternative", "The best Globex alternative", [("Globex", "alternative_page")]),
        ("/alternatives/initech", "Looking for Initech alternatives?",
         [("Initech", "alternative_page")]),
        # The heading names someone the URL does not: not evidence.
        ("/vs/globex", "Acme vs Initech", []),
        # Not a comparison URL.
        ("/blog/globex", "Acme vs Globex", []),
        # Two things compared, neither of them the company (a blog post).
        ("/blogs/data-analyst-vs-data-engineer",
         "Data Analyst vs Data Engineer: Which Career Is Better?", []),
        ("/blogs/certificate-vs-practical-skills-it-jobs",
         "Certificate vs Practical Skills: What Actually Gets You Hired in IT?", []),
        ("/blog/globex-vs-initech", "Globex vs Initech", []),
    ],
)  # fmt: skip
def test_comparison_and_alternative_pages(path: str, h1: str, expected: list[Any]) -> None:
    assert found(page(path, f"<h1>{h1}</h1>")) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We compete with Globex and Initech in the mid-market.",
         [("Globex", "stated_competition"), ("Initech", "stated_competition")]),
        # Two elements the page ran together: the name ends at the full stop.
        ("Acme is a modern alternative to Globex.Compare Acme vs Globex",
         [("Globex", "stated_alternative")]),
        ("Our competitors include Globex, Initech, and Hooli.",
         [("Globex", "stated_competition"), ("Initech", "stated_competition"),
          ("Hooli", "stated_competition")]),
        ("Acme is the leading alternative to Globex.", [("Globex", "stated_alternative")]),
        ("Unlike Globex, we price per warehouse.", [("Globex", "stated_contrast")]),
        # Co-mentions, customers, partners and integrations are not competitors.
        ("We integrate with Globex and Initech.", []),
        ("Our customers include Globex.", []),
        ("We partnered with Globex.", []),
        ("Globex competes with Initech.", []),
        ("We love working with Globex, a competitor of Initech.", []),
    ],
)  # fmt: skip
def test_own_site_statements(text: str, expected: list[Any]) -> None:
    assert found(page("/about", f"<p>{text}</p>")) == expected


def test_third_party_alternatives_list() -> None:
    p = page("/acme/alternatives",
             "<h1>Top 5 Acme Alternatives &amp; Competitors</h1>"
             "<h2>1. Globex</h2><p>Globex is great.</p><h2>2. Initech: best for teams</h2>"
             "<h2>Frequently asked questions</h2><h2>Acme review</h2>",
             site="https://reviews.example")  # fmt: skip
    assert found(p, first_party=False) == [
        ("Globex", "third_party_alternatives_list"),
        ("Initech", "third_party_alternatives_list"),
    ]
    # The site's menu and footer are not the article's list (live crawl of clay.com's).
    menus = page("/acme/alternatives",
                 "<nav><ul><li>Affiliate</li><li>Advertise</li></ul></nav>"
                 "<h1>Acme alternatives</h1><h2>1. Globex</h2>"
                 "<footer><ul><li>Agency Directory</li></ul></footer>",
                 site="https://reviews.example")  # fmt: skip
    assert found(menus, first_party=False) == [("Globex", "third_party_alternatives_list")]
    # A list of alternatives to someone else is not about the company.
    other = page("/x", "<h1>Globex Alternatives</h1><h2>1. Initech</h2>",
                 site="https://reviews.example")  # fmt: skip
    assert found(other, first_party=False) == []


def by_field(p: ParsedPage) -> list[tuple[str, str, str]]:
    drafts = extract_competitors(p, Context("acme.example", {"Acme"}, first_party=False))
    for d in drafts:
        check_draft(d, p)
    return [(d.field, d.value_verbatim, d.related.basis) for d in drafts if d.related]


@pytest.mark.parametrize(
    ("h1", "expected"),
    [
        ("Acme vs Globex: which CRM wins?",
         [("competitor", "Globex", "third_party_comparison")]),
        ("Compare Globex and Acme", [("competitor", "Globex", "third_party_comparison")]),
        # Neither side is the company.
        ("Globex vs Initech", []),
    ],
)  # fmt: skip
def test_third_party_comparison(h1: str, expected: list[Any]) -> None:
    p = page("/reviews/x", f"<h1>{h1}</h1>", site="https://reviews.example")
    assert by_field(p) == expected


def test_category_list_gives_candidates_only_when_the_company_is_listed() -> None:
    items = "<h2>1. Globex</h2><p>Good.</p><h2>2. Acme</h2><h2>3. Initech</h2><h2>FAQ</h2>"
    p = page("/best-crm", f"<h1>The 10 Best CRM Software for 2026</h1>{items}",
             site="https://blog.example")  # fmt: skip
    assert by_field(p) == [
        ("candidate_competitor", "Globex", "category_list"),
        ("candidate_competitor", "Initech", "category_list"),
    ]
    # The company is not one of the items: nothing ties the list to it.
    p = page("/best-crm", "<h1>Top CRM tools</h1><h2>1. Globex</h2><p>Acme is also fine.</p>",
             site="https://blog.example")  # fmt: skip
    assert by_field(p) == []
    # A heading that is no list of products.
    p = page("/x", f"<h1>Best practices for sales teams</h1>{items}", site="https://blog.example")
    assert by_field(p) == []


def test_company_listed_as_someone_elses_alternative() -> None:
    p = page("/globex-alternatives",
             "<h1>Best Globex Alternatives</h1><ul><li>Acme</li><li>Initech</li></ul>",
             site="https://reviews.example")  # fmt: skip
    assert by_field(p) == [
        ("competitor", "Globex", "listed_as_alternative"),
        ("candidate_competitor", "Initech", "category_list"),
    ]


def test_third_party_statements_need_the_company_name() -> None:
    body = ("<p>We compete with Initech.</p><p>Acme competes with Globex.</p>"
            "<p>Alternatives to Acme include Hooli and Initech.</p>")  # fmt: skip
    got = found(page("/article", body, site="https://news.example"), first_party=False)
    # "We" is the publisher here, so only the named statements count.
    assert got == [("Globex", "stated_competition"), ("Hooli", "stated_alternative"),
                   ("Initech", "stated_alternative")]  # fmt: skip


def test_third_party_pages_do_not_leak_publisher_facts() -> None:
    ctx = Context("acme.example", {"Acme"}, first_party=False)
    head = '<meta property="og:site_name" content="Review Site">' + ld(
        {"@type": "Organization", "name": "Review Site", "foundingDate": "2001"}
    )
    p = page("/products/acme", "<h1>Acme</h1><p>We raised $5M. We have 40 employees.</p>",
             head, site="https://reviews.example")  # fmt: skip
    assert extract_company(p, ctx) == []
    assert extract_traction(p, ctx) == []
    assert identify_products(p, ctx) == [] and extract_products(p, ctx) == []
    # JSON-LD that names the company's own URL is third-party evidence about it.
    about = ld({"@type": "Organization", "name": "Acme", "url": "https://acme.example/",
                "foundingDate": "2019"})  # fmt: skip
    p = page("/org/acme", "", about, site="https://directory.example")
    assert {(d.field, d.value_verbatim) for d in extract_company(p, ctx)} >= {
        ("company_name", "Acme"), ("founded_year", "2019"),
    }  # fmt: skip
