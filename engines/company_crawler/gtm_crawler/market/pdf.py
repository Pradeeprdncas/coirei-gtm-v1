"""Text PDFs as market evidence: detection, isolated extraction, page-level provenance.

A PDF is recognised by its signature (``%PDF-`` in the first 1024 bytes),
never by the URL alone. It is read by pdfplumber (pdfminer.six) in a child
process (``python -m gtm_crawler.market.pdf``): the file arrives on stdin,
the extraction leaves as JSON on stdout, and the parent kills the child at
``market.pdf_timeout``. On POSIX the child also gets memory and CPU limits
(``market.pdf_memory_mb``); on Windows only the timeout and the page and
character caps apply. Nothing in the file is executed or followed: no
JavaScript, no attachments, no links, no form actions.

The extraction is deterministic for one library version and
``PDF_TEXT_VERSION``. Per page (one-based) it keeps a normalized page text
built from blocks in reading order: headings and paragraphs (lines grouped
by spacing and font size, joined with single spaces) and ruled tables (rows
on their own lines, cells joined by `` | ``), separated by blank lines.
Each block records its character offsets in the page text, so a quote is
"page N, characters a-b". Tables keep their rows and cells, the caption
line above them and the unit it states ("EUR billion"); tables whose rows
have different lengths or empty cells are recorded as irregular and not
used for figures. Image-only pages (no text layer) are reported as such:
PDF OCR is not implemented.

Encrypted, malformed and non-PDF files are refused with a reason; a file
with more than ``market.pdf_max_pages`` pages is read up to that page and
says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import math
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import MarketSettings

PDF_TEXT_VERSION = "pdf-text-1"
PDF_TYPES = frozenset({"application/pdf", "application/x-pdf", "application/octet-stream",
                       "binary/octet-stream"})  # fmt: skip
_SIGNATURE_WINDOW = 1024
_UNIT = re.compile(
    r"\b(?P<cur>USD|US\$|EUR|GBP|INR|JPY|CNY|AUD|CAD|[$€£₹¥])\s?(?P<scale>trillion|billion|"
    r"million|thousand|crore|lakh|bn|mn|tn)\b|\b(?P<scale2>billion|million|thousand|crore|lakh)"
    r"s?\s+(?:of\s+)?(?P<cur2>USD|US\s?dollars|dollars|EUR|euros|GBP|INR|rupees)\b",
    re.I,
)  # fmt: skip


class PdfError(Exception):
    """Why a PDF could not be read: ``code`` is one of not_pdf, encrypted, malformed,
    timeout, too_large, unavailable, failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def is_pdf(body: bytes) -> bool:
    return b"%PDF-" in body[:_SIGNATURE_WINDOW]


def available() -> tuple[bool, str]:
    """Whether the optional dependency is installed (``pip install -e .[pdf]``)."""
    if importlib.util.find_spec("pdfplumber") is None:
        return False, "PDF support not installed (pip install -e .[pdf])"
    return True, "ok"


def library_versions() -> dict[str, str]:
    out = {}
    for name in ("pdfplumber", "pdfminer.six"):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            out[name] = importlib.metadata.version(name)
    return out


# --- in the child process ---------------------------------------------------------------


@dataclass
class _Line:
    top: float
    bottom: float
    size: float
    bold: bool
    text: str
    x0: float = 0.0
    column: int = 0


@dataclass
class _Block:
    kind: str  # heading, paragraph, table
    top: float
    text: str
    table: dict[str, Any] | None = None
    lines: list[_Line] = field(default_factory=list)


COLUMN_GAP = 18.0  # points of horizontal space that end a line segment (a column gutter)
COLUMN_ALIGN = 30.0  # segments starting within this many points share a column
MIN_COLUMN_LINES = 3  # a column needs this many line segments ...
MIN_COLUMN_CHARS = 20  # ... of at least this many characters


