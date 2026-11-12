"""PDF evidence (market/pdf.py, extract.pdf_*, verify.py): detection, isolated
extraction, page-level provenance, re-checkable quotes and use in the analysis.

Fixtures are written by hand (tests/pdfs.py) and served by a scripted web; the
extraction runs in its real child process. Needs ``pip install -e .[pdf]``.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from gtm_crawler.config import MarketSettings
from gtm_crawler.market import benchmark, pdf
from gtm_crawler.market.collect import Collector
from gtm_crawler.market.engine import run_research
from gtm_crawler.market.llm import retrieve
from gtm_crawler.market.models import (
    Budget,
    Competitor,
    EvidenceRecord,
    Locator,
    MetricKind,
    ProductInput,
    Report,
    ResearchMode,
    ResearchRequest,
    RetrievalStatus,
    SizingRequest,
    SourceRecord,
)
from gtm_crawler.market.report import to_markdown
from gtm_crawler.market.verify import QuoteCheck, reextract, verify_evidence, verify_report
from gtm_crawler.rawstore import RawStore

from .helpers import response
from .market_pdf_fixtures import (
    BARRIER_SENTENCE,
    GLOBAL_SENTENCE,
    PRICE_SENTENCE,
    REPORT_FIGURES,
    irregular_table_pdf,
    report_pdf,
    scanned_pdf,
)
from .test_market import TODAY, FakeSearch, html, settings

pytestmark = pytest.mark.skipif(not pdf.available()[0], reason="pip install -e .[pdf]")

PDF_HEADERS = {"content-type": "application/pdf"}
REPORT = report_pdf()


def web(extra: dict[str, tuple[bytes, dict[str, str]]] | None = None) -> httpx.MockTransport:
    pages: dict[str, tuple[bytes, dict[str, str]]] = {
        # A PDF whose URL has no .pdf extension.
        "https://stats.example.eu/outlook": (REPORT, PDF_HEADERS),
        # A .pdf URL that serves HTML.
        "https://news.example.org/brochure.pdf": (html(
            "EV charging infrastructure brochure",
            "Demand for EV charging infrastructure is growing fast across Europe, driven by "
            "new deployment targets."), {"content-type": "text/html; charset=utf-8"}),
        "https://broken.example.org/report.pdf": (REPORT[:600], PDF_HEADERS),
        "https://locked.example.org/secure": (pdf_encrypted(), {
            "content-type": "application/octet-stream"}),
        "https://junk.example.org/file": (b"\x00\x01binary, not a document" * 10, {
            "content-type": "application/octet-stream"}),
        "https://scan.example.org/scan.pdf": (scanned_only(), PDF_HEADERS),
    }  # fmt: skip
    pages.update(extra or {})

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return response(404)
        if url == "https://big.example.org/huge.pdf":  # 50 kB: over a 20 kB download cap
            return response(200, REPORT + b"%" * 50_000, **PDF_HEADERS)
        if url in pages:
            body, headers = pages[url]
            return response(200, body, **headers)
        return response(404)

    return httpx.MockTransport(handler)


def pdf_encrypted() -> bytes:
    from .pdfs import Text, make_pdf

    return make_pdf([[Text(56, 780, 12, "Confidential EV charging infrastructure report")]],
                    encrypt=True)  # fmt: skip


def scanned_only() -> bytes:
    from .pdfs import Image, make_pdf

    return make_pdf([[Image(50, 300, 400, 400)]])


def collect(tmp_path: Path, url: str, market: MarketSettings | None = None,
            transport: httpx.MockTransport | None = None,
            budget: Budget | None = None) -> Any:  # fmt: skip
    s = settings(tmp_path)
    if market is not None:
        s = replace(s, market=market)

    async def go() -> Any:
        c = Collector(s, budget or Budget(), transport=transport or web())
        try:
            return await c.collect(url, "test")
        finally:
            await c.aclose()

    return asyncio.run(go())


def industry_run(tmp_path: Path, urls: list[str], **kw: Any) -> Report:
    req = ResearchRequest(mode=ResearchMode.INDUSTRY, subject="EV charging infrastructure",
                          industry="electric vehicle charging", geography="Europe",
                          sizing=SizingRequest(market_definition="EV charging infrastructure "
                                               "in Europe", year=2023, currency="EUR"),
                          budget=Budget(max_queries=1), **kw)  # fmt: skip
    return asyncio.run(run_research(req, settings(tmp_path), search=FakeSearch(urls=urls,
                                    fail=None), transport=web(), today=TODAY))  # fmt: skip


# --- detection and extraction ----------------------------------------------------------


def test_detection_is_by_signature_not_url() -> None:
    assert pdf.is_pdf(REPORT) and pdf.is_pdf(b"\n" * 500 + REPORT)
    assert not pdf.is_pdf(b"<html>%PDF-</html>"[:5]) and not pdf.is_pdf(b"x" * 2000 + REPORT)


def test_extraction_keeps_pages_blocks_tables_and_metadata() -> None:
    x = asyncio.run(pdf.extract(REPORT, MarketSettings()))
    assert [p["number"] for p in x["pages"]] == [1, 2, 3] and x["page_count"] == 3
    assert x["metadata"]["title"] == "EV Charging Infrastructure Outlook 2024"
    assert x["metadata"]["created"] == "2024-03-15T12:00:00+00:00"
    assert set(x["libraries"]) == {"pdfplumber", "pdfminer.six"}
    p1, p2 = x["pages"][0], x["pages"][1]
    assert [b["kind"] for b in p1["blocks"]] == ["heading", "paragraph", "paragraph"]
    for b in p1["blocks"]:  # offsets index the page text
        assert p1["text"][b["start"] : b["end"]].strip() == p1["text"][b["start"] : b["end"]]
    table = next(b["table"] for b in p2["blocks"] if b["kind"] == "table")
    assert table["rows"][0] == ["Country", "2022", "2023"] and table["regular"]
    assert table["unit"] == {"currency": "EUR", "scale": "billion", "text": "EUR billion"}
    assert table["caption"].startswith("Table 1:")


def test_unreadable_pages_and_irregular_tables_are_said() -> None:
    x = asyncio.run(pdf.extract(scanned_pdf(), MarketSettings()))
    assert x["pages"][1]["issues"] == ["image-only page: no text layer (PDF OCR is not "
                                       "implemented)"]  # fmt: skip
    y = asyncio.run(pdf.extract(irregular_table_pdf(), MarketSettings()))
    assert "irregular" in y["pages"][0]["issues"][0]
    assert not next(b["table"] for b in y["pages"][0]["blocks"] if b["kind"] == "table")["regular"]


@pytest.mark.parametrize(("data", "code"), [
    (b"<html><body>not a pdf</body></html>", "not_pdf"),
    (REPORT[:600], "malformed"),
    (pdf_encrypted(), "encrypted"),
])  # fmt: skip
def test_bad_files_are_refused_with_a_reason(data: bytes, code: str) -> None:
    with pytest.raises(pdf.PdfError) as err:
        asyncio.run(pdf.extract(data, MarketSettings()))
    assert err.value.code == code


def test_non_ascii_text_crosses_the_process_boundary() -> None:
    """Found live: every real PDF failed on Windows because the child wrote its JSON in
    the console code page (cp1252): "₹", "≤", "●" crashed it, "é" / "€" broke decoding."""
    from .pdfs import Text, make_pdf

    doc = make_pdf([[Text(56, 780, 11, "The café market reached € 4.2 billion in 2023.")]])
    x = asyncio.run(pdf.extract(doc, MarketSettings()))
    assert x["pages"][0]["text"] == "The café market reached € 4.2 billion in 2023."


def test_child_output_is_utf8_whatever_the_console_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    monkeypatch.setattr(pdf, "extract_bytes", lambda *a: {"pages": [{"text": "₹ 5 crore ≤ ●"}]})
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # a Windows console pipe
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"%PDF-1.4")))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["pdf", "1", "1", "64", "1"])
    assert pdf.main() == 0
    sent = json.loads(out.buffer.getvalue().decode("utf-8"))  # type: ignore[attr-defined]
    assert sent["result"]["pages"][0]["text"] == "₹ 5 crore ≤ ●"


def test_limits_size_pages_and_time() -> None:
    with pytest.raises(pdf.PdfError, match="too_large"):
        asyncio.run(pdf.extract(REPORT, MarketSettings(pdf_max_bytes=1000)))
    x = asyncio.run(pdf.extract(REPORT, MarketSettings(pdf_max_pages=1)))
    assert len(x["pages"]) == 1 and x["issues"] == ["read 1 of 3 pages (market.pdf_max_pages)"]
    with pytest.raises(pdf.PdfError, match="timeout"):  # the child is killed
        asyncio.run(pdf.extract(REPORT, MarketSettings(pdf_timeout=0.001)))


# --- collection --------------------------------------------------------------------------


def test_collection_routes_by_content_not_by_extension(tmp_path: Path) -> None:
    got = collect(tmp_path, "https://stats.example.eu/outlook")
    s = got.source
    assert s.status is RetrievalStatus.FETCHED and got.pdf is not None
    assert s.status_detail == "pdf: 3 of 3 page(s) read" and s.raw_sha256 and s.extracted_sha256
    assert s.title == "EV Charging Infrastructure Outlook 2024"
    assert s.published_at is not None and s.published_at.year == 2024
    assert any("PDF's metadata" in n for n in s.limitations)
    stored = json.loads(RawStore(tmp_path / "raw").get(s.extracted_sha256))
    assert stored["pdf_sha256"] == s.raw_sha256  # the text is bound to this exact file
    html_pdf = collect(tmp_path, "https://news.example.org/brochure.pdf")
    assert html_pdf.source.status is RetrievalStatus.FETCHED and html_pdf.pdf is None


@pytest.mark.parametrize(("url", "status", "detail"), [
    ("https://broken.example.org/report.pdf", RetrievalStatus.UNREADABLE, "pdf_malformed"),
    ("https://locked.example.org/secure", RetrievalStatus.UNREADABLE, "pdf_encrypted"),
    ("https://junk.example.org/file", RetrievalStatus.UNREADABLE, "pdf_not_pdf"),
    ("https://scan.example.org/scan.pdf", RetrievalStatus.UNREADABLE, "pdf_no_text"),
])  # fmt: skip
def test_collection_refusals(tmp_path: Path, url: str, status: RetrievalStatus,
                             detail: str) -> None:  # fmt: skip
    s = collect(tmp_path, url).source
    assert s.status is status and (s.status_detail or "").startswith(detail), s.status_detail


def test_oversized_pdfs_are_refused(tmp_path: Path) -> None:
    small = MarketSettings(pdf_max_bytes=20_000)
    # Over the download cap (max of the page budget and pdf_max_bytes): never downloaded whole.
    s = collect(tmp_path, "https://big.example.org/huge.pdf", small,
                budget=Budget(max_content_bytes=10_000)).source  # fmt: skip
    assert s.status is RetrievalStatus.FAILED and s.status_detail == "too_large"
    # Downloaded within the page budget, but over pdf_max_bytes: not extracted.
    s = collect(tmp_path, "https://big.example.org/huge.pdf", small).source
    assert s.status is RetrievalStatus.UNREADABLE and s.status_detail.startswith("pdf_too_large")


def test_extraction_timeout_is_recorded(tmp_path: Path) -> None:
    s = collect(tmp_path, "https://stats.example.eu/outlook",
                MarketSettings(pdf_timeout=0.001)).source  # fmt: skip
    assert s.status is RetrievalStatus.UNREADABLE and s.status_detail.startswith("pdf_timeout")
    assert s.raw_sha256  # the file is kept even when it could not be read


def test_missing_optional_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "available", lambda: (False, "PDF support not installed (pip "
                                                          "install -e .[pdf])"))  # fmt: skip
    s = collect(tmp_path, "https://stats.example.eu/outlook").source
    assert s.status is RetrievalStatus.UNREADABLE and s.status_detail.startswith("pdf_unavailable")
    report = industry_run(tmp_path, ["https://broken.example.org/report.pdf"])
    skipped = next(x for x in report.sources if x.url == "https://broken.example.org/report.pdf")
    assert skipped.status is RetrievalStatus.SKIPPED
    assert (
        skipped.status_detail == "PDF not read: PDF support not installed (pip install -e .[pdf])"
    )


def test_pdf_disabled(tmp_path: Path) -> None:
    s = collect(tmp_path, "https://stats.example.eu/outlook",
                MarketSettings(pdf_enabled=False)).source  # fmt: skip
    # Without PDF types allowed, the fetcher refuses the body by its declared type.
    assert s.status is RetrievalStatus.FAILED and s.status_detail == "unsupported_content_type"


# --- evidence, locators and verification ---------------------------------------------


@pytest.fixture
def pdf_report(tmp_path: Path) -> Report:
    return industry_run(tmp_path, ["https://stats.example.eu/outlook",
                                   "https://news.example.org/brochure.pdf"])  # fmt: skip


def _pdf_evidence(report: Report) -> list[EvidenceRecord]:
    return [e for e in report.evidence if e.locator and e.locator.type == "pdf"]


def test_quotes_carry_their_page(pdf_report: Report) -> None:
    found = {e.excerpt: e.locator for e in _pdf_evidence(pdf_report)}
    assert found[GLOBAL_SENTENCE].page == 1 and found[GLOBAL_SENTENCE].value == "page=1"
    assert found[PRICE_SENTENCE].page == 3 and found[BARRIER_SENTENCE].page == 3
    row = found["Germany | 3.1 | 3.6"]
    assert (row.page, row.table, row.row, row.value) == (2, 0, 1, "page=2;table=0;row=1")
    assert all(loc.version == pdf.PDF_TEXT_VERSION for loc in found.values())


def test_every_pdf_quote_reverifies_from_the_stored_file(pdf_report: Report,
                                                         tmp_path: Path) -> None:  # fmt: skip
    checks = asyncio.run(verify_report(pdf_report, RawStore(tmp_path / "raw"), MarketSettings()))
    pdf_ids = {e.evidence_id for e in _pdf_evidence(pdf_report)}
    assert pdf_ids and {checks[i].status for i in pdf_ids} == {QuoteCheck.VERIFIED_REEXTRACTED}
    html_ids = {e.evidence_id for e in pdf_report.evidence if e.locator.type == "xpath"}
    assert html_ids and {checks[i].status for i in html_ids} == {QuoteCheck.VERIFIED_REEXTRACTED}


def _one(report: Report, excerpt: str) -> tuple[EvidenceRecord, SourceRecord]:
    e = next(e for e in report.evidence if e.excerpt == excerpt)
    return e, next(s for s in report.sources if s.source_id == e.source_id)


def test_wrong_locations_and_quotes_are_mismatches(pdf_report: Report, tmp_path: Path) -> None:
    raw = RawStore(tmp_path / "raw")
    e, s = _one(pdf_report, GLOBAL_SENTENCE)
    assert verify_evidence(e, s, raw).status is QuoteCheck.VERIFIED_STORED  # not re-extracted

    def check(**change: Any) -> Any:
        loc = e.locator.model_copy(update={k: v for k, v in change.items() if k != "excerpt"})
        bad = e.model_copy(update={"locator": loc, "excerpt": change.get("excerpt", e.excerpt)})
        return verify_evidence(bad, s, raw)

    wrong_page = check(page=3, value="page=3")
    assert (
        wrong_page.status is QuoteCheck.MISMATCH and "page(s) [1], not page 3" in wrong_page.detail
    )
    shifted = check(char_start=e.locator.char_start + 1, char_end=e.locator.char_end + 1)
    assert shifted.status is QuoteCheck.MISMATCH and "not" in shifted.detail
    assert "on page 1 at" in shifted.detail  # it is on the page, but not at those offsets
    edited = check(excerpt=GLOBAL_SENTENCE.replace("41.5", "45.1"))
    assert edited.status is QuoteCheck.MISMATCH and edited.detail == "quote is not on page 1"
    assert check(page=9).status is QuoteCheck.MISMATCH
    assert check(version="pdf-text-0").status is QuoteCheck.NOT_VERIFIABLE
    row, rs = _one(pdf_report, "Germany | 3.1 | 3.6")
    other_row = row.model_copy(update={"locator": row.locator.model_copy(update={"row": 2})})
    assert verify_evidence(other_row, rs, raw).status is QuoteCheck.MISMATCH


def test_missing_and_foreign_artifacts_are_not_verifiable(pdf_report: Report,
                                                          tmp_path: Path) -> None:  # fmt: skip
    raw = RawStore(tmp_path / "raw")
    e, s = _one(pdf_report, GLOBAL_SENTENCE)
    gone = s.model_copy(update={"extracted_sha256": "0" * 64})
    assert verify_evidence(e, gone, raw).status is QuoteCheck.NOT_VERIFIABLE
    # An extraction stored for another file never vouches for this one.
    other = raw.put(json.dumps({**json.loads(raw.get(s.extracted_sha256)),
                                "pdf_sha256": "f" * 64}).encode())  # fmt: skip
    foreign = s.model_copy(update={"extracted_sha256": other})
    assert verify_evidence(e, foreign, raw).status is QuoteCheck.NOT_VERIFIABLE
    assert verify_evidence(e.model_copy(update={"locator": None}), s, raw).status \
        is QuoteCheck.NOT_VERIFIABLE  # fmt: skip


def test_stored_text_that_disagrees_with_the_file_is_caught(pdf_report: Report,
                                                            tmp_path: Path) -> None:  # fmt: skip
    """A tampered stored extraction still contains the quote, but re-extracting the
    stored file does not: mismatch."""
    raw = RawStore(tmp_path / "raw")
    e, s = _one(pdf_report, GLOBAL_SENTENCE)
    stored = json.loads(raw.get(s.extracted_sha256))
    stored["pages"][0]["text"] = stored["pages"][0]["text"].replace("41.5", "99.9")
    fake = e.excerpt.replace("41.5", "99.9")
    tampered = s.model_copy(update={"extracted_sha256": raw.put(json.dumps(stored).encode())})
    again = asyncio.run(reextract(s, raw, MarketSettings()))
    got = verify_evidence(e.model_copy(update={"excerpt": fake}), tampered, raw,
                          reextraction=again)  # fmt: skip
    assert got.status is QuoteCheck.MISMATCH and "re-extraction of the stored file" in got.detail
    # Another library version: the stored check stands, and the detail says why no more.
    other = {**again, "libraries": {"pdfplumber": "0.0.1", "pdfminer.six": "1"}}
    v = verify_evidence(e, s, raw, reextraction=other)
    assert v.status is QuoteCheck.VERIFIED_STORED and "not comparable" in v.detail


def test_a_changed_document_never_validates_old_evidence(tmp_path: Path) -> None:
    """The URL later serves another edition: the old evidence still verifies against the
    stored original, and a check against the live document reports the change."""
    url = "https://stats.example.eu/outlook"
    first = collect(tmp_path, url)
    later = collect(tmp_path, url, transport=web({url: (report_pdf(changed=True), PDF_HEADERS)}))
    assert first.source.raw_sha256 != later.source.raw_sha256
    raw = RawStore(tmp_path / "raw")
    from gtm_crawler.market.extract import extract_evidence, pdf_locators, subject_terms

    old = extract_evidence(first.page, first.source.source_id, subject_terms("EV charging"),
                           today=TODAY)  # fmt: skip
    pdf_locators(old, first.pdf["version"])
    quote = next(e for e in old if "12.4" in e.excerpt)
    assert verify_evidence(quote, first.source, raw).status is QuoteCheck.VERIFIED_STORED
    changed = verify_evidence(quote, first.source, raw, current_sha256=later.source.raw_sha256)
    assert changed.status is QuoteCheck.SOURCE_CHANGED
    assert "against the stored copy: verified_stored" in changed.detail
    # The new edition's text does not contain the old quote at all.
    assert "12.4" not in later.pdf["pages"][0]["text"] and "13.9" in later.pdf["pages"][0]["text"]


# --- use in the analysis ---------------------------------------------------------------


def test_table_and_text_figures_keep_scope_and_sizing_obeys_m7(pdf_report: Report) -> None:
    figures = {(round(m.value), m.unit, m.year, m.geography) for e in _pdf_evidence(pdf_report)
               for m in e.metrics if m.kind is MetricKind.MARKET_SIZE}  # fmt: skip
    expected = {(round(v), c, y, g) for _, v, c, y, g in REPORT_FIGURES}
    assert figures == expected
    assert pdf_report.sizing is not None
    td = pdf_report.sizing.results[0]
    # The European 2023 total (text and table, one source) - never the global or a country.
    assert td.base.tam == 12.4e9
    reasons = {x.figure.split(" for ")[0]: x.reason for x in pdf_report.sizing.excluded}
    assert reasons["EUR 41.5 billion"] == "geography Global, not Europe"
    assert reasons["EUR 3.6 billion"] == "geography Germany, not Europe"


def test_report_cites_pages_and_retrieval_finds_pdf_evidence(pdf_report: Report) -> None:
    md = to_markdown(pdf_report)
    assert "p. 1" in md and "p. 2" in md and "<https://stats.example.eu/outlook>" in md
    hits = retrieve(pdf_report.evidence, "Germany EV charging market 2023", 3)
    assert hits[0][0].excerpt == "Germany | 3.1 | 3.6" and hits[0][0].locator.page == 2


def test_product_analysis_uses_pdf_evidence(tmp_path: Path) -> None:
    req = ResearchRequest(
        mode=ResearchMode.PRODUCT, subject="ChargeHub", industry="EV charging infrastructure",
        budget=Budget(max_queries=1),
        product=ProductInput(name="ChargeHub", problem="small operators find charging costly",
                             known_competitors=[Competitor(name="Outlook",
                                                           url="https://stats.example.eu/outlook")]),
    )  # fmt: skip
    report = asyncio.run(run_research(req, settings(tmp_path), search=FakeSearch(urls=[],
                                      fail=None), transport=web(), today=TODAY))  # fmt: skip
    pricing = [f for f in report.sections["pricing"].findings]
    assert any("EUR" in f.statement for f in pricing)
    price = next(e for e in report.evidence if e.excerpt == PRICE_SENTENCE)
    assert price.locator.page == 3 and price.metrics[0].kind is MetricKind.PRICE_POINT
    assert report.sections["barriers"].findings


def test_extraction_success_is_not_verification(pdf_report: Report) -> None:
    """A PDF figure is a source's statement: the total from it stays unverified (one
    source), whatever the extraction quality."""
    assert pdf_report.sizing is not None and not pdf_report.sizing.results[0].verified
    europe = next(m for m in pdf_report.metrics if m.geography == "Europe" and m.year == 2023
                  and m.kind is MetricKind.MARKET_SIZE)  # fmt: skip
    assert europe.independent_domains == 1


def test_benchmark_gates_pass_on_a_pdf_run(pdf_report: Report, tmp_path: Path) -> None:
    result = benchmark.evaluate_report(pdf_report, RawStore(tmp_path / "raw"), {"id": "pdf"})
    assert result["gates_passed"], {k: g for k, g in result["gates"].items() if not g["pass"]}
    assert result["gates"]["provenance"]["by_status"] == {"verified_reextracted": result[
        "gates"]["provenance"]["checked"]}  # fmt: skip
    # A table figure whose cell is changed fails the numeric gate.
    bad = pdf_report.model_copy(deep=True)
    row = next(e for e in bad.evidence if e.excerpt == "Germany | 3.1 | 3.6")
    row.metrics[0].value = 9.9e9
    assert not benchmark.numeric(bad, RawStore(tmp_path / "raw"))["pass"]


def test_chart_labels_are_not_prices() -> None:
    """Found live (RVO Proteins Study, p. 27): a chart's labels, flattened to text, gave
    five 'price points' because the price rule's "plan" matched "Plant-based"."""
    from gtm_crawler.market.extract import parse_metrics

    labels = ("21% $500 10% 10% $377 6% 0% -11% $128 $64 $10 $- -50% Plant-based Milk Other "
              "plant-based Plant-based meat Plant-basd meals Tofu and tempeh")  # fmt: skip
    assert [m for m in parse_metrics(labels, TODAY) if m.kind is MetricKind.PRICE_POINT] == []
    feedback = "Customers gave feedback worth $150 in vouchers during the trial programme."
    assert [m for m in parse_metrics(feedback, TODAY) if m.kind is MetricKind.PRICE_POINT] == []
    plan = "The Pro plan costs $29 per user per month for growing sales teams."
    assert [(m.value, m.unit) for m in parse_metrics(plan, TODAY)] == [(29.0, "USD/user/month")]


