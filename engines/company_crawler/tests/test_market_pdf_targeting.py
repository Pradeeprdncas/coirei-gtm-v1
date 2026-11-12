"""Page targeting in long PDFs (market/pdf.select_pages, engine.pdf_focus): a document
longer than its page budget is read on its first pages and on the pages the open research
questions point to (outline, contents, else a light text pass), never page 1 to the budget
by default; page locators and quote verification are unchanged.

Seen live (docs/MARKET_BENCHMARK.md, run 7): the RVO "Proteins Study" (84 pages) answered
the business-models question only on its pages 54-55, past the 50-page budget.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from gtm_crawler.config import MarketSettings
from gtm_crawler.market import pdf
from gtm_crawler.market.engine import run_research
from gtm_crawler.market.extract import dimension_patterns
from gtm_crawler.market.models import (
    Budget,
    Dimension,
    Report,
    ResearchMode,
    ResearchRequest,
    RetrievalStatus,
)
from gtm_crawler.market.verify import QuoteCheck, reextract, verify_report
from gtm_crawler.rawstore import RawStore

from .helpers import response
from .market_pdf_fixtures import _wrap
from .pdfs import make_pdf, paragraph
from .test_market import TODAY, settings
from .test_market_selection import ScriptedSearch

pytestmark = pytest.mark.skipif(not pdf.available()[0], reason="pip install -e .[pdf]")

SUBJECT = "plant-based meat"
FILLER = ("The soil survey of county {n} recorded loam and clay in the northern fields, "
          "with rainfall close to the long-run average for the season.")  # fmt: skip
BUSINESS = ("Plant-based meat companies sell through retail and food service distribution "
            "channels, and most brands price their products at a premium per pound.")  # fmt: skip
BUSINESS_2 = ("Direct sales to restaurants are a growing channel for plant-based meat brands, "
              "and private label pricing is lower than branded pricing.")  # fmt: skip
SIZE = ("The United States plant-based meat market was valued at USD 1.4 billion in 2023 "
        "according to retail scanner data.")  # fmt: skip
FOCUS = pdf.Focus(
    terms=["plant-based", "meat"],
    questions={"business_models": dimension_patterns([Dimension.PRICING, Dimension.DISTRIBUTION])},
)


def page(*sentences: str) -> list:
    items, y = [], 760.0
    for s in sentences:
        lines = _wrap(s)
        items += paragraph(56, y, lines)
        y -= 14 * len(lines) + 20
    return items


def long_report(n: int = 84, at: dict[int, list[str]] | None = None, **kw: object) -> bytes:
    """``n`` pages of filler; ``at``: one-based page -> its sentences instead."""
    at = at or {}
    pages = [page(*at.get(i, [FILLER.format(n=i)])) for i in range(1, n + 1)]
    return make_pdf(pages, **kw)  # type: ignore[arg-type]


def extract(data: bytes, max_pages: int, focus: pdf.Focus | None = FOCUS) -> dict:
    return asyncio.run(pdf.extract(data, MarketSettings(), max_pages, focus))


def read(ex: dict) -> list[int]:
    return [p["number"] for p in ex["pages"] if p.get("read", True)]


def test_relevant_pages_beyond_the_page_budget_are_read() -> None:
    ex = extract(long_report(at={54: [BUSINESS], 55: [BUSINESS_2]}), max_pages=10)
    assert {54, 55} <= set(read(ex)) and len(read(ex)) <= 10
    assert ex["targeting"]["method"] == "text"
    assert BUSINESS in ex["pages"][53]["text"]  # page N stays at index N-1
    assert ex["pages"][9]["text"] == "" and ex["pages"][9]["read"] is False
    assert "chosen for the open research questions" in ex["issues"][0]


def test_irrelevant_early_pages_are_not_read_by_default() -> None:
    ex = extract(long_report(at={54: [BUSINESS], 55: [BUSINESS_2]}), max_pages=4)
    assert read(ex) == [1, 2, 54, 55]  # the first pages, then the relevant ones
    # Without a focus (e.g. a competitor's PDF) the legacy read is unchanged.
    plain = extract(long_report(at={54: [BUSINESS]}), max_pages=4, focus=None)
    assert read(plain) == [1, 2, 3, 4] and "targeting" not in plain


def test_a_short_document_is_read_whole() -> None:
    ex = extract(long_report(n=6, at={5: [BUSINESS]}), max_pages=10)
    assert read(ex) == [1, 2, 3, 4, 5, 6] and "targeting" not in ex


def test_contents_page_points_to_its_section_with_unnumbered_front_matter() -> None:
    contents = page("Contents", "Introduction ........ 1", "Soil and climate ........ 5",
                    "Business models and distribution channels ........ 40",
                    "Annex ........ 60")  # fmt: skip
    # Two unnumbered front pages: printed page 40 is the file's page 42.
    body = {42: ["Business models and distribution channels", BUSINESS], 43: [BUSINESS_2]}
    data = make_pdf(
        [page("Proteins study"), contents]
        + [page(*body.get(i, [FILLER.format(n=i)])) for i in range(3, 85)]
    )
    ex = extract(data, max_pages=6)
    assert ex["targeting"]["method"] == "contents + text"
    assert {42, 43} <= set(read(ex)) and len(read(ex)) <= 6
    assert dict(ex["targeting"]["reasons"])[42].startswith("contents section 'Business models")


def test_outline_points_to_its_section() -> None:
    data = long_report(at={54: [BUSINESS], 55: [BUSINESS_2]},
                       outline=[("Soil", 2), ("Business models and pricing", 53),
                                ("Annex", 60)])  # fmt: skip
    ex = extract(data, max_pages=5)
    assert ex["targeting"]["method"] == "outline + text"
    assert {54, 55} <= set(read(ex))


def test_each_open_question_gets_a_page_before_any_gets_a_second() -> None:
    # Many pricing pages, one market-size page: a small grant still reads the size page.
    focus = pdf.Focus(
        terms=["plant-based", "meat"],
        questions={
            "business_models": FOCUS.questions["business_models"],
            "size": dimension_patterns([Dimension.MARKET_SIZE, Dimension.GROWTH]),
        },
    )
    at = {i: [BUSINESS, BUSINESS_2] for i in range(10, 30)} | {70: [SIZE]}
    ex = extract(long_report(at=at), max_pages=4, focus=focus)
    assert 70 in read(ex) and len(read(ex)) == 4
    reasons = dict(ex["targeting"]["reasons"])
    assert "open question 'size'" in reasons[70]


def test_pages_without_text_are_ranked_last_and_reported() -> None:
    data = long_report(at={54: [BUSINESS]}, blank_pages=(0, 54))  # pages 1 and 55
    ex = extract(data, max_pages=4)
    assert 54 in read(ex) and 55 not in read(ex)
    assert any("no text layer" in i or "empty page" in i for i in ex["pages"][0]["issues"])


def test_reextraction_reads_the_same_pages(tmp_path: Path) -> None:
    from gtm_crawler.market.models import SourceRecord

    data = long_report(at={54: [BUSINESS], 55: [BUSINESS_2]})
    ex = extract(data, max_pages=6)
    raw = RawStore(tmp_path)
    sha = raw.put(data)
    ex["pdf_sha256"] = sha
    import json

    stored = raw.put(json.dumps(ex, sort_keys=True, ensure_ascii=False).encode())
    src = SourceRecord(
        url="https://x.example/r.pdf",
        domain="x.example",
        raw_sha256=sha,
        extracted_sha256=stored,
        status=RetrievalStatus.FETCHED,
    )
    again = asyncio.run(reextract(src, raw, MarketSettings()))
    assert again is not None and read(again) == read(ex)
    assert [p["text"] for p in again["pages"]] == [p["text"] for p in ex["pages"]]


# --- through a research run -------------------------------------------------------------

REPORT_URL = "https://stats.example.gov/proteins-study.pdf"
BLOCKED_URL = "https://blocked.example.org/plant-based-outlook.pdf"
OTHER_URL = "https://data.example.org/plant-based-channels.pdf"


class Web:
    def __init__(self, pages: dict[str, bytes], disallow: tuple[str, ...] = ()) -> None:
        self.pages, self.disallow = pages, disallow
        self.requested: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            if any(request.url.host in d for d in self.disallow):
                return response(200, b"User-agent: *\nDisallow: /\n",
                                **{"content-type": "text/plain"})  # fmt: skip
            return response(404)
        self.requested.append(url)
        body = self.pages.get(url)
        if body is None:
            return response(404)
        return response(200, body, **{"content-type": "application/pdf"})


def research(tmp_path: Path, web: Web, urls: list[str], budget: Budget) -> Report:
    req = ResearchRequest(mode=ResearchMode.INDUSTRY, subject=SUBJECT, geography="United States",
                          budget=budget)  # fmt: skip
    search = ScriptedSearch([], default=[(u, "Plant-based meat study") for u in urls])
    return asyncio.run(
        run_research(
            req, settings(tmp_path), search=search, transport=httpx.MockTransport(web), today=TODAY
        )
    )


def test_a_run_reads_pages_54_55_and_their_quotes_verify(tmp_path: Path) -> None:
    data = long_report(at={54: [BUSINESS], 55: [BUSINESS_2], 70: [SIZE]})
    report = research(tmp_path, Web({REPORT_URL: data}), [REPORT_URL],
                      Budget(max_queries=1, max_pdf_pages=8))  # fmt: skip
    src = next(s for s in report.sources if s.url == REPORT_URL)
    assert src.status is RetrievalStatus.FETCHED and "targeted" in (src.status_detail or "")
    pages = {e.locator.page for e in report.evidence if e.locator and e.locator.type == "pdf"}
    assert {54, 55} <= pages and pages <= set(range(1, 85))
    assert report.stats["selection"]["used"]["pdf_pages"] <= 8
    checks = asyncio.run(verify_report(report, RawStore(tmp_path / "raw"), MarketSettings()))
    pdf_checks = [c for eid, c in checks.items()
                  if next(e for e in report.evidence if e.evidence_id == eid).locator.type
                  == "pdf"]  # fmt: skip
    assert pdf_checks and {c.status for c in pdf_checks} == {QuoteCheck.VERIFIED_REEXTRACTED}


def test_a_blocked_pdf_is_recorded_and_an_alternative_is_read(tmp_path: Path) -> None:
    web = Web({BLOCKED_URL: long_report(at={54: [BUSINESS]}),
               OTHER_URL: long_report(n=3, at={2: [BUSINESS_2]})},
              disallow=("blocked.example.org",))  # fmt: skip
    report = research(tmp_path, web, [BLOCKED_URL, OTHER_URL],
                      Budget(max_queries=1, max_pdf_pages=8, max_sources=1))  # fmt: skip
    blocked = next(s for s in report.sources if s.url == BLOCKED_URL)
    assert blocked.status is RetrievalStatus.BLOCKED and BLOCKED_URL not in web.requested
    assert not [e for e in report.evidence if e.source_id == blocked.source_id]
    assert any(e.excerpt == BUSINESS_2 for e in report.evidence)  # the alternative's quote


def test_the_run_page_budget_is_shared_and_exhausted(tmp_path: Path) -> None:
    first = long_report(at={54: [BUSINESS], 55: [BUSINESS_2]})
    second = long_report(at={30: [BUSINESS_2]})
    report = research(tmp_path, Web({REPORT_URL: first, OTHER_URL: second}),
                      [REPORT_URL, OTHER_URL],
                      Budget(max_queries=1, max_pdf_pages=4, max_extraction_pages=4))  # fmt: skip
    used = report.stats["selection"]["used"]["pdf_pages"]
    assert used <= 4
    other = next(s for s in report.sources if s.url == OTHER_URL)
    assert other.status is RetrievalStatus.SKIPPED
    assert "page budget" in (other.status_detail or "")


def test_the_text_pass_stops_at_its_deadline() -> None:
    import io

    import pdfplumber

    data = long_report(at={54: [BUSINESS]}, outline=[("Business models and pricing", 53)])
    with pdfplumber.open(io.BytesIO(data)) as doc:
        got = pdf.select_pages(doc, 4, FOCUS, pdf.SCAN_PAGES, deadline=0.0)  # already past
    assert got["scanned"] == 6  # the first pages only (contents)
    assert 54 in got["pages"]  # ranked by its outline section alone