def _lines(page: Any, skip: list[tuple[float, float, float, float]]) -> list[_Line]:
    """Text lines in reading order: words grouped by baseline, split at column gutters,
    then column by column (left to right), each top to bottom. A one-column page reads
    top to bottom as usual."""
    words = [w for w in page.extract_words(extra_attrs=["size", "fontname"])
             if not any(x0 <= (w["x0"] + w["x1"]) / 2 <= x1 and top <= (w["top"] + w["bottom"])
                        / 2 <= bottom for x0, top, x1, bottom in skip)]  # fmt: skip
    rows: list[list[dict[str, Any]]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= 2.0:
            rows[-1].append(w)
        else:
            rows.append([w])
    segments: list[list[dict[str, Any]]] = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        segments.append([row[0]])
        for w in row[1:]:
            if w["x0"] - segments[-1][-1]["x1"] > COLUMN_GAP:
                segments.append([w])
            else:
                segments[-1].append(w)
    starts: list[float] = []  # column left edges
    for x in sorted(s[0]["x0"] for s in segments):
        if not starts or x - starts[-1] > COLUMN_ALIGN:
            starts.append(x)
    # Text columns have long segments; the cells of an unruled table do not (its rows
    # must stay rows: "Germany 3.1 3.6").
    counts = [
        sum(
            1
            for s in segments
            if 0 <= s[0]["x0"] - c <= COLUMN_ALIGN
            and sum(len(w["text"]) + 1 for w in s) >= MIN_COLUMN_CHARS
        )
        for c in starts
    ]
    columns = [c for c, n in zip(starts, counts, strict=True) if n >= MIN_COLUMN_LINES] or starts[
        :1
    ]
    out = []
    for seg in segments:
        x0 = seg[0]["x0"]
        column = max((i for i, c in enumerate(columns) if c - COLUMN_ALIGN <= x0), default=0)
        size = statistics.median(w["size"] for w in seg)
        bold = all("bold" in str(w.get("fontname", "")).lower() for w in seg)
        text = " ".join(" ".join(w["text"] for w in seg).split())
        out.append(_Line(min(w["top"] for w in seg), max(w["bottom"] for w in seg),
                         round(size, 1), bold, text, x0, column))  # fmt: skip
    out.sort(key=lambda ln: (ln.column, ln.top))
    return out


def _paragraphs(lines: list[_Line]) -> list[_Block]:
    """Group lines: same font size and bold-ness, and a gap under 0.6 line heights."""
    if not lines:
        return []
    body = statistics.median(ln.size for ln in lines)
    blocks: list[_Block] = []
    for ln in lines:
        heading = ln.size >= body * 1.15 or (ln.bold and len(ln.text) <= 120)
        prev = blocks[-1] if blocks else None
        last = prev.lines[-1] if prev else None
        joins = (prev is not None and last is not None and prev.kind != "table"
                 and (prev.kind == "heading") == heading and abs(last.size - ln.size) <= 0.5
                 and ln.column == last.column
                 and ln.top - last.bottom <= 0.6 * (last.bottom - last.top))  # fmt: skip
        if joins and prev is not None:
            prev.lines.append(ln)
            prev.text += " " + ln.text
        else:
            blocks.append(_Block("heading" if heading else "paragraph", ln.top, ln.text,
                                 lines=[ln]))  # fmt: skip
    return blocks


def _unit(text: str) -> dict[str, str] | None:
    m = _UNIT.search(text)
    if m is None:
        return None
    cur = (m.group("cur") or m.group("cur2") or "").upper().replace(" ", "")
    cur = {
        "US$": "USD",
        "$": "USD",
        "USDOLLARS": "USD",
        "DOLLARS": "USD",
        "€": "EUR",
        "EUROS": "EUR",
        "£": "GBP",
        "₹": "INR",
        "RUPEES": "INR",
        "¥": "JPY",
    }.get(cur, cur)
    scale = (m.group("scale") or m.group("scale2") or "").lower()
    return {"currency": cur, "scale": scale, "text": m.group(0)}


def _tables(page: Any, lines_above: list[_Line]) -> list[_Block]:
    out = []
    for index, table in enumerate(page.find_tables()):
        rows = table.extract()
        width = len(rows[0]) if rows else 0
        cells = [c for row in rows for c in row]
        regular = (len(rows) >= 2 and width >= 2 and all(len(r) == width for r in rows)
                   and all(c is not None and c.strip() for c in cells))  # fmt: skip
        clean = [[" ".join((c or "").split()) for c in row] for row in rows]
        x0, top, x1, bottom = table.bbox
        caption = next((ln.text for ln in reversed(lines_above)
                        if ln.bottom <= top and top - ln.bottom <= 40), None)  # fmt: skip
        header_unit = next((u for c in clean[0] if (u := _unit(c))), None) if clean else None
        info = {"index": index, "rows": clean, "regular": regular, "caption": caption,
                "unit": header_unit or (_unit(caption) if caption else None),
                "bbox": [round(v, 1) for v in (x0, top, x1, bottom)]}  # fmt: skip
        out.append(_Block("table", top, "\n".join(" | ".join(r) for r in clean), table=info))
    return out


def _page(page: Any, number: int) -> dict[str, Any]:
    issues: list[str] = []
    if not page.chars:
        issues.append("image-only page: no text layer (PDF OCR is not implemented)"
                      if page.images else "empty page")  # fmt: skip
        return {"number": number, "text": "", "blocks": [], "issues": issues}
    found = page.find_tables()
    boxes = [t.bbox for t in found]
    lines = _lines(page, boxes)
    blocks = _paragraphs(lines) + _tables(page, lines)
    # Column by column; a table sits in the column its left edge is in (first if none).
    blocks.sort(key=lambda b: (b.lines[0].column if b.lines else 0, b.top))
    text, out_blocks = "", []
    for b in blocks:
        if text:
            text += "\n\n"
        start = len(text)
        text += b.text
        entry: dict[str, Any] = {"kind": b.kind, "start": start, "end": len(text)}
        if b.table is not None:
            entry["table"] = b.table
            if not b.table["regular"]:
                issues.append(f"table {b.table['index']}: irregular rows or empty cells; "
                              "not used for figures")  # fmt: skip
        out_blocks.append(entry)
    return {"number": number, "text": text, "blocks": out_blocks, "issues": issues}


def _date(value: Any) -> str | None:
    """A PDF date ("D:20240315120000Z") as ISO 8601, or None."""
    if isinstance(value, bytes):
        value = value.decode("latin-1", "replace")
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", str(value or ""))
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(p) if p else d for p, d in zip(m.groups(), (0, 1, 1, 0, 0, 0),
                                                                 strict=True))  # fmt: skip
    try:
        return datetime(y, mo, d, h, mi, sec, tzinfo=UTC).isoformat()
    except ValueError:
        return None


