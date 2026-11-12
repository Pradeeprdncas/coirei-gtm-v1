"""Re-checking that a quote is where its evidence record says, in what was collected.

``verify_evidence`` returns one of:

* ``verified_reextracted``: the quote re-derives at its location from the
  stored artifact itself: an HTML page re-parsed from its raw bytes
  (``evidence.resolve``: an element's text or a JSON-LD value), or a PDF
  re-extracted from the stored file with the same extraction version and
  library versions;
* ``verified_stored``: (PDF) the quote is at its page and offsets in the
  extraction persisted at collection, but the stored file was not
  re-extracted, or the re-extraction is not comparable (another
  ``PDF_TEXT_VERSION`` or library version; both are said);
* ``mismatch``: the artifacts are there and the quote is not at its location
  (the detail says where it is, if anywhere: another offset or page is still a
  mismatch); or the persisted text disagrees with a re-extraction of the same
  file;
* ``not_verifiable``: no locator of a checkable kind, an artifact missing or
  damaged (the raw store checks every hash), the extraction belonging to
  another file, or a locator from another extraction version;
* ``source_changed``: the document now served at the URL differs from the one
  collected (by hash); the check against the stored copy is in the detail.

A price must also hold together: each price's own text is in the excerpt, and
each quote that gave it its terms (a plan's heading, a card's "per user /
month", a table's row and column labels, a JSON-LD priceCurrency) is at its
own location; otherwise the record is a ``mismatch``.

Every check uses the artifacts the record names by hash (``raw_sha256``,
``extracted_sha256``), never the URL: a document changed since collection
can never confirm, or silently replace, the evidence taken from the old one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..config import MarketSettings, ParseSettings, UrlSettings
from ..evidence import Quote, resolve
from ..parse import ParsedPage, parse_page
from ..rawstore import RawStoreBackend, RawStoreError
from . import pdf
from .collect import parse_settings
from .models import EvidenceRecord, Report, SourceRecord


class QuoteCheck(StrEnum):
    VERIFIED_REEXTRACTED = "verified_reextracted"
    VERIFIED_STORED = "verified_stored"
    MISMATCH = "mismatch"
    NOT_VERIFIABLE = "not_verifiable"
    SOURCE_CHANGED = "source_changed"


VERIFIED = (QuoteCheck.VERIFIED_REEXTRACTED, QuoteCheck.VERIFIED_STORED)


@dataclass(frozen=True)
class Verification:
    status: QuoteCheck
    detail: str


def stored_extraction(source: SourceRecord, raw: RawStoreBackend) -> dict[str, Any] | None:
    """The PDF extraction persisted with ``source``, if present, intact and its own."""
    if not source.extracted_sha256:
        return None
    try:
        data: dict[str, Any] = json.loads(raw.get(source.extracted_sha256))
    except (RawStoreError, ValueError):
        return None
    return data if data.get("pdf_sha256") == source.raw_sha256 else None


async def reextract(
    source: SourceRecord, raw: RawStoreBackend, market: MarketSettings
) -> dict[str, Any] | None:
    """The stored PDF of ``source`` extracted again now; None when not possible."""
    if not source.raw_sha256:
        return None
    # A targeted read is re-done on the same pages (each page's text does not depend on
    # which other pages were read).
    stored = stored_extraction(source, raw)
    pages = (stored.get("targeting") or {}).get("pages") if stored else None
    try:
        data = raw.get(source.raw_sha256)
        out = await pdf.extract(data, market, len(pages) if pages else None,
                                pdf.Focus(pages=pages) if pages else None)  # fmt: skip
    except (RawStoreError, pdf.PdfError):
        return None
    out["pdf_sha256"] = source.raw_sha256
    return out


def _at(extraction: dict[str, Any], e: EvidenceRecord) -> str | None:
    """Why ``e``'s quote is not at its PDF location in ``extraction``; None if it is."""
    loc = e.locator
    assert loc is not None and loc.page is not None
    pages = extraction["pages"]
    if not 1 <= loc.page <= len(pages):
        return f"page {loc.page} is not in the document ({len(pages)} page(s) read)"
    text = pages[loc.page - 1]["text"]
    start, end = loc.char_start, loc.char_end
    if start is None or end is None or text[start:end] != e.excerpt:
        where = text.find(e.excerpt)
        if where >= 0:
            found = f"{where}-{where + len(e.excerpt)}"
            return f"quote is on page {loc.page} at {found}, not {start}-{end}"
        others = [p["number"] for p in pages if e.excerpt in p["text"]]
        if others:
            return f"quote is on page(s) {others}, not page {loc.page}"
        return f"quote is not on page {loc.page}"
    if loc.table is not None:
        tables = [b["table"] for b in pages[loc.page - 1]["blocks"]
                  if b.get("table") and b["table"]["index"] == loc.table]  # fmt: skip
        rows = tables[0]["rows"] if tables else []
        if (
            loc.row is None
            or not 0 <= loc.row < len(rows)
            or " | ".join(rows[loc.row]) != e.excerpt
        ):
            return f"quote is not row {loc.row} of table {loc.table} on page {loc.page}"
    return None


