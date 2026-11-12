"""Evidence locators, source classification, and recording/tracing observations."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text

from gtm_crawler.config import ParseSettings, Settings, SourceSettings, UrlSettings
from gtm_crawler.crawl import config_snapshot
from gtm_crawler.db.evidence import (
    EvidenceError,
    EvidenceStore,
    ObservationDraft,
    _drops_qualifier,
    _not_asserted,
)
from gtm_crawler.db.models import Evidence, Observation
from gtm_crawler.db.store import CrawlStore
from gtm_crawler.evidence import (
    Quote,
    QuoteError,
    Source,
    classify_source,
    find_quote,
    observation_confidence,
    quote_block,
    quote_json,
    quote_meta,
    resolve,
)
from gtm_crawler.fetch.http import FetchResult
from gtm_crawler.parse import ParsedPage, parse_page
from gtm_crawler.rawstore import RawStore

URL = "https://acme.example/about"
HTML = b"""<html><head><title>About</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Organization", "name": "Acme Robotics",
 "foundingDate": "2019", "numberOfEmployees": {"@type": "QuantitativeValue", "value": 120}}
</script></head><body>
<h1>About Acme</h1>
<p>Acme Robotics was founded in 2019 in Austin, Texas.</p>
<p>We serve more than 500 customers across North America.</p>
</body></html>"""


def parsed() -> ParsedPage:
    return parse_page(HTML, URL, "text/html", ParseSettings(), UrlSettings())


# --- locators ---------------------------------------------------------------


def test_find_quote_locates_exact_text_with_offsets() -> None:
    page = parsed()
    q = find_quote(page, "more than 500 customers")
    assert q is not None and q.locator_type == "xpath"
    block = next(b for b in page.blocks if b.xpath == q.locator)
    assert block.text[q.char_start : q.char_end] == "more than 500 customers"
    assert resolve(page, q) == q.text
    assert find_quote(page, "more than 600 customers") is None
    assert find_quote(page, "   ") is None


def test_quote_block_rejects_bad_ranges() -> None:
    block = parsed().blocks[1]
    for start, end in [(-1, 3), (5, 5), (3, 2), (0, len(block.text) + 1)]:
        with pytest.raises(QuoteError):
            quote_block(block, start, end)


def test_json_quotes() -> None:
    page = parsed()
    assert quote_json(page, '$.jsonld[0]["foundingDate"]').text == "2019"
    employees = quote_json(page, '$.jsonld[0]["numberOfEmployees"]["value"]')
    assert employees.text == "120" and resolve(page, employees) == "120"
    for bad in [
        '$.jsonld[0]["missing"]',
        '$.jsonld[0]["numberOfEmployees"]',  # not a scalar
        '$.jsonld[3]["name"]',
        "$.jsonld[0].name",
        '$.other[0]["name"]',
    ]:
        with pytest.raises(QuoteError):
            quote_json(page, bad)
    assert resolve(page, Quote("json_path", '$.jsonld[0]["gone"]', "x")) is None


# --- source hierarchy --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "tier", "source_type", "first_party"),
    [
        ("https://acme.com/", 1, "official_website", True),
        ("https://www.acme.com/pricing", 1, "official_pricing", True),
        ("https://docs.acme.com/start", 1, "official_documentation", True),
        ("https://acme.com/newsroom/2024/funding", 1, "official_press_release", True),
        ("https://ir.acme.com/", 1, "official_investor_information", True),
        ("https://acme.com/products/stock", 1, "official_product_page", True),
        ("https://www.sec.gov/cgi-bin/browse-edgar", 1, "regulatory_filing", False),
        ("https://www.crunchbase.com/organization/acme", 2, "business_database", False),
        ("https://www.reuters.com/tech/acme", 2, "major_publication", False),
        ("https://www.linkedin.com/company/acme", 3, "professional_profile", False),
        ("https://www.g2.com/products/acme", 4, "unverified_third_party", False),
        ("https://notreuters.com/x", 4, "unverified_third_party", False),
    ],
)
def test_classify_source(url: str, tier: int, source_type: str, first_party: bool) -> None:
    assert classify_source(url, "acme.com", SourceSettings()) == Source(
        tier, source_type, first_party
    )


@pytest.mark.parametrize(
    ("source", "kind", "expected"),
    [
        (Source(1, "official_pricing", True), "stated", 0.90),
        (Source(1, "regulatory_filing", False), "stated", 0.95),
        (Source(2, "business_database", False), "stated", 0.80),
        (Source(3, "professional_profile", False), "stated", 0.65),
        (Source(4, "unverified_third_party", False), "stated", 0.45),
        (Source(2, "major_publication", False), "derived", 0.72),
    ],
)
def test_observation_confidence(source: Source, kind: str, expected: float) -> None:
    assert observation_confidence(1.0, source, kind) == expected
    assert observation_confidence(0.0, source, kind) == 0.0


def test_company_site_is_third_party_for_another_company() -> None:
    source = classify_source("https://acme.com/pricing", "rival.com", SourceSettings())
    assert source == Source(4, "unverified_third_party", False)


# --- recording and tracing ---------------------------------------------------


@pytest.fixture
def stored(engine: Engine, tmp_path: Path) -> dict[str, Any]:
    settings = Settings()
    rawstore = RawStore(tmp_path / "raw")
    store = CrawlStore(engine)
    org_id, crawl_id = store.start_crawl("https://acme.example/", config_snapshot(settings))
    result = FetchResult(
        url=URL, final_url=URL, status=200, content_type="text/html",
        headers={"content-type": "text/html"}, body=HTML, attempts=1,
    )  # fmt: skip
    fetch_id = store.record_fetch(crawl_id, result, "http", rawstore.put(HTML))
    page = parsed()
    page_id = store.record_page(fetch_id, page, None)
    return {
        "evidence": EvidenceStore(engine, rawstore, SourceSettings()),
        "rawstore": rawstore, "org_id": org_id, "crawl_id": crawl_id,
        "page_id": page_id, "page": page, "fetched_at": result.fetched_at,
    }  # fmt: skip


def draft(page: ParsedPage, needle: str, value: str, **overrides: Any) -> ObservationDraft:
    quote = find_quote(page, needle)
    assert quote is not None
    fields: dict[str, Any] = dict(
        field="customer_claim", value_verbatim=value, quote=quote, extractor="test",
        extractor_version="1", extraction_certainty=0.9,
    )  # fmt: skip
    return ObservationDraft(**{**fields, **overrides})


def record(s: dict[str, Any], *drafts: ObservationDraft) -> list[int]:
    run = s["evidence"].start_extraction(s["crawl_id"])
    ids: list[int] = s["evidence"].record(run, s["org_id"], s["page_id"], list(drafts))
    return ids


@pytest.mark.db
def test_record_and_trace(stored: dict[str, Any]) -> None:
    page = stored["page"]
    claim = draft(
        page, "We serve more than 500 customers", "more than 500 customers",
        value_normalized={"qualifier": "gt", "value": 500},
    )  # fmt: skip
    founded = ObservationDraft(
        field="founded_year", value_verbatim="2019",
        quote=quote_json(page, '$.jsonld[0]["foundingDate"]'), extractor="test",
        extractor_version="1", extraction_certainty=1.0,
    )  # fmt: skip
    claim_id, founded_id = record(stored, claim, founded)

    t = stored["evidence"].trace(claim_id)
    assert t["field"] == "customer_claim" and t["value_verbatim"] == "more than 500 customers"
    assert t["value_normalized"] == {"qualifier": "gt", "value": 500}
    assert t["source"] == {"tier": 1, "type": "official_website", "first_party": True}
    assert t["confidence"] == Decimal("0.81")  # 0.9 certainty x 0.90 first party
    assert t["evidence"]["quote"] == "We serve more than 500 customers"
    assert t["url"] == URL and t["fetch"]["method"] == "http"
    assert t["retrieved_at"] == stored["fetched_at"]
    assert t["crawl"]["id"] == stored["crawl_id"]
    assert stored["evidence"].trace(founded_id)["evidence"]["locator_type"] == "json_path"

    for obs_id in (claim_id, founded_id):
        evidence_id = stored["evidence"].trace(obs_id)["evidence"]["id"]
        assert stored["evidence"].verify(evidence_id) is None


@pytest.mark.db
@pytest.mark.parametrize(
    ("needle", "value", "overrides", "message"),
    [
        # The value is not what the quote says: 500 is not "more than 500".
        ("more than 500 customers", "500 customers", {}, "drops a qualifier"),
        ("more than 500 customers", "600 customers", {}, "not in its quote"),
        ("more than 500 customers", "more than 500 customers",
         {"value_normalized": {"value": 500}}, "qualifier"),
        ("more than 500 customers", "more than 500 customers",
         {"value_normalized": {"qualifier": "exactly", "value": 500}}, "qualifier"),
        ("more than 500 customers", "more than 500 customers",
         {"extraction_certainty": 1.5}, "between"),
        ("more than 500 customers", "more than 500 customers", {"kind": "guessed"}, "kind"),
    ],
)  # fmt: skip
def test_record_refuses_unsupported_observations(
    engine: Engine, stored: dict[str, Any], needle: str, value: str,
    overrides: dict[str, Any], message: str,
) -> None:  # fmt: skip
    before = _count(engine)
    good = draft(stored["page"], "founded in 2019", "2019", field="founded_year")
    with pytest.raises(EvidenceError, match=message):
        record(stored, good, draft(stored["page"], needle, value, **overrides))
    assert _count(engine) == before  # all or nothing


@pytest.mark.db
def test_record_refuses_quotes_not_at_their_location(stored: dict[str, Any]) -> None:
    real = draft(stored["page"], "more than 500 customers", "more than 500 customers")
    q = real.quote
    moved = replace(q, char_start=q.char_start + 1, char_end=q.char_end + 1)  # type: ignore[operator]
    invented = replace(q, text="more than 900 customers")
    elsewhere = replace(q, locator="/html/body/p[9]")
    for quote in (moved, invented, elsewhere):
        with pytest.raises(EvidenceError, match="quote not at"):
            record(stored, replace(real, quote=quote, value_verbatim=quote.text[:4]))


@pytest.mark.parametrize(
    ("value", "quote", "drops"),
    [
        ("500 customers", "We serve more than 500 customers", True),
        ("500", "Over 500+ teams", True),
        ("500", "500+ teams", True),
        ("$20M", "raised approx. $20M", True),
        ("500 customers", "We serve 500 customers", False),
        ("more than 500 customers", "We serve more than 500 customers", False),
        # One unqualified occurrence is enough to support the value.
        ("500", "over 500 teams; exactly 500 in Europe", False),
        ("Moreover", "Moreover, it works", False),
        ("500 rooms", "a handsome 500 rooms", False),
        ("500", "Moreover 500 teams", False),
        # A lowest price is not the price.
        ("$49 per month", "Plans start at $49 per month", True),
        ("start at $49 per month", "Plans start at $49 per month", False),
    ],
)
def test_drops_qualifier(value: str, quote: str, drops: bool) -> None:
    assert _drops_qualifier(value, quote) is drops


@pytest.mark.db
def test_derived_values_need_not_appear_verbatim(stored: dict[str, Any]) -> None:
    (obs_id,) = record(
        stored,
        draft(stored["page"], "founded in 2019 in Austin, Texas", "Austin, TX, US",
              field="headquarters", kind="derived"),
    )  # fmt: skip
    t = stored["evidence"].trace(obs_id)
    assert t["kind"] == "derived" and t["confidence"] == Decimal("0.73")  # 0.9 x 0.9 x 0.9


@pytest.mark.db
def test_verify_detects_changed_or_missing_raw(engine: Engine, stored: dict[str, Any]) -> None:
    (obs_id,) = record(stored, draft(stored["page"], "founded in 2019", "2019"))
    evidence = stored["evidence"]
    evidence_id = evidence.trace(obs_id)["evidence"]["id"]
    digest = evidence.trace(obs_id)["fetch"]["raw_sha256"]
    assert digest in evidence.evidence_raw_digests()

    # An evidence row written around the API, with a quote the page does not contain.
    with engine.begin() as conn:
        forged = conn.execute(
            text(
                "INSERT INTO evidence (page_id, quote, locator_type, locator, char_start, char_end)"
                " SELECT page_id, 'founded in 2020', locator_type, locator, char_start, char_end"
                " FROM evidence WHERE id = :id RETURNING id"
            ),
            {"id": evidence_id},
        ).scalar_one()
    assert "quote differs" in (evidence.verify(forged) or "")

    path = stored["rawstore"]._path(digest)
    path.unlink()
    assert "raw document unavailable" in (evidence.verify(evidence_id) or "")


def _count(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        return (
            conn.execute(select(func.count()).select_from(Evidence)).scalar_one(),
            conn.execute(select(func.count()).select_from(Observation)).scalar_one(),
        )


def test_quote_in_later_run_of_mixed_content_element() -> None:
    doc = b"<html><body><div>Intro text<p>Para</p>Tail with 500 customers</div></body></html>"
    page = parse_page(doc, URL, "text/html", ParseSettings(), UrlSettings())
    q = find_quote(page, "500 customers")
    assert q is not None
    assert [b.xpath for b in page.blocks].count(q.locator) == 2  # two runs share the XPath
    assert resolve(page, q) == "500 customers"


def test_meta_quotes() -> None:
    head = (
        b'<meta property="og:site_name" content="Acme"><meta name="description" content="Robots">'
    )
    page = parse_page(b"<html><head>" + head + b"</head></html>", URL, "text/html",
                      ParseSettings(), UrlSettings())  # fmt: skip
    q = quote_meta(page, "og:site_name")
    assert (q.locator_type, q.locator, q.text) == ("meta", "og:site_name[0]", "Acme")
    assert resolve(page, q) == "Acme"
    assert resolve(page, Quote("meta", "og:site_name[1]", "Acme")) is None
    assert resolve(page, Quote("meta", "garbage", "Acme")) is None
    with pytest.raises(QuoteError):
        quote_meta(page, "og:title")


@pytest.mark.parametrize(
    ("value", "quote", "hedged"),
    [
        ("enterprise expansion", "We are exploring enterprise expansion", True),
        ("Berlin", "We plan to open an office in Berlin", True),
        ("500 customers", "We do not have 500 customers", True),
        ("500 customers", "We don't have 500 customers yet", True),
        ("SOC 2", "We are no longer SOC 2 certified", True),
        ("1,000 customers", "We will reach 1,000 customers next year", True),
        ("$10M", "expected to raise $10M", True),
        # The value's own words are what the page says; nothing is lost.
        ("exploring enterprise expansion", "We are exploring enterprise expansion", False),
        ("500 customers", "We serve 500 customers", False),
        # A hedge in another sentence, or after the value, is about something else.
        ("500 customers", "We will not stop. We serve 500 customers", False),
        ("500 customers", "We serve 500 customers and will grow", False),
        # One plain occurrence supports the value.
        ("Berlin", "We plan to open in Berlin; our Berlin office opened in 2020", False),
        ("2019", "Will Smith founded Acme in 2019", False),
        ("Notion", "Notion is a customer", False),
    ],
)
def test_not_asserted(value: str, quote: str, hedged: bool) -> None:
    assert _not_asserted(value, quote) is hedged