# --- page targeting (long documents) -----------------------------------------------------
#
# A document longer than its page budget is not read from page 1 to the budget: the first
# pages (title, contents, summary) are read, then the pages the open research questions
# point to: the sections of the table of contents or the outline whose titles match the
# questions' terms, else the pages whose text matches them best (a light text pass, no
# layout or table analysis). A match only ranks a page; whether it states a usable figure
# is still decided by the extraction rules. Unread pages are kept as empty entries so page
# N stays at index N-1; ``targeting`` records which pages were read and why, so a
# re-extraction reads the same pages.

HEAD_PAGES = 2
SCAN_PAGES = 500  # the light text pass ranks at most this many pages ...
SCAN_SHARE = 0.4  # ... in at most this share of the extraction's time limit
_TOC_LINE = re.compile(r"^(?P<t>[A-Za-z][^\n]{2,120}?)\s*(?:\.{2,}|…+|·{2,}|\s)\s*(?P<p>\d{1,4})$")


@dataclass
class Focus:
    """What the open questions look for: ``terms`` (the subject's words, the place) and,
    per open question key, its dimension rules (``market/extract.py``)."""

    terms: list[str] = field(default_factory=list)
    questions: dict[str, list[str]] = field(default_factory=dict)
    pages: list[int] | None = None  # explicit pages (a re-extraction of a targeted read)


_FIGURES = (r"\d\s?%", r"\b(?:billion|million|bn|mn)\b|[$€£₹¥]\s?\d", r"\b(?:19|20)\d\d\b",
            r"\b(?:table|figure|exhibit)\s\d")  # fmt: skip


def _hits(rules: list[re.Pattern[str]], text: str) -> int:
    return sum(min(3, len(r.findall(text))) for r in rules)


def _toc(texts: dict[int, str]) -> list[tuple[str, int]]:
    """(title, printed page) of the contents lines of the first pages."""
    out = []
    for text in texts.values():
        for line in text.splitlines():
            m = _TOC_LINE.match(line.strip())
            if m and int(m.group("p")) > 0:
                out.append((m.group("t").strip(" .…·"), int(m.group("p"))))
    return out


