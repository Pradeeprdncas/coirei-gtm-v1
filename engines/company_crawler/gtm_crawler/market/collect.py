"""Fetch single pages for market research under the crawler's safeguards.

Each page is requested once through ``HttpFetcher`` (network guard: public
addresses and allowed ports only, on every redirect hop; size and
decompression limits; bounded retries), after ``PolicyEngine`` checked
robots.txt, inside a ``HostThrottle`` slot. Links are never followed (the
approved external-domain policy: no open-web crawling). The raw body goes
to the raw store, so every quote can be re-checked later; the parsed page's
main text is hashed (``content_sha256``); the engine marks a page with the
same text as an earlier-ranked one ``DUPLICATE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import ParseSettings, Settings
from ..fetch.browser import BrowserFetcher, browser_trigger
from ..fetch.http import FetchError, FetchResult, HttpFetcher
from ..parse import ParsedPage, parse_page
from ..policy import PolicyEngine
from ..rawstore import RawStoreBackend, make_rawstore
from ..robots import RobotsCache
from ..throttle import HostThrottle
from ..urls import url_host
from . import pdf
from .assessment import classify, domain_of
from .extract import pdf_page as extract_pdf_page
from .extract import states_money
from .models import Budget, RetrievalStatus, SourceRecord, SourceType
from .ocr import Ocr, image_urls

log = logging.getLogger(__name__)

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# SourceRecord.text_version of pages parsed with inline boundaries spaced.
# html-text-2: inline boundaries spaced; html-text-3: and landmark regions.
HTML_TEXT_VERSION = "html-text-3"


def parse_settings(base: ParseSettings, text_version: str | None) -> ParseSettings:
    """The parse settings that give a source's stored page its collected text."""
    return replace(base, inline_spacing=text_version in ("html-text-2", "html-text-3"),
                   landmark_regions=text_version == "html-text-3",
                   skip_aria_hidden=False)  # fmt: skip


# Browser renders per run (competitor pages only).
MAX_RENDERS = 8


@dataclass
class Collected:
    source: SourceRecord
    page: ParsedPage | None = None
    ocr: list[tuple[str, str]] = field(default_factory=list)  # (image url, text)
    pdf: dict[str, Any] | None = None  # a PDF's extraction (market/pdf.py)
    pdf_body: bool = False  # the body had the PDF signature (whatever the URL)


