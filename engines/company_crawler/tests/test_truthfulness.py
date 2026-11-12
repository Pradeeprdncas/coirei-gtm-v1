"""End-to-end truthfulness: the dossier states only what fetched pages support.

Each test runs the whole pipeline (crawl, external phase, extraction,
competitors, assessment, dossier) against a simulated site and checks what a
user would read. Plan section 34: the system must demonstrably refuse
unsupported claims.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ExternalSettings, SearchSettings, Settings
from gtm_crawler.db.evidence import EvidenceStore
from gtm_crawler.db.models import Evidence, Fetch, Page
from gtm_crawler.pipeline import run_pipeline
from gtm_crawler.rawstore import RawStore

from .helpers import response
from .test_crawl import html, settings_for
from .test_extract_company import ld

pytestmark = pytest.mark.db


class Site:
    """Serves fixed routes by full URL, with redirects; records every request."""

    def __init__(self, routes: dict[str, Any], redirects: dict[str, str] | None = None) -> None:
        self.routes = routes
        self.redirects = redirects or {}
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.redirects:
            return response(301, **{"Location": self.redirects[url]})
        route = self.routes.get(url)
        if route is None:
            return response(404)
        content_type, body = route
        return response(200, body, **{"Content-Type": content_type})


def pipeline(engine: Engine, settings: Settings, seed: str, site: Site,
             search: Any = None) -> dict[str, Any]:  # fmt: skip
    return asyncio.run(run_pipeline(
        seed, settings, engine, transport=httpx.MockTransport(site),
        search_transport=httpx.MockTransport(search) if search else None,
    ))  # fmt: skip


def field(dossier: dict[str, Any], name: str) -> Any:
    return dossier["company"]["fields"][name]


def values(entries: list[dict[str, Any]]) -> list[Any]:
    return [e.get("value") for e in entries if e["state"] != "UNKNOWN"]


def verify_all_citations(engine: Engine, settings: Settings, crawl_ids: list[int]) -> int:
    """Every stored quote is still exactly where it claims to be in the raw document."""
    store = EvidenceStore(engine, RawStore(settings.rawstore.root), settings.sources)
    with engine.connect() as conn:
        ids = conn.execute(
            select(Evidence.id).join(Page, Page.id == Evidence.page_id)
            .join(Fetch, Fetch.id == Page.fetch_id).where(Fetch.crawl_id.in_(crawl_ids))
        ).scalars().all()  # fmt: skip
    failures = {i: r for i in ids if (r := store.verify(i)) is not None}
    assert failures == {}
    return len(ids)


# --- a static company site with most structures at once ----------------------------

ACME = "https://acme-robotics.com"
ORG = {"@context": "https://schema.org", "@type": "Organization", "name": "Acme Robotics",
       "url": f"{ACME}/", "foundingDate": "2019"}  # fmt: skip
ACME_ROUTES: dict[str, Any] = {
    f"{ACME}/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /private/\n"),
    f"{ACME}/": ("text/html", html(
        "Acme Robotics",
        "<p>We raised $10M in our Series A. We serve more than 500 customers. "
        "Our team has 45 employees.</p>"
        '<a href="/products/stock">Stock</a><a href="/products/fleet">Fleet</a>'
        '<a href="/pricing">Pricing</a><a href="/blog/industry-news">Blog</a>'
        '<a href="/about-us">About</a><a href="/about-copy">About</a>'
        '<a href="/private/financials">Financials</a>'
        '<a href="https://docs.acme-robotics.com/">Docs</a>',
        ld(ORG),
    )),
    f"{ACME}/about": ("text/html", html(
        "About", "<p>Acme Robotics is headquartered in Austin, Texas.</p>")),
    f"{ACME}/about-copy": ("text/html", html(
        "About", "<p>Acme Robotics is headquartered in Austin, Texas.</p>")),
    f"{ACME}/private/financials": ("text/html", html(
        "Financials", "<p>Our annual revenue was $80M in 2025.</p>")),
    f"{ACME}/products/stock": ("text/html", html(
        "Stock", "<h1>Acme Stock</h1><p>Inventory counts that stay right.</p>"
        "<h2>Features</h2><ul><li>Barcode scanning</li></ul>")),
    f"{ACME}/products/fleet": ("text/html", html("Fleet", "<h1>Fleet</h1><p>Robots.</p>")),
    f"{ACME}/pricing": ("text/html", html(
        "Pricing", "<h1>Pricing</h1><h3>Starter</h3><p>$29/month</p>"
        "<h3>Enterprise</h3><p>Contact sales</p>")),
    f"{ACME}/blog/industry-news": ("text/html", html(
        "News", "<p>Globex raised $200M at a $2B valuation this week.</p>"
        "<p>Globex has more than 9,000 employees.</p>")),
    "https://docs.acme-robotics.com/robots.txt": ("text/plain", b""),
    "https://docs.acme-robotics.com/": ("text/html", html("Docs", "<p>Getting started.</p>")),
}  # fmt: skip
ACME_REDIRECTS = {f"{ACME}/about-us": f"{ACME}/about"}


@pytest.fixture(scope="module")
def acme(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    settings = settings_for(tmp_path_factory.mktemp("acme"))
    site = Site(ACME_ROUTES, ACME_REDIRECTS)
    result = pipeline(engine, settings, f"{ACME}/", site)
    return {"result": result, "dossier": result["dossier"], "site": site, "settings": settings}


def test_no_revenue_on_the_site_means_unknown(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    # The only revenue statement is behind robots.txt: never fetched, never used.
    assert f"{ACME}/private/financials" not in acme["site"].requested
    for name in ("annual_revenue", "arr", "mrr"):
        assert field(d, name)["state"] == "UNKNOWN" and "value" not in field(d, name)


def test_funding_is_not_valuation_or_total(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    (round_,) = field(d, "funding_round")
    # High risk and only the company says so: a claim, never presented as a fact.
    assert round_["state"] == "UNSUPPORTED" and "value" not in round_
    assert round_["claimed_value"] == "$10M" and round_["details"]["normalized"]["amount"] == 1e7
    assert field(d, "valuation")["state"] == "UNKNOWN"
    assert field(d, "total_funding")["state"] == "UNKNOWN"


def test_vague_counts_are_not_made_precise(acme: dict[str, Any]) -> None:
    customers = field(acme["dossier"], "customer_count")
    assert customers["state"] == "UNSUPPORTED"  # high risk, company-only
    assert customers["claimed_value"] == 500 and customers["details"]["qualifier"] == "gte"
    assert customers["citations"][0]["value_verbatim"] == "more than 500 customers"


def test_employee_count_is_not_company_size(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    assert field(d, "employee_count")["claimed_value"] == 45
    assert field(d, "company_size")["state"] == "UNKNOWN"


def test_prices_are_not_turned_into_revenue(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    assert sorted(values(field(d, "price"))) == ["$29/month", "Contact sales"]
    assert field(d, "arr")["state"] == "UNKNOWN"


def test_facts_about_other_companies_are_not_attributed(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    text = json.dumps(d)
    assert "$2B" not in text and "$200M" not in text and "9,000" not in text
    assert field(d, "employee_count")["claimed_value"] == 45


def test_company_fields_are_cited(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    hq = field(d, "headquarters")
    assert hq["value"] == "Austin, Texas" and hq["state"] == "SUPPORTED"
    assert all(c["url"] == f"{ACME}/about" for c in hq["citations"])  # redirect resolved
    assert len(hq["citations"]) == 1  # the duplicate page is not a second source
    assert field(d, "founded_year")["value"] == 2019
    assert d["company"]["domain"] == "acme-robotics.com"
    assert d["company"]["subdomains"]["value"] == ["docs.acme-robotics.com"]


def test_products_are_separate(acme: dict[str, Any]) -> None:
    d = acme["dossier"]
    assert sorted(p["name"] for p in d["products"]) == ["Acme Stock", "Fleet"]
    assert field(d, "number_of_products")["value"] == 2
    stock = next(p for p in d["products"] if p["slug"] == "stock")
    assert values(stock["fields"]["key_feature"]) == ["Barcode scanning"]
    fleet = next(p for p in d["products"] if p["slug"] == "fleet")
    assert values(fleet["fields"]["key_feature"]) == []


def test_every_citation_is_in_its_raw_document(engine: Engine, acme: dict[str, Any]) -> None:
    assert verify_all_citations(engine, acme["settings"], [acme["result"]["crawl_id"]]) > 5


def test_every_value_has_a_citation(acme: dict[str, Any]) -> None:
    def entries(fields: dict[str, Any]) -> list[dict[str, Any]]:
        return [e for v in fields.values() for e in (v if isinstance(v, list) else [v])]

    d = acme["dossier"]
    all_entries = entries(d["company"]["fields"])
    for p in d["products"]:
        all_entries += entries(p["fields"])
    for e in all_entries:
        if "value" in e:
            assert e["citations"] and e["state"] in ("SUPPORTED", "CORROBORATED", "STALE"), e
        elif "claimed_value" in e:
            assert e["citations"] and e["state"] == "UNSUPPORTED", e
        else:
            assert e["state"] in ("UNKNOWN", "CONFLICTING")


# --- a site with nothing to say ---------------------------------------------------------

EMPTY = "https://blank-company.com"


def test_empty_site_is_all_unknown(engine: Engine, tmp_path: Path) -> None:
    site = Site(
        {
            f"{EMPTY}/robots.txt": ("text/plain", b""),
            f"{EMPTY}/": ("text/html", html("Welcome", "<p>Coming soon.</p>")),
        }
    )
    d = pipeline(engine, settings_for(tmp_path), f"{EMPTY}/", site)["dossier"]
    states = {
        name: (v["state"] if isinstance(v, dict) else [e["state"] for e in v])
        for name, v in d["company"]["fields"].items()
    }
    assert all(s == "UNKNOWN" or s == ["UNKNOWN"] for s in states.values()), states
    assert d["products"] == [] and d["competitors"] == []


# --- a JavaScript-rendered site ---------------------------------------------------------

SPA = "https://spa-company.com"


def test_javascript_site_is_rendered_and_cited(engine: Engine, tmp_path: Path) -> None:
    script = (b"document.getElementById('root').innerHTML = "
              b"'<p>Spa Company is headquartered in Berlin, Germany.</p>';")  # fmt: skip
    site = Site({
        f"{SPA}/robots.txt": ("text/plain", b""),
        f"{SPA}/": ("text/html", html("Spa Company", '<div id="root"></div>'
                                      '<script src="/app.js"></script>',
                                      '<meta property="og:site_name" content="Spa Company">')),
        f"{SPA}/app.js": ("application/javascript", script),
    })  # fmt: skip
    settings = settings_for(tmp_path)
    settings = replace(settings, browser=replace(settings.browser, min_text_chars=50))
    result = pipeline(engine, settings, f"{SPA}/", site)
    hq = field(result["dossier"], "headquarters")
    assert hq["value"] == "Berlin, Germany" and hq["state"] == "SUPPORTED"
    verify_all_citations(engine, settings, [result["crawl_id"]])


# --- sources that disagree --------------------------------------------------------------

CONFLICT = "https://conflict-co.com"


def test_disagreeing_sources_are_conflicting_not_resolved(engine: Engine, tmp_path: Path) -> None:
    site = Site({
        f"{CONFLICT}/robots.txt": ("text/plain", b""),
        f"{CONFLICT}/": ("text/html", html(
            "Conflict Co", "<p>Conflict Co was founded in 2019.</p>",
            '<meta property="og:site_name" content="Conflict Co">')),
        "https://www.crunchbase.com/robots.txt": ("text/plain", b""),
        "https://www.crunchbase.com/organization/conflict-co": ("text/html", html(
            "Conflict Co", "<p>Conflict Co was founded in 2017 in Denver.</p>"
            f'<a href="{CONFLICT}/">Website</a>')),
    })  # fmt: skip

    def search(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        urls = (["https://www.crunchbase.com/organization/conflict-co"]
                if body.get("includeDomains") else [])  # fmt: skip
        return httpx.Response(200, json={"results": [{"url": u} for u in urls]})

    settings = replace(settings_for(tmp_path), search=SearchSettings(api_key="k"),
                       external=ExternalSettings())  # fmt: skip
    d = pipeline(engine, settings, f"{CONFLICT}/", site, search)["dossier"]
    founded = field(d, "founded_year")
    assert founded["state"] == "CONFLICTING" and "value" not in founded
    assert sorted(c["value_verbatim"] for c in founded["citations"]) == ["2017", "2019"]
    assert {c["first_party"] for c in founded["citations"]} == {True, False}
