"""Documents a company gives about itself (onboarding: "URL / docs") as evidence.

A document is read to text (PDF in the isolated child process of
``market/pdf.py``, DOCX from its ``word/document.xml``, PPTX from its slides'
``ppt/slides/slideN.xml`` in slide order, plain text and Markdown as they are),
wrapped in minimal HTML and stored like a fetched page in the company's
*documents crawl*: one crawl per company with no job and the seed
``upload://<domain>/``. Extraction is current per crawl (``models.is_current``),
so re-extracting the documents crawl never touches the web crawls' evidence.
Its pages are cited as ``upload://<domain>/documents/<sha>/<file name>`` with the
source type ``company_document`` (``evidence.classify_source``): what the company
says about itself, never shown as a web page.

The original bytes are kept in the raw store too; the fetch headers name them.
The kind is decided by the bytes, not the file name. Nothing in a document is
executed or followed.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from lxml import etree
from sqlalchemy import Engine, select

from .config import Settings
from .crawl import config_snapshot
from .db.models import Crawl, Organization
from .db.store import CrawlStore
from .fetch.http import FetchResult
from .market import pdf
from .parse import parse_page
from .pipeline import reextract
from .rawstore import make_rawstore
from .versions import current_versions

UPLOAD_SCHEME = "upload"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
# A DOCX's or PPTX's text parts larger than this (uncompressed) are refused: a zip bomb guard.
MAX_DOCX_XML_BYTES = 50_000_000
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


class DocumentError(ValueError):
    """The document cannot be read. ``code`` is stable for clients."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class DocumentText:
    kind: str  # pdf | docx | pptx | text
    paragraphs: list[str]
    pages: int  # PDF pages read; 1 for other kinds
    issues: list[str]


def documents_seed(domain: str) -> str:
    return f"{UPLOAD_SCHEME}://{domain}/"


def document_url(domain: str, sha256: str, filename: str) -> str:
    return f"{UPLOAD_SCHEME}://{domain}/documents/{sha256[:16]}/{quote(filename, safe='')}"


def _office_document(data: bytes) -> DocumentText:
    """A DOCX's paragraphs, or a PPTX's paragraphs slide by slide."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("unsupported", "a damaged ZIP file") from exc
    names = archive.namelist()
    slides = sorted((int(m.group(1)), n) for n in names if (m := _SLIDE.match(n)))
    if "word/document.xml" in names:
        kind, ns, parts = "docx", _W, ["word/document.xml"]
    elif slides:
        kind, ns, parts = "pptx", _A, [n for _, n in slides]
    else:
        raise DocumentError("unsupported", "a ZIP file that is not a DOCX or PPTX document")
    infos = [archive.getinfo(n) for n in parts]
    if sum(i.file_size for i in infos) > MAX_DOCX_XML_BYTES:  # a zip bomb guard
        raise DocumentError("too_large", f"the {kind.upper()} text is too large")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    out = []
    for info in infos:
        try:
            root = etree.fromstring(archive.read(info), parser)
        except etree.XMLSyntaxError as exc:
            raise DocumentError("malformed", f"the {kind.upper()} text is not valid XML") from exc
        for p in root.iter(f"{ns}p"):
            text = "".join(t.text or "" for t in p.iter(f"{ns}t")).strip()
            if text:
                out.append(text)
    return DocumentText(kind, out, len(infos) if kind == "pptx" else 1, [])


def _text_paragraphs(data: bytes) -> list[str]:
    if b"\x00" in data:
        raise DocumentError("unsupported", "binary data: send a PDF, DOCX, PPTX, TXT or MD file")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentError("unsupported", "text that is not UTF-8") from exc
    return [p.strip() for p in _PARAGRAPH_BREAK.split(text) if p.strip()]


async def read_document(data: bytes, settings: Settings) -> DocumentText:
    """The document's text, by the kind its bytes show. Raises DocumentError."""
    if not data:
        raise DocumentError("empty", "the document is empty")
    if pdf.is_pdf(data):
        try:
            result = await pdf.extract(data, settings.market)
        except pdf.PdfError as exc:
            raise DocumentError(f"pdf_{exc.args[0]}", str(exc.args[-1])) from exc
        paragraphs = [
            p.strip()
            for page in result["pages"]
            for p in _PARAGRAPH_BREAK.split(page["text"])
            if p.strip()
        ]
        return DocumentText("pdf", paragraphs, pdf.pages_read(result), list(result["issues"]))
    if data[:4] == b"PK\x03\x04":
        return _office_document(data)
    return DocumentText("text", _text_paragraphs(data), 1, [])


def as_html(title: str, paragraphs: list[str]) -> bytes:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
    ).encode()


def _documents_crawl(engine: Engine, store: CrawlStore, settings: Settings, domain: str) -> int:
    seed = documents_seed(domain)
    with engine.connect() as conn:
        found = conn.execute(
            select(Crawl.id)
            .where(Crawl.seed_url == seed, Crawl.job_id.is_(None))
            .order_by(Crawl.id)
            .limit(1)
        ).scalar()
    if found is not None:
        return int(found)
    _, crawl_id = store.start_crawl(
        seed, config_snapshot(settings), None, current_versions(settings)
    )
    store.finish_crawl(crawl_id, "completed", {"kind": "documents"})
    return crawl_id


def _domain(engine: Engine, organization_id: int) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            select(Organization.registrable_domain).where(Organization.id == organization_id)
        ).scalar()


async def ingest_document(
    engine: Engine, settings: Settings, organization_id: int, filename: str, data: bytes
) -> dict[str, Any]:
    """Store one document as the company's evidence, then re-extract and reassess."""
    domain = await asyncio.to_thread(_domain, engine, organization_id)
    if domain is None:
        raise LookupError(f"no company {organization_id}")
    doc = await read_document(data, settings)
    if not doc.paragraphs:
        raise DocumentError("no_text", "no text found (scanned PDFs need OCR, not supported)")

    return await asyncio.to_thread(
        _store, engine, settings, organization_id, domain, filename, data, doc
    )


def _store(
    engine: Engine,
    settings: Settings,
    organization_id: int,
    domain: str,
    filename: str,
    data: bytes,
    doc: DocumentText,
) -> dict[str, Any]:
    store = CrawlStore(engine)
    raw = make_rawstore(settings.rawstore)
    crawl_id = _documents_crawl(engine, store, settings, domain)
    original_sha = raw.put(data)
    page_html = as_html(filename, doc.paragraphs)
    page_sha = raw.put(page_html)
    url = document_url(domain, original_sha, filename)
    headers = {
        "content-type": "text/html; charset=utf-8",
        "x-document-filename": filename,
        "x-document-kind": doc.kind,
        "x-document-sha256": original_sha,
    }
    result = FetchResult(
        url=url,
        final_url=url,
        status=200,
        headers=headers,
        content_type="text/html",
        body=page_html,
        attempts=1,
        fetched_at=datetime.now(UTC),
    )
    fetch_id = store.record_fetch(crawl_id, result, "upload", page_sha, trigger="document")
    page = parse_page(page_html, url, headers["content-type"], settings.parse, settings.urls)
    page_id = store.record_page(fetch_id, page, None)
    run = reextract(engine, settings, crawl_id, organization_id, trigger="document")
    return {
        "document_id": page_id,
        "filename": filename,
        "kind": doc.kind,
        "sha256": original_sha,
        "url": url,
        "pages_read": doc.pages,
        "paragraphs": len(doc.paragraphs),
        "issues": doc.issues,
        "observations": run["extraction"].get("observations"),
    }