class Collector:
    def __init__(
        self,
        settings: Settings,
        budget: Budget,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rawstore: RawStoreBackend | None = None,
        ocr: Ocr | None = None,
    ) -> None:
        f = settings.fetch
        m = settings.market
        # PDFs may be larger than pages (market.pdf_max_bytes); HTML is held to the budget
        # after the download (_collect).
        body_cap = max(budget.max_content_bytes, m.pdf_max_bytes) if m.pdf_enabled else \
            budget.max_content_bytes  # fmt: skip
        fetch = replace(
            f, attempt_timeout=min(f.attempt_timeout, budget.request_timeout_seconds),
            read_timeout=min(f.read_timeout, budget.request_timeout_seconds),
            ttfb_timeout=min(f.ttfb_timeout, budget.request_timeout_seconds),
            max_body_bytes=min(f.max_body_bytes, body_cap),
            max_retries=min(f.max_retries, 2),
        )  # fmt: skip
        self.types = HTML_TYPES | pdf.PDF_TYPES if m.pdf_enabled else HTML_TYPES
        self.settings = settings
        self.parse = parse_settings(settings.parse, HTML_TEXT_VERSION)
        self.budget = budget
        self.fetcher = HttpFetcher(fetch, settings.identity, settings.urls, transport=transport)
        self.robots = RobotsCache(self.fetcher, settings.robots, settings.identity.product_token)
        self.policy = PolicyEngine(self.robots, lambda _url: True)
        c = settings.crawl
        self.throttle = HostThrottle(c.per_host_concurrency, c.min_delay_seconds)
        self.rawstore = rawstore if rawstore is not None else make_rawstore(settings.rawstore)
        self.ocr = ocr
        # Competitors' own pages only: the crawler's guarded browser fallback (Chromium
        # requests go through this fetcher, robots.txt and throttle).
        self.browser: BrowserFetcher | None = None
        if settings.browser.enabled:
            self.browser = BrowserFetcher(settings.browser, settings.urls, self.fetcher,
                                          self.robots, self.throttle)  # fmt: skip
        self.renders = 0

    async def aclose(self) -> None:
        if self.browser is not None:
            await self.browser.aclose()
        await self.fetcher.aclose()

    async def collect(self, url: str, discovered_by: str, *, vendor: bool = False,
                      max_pdf_pages: int | None = None,
                      pdf_focus: pdf.Focus | None = None) -> Collected:  # fmt: skip
        """One page or PDF; ``max_pdf_pages``: the pages a PDF may still use (budget;
        0: a PDF is downloaded but not read; None: no budget, e.g. competitor pages);
        ``pdf_focus``: what the open questions look for, to target a long PDF's pages."""
        source_type, tier = classify(url, self.settings.sources, self.settings.market,
                                     vendor=vendor)  # fmt: skip
        source = SourceRecord(
            url=url, domain=domain_of(url), source_type=source_type, tier=tier,
            status=RetrievalStatus.FAILED, discovered_by=discovered_by,
        )  # fmt: skip
        try:
            return await self._collect(url, source, max_pdf_pages, pdf_focus)
        except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
            log.warning("market.collect_failed", extra={"url": url, "error": repr(exc)[:300]})
            source.status_detail = f"{type(exc).__name__}: {exc}"[:300]
            return Collected(source)

    async def _collect(self, url: str, source: SourceRecord, max_pdf_pages: int | None = None,
                       pdf_focus: pdf.Focus | None = None) -> Collected:  # fmt: skip
        decision = await self.policy.check(url, external=True)
        if not decision.allowed:
            source.status = RetrievalStatus.BLOCKED
            source.status_detail = decision.reason
            return Collected(source)
        async with self.throttle.slot(url_host(url), decision.crawl_delay):
            result = await asyncio.wait_for(
                self.fetcher.fetch(url, self.types),
                # Retries with backoff included; the fetcher's own limits apply per attempt.
                self.budget.request_timeout_seconds * 3,
            )
        source.retrieved_at = result.fetched_at
        source.final_url = result.final_url if result.final_url != url else None
        if not result.ok or result.body is None:
            source.status = (RetrievalStatus.BLOCKED if result.error is FetchError.BLOCKED
                             else RetrievalStatus.FAILED)  # fmt: skip
            source.status_detail = str(result.error or f"http_{result.status}")
            return Collected(source)
        # A PDF by its signature, whatever the URL or the declared type says.
        source.size_bytes = len(result.body)
        if pdf.is_pdf(result.body):
            got = await self._pdf(result.body, result.final_url, source, max_pdf_pages,
                                  pdf_focus)  # fmt: skip
            got.pdf_body = True
            return got
        if result.content_type not in HTML_TYPES:
            source.status = RetrievalStatus.UNREADABLE
            source.status_detail = (f"pdf_not_pdf: served as {result.content_type} without a "
                                    "%PDF- signature")  # fmt: skip
            return Collected(source)
        if len(result.body) > self.budget.max_content_bytes:
            source.status_detail = f"too_large: page over {self.budget.max_content_bytes} bytes"
            return Collected(source)
        source.raw_sha256 = await asyncio.to_thread(self.rawstore.put, result.body)
        source.text_version = HTML_TEXT_VERSION
        page = await asyncio.to_thread(
            parse_page, result.body, result.final_url, result.headers.get("content-type"),
            self.parse, self.settings.urls,
        )  # fmt: skip
        if page.parse_error:
            source.status_detail = f"parse: {page.parse_error}"[:300]
            return Collected(source)
        if source.source_type is SourceType.COMPANY_WEBSITE:
            page = await self._rendered(url, result, page, source)
        source.title = page.title
        source.published_at = page.published_at
        source.content_sha256 = page.text_sha256
        source.publisher = _publisher(page)
        source.status = RetrievalStatus.FETCHED
        out = Collected(source, page)
        if self.ocr is not None and self.ocr.available:
            urls = image_urls(result.body, result.final_url, self.settings.market)
            out.ocr = await self.ocr.read(self.fetcher, self.policy, urls)
        return out

    async def _pdf(self, body: bytes, final_url: str, source: SourceRecord,
                   max_pages: int | None = None,
                   focus: pdf.Focus | None = None) -> Collected:  # fmt: skip
        """Store the file, extract it in a child process, store the extraction too (so
        its quotes stay checkable against what was read), and build the page."""
        market = self.settings.market
        source.raw_sha256 = await asyncio.to_thread(self.rawstore.put, body)
        source.status = RetrievalStatus.UNREADABLE
        if not market.pdf_enabled:
            source.status_detail = "pdf_disabled: market.pdf_enabled = false"
            return Collected(source)
        if max_pages is not None and max_pages <= 0:
            source.status = RetrievalStatus.SKIPPED
            source.status_detail = ("PDF not read: over the PDF allowance or page budget "
                                    "(served from a URL without .pdf)")  # fmt: skip
            return Collected(source)
        try:
            extraction = await pdf.extract(body, market, max_pages, focus)
        except pdf.PdfError as exc:
            source.status_detail = f"pdf_{exc.code}: {exc.detail}"[:300]
            log.info("market.pdf_unreadable", extra={"url": final_url, "code": exc.code,
                                                     "bytes": len(body)})  # fmt: skip
            return Collected(source)
        issues = [f"page {p['number']}: {i}" for p in extraction["pages"] for i in p["issues"]]
        source.limitations += (extraction["issues"] + issues)[:10]
        if not any(p["text"] for p in extraction["pages"]):
            source.status_detail = "pdf_no_text: no page has a text layer (scanned? PDF OCR is " \
                                   "not implemented)"  # fmt: skip
            return Collected(source)
        extraction["pdf_sha256"] = source.raw_sha256  # binds the text to this exact file
        stored = json.dumps(extraction, sort_keys=True, ensure_ascii=False).encode("utf-8")
        source.extracted_sha256 = await asyncio.to_thread(self.rawstore.put, stored)
        page = extract_pdf_page(extraction, final_url)
        source.title = page.title
        source.published_at = page.published_at
        if page.published_at is not None:
            source.limitations.append("publication date from the PDF's metadata (creation "
                                      "date), not from its text")  # fmt: skip
        source.content_sha256 = page.text_sha256
        source.status = RetrievalStatus.FETCHED
        source.status_detail = (f"pdf: {pdf.pages_read(extraction)} of "
                                f"{extraction['page_count']} page(s) read"
                                + (f", targeted ({extraction['targeting']['method']})"
                                   if extraction.get("targeting") else ""))  # fmt: skip
        return Collected(source, page, pdf=extraction)

    async def _rendered(
        self, url: str, result: FetchResult, page: ParsedPage, source: SourceRecord
    ) -> ParsedPage:
        """A competitor page rendered in the browser when its HTML is a script shell, or
        a pricing page whose HTML states no price (prices filled in by JavaScript)."""
        trigger = browser_trigger(page, result.body or b"", self.settings.browser)
        pricing = "pric" in urlsplit(url).path.lower() and not states_money(page.main_text)
        if trigger is None and pricing:
            trigger = "pricing_page_without_prices"
        if trigger is None or self.browser is None:
            return page
        if self.renders >= MAX_RENDERS:
            source.limitations.append(f"not rendered ({trigger}): render budget used")
            return page
        self.renders += 1
        rendered = await self.browser.render(result)
        if not rendered.ok or rendered.body is None:
            source.limitations.append(f"browser render failed: {rendered.error}")
            return page
        if domain_of(rendered.final_url) != domain_of(url):
            return page  # a script navigated elsewhere: not this competitor's page
        better = await asyncio.to_thread(
            parse_page, rendered.body, rendered.final_url, rendered.headers.get("content-type"),
            self.parse, self.settings.urls,
        )  # fmt: skip
        gained = len(better.cleaned_text) > len(page.cleaned_text) or (
            pricing and states_money(better.main_text))  # fmt: skip
        if better.parse_error or not gained:
            return page
        # Quotes are taken from, and re-checkable against, the rendered document.
        source.raw_sha256 = await asyncio.to_thread(self.rawstore.put, rendered.body)
        source.status_detail = f"rendered in a browser ({trigger})"
        return better


def _publisher(page: ParsedPage) -> str | None:
    for key in ("og:site_name", "application-name", "author"):
        if page.meta.get(key):
            return page.meta[key][0][:200]
    return None