def _outline(pdf: Any) -> list[tuple[str, int]]:
    """(title, one-based page) of the document outline (bookmarks), when it resolves."""
    from pdfminer.pdftypes import resolve1  # type: ignore[import-not-found,unused-ignore]

    ids = {p.page_obj.pageid: i for i, p in enumerate(pdf.pages, start=1)}
    out = []
    try:
        for _level, title, dest, action, _se in pdf.doc.get_outlines():
            if dest is None and action is not None:
                dest = resolve1(action).get("D")
            dest = resolve1(dest)
            if isinstance(dest, dict):
                dest = resolve1(dest.get("D"))
            if isinstance(dest, list) and dest:
                page = ids.get(getattr(dest[0], "objid", None))
                if page and isinstance(title, str):
                    out.append((title, page))
    except Exception:  # noqa: BLE001 - no outline, or one that does not resolve: none used
        return []
    return out


def _cheap_text(page: Any) -> str:
    try:
        return str(page.extract_text() or "")
    except Exception:  # noqa: BLE001 - an unreadable page ranks last
        return ""


SECTION_BONUS = 3.0  # a page in a section whose title matches a question's rules


def select_pages(pdf: Any, budget: int, focus: Focus, scan_limit: int,
                 deadline: float = math.inf) -> dict[str, Any]:  # fmt: skip
    """Which pages of a document longer than ``budget`` to read, and why (``targeting``):
    the first pages, then, question by question, the best page for each open question
    (its rules' matches in a light text pass, more in a matching outline or contents
    section), round after round. The text pass stops at ``deadline`` (monotonic time) so
    the extraction keeps its time for the chosen pages; unscanned pages rank by their
    section alone."""
    total = len(pdf.pages)
    if focus.pages is not None:
        pages = sorted({p for p in focus.pages if 1 <= p <= total})[:budget]
        return {"method": "explicit", "pages": pages, "reasons": []}
    rules = {k: [re.compile(p, re.I) for p in ps] for k, ps in focus.questions.items()}
    terms = [re.compile(rf"\b{re.escape(t)}\b", re.I) for t in focus.terms]
    head = list(range(1, min(HEAD_PAGES, budget, total) + 1))
    chosen: dict[int, str] = {p: "first pages (title, contents, summary)" for p in head}
    last = min(total, scan_limit)
    texts: dict[int, str] = {}
    for p in range(1, last + 1):
        if time.monotonic() > deadline and p > 6:
            break
        texts[p] = _cheap_text(pdf.pages[p - 1])

    # Outline (bookmarks), else the contents lines of the first pages, as sections.
    entries, method = _outline(pdf), "outline"
    if not entries:
        found = _toc({p: texts[p] for p in range(1, min(last, 6) + 1)})
        offset = _printed_offset(found, texts)
        entries, method = [(t, p + offset) for t, p in found], "contents"
    starts = sorted({p for _, p in entries})
    section: dict[int, tuple[str, set[str]]] = {}
    for title, start in entries:
        matched = {k for k, rs in rules.items() if any(r.search(title) for r in rs)}
        end = next((s for s in starts if s > start), start + 1)
        for p in range(start, min(end, start + max(2, budget // 3))):
            if matched and p not in section:
                section[p] = (title, matched)
    method = f"{method} + text" if section else "text"

    scores: dict[int, dict[str, float]] = {}
    for p in range(1, last + 1):
        text = texts.get(p)
        if text is None and p in section and p not in chosen:  # not scanned in time
            scores[p] = {k: SECTION_BONUS * (k in section[p][1]) for k in rules}
            scores[p]["*"] = 2 * sum(scores[p].values())
            continue
        if p in chosen or text is None or len(text.strip()) < 40:
            continue
        on_subject = not terms or any(t.search(text) for t in terms)
        per = {k: float(_hits(rs, text)) for k, rs in rules.items()}
        for k in section.get(p, ("", set()))[1]:
            per[k] += SECTION_BONUS
        if not on_subject:
            per = {k: v * 0.3 for k, v in per.items()}
        figures = sum(0.5 for rx in _FIGURES if re.search(rx, text, re.I))
        per["*"] = 2 * sum(per.values()) + sum(1 for t in terms if t.search(text)) + figures
        scores[p] = per

    def why(p: int, k: str) -> str:
        where = f"{method.split()[0]} section '{section[p][0][:60]}', " if p in section else ""
        return f"{where}matches the open question '{k}' (score {scores[p][k]:g})"

    # Round after round, the best unread page for each open question.
    while len(chosen) < budget:
        added = False
        for k in rules:
            best = max((p for p in scores if p not in chosen and scores[p][k] > 0),
                       key=lambda p: (scores[p][k], scores[p]["*"], -p), default=None)  # fmt: skip
            if best is not None and len(chosen) < budget:
                chosen[best], added = why(best, k), True
        if not added:
            break
    return {"method": method, "pages": sorted(chosen), "scanned": len(texts),
            "reasons": [[p, r] for p, r in sorted(chosen.items())]}  # fmt: skip


def _printed_offset(entries: list[tuple[str, int]], texts: dict[int, str]) -> int:
    """Printed page numbers run behind the file's when front matter is unnumbered: the
    offset that puts contents entries on a page that has their title."""
    for title, printed in entries[:5]:
        words = " ".join(title.lower().split()[:4])
        for offset in range(0, 9):
            text = texts.get(printed + offset, "")
            if words and len(words) > 6 and words in " ".join(text.lower().split()):
                return offset
    return 0


def extract_bytes(data: bytes, max_pages: int, max_chars: int, focus: Focus | None = None,
                  time_limit: float = 60.0) -> dict[str, Any]:  # fmt: skip
    """The extraction of ``data`` (run in the child process; raises PdfError). ``focus``:
    for a document longer than ``max_pages``, read the pages it points to (``targeting``)
    instead of the first ``max_pages``."""
    import pdfplumber  # type: ignore[import-not-found,unused-ignore]
    from pdfminer.pdfdocument import PDFPasswordIncorrect  # type: ignore[import-not-found,unused-ignore]
    from pdfminer.pdfparser import PDFSyntaxError  # type: ignore[import-not-found,unused-ignore]

    if not is_pdf(data):
        raise PdfError("not_pdf", "no %PDF- signature in the first 1024 bytes")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - untrusted bytes: any parser failure
        # pdfplumber wraps pdfminer's errors; look through the chain and the arguments.
        chain: list[BaseException] = []
        cur: BaseException | None = exc
        while cur is not None and len(chain) < 10:
            chain.append(cur)
            chain += [a for a in cur.args if isinstance(a, BaseException)]
            cur = cur.__cause__ or cur.__context__
        if any(isinstance(e, PDFPasswordIncorrect) for e in chain) or b"/Encrypt" in data[-4096:]:
            raise PdfError("encrypted", "the file is encrypted and needs a password") from exc
        if any(isinstance(e, PDFSyntaxError) for e in chain):
            raise PdfError("malformed", str(exc)[:200]) from exc
        raise PdfError("malformed", f"{type(exc).__name__}: {exc}"[:200]) from exc
    with pdf:
        if getattr(pdf.doc, "encryption", None):
            raise PdfError("encrypted", "the file is encrypted")
        meta = pdf.metadata or {}
        pages: list[dict[str, Any]] = []
        chars, issues = 0, []
        total = len(pdf.pages)
        targeting = None
        if focus is not None and (total > max_pages or focus.pages is not None):
            targeting = select_pages(
                pdf, max_pages, focus, SCAN_PAGES, time.monotonic() + SCAN_SHARE * time_limit
            )
        wanted = set(targeting["pages"]) if targeting else set(range(1, min(total, max_pages) + 1))
        for number in range(1, max(wanted, default=0) + 1):
            if number not in wanted:  # not targeted: kept empty so page N stays at N-1
                pages.append({"number": number, "text": "", "blocks": [], "issues": [],
                              "read": False})  # fmt: skip
                continue
            try:
                entry = _page(pdf.pages[number - 1], number)
            except Exception as exc:  # noqa: BLE001 - one bad page is reported, not fatal
                entry = {"number": number, "text": "", "blocks": [],
                         "issues": [f"page not readable: {type(exc).__name__}"]}  # fmt: skip
            chars += len(entry["text"])
            pages.append(entry)
            if chars > max_chars:
                issues.append(f"stopped after page {number}: over {max_chars} characters")
                break
        read = pages_read({"pages": pages})
        if targeting is not None and not issues:
            issues.append(
                f"read {read} of {total} pages, chosen for the open research "
                f"questions ({targeting['method']}; pages {_ranges(targeting['pages'])})"
            )
        elif total > read and not issues:
            issues.append(f"read {read} of {total} pages (market.pdf_max_pages)")
    out: dict[str, Any] = {
        "version": PDF_TEXT_VERSION, "libraries": library_versions(), "page_count": total,
        "metadata": {"title": str(meta.get("Title") or "") or None,
                     "author": str(meta.get("Author") or "") or None,
                     "created": _date(meta.get("CreationDate")),
                     "modified": _date(meta.get("ModDate"))},
        "pages": pages, "issues": issues,
    }  # fmt: skip
    if targeting is not None:
        out["targeting"] = targeting
    return out


def pages_read(extraction: dict[str, Any]) -> int:
    """Pages actually read (a targeted read keeps the others as empty entries)."""
    return sum(1 for p in extraction["pages"] if p.get("read", True))


def _ranges(pages: list[int]) -> str:
    out: list[str] = []
    for p in pages:
        if out and int(out[-1].split("-")[-1]) == p - 1:
            out[-1] = f"{out[-1].split('-')[0]}-{p}"
        else:
            out.append(str(p))
    return ", ".join(out)


def _limit_resources(memory_mb: int, cpu_seconds: int) -> None:
    if sys.platform == "win32":
        return  # no rlimits on Windows: the parent's timeout and the caps still apply
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 2**20, memory_mb * 2**20))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))


