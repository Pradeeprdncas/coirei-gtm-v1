"""Price attribution (market/pricing.py, extract.py): which product and plan a price
belongs to, read from the DOM's structure and the price's own words, with spaced inline
text (ParseSettings.inline_spacing); uncertain attribution is said, never guessed."""

import asyncio
from datetime import date
from pathlib import Path

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.market.analyze import not_comparable, price_comparison
from gtm_crawler.market.collect import HTML_TEXT_VERSION, parse_settings
from gtm_crawler.market.extract import extract_evidence
from gtm_crawler.market.models import (
    Attribution,
    EvidenceRecord,
    PricingType,
    RetrievalStatus,
    SourceRecord,
)
from gtm_crawler.market.verify import QuoteCheck, verify_evidence, verify_report
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

SPACED = parse_settings(ParseSettings(), HTML_TEXT_VERSION)


def doc(body: str, title: str = "Pricing") -> bytes:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


def page(body: str, url: str, title: str = "Pricing") -> ParsedPage:
    return parse_page(doc(body, title), url, "text/html", SPACED, UrlSettings())


def evidence(body: str, url: str, title: str = "Pricing",
             vendor: str | None = None) -> list[EvidenceRecord]:  # fmt: skip
    return extract_evidence(
        page(body, url, title),
        "src",
        set(),
        vendor=vendor,
        always_relevant=vendor is not None,
        today=date(2026, 10, 21),
    )


def prices(body: str, url: str, title: str = "Pricing", vendor: str | None = None
           ) -> list[tuple[float, str | None, str | None, str]]:  # fmt: skip
    return [(m.value, m.price.plan, m.price.product, m.price.pricing_type.value)
            for e in evidence(body, url, title, vendor) for m in e.metrics
            if m.price is not None]  # fmt: skip


def terms(body: str, url: str, **kw: str) -> list:
    return [m.price for e in evidence(body, url, **kw) for m in e.metrics if m.price]


def test_adjacent_inline_elements_name_product_and_plan() -> None:
    # Seen live: "EmailOctopusPro" when inline elements were joined without a space.
    body = (
        "<h2>Best email tools</h2><table><tr><th>Tool</th><th>Price</th></tr>"
        "<tr><td><span>EmailOctopus</span><span>Pro</span></td>"
        "<td><span>$</span><span>24</span><span>/mo</span></td></tr></table>"
    )
    got = prices(body, "https://reviews.example.net/best-email-tools", "Best email tools")
    assert got == [(24.0, "Pro", "EmailOctopus", "subscription")]


def test_pricing_cards_with_nested_formatting() -> None:
    body = (
        "<section><div class='card'><h3><span><b>Growth</b></span></h3><div><span>€</span>"
        "<span><strong>49</strong></span><span>/month</span></div><div>billed annually</div>"
        "</div><div class='card'><h3><i>Scale</i></h3><div><span>€</span><span>99</span>"
        "<span>/month</span></div></div></section>"
    )
    ps = terms(body, "https://acme.example/pricing", vendor="Acme")
    assert [(p.plan, p.currency, p.period, p.billed, p.attribution) for p in ps] == [
        ("Growth", "EUR", "month", "annually", Attribution.HEADING),
        ("Scale", "EUR", "month", None, Attribution.HEADING),
    ]


def test_brand_prefix_is_not_part_of_the_plan() -> None:
    body = "<p>Staffly Basic is S$8 per employee per month, and Staffly Plus is S$14.</p>"
    got = prices(body, "https://staffly.sg/pricing", "Staffly pricing", vendor="Staffly")
    assert [(v, plan, product) for v, plan, product, _ in got] == [
        (8.0, "Basic", None),
        (14.0, "Plus", None),
    ]


def test_multiple_products_on_one_page() -> None:
    body = (
        "<h2>Pricing</h2><div><h3>Payroll</h3><p>Payroll costs $40 per month plus $6 per "
        "employee per month.</p></div><div><h3>Time Tracking</h3><p>Time Tracking costs "
        "$8 per user per month.</p></div>"
    )
    got = prices(body, "https://payco.example/pricing", "PayCo pricing", vendor="PayCo")
    assert [(v, plan) for v, plan, _, _ in got] == [
        (40.0, "Payroll"),
        (6.0, "Payroll"),
        (8.0, "Time Tracking"),
    ]


