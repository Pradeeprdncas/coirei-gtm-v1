"""Price terms (market/pricing.py): extraction in context, comparison only in compatible
terms, and the checks that a price holds together with what was collected."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gtm_crawler.config import load_settings
from gtm_crawler.market import analyze, pricing
from gtm_crawler.market.benchmark import _price_reread, reparse_price
from gtm_crawler.market.extract import MAX_EVIDENCE_PER_SOURCE, extract_evidence
from gtm_crawler.market.models import (
    Dimension,
    EvidenceRecord,
    Metric,
    MetricKind,
    Price,
    PriceBound,
    PricingType,
    RetrievalStatus,
    SourceRecord,
)
from gtm_crawler.market.verify import QuoteCheck, verify_evidence
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

SETTINGS = load_settings()
ROOT = Path(__file__).resolve().parents[1]


def terms(text: str, ctx: pricing.Context | None = None) -> list[tuple[Any, ...]]:
    return [(m.value, m.price.pricing_type.value, m.price.currency, m.price.period,
             m.price.billed, m.price.basis, m.price.bound.value)
            for m in pricing.prices(text, ctx) if m.price is not None]  # fmt: skip


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pro costs $29/month per user.",
         [(29.0, "subscription", "USD", "month", None, "user", "exact")]),
        ("There is a $99 setup fee.", [(99.0, "setup", "USD", None, None, None, "exact")]),
        ("Payments cost 2.9% + $0.30 per transaction.",
         [(2.9, "transaction", None, None, None, "transaction", "exact"),
          (0.3, "transaction", "USD", None, None, "transaction", "exact")]),
        ("API usage is $5 per 1,000 API calls.",
         [(5.0, "usage", "USD", None, None, "api call", "exact")]),
        ("Get the reporting add-on for $49.", [(49.0, "add_on", "USD", None, None, None, "exact")]),
        ("Save $100 when you pay yearly.", []),
        ("Spend at least $500 to qualify.", []),
        ("New accounts get $200 in credits.", []),
        ("Contact sales for pricing.", [(0.0, "quote", None, None, None, None, "exact")]),
        # Billed annually: the billing, not a period; the stated period stays.
        ("Pro is $25/month, billed annually.",
         [(25.0, "subscription", "USD", "month", "annually", None, "exact")]),
        ("Organic coffee beans are $5/lb.", [(5.0, "unit", "USD", None, None, "lb", "exact")]),
        ("It is $99 setup and $20/month.",
         [(99.0, "setup", "USD", None, None, None, "exact"),
          (20.0, "subscription", "USD", "month", None, None, "exact")]),
        ("Plans starts at $15/month.",
         [(15.0, "subscription", "USD", "month", None, None, "from")]),
        ("Plans cost up to $100 per month.",
         [(100.0, "subscription", "USD", "month", None, None, "up_to")]),
        # Another dollar's symbol is its own currency: never USD, never converted.
        ("Pricing: HK$1,200 per month.", [(1200.0, "subscription", "HKD", "month", None, None,
                                          "exact")]),
        ("Pricing: S$29 per month.", [(29.0, "subscription", "SGD", "month", None, None, "exact")]),
        ("Growth costs ₹999 per month.",
         [(999.0, "subscription", "INR", "month", None, None, "exact")]),
        # Seen live (Wave): a code after the amount names its currency.
        ("$25CAD/month", [(25.0, "subscription", "CAD", "month", None, None, "exact")]),
        # Seen live: one unit after a list of amounts is each one's.
        ("systeme.io costs $0, $17, $47 or $97 a month.",
         [(v, "subscription", "USD", "month", None, None, "exact") for v in (0.0, 17.0, 47.0,
                                                                              97.0)]),
        # Seen live: an allowance, a subsidy, a market figure are not prices.
        ("The free plan includes $5 of mobile messages per month.", []),
        ("Subsidies in France and Germany amount to between €7,000 and €9,000 per EV.", []),
        ("Subscription pricing will grow from 5.0 USD Billion to 10.5 USD Billion.", []),
        ("Businesses spending under $30/month should pick MailerLite.", []),
    ],
)  # fmt: skip
def test_price_terms_from_the_words_next_to_each_amount(text: str,
                                                         expected: list[Any]) -> None:  # fmt: skip
    assert terms(text) == expected


def test_a_period_is_never_assumed_and_a_bare_dollar_is_said_to_be_assumed() -> None:
    (m,) = pricing.prices("The Pro plan costs $40.")
    assert m.price is not None and m.price.period is None
    assert "no billing period stated" in m.price.ambiguities
    assert m.price.currency_assumed and any("bare '$'" in a for a in m.price.ambiguities)
    # On a page about Singapore, "$" is not read as USD.
    ctx = pricing.Context(dollar=pricing.dollar_place("https://shop.example.sg/pricing"))
    (sg,) = pricing.prices("The Pro plan costs $40 per month.", ctx)
    assert sg.price is not None and sg.price.currency is None and sg.unit == "unknown/month"


def test_comparison_pages_name_products_and_vendor_pages_plans() -> None:
    roundup = pricing.Context(third_party=True)
    (m,) = pricing.prices("Moosend: $9/month for 500 contacts.", roundup)
    assert m.price is not None and m.price.product == "Moosend" and m.price.plan is None
    assert m.price.included == "500 contacts"
    (own,) = pricing.prices("Starter: $9/month.", pricing.Context(vendor="Acme"))
    assert own.price is not None and own.price.plan == "Starter" and own.price.vendor == "Acme"
    assert not pricing.pricing_page("https://site.example/email-marketing-pricing-comparison",
                                    "Email marketing pricing comparison")  # fmt: skip
    assert pricing.pricing_page("https://acme.example/pricing", "Plans")


def page(html: str, url: str = "https://acme.example/pricing") -> ParsedPage:
    return parse_page(f"<html><head><title>Acme pricing</title></head><body>{html}</body></html>"
                      .encode(), url, "text/html", SETTINGS.parse, SETTINGS.urls)  # fmt: skip


def prices_of(p: ParsedPage, vendor: str | None = "Acme") -> list[tuple[str, Metric]]:
    return [(e.excerpt, m) for e in extract_evidence(p, "s", set(), vendor=vendor,
                                                     always_relevant=True)
            for m in e.metrics if m.price is not None]  # fmt: skip


def test_pricing_cards_short_sentences_and_tables() -> None:
    p = page("<main><div><h3>Team</h3><div>$15</div><div>per user / month</div></div>"
             "<p>The Studio plan is $300/year. If you prefer monthly billing, it is more.</p>"
             "<table><tr><th>Plan</th><th>Monthly</th></tr><tr><td>Pro</td><td>$49</td></tr>"
             "</table></main>")  # fmt: skip
    got = {m.value: m for _, m in prices_of(p)}
    card = got[15.0].price
    assert card is not None and (card.plan, card.basis, card.period) == ("Team", "user", "month")
    assert [c.role for c in card.context] == ["heading", "card"]
    assert got[300.0].unit == "USD/year"  # a 29-character sentence of a longer paragraph
    cell = got[49.0].price
    assert cell is not None and (cell.plan, cell.billed, cell.period) == ("Pro", "monthly", None)
    assert {c.role for c in cell.context} == {"row", "column"}


def test_prices_past_the_evidence_cap_and_cards_in_an_aside_of_a_pricing_page() -> None:
    filler = "".join(f"<p>Email marketing adoption grew among retailers in survey wave {i}.</p>"
                     for i in range(MAX_EVIDENCE_PER_SOURCE + 5))  # fmt: skip
    p = page(f"<main>{filler}<p>Our Pro plan costs $29 per month for every team.</p></main>"
             "<aside><div>$19USD/month</div></aside>")  # fmt: skip
    values = {m.value for _, m in prices_of(p)}
    assert {29.0, 19.0} <= values


def _priced(value: float, vendor: str, **kw: Any) -> EvidenceRecord:
    terms_ = {"pricing_type": PricingType.SUBSCRIPTION, "currency": "USD", "period": "month",
              "vendor": vendor, **kw}  # fmt: skip
    return EvidenceRecord(source_id="src_a", claim="c", excerpt=f"${value:g}",
                          dimensions=[Dimension.PRICING], metrics=[Metric(
                              kind=MetricKind.PRICE_POINT, value=value, unit="USD/month",
                              text=f"${value:g}", price=Price(**terms_))])  # fmt: skip


def test_prices_are_compared_only_in_the_same_terms_and_exclusions_say_why() -> None:
    evs = [
        _priced(29, "Acme"),
        _priced(49, "Globex"),
        _priced(39, "Acme"),
        _priced(20, "Acme", billed="annually"),
        _priced(15, "Initech", basis="user"),
        _priced(25, "Hooli", currency=None),
        _priced(9, "Pied", promotional=True),
        _priced(99, "Umbrella", period=None),
        _priced(300, "Vandelay", period="year"),
    ]
    found, left = analyze.price_comparison(evs, {})
    first = found[0].statement
    assert first.startswith("Prices in USD per month, subscription: Acme USD 29.00 / USD 39.00; "
                            "Globex USD 49.00")  # fmt: skip
    assert "different amounts stated for Acme" in first
    # Each other set of terms is its own finding: no monthly <-> annual, no per-user mixing.
    others = {f.statement.split(":")[0] for f in found[1:]}
    assert others == {"Prices in USD per month, subscription, billed annually",
                      "Prices in USD per user per month, subscription",
                      "Prices in USD per year, subscription"}  # fmt: skip
    reasons = " | ".join(left)
    assert "currency not stated or unclear" in reasons and "a promotional price" in reasons
    assert "no billing period stated (none is assumed)" in reasons


def _stored(tmp_path: Path, html: str, url: str) -> tuple[SourceRecord, ParsedPage, RawStore]:
    raw = RawStore(tmp_path / "raw")
    body = f"<html><head><title>Acme pricing</title></head><body>{html}</body></html>".encode()
    src = SourceRecord(url=url, domain="acme.example", status=RetrievalStatus.FETCHED,
                       raw_sha256=raw.put(body))  # fmt: skip
    return src, parse_page(body, url, "text/html", SETTINGS.parse, SETTINGS.urls), raw


def test_verifier_checks_price_text_context_quotes_and_json_ld(tmp_path: Path) -> None:
    offer = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Mug",
            "offers": {"@type": "Offer", "price": "15.00", "priceCurrency": "EUR"},
        }
    )
    src, p, raw = _stored(tmp_path, "<main><div><h3>Team</h3><div>$15</div><div>per user / "
                          f"month</div></div></main><script type='application/ld+json'>{offer}"
                          "</script>", "https://acme.example/pricing")  # fmt: skip
    evs = [e for e in extract_evidence(p, src.source_id, set(), vendor="Acme",
                                       always_relevant=True)
           if any(m.price for m in e.metrics)]  # fmt: skip
    assert {e.locator.type for e in evs if e.locator} == {"xpath", "json_path"}
    for e in evs:
        assert verify_evidence(e, src, raw).status is QuoteCheck.VERIFIED_REEXTRACTED
        for m in e.metrics:
            assert _price_reread(e, m) is None
    card = next(e for e in evs if e.locator and e.locator.type == "xpath")
    price = card.metrics[0].price
    assert price is not None
    # A context quote that is not on the page, or a price text not in the excerpt: mismatch.
    price.context[0].text = "Enterprise"
    assert verify_evidence(card, src, raw).status is QuoteCheck.MISMATCH
    card.metrics[0].text = "$16"
    assert "not in the excerpt" in verify_evidence(card, src, raw).detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [("$29/month per user", (29.0, "$")), ("S$29", (29.0, "SGD")), ("29 EUR", (29.0, "EUR")),
     ("30¢", (0.3, "$")), ("2.9%", (2.9, "percent")), ("$1.2k", (1200.0, "$")),
     ("$25CAD/month", (25.0, "CAD")), ("Rs. 499/month", (499.0, "rs")),
     ("29 € / month", (29.0, "EUR")), ("29 dollars", (29.0, "$")), ("¥500", (500.0, "¥")),
     ("$5 per 1,000 API calls", (5.0, "$")), ("29.00", (29.0, ""))],
)  # fmt: skip
def test_benchmark_rereads_a_price_independently(text: str, expected: tuple[float, str]) -> None:
    assert reparse_price(text) == expected


def test_benchmark_gate_fails_a_price_whose_currency_does_not_reread() -> None:
    e = _priced(29, "Acme")
    assert _price_reread(e, e.metrics[0]) is not None  # "$29" with USD not marked assumed
    p = e.metrics[0].price
    assert p is not None
    p.currency_assumed = True
    assert _price_reread(e, e.metrics[0]) is None
    p.currency = "SGD"
    assert "re-read 29 $" in (_price_reread(e, e.metrics[0]) or "")


def test_pricing_fixture_benchmark_holds() -> None:
    """The labelled pricing fixtures (benchmarks/market/pricing): a floor under the
    scores reported in docs/MARKET_BENCHMARK.md, so a change that loses prices fails."""
    out = subprocess.run([sys.executable, "benchmarks/market/pricing/score.py", "fixtures"],
                         cwd=ROOT, capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(ROOT), "PATH": "", "SYSTEMROOT":
                              __import__("os").environ.get("SYSTEMROOT", "")})  # fmt: skip
    result = json.loads(out.stdout)
    assert result["precision"] >= 97 and result["recall"] >= 88
    assert result["exact_unit_accuracy"] >= 85
    assert result["render"]["render_triggered"] == result["render"]["needs_render"]


@pytest.mark.parametrize(
    ("text", "ctx", "expected"),
    [
        # A count of periods is not that period.
        ("Pro costs $120 per 12 months.", None, [(120.0, "USD", None)]),
        # Billing stated in another sentence is not this price's.
        ("Business is billed annually. Pro costs $29/month.", None,
         [(29.0, "USD/month", None)]),
        ("Pro is $29/month. Enterprise is billed annually.", None, [(29.0, "USD/month", None)]),
        ("Our revenue was strong. Pro is $29/month.", None, [(29.0, "USD/month", None)]),
        ("Customers spend an average of $300 per year on tools.", None, []),
        ("Companies generate $2,000 per month.", None, []),
        ("$10", pricing.Context(card=True, labels="Referral credit / month"), []),
    ],
)  # fmt: skip
def test_review_regressions(text: str, ctx: pricing.Context | None,
                            expected: list[Any]) -> None:  # fmt: skip
    assert [(m.value, m.unit, m.price.billed) for m in pricing.prices(text, ctx)
            if m.price is not None] == expected  # fmt: skip


def test_symbols_shared_by_several_currencies_are_flagged() -> None:
    (rs,) = pricing.prices("Rs 2,999 per month for Basic.")
    assert rs.price is not None and rs.price.currency == "INR" and rs.price.currency_assumed
    (yen,) = pricing.prices("Pro: ¥500 per month.")
    assert yen.price is not None and yen.price.currency is None
    assert pricing.dollar_place("https://example.com/en-ca/pricing", "Pricing") == ".ca"


def test_tax_and_allowance_are_terms_of_the_comparison() -> None:
    evs = [
        _priced(29, "Acme", tax="excluded"),
        _priced(29, "Globex", tax="included"),
        _priced(10, "Initech", included="500 contacts"),
        _priced(20, "Hooli"),
    ]
    found, _ = analyze.price_comparison(evs, {})
    assert len(found) == 4


def test_long_table_cells_are_not_price_cells() -> None:
    p = page("<main><table><tr><th>Plan</th><th>Notes</th></tr><tr><td>Pro</td><td>"
             + "Long prose about the plan. " * 90 + "It costs $29 per user.</td></tr></table>"
             "</main>")  # fmt: skip
    assert all(len(x) <= 2000 for x, _ in prices_of(p))


def test_json_ld_currency_in_a_price_specification_is_quoted(tmp_path: Path) -> None:
    offer = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "App",
                        "offers": {"@type": "Offer", "price": "9.99", "priceSpecification": {
                            "@type": "UnitPriceSpecification", "priceCurrency": "GBP",
                            "billingDuration": "P1M"}}})  # fmt: skip
    src, p, raw = _stored(tmp_path, f"<main><p>App</p></main><script type='application/ld+json'>"
                          f"{offer}</script>", "https://acme.example/app")  # fmt: skip
    (e,) = [e for e in extract_evidence(p, src.source_id, set(), vendor="Acme",
                                        always_relevant=True) if e.metrics]  # fmt: skip
    m = e.metrics[0]
    assert m.price is not None and (m.price.currency, m.price.period) == ("GBP", "month")
    assert [c.text for c in m.price.context] == ["GBP"]
    assert verify_evidence(e, src, raw).status is QuoteCheck.VERIFIED_REEXTRACTED
    assert _price_reread(e, m) is None


def test_prices_bounds_are_their_own() -> None:
    (low, high) = pricing.prices("Plans range from $10 to $50 per month.")
    assert low.price is not None and high.price is not None
    assert (low.price.bound, high.price.bound) == (PriceBound.FROM, PriceBound.UP_TO)
    assert low.unit == high.unit == "USD/month"