def main() -> int:
    """Child process: PDF on stdin, ``{"ok": ...}`` JSON on stdout."""
    max_pages, max_chars, memory_mb, cpu = (int(a) for a in sys.argv[1:5])
    focus = Focus(**json.loads(sys.argv[5])) if len(sys.argv) > 5 else None
    _limit_resources(memory_mb, cpu)
    data = sys.stdin.buffer.read()
    try:
        out: dict[str, Any] = {
            "ok": True,
            "result": extract_bytes(data, max_pages, max_chars, focus, cpu),
        }
    except PdfError as exc:
        out = {"ok": False, "code": exc.code, "detail": exc.detail}
    except MemoryError:
        out = {"ok": False, "code": "too_large", "detail": "memory limit reached"}
    # UTF-8 bytes whatever the platform's stdout encoding (cp1252 on Windows cannot
    # carry "₹" or "≤", and the parent decodes UTF-8): found by the live benchmark.
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.flush()
    return 0


# --- in the parent ----------------------------------------------------------------------


async def extract(
    data: bytes,
    market: MarketSettings,
    max_pages: int | None = None,
    focus: Focus | None = None,
) -> dict[str, Any]:
    """Run the extraction in a child process within ``market.pdf_timeout``; raises PdfError.
    ``max_pages`` lowers ``market.pdf_max_pages`` (a run's remaining page budget);
    ``focus``: target a longer document's pages (``select_pages``)."""
    ok, reason = available()
    if not ok:
        raise PdfError("unavailable", reason)
    if len(data) > market.pdf_max_bytes:
        raise PdfError("too_large", f"{len(data)} bytes > market.pdf_max_bytes")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gtm_crawler.market.pdf",
        str(min(market.pdf_max_pages, max_pages or market.pdf_max_pages)),
        str(market.pdf_max_chars), str(market.pdf_memory_mb),
        str(max(1, int(market.pdf_timeout))),
        *([json.dumps(asdict(focus))] if focus is not None else []),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(data), market.pdf_timeout)
    except TimeoutError as exc:
        raise PdfError("timeout", f"extraction took over {market.pdf_timeout:g} s") from exc
    finally:  # timed out or cancelled: the child never outlives the request
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    try:
        out = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # The child died (crash, memory or CPU limit): its stderr is not logged (contents).
        raise PdfError("failed", f"extraction process exited with {proc.returncode}") from exc
    if not out.get("ok"):
        raise PdfError(str(out.get("code")), str(out.get("detail"))[:300])
    result: dict[str, Any] = out["result"]
    return result


if __name__ == "__main__":
    sys.exit(main())
