"""Price recall (market/pricing.py, extract.py, parse/dom.py landmark regions): prices the
rules used to miss on unseen pages (heldout-v1, docs/MARKET_BENCHMARK.md), each from the
page shape it was missed on, and the non-prices next to them that must stay out."""

from datetime import date
from pathlib import Path

from gtm_crawler.config import ParseSettings, UrlSettings
from gtm_crawler.market import pricing
from gtm_crawler.market.benchmark import reparse_price
from gtm_crawler.market.collect import HTML_TEXT_VERSION, parse_settings
from gtm_crawler.market.extract import extract_evidence
from gtm_crawler.market.models import PricingType, RetrievalStatus, SourceRecord
from gtm_crawler.market.verify import QuoteCheck, verify_evidence
from gtm_crawler.parse import parse_page
from gtm_crawler.rawstore import RawStore

SETTINGS = parse_settings(ParseSettings(), HTML_TEXT_VERSION)


def doc(body: str, title: str = "Fees") -> bytes:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


def evidence(body: str, url: str, vendor: str | None = None, title: str = "Fees",
             settings: ParseSettings = SETTINGS) -> list:  # fmt: skip
    page = parse_page(doc(body, title), url, "text/html", settings, UrlSettings())
    return extract_evidence(page, "src", set(), vendor=vendor, always_relevant=vendor is not None,
                            today=date(2026, 10, 21))  # fmt: skip


def amounts(body: str, url: str, **kw: object) -> list[float]:
    return sorted(m.value for e in evidence(body, url, **kw) for m in e.metrics  # type: ignore[arg-type]
                  if m.price is not None and m.value is not None)  # fmt: skip


# A WordPress theme's wrapper inside <main>, a HubSpot "widget" module, a section <header>.
LANDMARKS = (
    "<main><div class='cs-site-content cs-sidebar-enabled'><article>"
    "<p>Our review of SMS providers for small businesses in India this year.</p>"
    "<ul><li>Transactional SMS – 1250 INR + GST for 5,000 SMS Units</li></ul></article></div>"
    "<div class='hs_cos_wrapper_widget'><p>The Base plan costs $250/month for 2 users.</p></div>"
    "<section><header><h3>Pro</h3><p>$29 per month</p></header></section></main>"
    "<aside class='sidebar'><p>Sponsored: hosting from $1 per month.</p></aside>"
)


def test_class_names_inside_main_are_no_landmark() -> None:
    got = amounts(LANDMARKS, "https://reviews.example/sms")
    assert {1250, 250, 29} <= set(got) and 1 not in got  # the real <aside> is still skipped
    # PARSER_VERSION 3 regions (the crawler's) are unchanged.
    legacy = parse_settings(ParseSettings(), "html-text-2")
    assert 1250 not in amounts(LANDMARKS, "https://reviews.example/sms", settings=legacy)


def test_table_cells_whose_text_is_in_inner_elements(tmp_path: Path) -> None:
    body = (
        "<table><tr><th><p>Course</p></th><th><p>Subject fee</p></th><th><p>Course fee</p></th>"
        "</tr><tr><td><p>Diploma of Business</p></td><td><p>$2,880</p></td>"
        "<td><p>$23,040</p></td></tr></table>"
    )
    url = "https://college.example/fees"
    recs = evidence(body, url, vendor="College")
    assert sorted(m.value for e in recs for m in e.metrics) == [2880, 23040]
    raw = RawStore(tmp_path)
    source = SourceRecord(source_id="src", url=url, domain="college.example",
                          raw_sha256=raw.put(doc(body)), status=RetrievalStatus.FETCHED,
                          text_version=HTML_TEXT_VERSION)  # fmt: skip
    assert {verify_evidence(e, source, raw).status for e in recs} == {
        QuoteCheck.VERIFIED_REEXTRACTED
    }


def test_a_heading_that_states_a_price_is_read() -> None:
    body = ("<div><h2>Lite</h2><h3>₹9,999/yr</h3></div>"
            "<h3>Could we add 500 users and still just pay $329/month total on Pro Unlimited?</h3>"
            "<h2>Plans and pricing</h2>")  # fmt: skip
    assert amounts(body, "https://shop.example/pricing", vendor="Shop") == [329, 9999]


def test_amount_forms() -> None:
    def read(text: str) -> list[tuple[float | None, str | None]]:
        return [(m.value, m.price.currency) for m in pricing.prices(text) if m.price]

    assert read("UK cards 1.4% + 20p per transaction")[1] == (0.2, "GBP")
    assert read("SMS at the rate of 24.5 paisa") == [(0.245, "INR")]
    assert read("10,00,000 Messages – 1,70,000 INR") == [(170000, "INR")]
    assert read("Meet at 3 p.m. for 20 minutes") == []  # "3 p" is no pence
    assert reparse_price("20p") == (0.2, "GBP") and reparse_price("24.5 paisa") == (0.245, "INR")


def test_price_cues_without_price_words() -> None:
    (pack,) = pricing.prices("Transactional SMS – 1250 INR + GST for 5,000 SMS Units")
    assert pack.price.pricing_type is PricingType.USAGE and pack.price.basis == "sms"
    assert pack.price.basis_quantity == 5000 and pack.price.tax == "excluded"
    (listed,) = pricing.prices("1,000 Messages – 295 INR")
    assert listed.price.basis == "message" and listed.price.basis_quantity == 1000
    assert [m.value for m in pricing.prices("Adult From £51")] == [51]
    assert [m.value for m in pricing.prices("Paris starts at £52 one-way or £78 return.")] == [
        52,
        78,
    ]
    assert [m.value for m in pricing.prices("£29.50 was £36.00")] == [29.5, 36]
    (extra,) = pricing.prices("Its AI assistant is an extra $10.")
    assert extra.price.pricing_type is PricingType.ADD_ON
    # A plan's allowance is no pack; a salary, a "was" without a sale price are no prices.
    (plan,) = pricing.prices("The Free plan is $0 for 1,000 API calls per month.")
    assert plan.price.pricing_type is not PricingType.USAGE
    assert pricing.prices("We are hiring engineers with salaries from $140,000.") == []
    assert pricing.prices("In most cases it was 4p.") == []