def _verify_pdf(e: EvidenceRecord, source: SourceRecord, raw: RawStoreBackend,
                reextraction: dict[str, Any] | None) -> Verification:  # fmt: skip
    loc = e.locator
    assert loc is not None
    if loc.page is None or not source.raw_sha256 or not source.extracted_sha256:
        return Verification(QuoteCheck.NOT_VERIFIABLE, "no page, stored PDF or stored extraction")
    stored = stored_extraction(source, raw)
    if stored is None:
        return Verification(QuoteCheck.NOT_VERIFIABLE, "stored extraction missing, damaged or "
                            "not of this file")  # fmt: skip
    if loc.version != stored["version"]:
        return Verification(QuoteCheck.NOT_VERIFIABLE, f"locator version {loc.version} differs "
                            f"from the extraction's {stored['version']}")  # fmt: skip
    problem = _at(stored, e) or _price_problem(e, None, stored)
    if problem is not None:
        return Verification(QuoteCheck.MISMATCH, problem)
    place = f"page {loc.page}, characters {loc.char_start}-{loc.char_end}"
    if reextraction is None:
        return Verification(QuoteCheck.VERIFIED_STORED, f"{place} of the stored extraction; the "
                            "stored file was not re-extracted")  # fmt: skip
    if reextraction.get("pdf_sha256") != source.raw_sha256:
        return Verification(
            QuoteCheck.NOT_VERIFIABLE, "the re-extraction is not of the stored file"
        )
    if (reextraction["version"], reextraction["libraries"]) != (stored["version"],
                                                                stored["libraries"]):  # fmt: skip
        return Verification(QuoteCheck.VERIFIED_STORED, f"{place} of the stored extraction "
                            f"({stored['version']}, {stored['libraries']}); a re-extraction with "
                            f"{reextraction['version']}, {reextraction['libraries']} is not "
                            "comparable")  # fmt: skip
    problem = _at(reextraction, e)
    if problem is not None:
        return Verification(QuoteCheck.MISMATCH, "the stored text has the quote but a "
                            f"re-extraction of the stored file does not: {problem}")  # fmt: skip
    return Verification(QuoteCheck.VERIFIED_REEXTRACTED, f"{place}, re-extracted from the stored "
                        f"file {source.raw_sha256[:12]}")  # fmt: skip


def verify_evidence(
    e: EvidenceRecord,
    source: SourceRecord,
    raw: RawStoreBackend,
    *,
    reextraction: dict[str, Any] | None = None,
    page: ParsedPage | None = None,
    current_sha256: str | None = None,
) -> Verification:
    """Check ``e`` against ``source``'s stored artifacts. ``reextraction``: the stored
    PDF extracted again (``reextract``); ``page``: the stored HTML already parsed;
    ``current_sha256``: the hash of what the URL serves now, to detect a change."""
    if current_sha256 is not None and source.raw_sha256 and current_sha256 != source.raw_sha256:
        inner = verify_evidence(e, source, raw, reextraction=reextraction, page=page)
        return Verification(QuoteCheck.SOURCE_CHANGED, f"{source.url} now serves "
                            f"{current_sha256[:12]}, collected {source.raw_sha256[:12]}; against "
                            f"the stored copy: {inner.status.value}: {inner.detail}")  # fmt: skip
    loc = e.locator
    if loc is None:
        return Verification(QuoteCheck.NOT_VERIFIABLE, "no locator")
    if loc.type == "pdf":
        return _verify_pdf(e, source, raw, reextraction)
    if loc.type == "ad":
        return _verify_ad(e, source, raw)
    if loc.type not in ("xpath", "json_path"):
        return Verification(QuoteCheck.NOT_VERIFIABLE, f"{loc.type} locators are not checked "
                            "here (dossier: the crawler's lineage; image: OCR)")  # fmt: skip
    if page is None:
        if not source.raw_sha256:
            return Verification(QuoteCheck.NOT_VERIFIABLE, "no stored page")
        try:
            page = parse_page(raw.get(source.raw_sha256), source.final_url or source.url,
                              "text/html", parse_settings(ParseSettings(), source.text_version),
                              UrlSettings())  # fmt: skip
        except RawStoreError as exc:
            return Verification(QuoteCheck.NOT_VERIFIABLE, f"stored page: {exc}")
    quote = Quote(loc.type, loc.value, e.excerpt, loc.char_start, loc.char_end)
    if resolve(page, quote) != e.excerpt:
        return Verification(QuoteCheck.MISMATCH, f"excerpt not at {loc.value} "
                            f"{loc.char_start}-{loc.char_end}")  # fmt: skip
    problem = _price_problem(e, page, None)
    if problem is not None:
        return Verification(QuoteCheck.MISMATCH, problem)
    return Verification(QuoteCheck.VERIFIED_REEXTRACTED, "re-parsed from the stored page")