def test_comparison_page_attributes_each_vendor() -> None:
    body = (
        "<h2>Invoicing apps compared</h2><p>Ledgerly Pro is $20 a month, while "
        "Billtastic's Growth plan costs $35 a month.</p>"
    )
    got = prices(body, "https://blog.example.com/invoicing-apps-compared", "Invoicing compared")
    assert [(v, plan, product) for v, plan, product, _ in got] == [
        (20.0, "Pro", "Ledgerly"),
        (35.0, "Growth", "Billtastic"),
    ]


def test_base_plan_next_to_add_ons() -> None:
    body = (
        "<h2>Business</h2><p>Business costs $149 per month.</p>"
        "<h3>Advanced Reporting</h3><p>Advanced Reporting is optional: you pay $49 for the "
        "add-on each month, on top of your Business plan.</p>"
        "<h2>Add-ons</h2><ul><li>WhatsApp channel - $25 per month</li>"
        "<li>Extra phone number - $3 per number per month</li></ul>"
    )
    got = prices(body, "https://metricwave.io/pricing", vendor="MetricWave")
    # Short blocks are read on the card pass, after the sentences: compare without order.
    assert sorted(got) == [
        (3.0, None, "Extra phone number", "add_on"),
        (25.0, None, "WhatsApp channel", "add_on"),
        (49.0, "Business", "Advanced Reporting", "add_on"),
        (149.0, "Business", None, "subscription"),
    ]


def test_items_are_products_not_plans() -> None:
    body = (
        "<ul><li>Vine tomatoes - $3.99/kg</li><li>Baby spinach - $2.49 per bag</li></ul>"
        "<p>Outbound calls cost $0.014 per minute.</p>"
    )
    got = prices(body, "https://greengrocer.example/produce", "Produce", vendor="GreenGrocer")
    assert [(v, plan, product, kind) for v, plan, product, kind in got] == [
        (3.99, None, "Vine tomatoes", "unit"),
        (2.49, None, "Baby spinach", "unit"),
        (0.014, None, "Outbound calls", "usage"),
    ]


def test_table_prices_take_their_row_and_column_and_skip_volume_tiers() -> None:
    body = (
        "<table><tr><th>Plan</th><th>Monthly</th><th>Annual</th></tr>"
        "<tr><td>Starter</td><td>$10</td><td>$100</td></tr>"
        "<tr><td>Pro</td><td>$25</td><td>$250</td></tr></table>"
        "<table><tr><th>Monthly verifications</th><th>Price per verification</th></tr>"
        "<tr><td>1 - 1,000</td><td>$1.50</td></tr></table>"
    )
    ps = terms(body, "https://verify.example/pricing", vendor="Verify")
    assert [(p.plan, p.billed, p.attribution) for p in ps] == [
        ("Starter", "monthly", Attribution.TABLE),
        ("Starter", "annually", Attribution.TABLE),
        ("Pro", "monthly", Attribution.TABLE),
        ("Pro", "annually", Attribution.TABLE),
        (None, None, None),
    ]  # "1 - 1,000" is a volume tier, not a plan


def test_multiple_currencies_and_billing_periods() -> None:
    body = (
        "<p>The Team plan is $12 per user per month, €11 per user per month in Europe, "
        "or £100 per user per year.</p>"
    )
    ps = terms(body, "https://teamly.example/pricing", vendor="Teamly")
    assert [(p.plan, p.currency, p.period, p.basis) for p in ps] == [
        ("Team", "USD", "month", "user"),
        ("Team", "EUR", "month", "user"),
        ("Team", "GBP", "year", "user"),
    ]


def test_ambiguous_plan_is_marked_uncertain_not_guessed() -> None:
    # Two plans on the page; a price whose card names neither is kept, unattributed.
    body = (
        "<div><h3>Starter</h3><p>$9 per month</p></div><div><h3>Pro</h3><p>$29 per month"
        "</p></div><div><p>Annual billing: $290 per year.</p></div>"
    )
    ps = terms(body, "https://toolco.example/pricing", vendor="ToolCo")
    assert [(p.plan, p.attribution) for p in ps] == [
        ("Starter", Attribution.HEADING),
        ("Pro", Attribution.HEADING),
        (None, Attribution.UNCERTAIN),
    ]
    assert any(a.startswith("plan not identified") for a in ps[2].ambiguities)
    rec = evidence(body, "https://toolco.example/pricing", vendor="ToolCo")
    source = SourceRecord(
        source_id="src",
        url="https://toolco.example/pricing",
        domain="toolco.example",
        status=RetrievalStatus.FETCHED,
    )
    findings, _ = price_comparison(rec, {"src": source})
    assert "(plan not identified)" in " ".join(f.statement for f in findings)


