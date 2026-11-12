"""PDF extraction quality on a small labelled fixture set (docs/MARKET_ENGINE_IMPLEMENTATION.md,
"PDF evidence"). Five hand-made documents with known text, figures, tables and unreadable
pages; the measured values are asserted so a regression shows. They describe these
fixtures only: real PDFs (scans, multi-column reports, complex tables) vary far more.
"""

import asyncio
from datetime import date
from typing import Any

import pytest

from gtm_crawler.config import MarketSettings
from gtm_crawler.market import pdf
from gtm_crawler.market.extract import (
    extract_evidence,
    pdf_locators,
    pdf_page,
    pdf_table_evidence,
    subject_terms,
)
from gtm_crawler.market.models import MetricKind, RetrievalStatus, SourceRecord
from gtm_crawler.market.verify import QuoteCheck, verify_evidence
from gtm_crawler.rawstore import RawStore

from .market_pdf_fixtures import (
    LEFT_COLUMN,
    REPORT_FIGURES,
    REPORT_TEXT,
    RIGHT_COLUMN,
    TABLE_ROWS,
    UNRULED_ROWS,
    irregular_table_pdf,
    report_pdf,
    scanned_pdf,
    two_column_pdf,
    unruled_table_pdf,
)

pytestmark = pytest.mark.skipif(not pdf.available()[0], reason="pip install -e .[pdf]")
TODAY = date(2026, 10, 20)
TERMS = subject_terms("EV charging infrastructure")
VOCAB = TERMS | {"ev", "electric", "vehicle", "charging", "infrastructure"}


def _read(data: bytes, raw: RawStore) -> tuple[dict[str, Any], SourceRecord, list[Any]]:
    x = asyncio.run(pdf.extract(data, MarketSettings()))
    source = SourceRecord(url="https://fixture.example/doc", domain="fixture.example",
                          status=RetrievalStatus.FETCHED, raw_sha256=raw.put(data))  # fmt: skip
    import json

    x["pdf_sha256"] = source.raw_sha256
    source.extracted_sha256 = raw.put(json.dumps(x, sort_keys=True).encode())
    evidence = extract_evidence(pdf_page(x, source.url), source.source_id, TERMS, today=TODAY,
                                vocabulary=VOCAB)  # fmt: skip
    pdf_locators(evidence, x["version"])
    evidence += pdf_table_evidence(x, source.source_id, TERMS, today=TODAY, vocabulary=VOCAB)
    return x, source, evidence


def test_extraction_quality_on_the_labelled_set(tmp_path: Any) -> None:
    raw = RawStore(tmp_path / "raw")
    measured: dict[str, tuple[int, int]] = {}

    # Text: each labelled passage verbatim on its labelled page.
    report, source, evidence = _read(report_pdf(), raw)
    columns, _, _ = _read(two_column_pdf(), raw)
    passages = [(report, page, text) for page, text in REPORT_TEXT] + [
        (columns, 1, LEFT_COLUMN), (columns, 1, RIGHT_COLUMN)]  # fmt: skip
    measured["text passages on their page"] = (
        sum(text in x["pages"][page - 1]["text"] for x, page, text in passages),
        len(passages),
    )

    # Figures: value, currency, year and geography, on the labelled page.
    got = {(e.locator.page, m.value, m.unit, m.year, m.geography) for e in evidence
           for m in e.metrics if m.kind is MetricKind.MARKET_SIZE}  # fmt: skip
    measured["figures with value, unit, year, place"] = (
        sum(f in got for f in REPORT_FIGURES),
        len(REPORT_FIGURES),
    )
    measured["figures not in the labels (false)"] = (len(got - set(REPORT_FIGURES)), 0)

    # Page locators and quote verification (re-extracting the stored file).
    again = {**asyncio.run(pdf.extract(raw.get(source.raw_sha256), MarketSettings())),
             "pdf_sha256": source.raw_sha256}  # fmt: skip
    checks = [verify_evidence(e, source, raw, reextraction=again) for e in evidence]
    labelled_page = {text: page for page, text in REPORT_TEXT}
    on_page = [e for e in evidence if e.excerpt in labelled_page]
    measured["locators on the labelled page"] = (
        sum(e.locator.page == labelled_page[e.excerpt] for e in on_page),
        len(on_page),
    )
    measured["quotes verified by re-extraction"] = (
        sum(c.status is QuoteCheck.VERIFIED_REEXTRACTED for c in checks),
        len(checks),
    )

    # Tables: ruled ones reconstructed cell for cell; the irregular one flagged; the
    # unruled one is not detected (a known limitation, measured, not hidden).
    ruled = [b["table"] for b in report["pages"][1]["blocks"] if b["kind"] == "table"]
    irregular, _, _ = _read(irregular_table_pdf(), raw)
    unruled, _, unruled_ev = _read(unruled_table_pdf(), raw)
    measured["ruled tables reconstructed exactly"] = (
        sum(t["rows"] == TABLE_ROWS for t in ruled),
        1,
    )
    measured["irregular tables flagged"] = (
        sum("irregular" in i for i in irregular["pages"][0]["issues"]),
        1,
    )
    unruled_tables = [b for b in unruled["pages"][0]["blocks"] if b["kind"] == "table"]
    measured["unruled tables detected"] = (len(unruled_tables), 1)
    rows_as_lines = [" ".join(r) for r in UNRULED_ROWS]
    measured["unruled rows kept as text lines"] = (
        sum(r in unruled["pages"][0]["text"] for r in rows_as_lines),
        len(rows_as_lines),
    )
    measured["figures from the unruled table"] = (sum(len(e.metrics) for e in unruled_ev), 4)

    # Unreadable pages: the image-only page is reported, not skipped silently.
    scanned, _, _ = _read(scanned_pdf(), raw)
    measured["image-only pages reported"] = (
        sum("image-only" in i for p in scanned["pages"] for i in p["issues"]),
        1,
    )

    print("\nPDF extraction quality (labelled fixtures):")
    for name, (hit, total) in measured.items():
        print(f"  {name}: {hit}/{total}")
    assert measured == {
        "text passages on their page": (9, 9),
        "figures with value, unit, year, place": (8, 8),
        "figures not in the labels (false)": (0, 0),
        "locators on the labelled page": (len(on_page), len(on_page)),
        "quotes verified by re-extraction": (len(checks), len(checks)),
        "ruled tables reconstructed exactly": (1, 1),
        "irregular tables flagged": (1, 1),
        "unruled tables detected": (0, 1),  # limitation: only ruled tables are found
        "unruled rows kept as text lines": (3, 3),
        "figures from the unruled table": (0, 4),  # so its figures are not parsed
        "image-only pages reported": (1, 1),
    }
