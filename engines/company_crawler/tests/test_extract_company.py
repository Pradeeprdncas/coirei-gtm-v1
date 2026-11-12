"""Company information extraction: what is found, and what must not be attributed."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.evidence import EvidenceStore, check_draft
from gtm_crawler.db.models import Evidence, Observation
from gtm_crawler.extract import extract_crawl
from gtm_crawler.extract.common import Context, sentences
from gtm_crawler.extract.company import company_names, extract_company
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

from .test_crawl import SITE, FakeSite, html, run, settings_for

DOMAIN = "acme.example"


def page(body: str, head: str = "", path: str = "/about") -> ParsedPage:
    doc = f"<html><head><title>T</title>{head}</head><body>{body}</body></html>".encode()
    return parse_page(doc, f"{SITE}{path}", "text/html", ParseSettings(), UrlSettings())


def ld(data: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def found(p: ParsedPage, names: set[str] | None = None) -> set[tuple[str, str, str | None]]:
    ctx = Context(DOMAIN, {"Acme Robotics"} if names is None else names)
    drafts = extract_company(p, ctx)
    for d in drafts:
        check_draft(d, p)  # every draft is supported by its own quote
    return {
        (d.field, d.value_verbatim, json.dumps(d.value_normalized) if d.value_normalized else None)
        for d in drafts
    }


def fields(p: ParsedPage, names: set[str] | None = None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for field, value, _ in found(p, names):
        out.setdefault(field, set()).add(value)
    return out


ORG = {
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "Acme Robotics",
    "legalName": "Acme Robotics, Inc.",
    "url": "https://acme.example/",
    "foundingDate": "2019-03-01",
    "description": "Warehouse robots for mid-size retailers.",
    "numberOfEmployees": {"@type": "QuantitativeValue", "minValue": 51, "maxValue": 200},
    "address": {"@type": "PostalAddress", "addressLocality": "Austin",
                "addressCountry": {"@type": "Country", "name": "US"}},
    "tickerSymbol": "ACME",
    "naics": "333922",
}  # fmt: skip


def test_json_ld_organization() -> None:
    got = found(page("", ld(ORG)))
    assert ("company_name", "Acme Robotics", None) in got
    assert ("legal_name", "Acme Robotics, Inc.", None) in got
    assert ("website", "https://acme.example/", None) in got
    assert ("founded_year", "2019-03-01", '{"qualifier": "eq", "value": 2019}') in got
    assert ("company_size", "51", '{"qualifier": "gte", "value": 51}') in got
    assert ("company_size", "200", '{"qualifier": "lte", "value": 200}') in got
    assert ("company_location", "Austin", None) in got
    assert ("company_country", "US", None) in got
    assert ("ownership_type", "ACME", '{"qualifier": "eq", "value": "public"}') in got
    assert ("company_description", "Warehouse robots for mid-size retailers.", None) in got
    assert any(f == "market_domain" and v == "333922" for f, v, _ in got)
    assert not any(f == "headquarters" for f, _, _ in got)  # an address is not an HQ


def test_json_ld_in_graph_and_exact_employee_count() -> None:
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "name": "Acme site"},
        {"@type": "Corporation", "name": "Acme Robotics", "numberOfEmployees": 120},
    ]}  # fmt: skip
    got = found(page("", ld(graph)))
    assert ("company_name", "Acme Robotics", None) in got
    assert ("employee_count", "120", '{"qualifier": "eq", "value": 120}') in got
    assert ("company_name", "Acme site", None) not in got


def test_json_ld_about_other_organizations_is_ignored() -> None:
    article = {"@type": "NewsArticle", "headline": "x",
               "publisher": {"@type": "Organization", "name": "Daily News"}}  # fmt: skip
    other = {"@type": "Organization", "name": "Globex", "url": "https://globex.com/",
             "foundingDate": "1990"}  # fmt: skip
    assert fields(page("", ld(article) + ld(other))) == {}
    assert company_names(page("", ld(article) + ld(other)), DOMAIN) == set()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Acme Robotics was founded in 2019 and is headquartered in Austin, Texas.",
         {"founded_year": {"2019"}, "headquarters": {"Austin, Texas"}}),
        ("Our headquarters is located in San Francisco, California and we have offices in Paris.",
         {"headquarters": {"San Francisco, California"}}),
        ("We are based in Berlin.", {"company_location": {"Berlin"}}),
        ("Today we have more than 250 employees worldwide.",
         {"employee_count": {"more than 250 employees"}}),
        ("Our 1,200+ employees love robots.", {"employee_count": {"1,200+ employees"}}),
        ("We are a team of 11-50 employees.", {"company_size": {"11-50 employees"}}),
        ("We are a privately held company.", {"ownership_type": {"privately held"}}),
        ("Acme Robotics (NASDAQ: ACME) builds robots.", {"ownership_type": {"NASDAQ: ACME"}}),
        ("Acme Robotics is a wholly owned subsidiary of Globex Corporation.",
         {"ownership_type": {"wholly owned subsidiary of Globex Corporation"}}),
        ("We are a nonprofit organization.", {"ownership_type": {"nonprofit"}}),
        ("Acme Robotics is a registered 501(c)(3) charity.", {"ownership_type": {"501(c)(3)"}}),
        ("We are a B2B SaaS platform for retailers.", {"business_model": {"B2B", "SaaS"}}),
        ("Acme Robotics is an open-source marketplace.",
         {"business_model": {"open-source", "marketplace"}}),
        ("We build software for the logistics industry.", {"market_domain": {"logistics"}}),
    ],
)  # fmt: skip
def test_self_referencing_statements(text: str, expected: dict[str, set[str]]) -> None:
    assert fields(page(f"<p>{text}</p>")) == expected


@pytest.mark.parametrize(
    "text",
    [
        # About another company (no we/our/name).
        "Stripe was founded in 2010 and is headquartered in San Francisco.",
        "Globex has more than 90,000 employees.",
        "Globex is a publicly traded company.",
        # A blog post's aside: "our" comes after the claim (live crawl of agorapulse.com).
        "Facebook is an evil publicly listed company that only wants our money.",
        # About customers, not the company's own model.
        "We help SaaS companies grow.",
        # Nonprofits as customers or a discount (live crawl of buffer.com's competitors).
        "We are always happy to support nonprofit organizations with a 50% discount.",
        "Does Acme Robotics offer special pricing for nonprofit organizations?",
        "I am a social media manager at a nonprofit organization and we post daily.",
        "We are a platform for B2B marketplaces.",
        # Not an employee count.
        "We serve more than 500 customers and 20,000 people use our app.",
        # Headquarters without a proper place name.
        "We are headquartered in the heart of the city.",
    ],
)
def test_statements_not_attributed(text: str) -> None:
    assert fields(page(f"<p>{text}</p>")) == {}


def test_employee_qualifiers_are_kept() -> None:
    got = found(page("<p>We employ about 80 employees. We have 12 employees.</p>"))
    normalized = {v: n for f, v, n in got if f == "employee_count"}
    assert normalized == {
        "about 80 employees": '{"qualifier": "approx", "value": 80}',
        "12 employees": '{"qualifier": "eq", "value": 12}',
    }


def test_meta_site_name_and_home_description() -> None:
    head = ('<meta property="og:site_name" content="Acme">'
            '<meta name="description" content="Robots for warehouses.">')  # fmt: skip
    assert fields(page("", head, path="/")) == {
        "company_name": {"Acme"}, "company_description": {"Robots for warehouses."},
        "website": {"https://acme.example/"},  # the home page's own address
    }  # fmt: skip
    # A deep page's description describes the page, and its site name may name
    # the section ("Changelog"), not the company.
    assert fields(page("", head, path="/blog/post")) == {}


def test_sentences_respect_abbreviations() -> None:
    p = page("<p>Acme Inc. was founded in 2019. We are B2B. U.S. offices opened!</p>")
    assert [s.text for s in sentences(p)] == [
        "Acme Inc. was founded in 2019.", "We are B2B.", "U.S. offices opened!",
    ]  # fmt: skip


# --- end to end ------------------------------------------------------------------

SITE_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", '<a href="/about">About</a><a href="/blog/globex">Blog</a>'
        "<p>We are a B2B SaaS platform for warehouses.</p>",
        '<meta property="og:site_name" content="Acme Robotics">' + ld(ORG),
    )),
    f"{SITE}/about": ("text/html", html(
        "About", "<p>Acme Robotics was founded in 2019 and is headquartered in Austin, Texas.</p>",
        '<meta property="og:site_name" content="Acme Robotics">',
    )),
    f"{SITE}/blog/globex": ("text/html", html(
        "Blog", "<p>Globex was founded in 1990 and is headquartered in Springfield.</p>",
    )),
}  # fmt: skip


@pytest.mark.db
def test_extract_crawl_records_verifiable_observations(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    summary = run(engine, settings, FakeSite(SITE_ROUTES))
    stats = extract_crawl(engine, settings, summary.crawl_id)
    assert stats["rejected"] == 0 and stats["pages"] == 3
    assert stats["company_names"] == ["Acme Robotics"]

    with engine.connect() as conn:
        rows = conn.execute(
            select(Observation.field, Observation.value_verbatim, Observation.evidence_id,
                   Observation.source_type)
            .where(Observation.crawl_id == summary.crawl_id)
        ).all()  # fmt: skip
        evidence_ids = conn.execute(select(Evidence.id)).scalars().all()
    values = [(r.field, r.value_verbatim) for r in rows]
    assert ("headquarters", "Austin, Texas") in values
    assert ("founded_year", "2019") in values and ("founded_year", "1990") not in values
    assert ("headquarters", "Springfield") not in values
    assert values.count(("company_name", "Acme Robotics")) == 1  # repeated, recorded once
    assert {r.source_type for r in rows} == {"official_website"}

    store = EvidenceStore(engine, RawStore(settings.rawstore.root), settings.sources)
    assert all(store.verify(e) is None for e in evidence_ids if e in {r.evidence_id for r in rows})


def home(title: str, body: str = "") -> ParsedPage:
    doc = f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()
    return parse_page(doc, f"{SITE}/", "text/html", ParseSettings(), UrlSettings())


def test_a_site_without_metadata_names_itself_in_its_title_and_footer() -> None:
    # coirei.com: no og:site_name, description or JSON-LD; a title and a footer.
    footer = (
        "<footer><div><p>Acme Robotics Pvt Ltd,</p></div>"
        "<div><p>7th Floor, Spencer Plaza, Mount Road, Chennai, Tamil Nadu, India</p></div>"
        "<p>© 2026 Acme Robotics Pvt Ltd. All rights reserved.</p><p>info@acme.example</p></footer>"
    )
    p = home("acme — Robots for warehouses", footer)
    names = company_names(p, DOMAIN)
    assert names == {"acme"}  # what the crawl searches for and recognises
    assert fields(p, names) == {
        "company_name": {"acme"},
        "website": {"https://acme.example/"},
        "legal_name": {"Acme Robotics Pvt Ltd"},
        "company_location": {"7th Floor, Spencer Plaza, Mount Road, Chennai, Tamil Nadu, India"},
        "contact_email": {"info@acme.example"},
    }


@pytest.mark.parametrize(
    ("title", "name"),
    [("Home | Acme", "Acme"), ("Acme Robotics", None), ("Welcome", None), ("Acme", "Acme")],
)
def test_the_title_names_the_company_only_where_it_spells_the_domain(
    title: str, name: str | None
) -> None:
    got = fields(home(title), set()).get("company_name")
    assert got == ({name} if name else None)


def test_a_legal_name_is_the_company_s_only_under_its_name() -> None:
    text = (
        "<p>Built with Globex Pvt Ltd and Acme Robotics, Inc.</p>"
        "<p>Globex Ltd,</p><p>1 Main St, Pune, India</p>"  # another company's address
    )
    assert fields(page(text), {"Acme Robotics"}) == {"legal_name": {"Acme Robotics, Inc"}}


def test_a_linkedin_company_page_gives_its_size_band_and_headcount() -> None:
    body = ("<p># Acme</p><p>- Company Size: 51-200 employees</p><p>- Employees: 77</p>"
            "<p>- Talent Sources: Globex (2)</p>")  # fmt: skip
    doc = f"<html><head><title>Acme</title></head><body>{body}</body></html>".encode()
    profile = parse_page(doc, "https://www.linkedin.com/company/acme", "text/html",
                         ParseSettings(), UrlSettings())  # fmt: skip
    ctx = Context(DOMAIN, {"Acme Robotics"}, first_party=False)
    drafts = extract_company(profile, ctx)
    for d in drafts:
        check_draft(d, profile)
    assert {(d.field, d.value_verbatim, json.dumps(d.value_normalized)) for d in drafts} == {
        ("company_size", "51-200 employees", '{"qualifier": "range", "min": 51, "max": 200}'),
        ("employee_count", "77", '{"qualifier": "eq", "value": 77}'),
    }
    # An open band; and the same lines on any other site name nobody.
    big = parse_page(doc.replace(b"51-200", b"10,001+"), "https://www.linkedin.com/company/acme",
                     "text/html", ParseSettings(), UrlSettings())  # fmt: skip
    assert ("company_size", '{"qualifier": "gte", "value": 10001}') in {
        (d.field, json.dumps(d.value_normalized)) for d in extract_company(big, ctx)
    }
    elsewhere = parse_page(doc, "https://directory.example/acme", "text/html",
                           ParseSettings(), UrlSettings())  # fmt: skip
    assert extract_company(elsewhere, ctx) == []


def test_every_branch_address_in_the_companys_json_ld_is_an_office() -> None:
    branch = {"@context": "https://schema.org", "@type": "LocalBusiness",
              "name": "Acme Porur", "url": "https://porur.acme.example/",
              "address": {"@type": "PostalAddress", "streetAddress": "4th Floor, Mount Rd",
                          "addressLocality": "Chennai", "postalCode": "600116",
                          "addressCountry": "IN"}}  # fmt: skip
    other = {"@type": "Organization", "name": "Globex", "url": "https://globex.example/",
             "address": {"streetAddress": "1 Globex Way"}}  # fmt: skip
    p = page(ld(branch) + ld(other), path="/")
    offices = [
        d for d in extract_company(p, Context(DOMAIN, {"Acme"})) if d.field == "office_location"
    ]
    for d in offices:
        check_draft(d, p)
    assert [(d.value_verbatim, d.value_normalized) for d in offices] == [
        ("4th Floor, Mount Rd", {"qualifier": "eq", "street": "4th Floor, Mount Rd",
                                 "locality": "Chennai", "postal_code": "600116",
                                 "country": "IN", "name": "Acme Porur"}),
    ]  # fmt: skip
    # The sub-site's node is a branch, not the company: no company facts from it.
    assert not any(d.field == "company_name" for d in extract_company(p, Context(DOMAIN)))
