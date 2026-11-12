"""Product identification and product information extraction."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.db.evidence import EvidenceStore, check_draft
from gtm_crawler.db.models import Evidence, Observation, Product
from gtm_crawler.extract import extract_crawl
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.product import extract_products, identify_products, slugify
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

from .test_crawl import SITE, FakeSite, html, run, settings_for
from .test_extract_company import ld

DOMAIN = "acme.example"
IDS = {"stock": 1, "fleet": 2}


def page(path: str, body: str, head: str = "", published: str | None = None) -> ParsedPage:
    if published:
        head += f'<meta property="article:published_time" content="{published}">'
    doc = f"<html><head><title>T</title>{head}</head><body>{body}</body></html>".encode()
    return parse_page(doc, f"{SITE}{path}", "text/html", ParseSettings(), UrlSettings())


def ctx() -> Context:
    return Context(DOMAIN, {"Acme"}, dict(IDS))


def found(p: ParsedPage) -> list[tuple[str, int | None, str, str | None, Any]]:
    drafts = extract_products(p, ctx())
    for d in drafts:
        check_draft(d, p)
    return [(d.field, d.product_id, d.value_verbatim, d.qualifier, d.value_normalized)
            for d in drafts]  # fmt: skip


def test_slugify() -> None:
    assert slugify("Acme Stock", {"Acme"}) == "stock"
    assert slugify("Acme", {"Acme"}) == "acme"  # a product named like the company keeps it
    assert slugify("Fleet  Manager 2.0!") == "fleet-manager-2-0"


def test_product_pages_are_identified_only_when_named() -> None:
    named = page("/products/stock", "<h1>Acme Stock</h1><p>Inventory counts in real time.</p>")
    assert [(c.slug, c.name, c.basis) for c in identify_products(named, ctx())] == [
        ("stock", "Acme Stock", "product_page")
    ]
    # The <h1> is a slogan, but og:title names the product.
    slogan = page("/products/fleet", "<h1>Move faster</h1>",
                  '<meta property="og:title" content="Fleet | Acme">')  # fmt: skip
    assert [(c.slug, c.name) for c in identify_products(slogan, ctx())] == [("fleet", "Fleet")]
    # Neither names it: not identified (a product is never invented from a URL alone).
    assert identify_products(page("/products/x", "<h1>Welcome</h1>"), ctx()) == []
    # Feature and pricing pages are not products.
    assert identify_products(page("/features", "<h1>Features</h1>"), ctx()) == []
    assert identify_products(page("/pricing", "<h1>Pricing</h1>"), ctx()) == []


def test_json_ld_products() -> None:
    app = {"@context": "https://schema.org", "@type": "SoftwareApplication",
           "name": "Acme Stock", "applicationCategory": "BusinessApplication",
           "description": "Inventory software.", "featureList": ["Barcode scanning", "Sync"],
           "offers": {"@type": "Offer", "name": "Starter", "price": "29",
                      "priceCurrency": "USD"}}  # fmt: skip
    partner = {"@type": "Product", "name": "Globex Scanner", "url": "https://globex.com/s"}
    p = page("/", "", ld(app) + ld(partner))
    assert [(c.slug, c.basis) for c in identify_products(p, ctx())] == [
        ("stock", "json_ld:SoftwareApplication")
    ]
    got = found(p)
    assert ("product_name", 1, "Acme Stock", None, None) in got
    assert ("product_category", 1, "BusinessApplication", None, None) in got
    assert ("product_description", 1, "Inventory software.", None, None) in got
    assert ("key_feature", 1, "Barcode scanning", None, None) in got
    assert ("price", 1, "29", "Starter",
            {"qualifier": "eq", "amount": 29.0, "currency": "USD"}) in got  # fmt: skip
    assert not any(v == "Globex Scanner" for _, _, v, _, _ in got)


def test_product_page_fields() -> None:
    p = page(
        "/products/stock",
        "<main><h1>Acme Stock</h1><p>The inventory system retailers trust.</p>"
        "<h2>Features</h2><ul><li>Barcode scanning</li><li>Real-time sync</li></ul>"
        "<p>Everything syncs.</p><h2>Use cases</h2><h3>Retail stores</h3>"
        "<h2>Customers</h2><ul><li>Not a feature</li></ul></main>",
        '<meta name="description" content="Stock tracks inventory.">',
    )
    got = {(f, v) for f, prod, v, _, _ in found(p) if prod == 1}
    assert got == {
        ("product_name", "Acme Stock"),
        ("product_description", "Stock tracks inventory."),
        ("product_positioning", "The inventory system retailers trust."),
        ("key_feature", "Barcode scanning"),
        ("key_feature", "Real-time sync"),
        ("main_use_case", "Retail stores"),
    }


def test_company_wide_features_have_no_product() -> None:
    p = page("/features", "<h1>Features</h1><ul><li>Scanning</li></ul>")
    # Which product these describe is not stated, so none is assumed.
    assert found(p) == [("key_feature", None, "Scanning", None, None)]


PRICING = (
    "<h1>Pricing</h1>"
    "<div><h3>Starter</h3><p>$29 /month billed annually</p></div>"
    "<div><h3>Growth</h3><p>Starting at €99 per user per month</p></div>"
    "<div><h3>Enterprise</h3><p>Contact sales</p></div>"
    "<div><h3>Hobby</h3><p>Free</p></div>"
    "<h2>Frequently asked questions</h2><p>Can I cancel anytime? Yes.</p>"
)


def test_pricing() -> None:
    got = found(page("/pricing", PRICING))
    prices = [(v, q, n) for f, _, v, q, n in got if f == "price"]
    assert prices == [
        ("$29 /month billed annually", "Starter",
         {"qualifier": "eq", "amount": 29.0, "currency": "USD", "per": ["month"],
          "billing": "annual"}),
        ("Starting at €99 per user per month", "Growth",
         {"qualifier": "gte", "amount": 99.0, "currency": "EUR", "per": ["user", "month"]}),
        ("Contact sales", "Enterprise", {"qualifier": "eq", "value": "contact_sales"}),
        ("Free", "Hobby", {"qualifier": "eq", "amount": 0}),
    ]  # fmt: skip
    plans = [v for f, _, v, _, _ in got if f == "plan_name"]
    assert plans == ["Starter", "Growth", "Enterprise", "Hobby"]  # not the FAQ heading
    assert all(prod is None for _, prod, _, _, _ in got)
    # Prices on a product's own pricing page belong to that product.
    assert {prod for _, prod, _, _, _ in found(page("/products/stock/pricing", PRICING))} == {1}
    # A price right under the page title has no plan: the title is not a plan name.
    assert found(page("/pricing", "<h1>Simple pricing</h1><p>From $10/month</p>")) == [
        ("price", None, "From $10/month", None,
         {"qualifier": "gte", "amount": 10.0, "currency": "USD", "per": ["month"]}),
    ]  # fmt: skip
    # "Plans start at" is the lowest price, not every plan's.
    assert found(page("/pricing", "<h1>Pricing</h1><p>Plans start at $49 per month</p>")) == [
        ("price", None, "start at $49 per month", None,
         {"qualifier": "gte", "amount": 49.0, "currency": "USD", "per": ["month"]}),
    ]  # fmt: skip
    # Prices elsewhere (a blog post) are not pricing.
    assert found(page("/blog/costs", "<p>It costs $29/month.</p>")) == []


def test_recent_updates() -> None:
    changelog = page("/changelog", "<h1>Changelog</h1><h2>March 3, 2026: Bulk import</h2>"
                     "<p>...</p><h2>2026-01-15 Faster sync</h2><h2>Roadmap</h2>")  # fmt: skip
    got = [(v, n) for f, _, v, _, n in found(changelog) if f == "recent_update"]
    assert got == [
        ("March 3, 2026: Bulk import", {"qualifier": "eq", "date": "2026-03-03"}),
        ("2026-01-15 Faster sync", {"qualifier": "eq", "date": "2026-01-15"}),
    ]
    article = page("/news/acme-launches-fleet", "<h1>Acme launches Fleet</h1><p>Today.</p>",
                   published="2026-05-01")  # fmt: skip
    assert [(f, v) for f, _, v, _, _ in found(article)] == [
        ("recent_update", "Acme launches Fleet")
    ]
    # An undated news page is not presented as a (recent) update.
    assert found(page("/news/old", "<h1>Something happened</h1>")) == []


# --- end to end ------------------------------------------------------------------

SITE_ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", '<a href="/products/stock">Stock</a><a href="/products/fleet">Fleet</a>'
        '<a href="/pricing">Pricing</a><p>We are Acme.</p>',
        '<meta property="og:site_name" content="Acme">',
    )),
    f"{SITE}/products/stock": ("text/html", html(
        "Stock", "<h1>Acme Stock</h1><p>The inventory system retailers trust.</p>"
        "<h2>Features</h2><ul><li>Barcode scanning</li></ul>",
    )),
    f"{SITE}/products/fleet": ("text/html", html(
        "Fleet", "<h1>Fleet</h1><p>Route planning for delivery vans.</p>"
        "<h2>Features</h2><ul><li>Barcode scanning</li></ul>",
    )),
    f"{SITE}/pricing": ("text/html", html("Pricing", PRICING)),
}  # fmt: skip


@pytest.mark.db
def test_extract_crawl_creates_separate_products(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    summary = run(engine, settings, FakeSite(SITE_ROUTES))
    stats = extract_crawl(engine, settings, summary.crawl_id)
    assert stats["rejected"] == 0 and stats["products"] == ["fleet", "stock"]

    with engine.connect() as conn:
        products = {
            r.slug: r
            for r in conn.execute(
                select(Product).where(Product.organization_id == summary.organization_id)
            )
        }
        rows = conn.execute(
            select(Observation.field, Observation.product_id, Observation.value_verbatim,
                   Observation.evidence_id)
            .where(Observation.crawl_id == summary.crawl_id)
        ).all()  # fmt: skip
    assert products["stock"].name == "Acme Stock" and products["fleet"].name == "Fleet"
    features = {(r.product_id, r.value_verbatim) for r in rows if r.field == "key_feature"}
    # The same feature text on two product pages is two observations, one per product.
    assert features == {(products["stock"].id, "Barcode scanning"),
                        (products["fleet"].id, "Barcode scanning")}  # fmt: skip
    assert sum(r.field == "price" for r in rows) == 4

    store = EvidenceStore(engine, RawStore(settings.rawstore.root), settings.sources)
    for r in rows:
        assert store.verify(r.evidence_id) is None, (r.field, r.value_verbatim)
    with engine.connect() as conn:
        assert conn.execute(select(Evidence.locator_type).distinct()).scalars().all()
    assert json.dumps(stats)  # serializable for the CLI


def test_help_and_comparison_labels_are_not_plans_or_prices() -> None:
    # A question over a sales link is help, not a plan; in a comparison grid a
    # header or term ("Free") names a plan's column, it is not a price.
    body = (
        "<h1>Pricing</h1><div><h3>Pro</h3><p>$15/month</p></div>"
        "<h2>Still need help choosing?</h2><p>Contact us</p>"
        "<h3>Feature comparison</h3><p>Free</p><p>Pro</p><h4>Drafts</h4>"
        "<dl><dt>Free</dt><dd>Unlimited</dd><dt>Pro</dt><dd>Unlimited</dd></dl>"
    )
    got = found(page("/pricing", body))
    assert [v for f, _, v, _, _ in got if f == "plan_name"] == ["Pro"]
    assert [v for f, _, v, _, _ in got if f == "price"] == ["$15/month"]


def test_price_units_and_long_month_names_are_kept() -> None:
    got = found(page("/pricing", "<h1>Pricing</h1><h3>Starter</h3><p>$49 per truck per month</p>"))
    assert ("price", None, "$49 per truck per month", "Starter",
            {"qualifier": "eq", "amount": 49.0, "currency": "USD", "per": ["truck", "month"]}
            ) in got  # fmt: skip
    changelog = page("/changelog", "<h1>Changelog</h1><h2>September 2, 2026: Rerouting</h2>"
                     "<h2>Sept 9, 2026: Fix</h2>")  # fmt: skip
    dates = [n["date"] for f, _, _, _, n in found(changelog) if f == "recent_update"]
    assert dates == ["2026-09-02", "2026-09-09"]


def positioning(path: str, body: str) -> list[str]:
    return [v for f, prod, v, _, _ in found(page(path, body)) if f == "product_positioning"]


def test_the_home_page_headline_is_the_company_value_proposition() -> None:
    home = "<h1>Stress-free inventory management</h1><p>From counts to reorders.</p>"
    assert positioning("/", home) == ["Stress-free inventory management"]
    assert all(prod is None for f, prod, *_ in found(page("/", home)) if f.startswith("product"))
    # A headline that is only the company's name says nothing: the next heading does.
    brand_first = "<h1>Acme</h1><h2>Your stock workspace</h2>"
    assert positioning("/", brand_first) == ["Your stock workspace"]
    # Elsewhere an <h1> is a page title, and a help centre's home is not the pitch.
    assert positioning("/about", "<h1>About us and our story</h1>") == []
    help_home = page("/", "<h1>What can we help you with?</h1>")
    help_home.url = "https://support.acme.example/"
    assert extract_products(help_home, ctx()) == []


def test_a_badge_in_a_plan_heading_is_not_its_name() -> None:
    # Buffer's plan cards: the name and a badge are sibling spans in the heading, and
    # cards without a badge hold an aria-hidden "Placeholder" one to keep their height;
    # "Free" and "forever" are two spans, read as one word ("Freeforever").
    badge = "<span class='Badge'>{}</span>"
    placeholder = "<span class='Badge' aria-hidden='true'><span>Placeholder</span></span>"
    body = "<h1>Pricing</h1>" + "".join(
        f"<div><h3><span>{name}</span>{extra}</h3><p>{price}</p></div>"
        for name, extra, price in [
            ("Free", placeholder, "<span>Free</span><span>forever</span>"),
            ("Essentials", badge.format("Recommended"), "$5/month"),
            ("Team", placeholder, "$10/month"),
            ("Agency", badge.format("Most popular"), "$100/month"),
        ]
    )
    got = found(page("/pricing", body))
    assert [v for f, _, v, _, _ in got if f == "plan_name"] == [
        "Free",
        "Essentials",
        "Team",
        "Agency",
    ]
    assert [q for f, _, _, q, _ in got if f == "price"] == ["Free", "Essentials", "Team", "Agency"]