def test_missing_plan_on_a_single_offer_page_is_simply_none() -> None:
    ps = terms(
        "<p>Unlimited invoices for $15 per month.</p>",
        "https://solo.example/pricing",
        vendor="Solo",
    )
    assert [(p.plan, p.product, p.attribution) for p in ps] == [(None, None, None)]


def test_page_heading_and_date_are_not_every_prices() -> None:
    body = (
        "<h1>Acme Analytics</h1><p>Last updated: March 2025</p>"
        "<div><h3>Basic</h3><p>$19 per month</p></div>"
        "<p>The Cloud plan is $49 per month.</p>"
    )
    ps = terms(body, "https://acme.example/pricing", vendor="Acme")
    assert [p.plan for p in ps] == ["Basic", "Cloud"]
    assert all(p.product is None for p in ps)
    # On an article, "last updated" dates the article, not each price it mentions.
    ps = terms(
        "<p>Last updated: March 2025</p><p>Kit's Creator plan costs $33 per month.</p>",
        "https://blog.example.com/kit-review",
        title="Kit review",
    )
    assert [(p.product, p.plan, p.effective) for p in ps] == [("Kit", "Creator", None)]
    ps = terms(
        "<p>Prices as of May 2024.</p><p>Kit's Creator plan costs $33 per month.</p>",
        "https://blog.example.com/kit-review",
        title="Kit review",
    )
    assert ps[0].effective == "May 2024"


def test_review_page_numbers_are_not_prices() -> None:
    body = (
        "<p>We spent $500 testing these wallets over three months.</p>"
        "<p>Rated 4.5 out of 5 by 2,300 reviewers.</p>"
        "<p>It saves you $30 a month compared with paper invoices.</p>"
        "<p>At $129 the Nova wallet is our pick.</p>"
    )
    got = prices(body, "https://reviews.example.net/best-wallets", "Best hardware wallets")
    assert [v for v, *_ in got] == [129.0]  # not the spend, rating or savings


def test_research_report_licences_are_never_compared() -> None:
    body = (
        "<p>Vendors typically charge $6 per employee per month.</p>"
        "<p>Single User License: $4,650</p><p>The full report costs $2,950.</p>"
    )
    rec = evidence(body, "https://research.example.com/hr-software-market", "HR software market")
    ps = [m for e in rec for m in e.metrics if m.price]
    assert [(m.value, m.price.pricing_type, m.price.product) for m in ps] == [
        (6.0, PricingType.SUBSCRIPTION, None),
        (4650.0, PricingType.ONE_TIME, "research report"),
        (2950.0, PricingType.ONE_TIME, "research report"),
    ]
    assert [not_comparable(m) is None for m in ps] == [True, False, False]


def test_spaced_quotes_verify_against_the_stored_page(tmp_path: Path) -> None:
    body = (
        "<div><h3><span>Team</span><span>Plus</span></h3><div><span>$</span><span>49</span>"
        "<span>/mo</span></div></div>"
    )
    raw = RawStore(tmp_path)
    sha = raw.put(doc(body))
    url = "https://teamco.example/pricing"
    rec = evidence(body, url, vendor="TeamCo")
    assert rec and rec[0].metrics[0].price.plan == "Team Plus"
    source = SourceRecord(
        source_id="src",
        url=url,
        domain="teamco.example",
        raw_sha256=sha,
        status=RetrievalStatus.FETCHED,
        text_version=HTML_TEXT_VERSION,
    )
    for e in rec:
        assert verify_evidence(e, source, raw).status is QuoteCheck.VERIFIED_REEXTRACTED
    # A source collected before spacing (no text_version) is re-parsed the legacy way.
    legacy = source.model_copy(update={"text_version": None})
    assert verify_evidence(rec[0], legacy, raw).status is QuoteCheck.MISMATCH
    from gtm_crawler.config import MarketSettings
    from gtm_crawler.market.models import Report, ResearchMode, ResearchRequest, RunStatus

    report = Report(analysis_version="test", research_id="mr_000000000000",
                    mode=ResearchMode.INDUSTRY, subject="x", status=RunStatus.PARTIAL,
                    request=ResearchRequest(mode=ResearchMode.INDUSTRY, subject="x"),
                    executive_summary=[], sections={}, sources=[source],
                    evidence=rec)  # fmt: skip
    checks = asyncio.run(verify_report(report, raw, MarketSettings()))
    assert {c.status for c in checks.values()} == {QuoteCheck.VERIFIED_REEXTRACTED}