def _verify_ad(e: EvidenceRecord, source: SourceRecord, raw: RawStoreBackend) -> Verification:
    """An ad quote against the capture stored at import (``market.ads``): the field's
    verbatim text (the card's, for one text per card) must be the excerpt."""
    assert e.locator is not None
    if not source.raw_sha256:
        return Verification(QuoteCheck.NOT_VERIFIABLE, "no stored ad capture")
    try:
        record = json.loads(raw.get(source.raw_sha256))
    except (RawStoreError, ValueError) as exc:
        return Verification(QuoteCheck.NOT_VERIFIABLE, f"stored ad capture: {exc}")
    value = (record.get("fields", {}).get(e.locator.value) or {}).get("verbatim")
    if isinstance(value, list):
        row = e.locator.row or 0
        value = value[row] if row < len(value) else None
    if value != e.excerpt:
        return Verification(QuoteCheck.MISMATCH, f"excerpt is not the stored ad's "
                            f"{e.locator.value}")  # fmt: skip
    return Verification(QuoteCheck.VERIFIED_STORED, f"{e.locator.value} of the stored ad "
                        f"capture {source.raw_sha256[:12]}")  # fmt: skip


def _price_problem(e: EvidenceRecord, page: ParsedPage | None,
                   extraction: dict[str, Any] | None) -> str | None:  # fmt: skip
    """Why ``e``'s prices do not hold together with what was collected; None if they do."""
    for m in e.metrics:
        if m.price is None:
            continue
        if m.text not in e.excerpt:
            return f"price text {m.text!r} is not in the excerpt"
        for c in m.price.context:
            loc = c.locator
            if loc.type == "pdf" and extraction is not None and loc.page is not None:
                pages = extraction["pages"]
                ok = 1 <= loc.page <= len(pages) and pages[loc.page - 1]["text"][
                    loc.char_start : loc.char_end] == c.text  # fmt: skip
            elif loc.type in ("xpath", "json_path") and page is not None:
                ok = resolve(page, Quote(loc.type, loc.value, c.text, loc.char_start,
                                         loc.char_end)) == c.text  # fmt: skip
            else:
                continue  # not checkable against this artifact
            if not ok:
                return f"the price's {c.role} quote {c.text[:60]!r} is not at {loc.value}"
    return None


async def verify_report(
    report: Report, raw: RawStoreBackend, market: MarketSettings, *, reextract_pdfs: bool = True
) -> dict[str, Verification]:
    """Every evidence record of ``report`` with a checkable locator, by evidence id;
    each stored PDF is re-extracted once (when the optional dependency is installed)."""
    sources = {s.source_id: s for s in report.sources}
    pages: dict[str, ParsedPage | None] = {}
    again: dict[str, dict[str, Any] | None] = {}
    out: dict[str, Verification] = {}
    for e in report.evidence:
        if e.locator is None or e.locator.type not in ("xpath", "json_path", "pdf", "ad"):
            continue
        s = sources.get(e.source_id)
        if s is None:
            out[e.evidence_id] = Verification(QuoteCheck.NOT_VERIFIABLE, "unknown source")
            continue
        if e.locator.type == "ad":
            out[e.evidence_id] = _verify_ad(e, s, raw)
            continue
        if e.locator.type == "pdf":
            if reextract_pdfs and s.source_id not in again:
                again[s.source_id] = await reextract(s, raw, market)
            out[e.evidence_id] = verify_evidence(e, s, raw, reextraction=again.get(s.source_id))
            continue
        if s.source_id not in pages and s.raw_sha256:
            try:
                pages[s.source_id] = parse_page(
                    raw.get(s.raw_sha256),
                    s.final_url or s.url,
                    "text/html",
                    parse_settings(ParseSettings(), s.text_version),
                    UrlSettings(),
                )
            except RawStoreError:
                pages[s.source_id] = None
        out[e.evidence_id] = verify_evidence(e, s, raw, page=pages.get(s.source_id))
    return out