def test_prose_in_table_cells_is_not_a_row(tmp_path: Path) -> None:
    """Found live: a table whose cells held paragraphs gave a 2,000+ character "row";
    its record failed validation and the exception ended the whole run."""
    from gtm_crawler.market.extract import pdf_table_evidence, subject_terms

    long_cell = "Characterization of plant-based protein ingredients " * 50
    rows = [["Step", "2022", "2023"], ["Extrusion", "1.0", "1.2"], [long_cell, "x", "y"]]
    text = "\n".join(" | ".join(r) for r in rows)
    extraction = {"version": pdf.PDF_TEXT_VERSION, "metadata": {}, "pages": [{
        "number": 1, "text": text, "issues": [], "blocks": [{
            "kind": "table", "start": 0, "end": len(text), "table": {
                "index": 0, "rows": rows, "regular": True, "unit": None,
                "caption": "Plant-based meat industry processing steps"}}]}]}  # fmt: skip
    got = pdf_table_evidence(extraction, "s", subject_terms("plant-based meat"))
    assert [
        e.excerpt for e in got
    ] == []  # the short row has no figure rule, the long one is skipped


def test_one_source_failing_extraction_does_not_end_the_run(tmp_path: Path,
                                                             monkeypatch: Any) -> None:  # fmt: skip
    from gtm_crawler.market import engine

    real = engine._extract

    def flaky(c: Any, *a: Any) -> Any:
        if c.pdf is not None:
            raise ValueError("boom")
        return real(c, *a)

    monkeypatch.setattr(engine, "_extract", flaky)
    report = industry_run(tmp_path, ["https://stats.example.eu/outlook",
                                     "https://news.example.org/brochure.pdf"])  # fmt: skip
    outlook = next(s for s in report.sources if s.url == "https://stats.example.eu/outlook")
    assert "extraction failed: ValueError" in outlook.limitations
    assert any("extraction failed" in i.message for i in report.issues)
    assert report.evidence  # the HTML page's evidence is still there


def test_pdf_locator_validation() -> None:
    with pytest.raises(ValueError):
        Locator(type="pdf", value="page=0", page=0)
    assert Locator(type="pdf", value="page=2", page=2, table=0, row=1).row == 1
