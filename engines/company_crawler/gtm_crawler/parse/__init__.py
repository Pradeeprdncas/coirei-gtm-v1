"""Turns a fetched HTML document into a ``ParsedPage``.

The raw bytes are not modified or discarded here; they live in the raw store.
``ParsedPage`` is a derived, re-computable view of them (spec section 12):

    raw response -> validate -> decode -> HTML -> main text -> links
                 -> metadata (title, description, headings, OpenGraph/meta)
                 -> structured data (JSON-LD, Schema.org microdata)

Parsing never raises for bad input: pages are untrusted, and a document that
cannot be parsed yields a ``ParsedPage`` with ``parse_error`` set, so the raw
artifact is still stored and the crawl continues. Content that libxml2's
safety limits drop (nesting deeper than 256, huge text nodes) is reported in
``parse_warnings``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lxml import etree

from ..config import ParseSettings, UrlSettings
from .dom import (
    DomExtract,
    Link,
    ListBlock,
    MicrodataItem,
    Table,
    TextBlock,
    extract_dom,
    parse_document,
)
from .fingerprint import simhash, text_sha256
from .structured import JsonLdNode, extract_json_ld, published_at

log = logging.getLogger(__name__)

BOILERPLATE_REGIONS = frozenset({"nav", "header", "footer", "aside"})
_DESCRIPTION_KEYS = ("description", "og:description", "twitter:description")
# Stored with every page (spec section 24). Bump when parse output changes for
# the same input, so pages parsed by different versions can be told apart.
# 3: class/id region hints (main text), description, headings, microdata.
# 4: inline element boundaries spaced, aria-hidden inline text skipped.
# 5: an inline element with a box layout class (block, flex, grid) is a word break.
PARSER_VERSION = "5"

__all__ = [
    "BOILERPLATE_REGIONS", "PARSER_VERSION", "Link", "ListBlock", "MicrodataItem", "ParsedPage",
    "Table", "TextBlock", "parse_page", "primary_language",
]  # fmt: skip


@dataclass
class ParsedPage:
    url: str
    encoding: str | None = None
    title: str | None = None
    language: str | None = None
    canonical_url: str | None = None
    meta: dict[str, list[str]] = field(default_factory=dict)
    blocks: list[TextBlock] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    lists: list[ListBlock] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    json_ld: list[Any] = field(default_factory=list)
    json_ld_nodes: list[JsonLdNode] = field(default_factory=list)
    json_ld_errors: list[str] = field(default_factory=list)
    microdata: list[MicrodataItem] = field(default_factory=list)
    published_at: datetime | None = None
    published_at_source: str | None = None
    invalid_links: int = 0
    blocks_truncated: bool = False
    links_truncated: bool = False
    parse_error: str | None = None
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def cleaned_text(self) -> str:
        return "\n".join(block.text for block in self.blocks)

    @property
    def main_text(self) -> str:
        """Text outside navigation, headers, footers, sidebars and similar boilerplate.

        Duplicate detection uses this: pages sharing a large nav and footer
        but saying different things must not look alike. A page with nothing
        outside those regions falls back to all its text.
        """
        content = [b.text for b in self.blocks if b.region not in BOILERPLATE_REGIONS]
        return "\n".join(content) if content else self.cleaned_text

    # The name the duplicate-detection code has always used.
    content_text = main_text

    @property
    def description(self) -> str | None:
        for key in _DESCRIPTION_KEYS:
            if self.meta.get(key):
                return self.meta[key][0]
        return None

    @property
    def headings(self) -> list[tuple[int, str]]:
        return [(b.heading_level, b.text) for b in self.blocks if b.heading_level]

    @property
    def text_sha256(self) -> str:
        return text_sha256(self.main_text)

    @property
    def simhash(self) -> int:
        return simhash(self.main_text)


def parse_page(
    body: bytes,
    url: str,
    content_type: str | None,
    settings: ParseSettings,
    url_settings: UrlSettings,
) -> ParsedPage:
    """Parse an HTML response body fetched from ``url``. Never raises for bad input."""
    page = ParsedPage(url=url)
    try:
        doc, encoding = parse_document(body, content_type, page.parse_warnings)
        dom: DomExtract = extract_dom(doc, url, encoding, settings, url_settings)
        json_ld = extract_json_ld(doc)
        published, published_source = published_at(json_ld.nodes, dom.meta, doc)
    except (etree.ParserError, ValueError) as exc:
        page.parse_error = f"{type(exc).__name__}: {exc}"
        log.info("parse.failed", extra={"url": url, "error": page.parse_error})
        return page
    except Exception as exc:  # noqa: BLE001 - untrusted input must never stop the crawl
        page.parse_error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("parse.crashed", extra={"url": url, "error": page.parse_error},
                    exc_info=True)  # fmt: skip
        return page

    page.encoding = encoding
    page.title = dom.title
    page.language = dom.language or _meta_language(dom.meta)
    page.canonical_url = dom.canonical_url
    page.meta = dom.meta
    page.blocks = dom.blocks
    page.links = dom.links
    page.lists = dom.lists
    page.tables = dom.tables
    page.microdata = dom.microdata
    page.json_ld = json_ld.scripts
    page.json_ld_nodes = json_ld.nodes
    page.json_ld_errors = json_ld.errors
    page.published_at = published
    page.published_at_source = published_source
    page.invalid_links = dom.invalid_links
    page.blocks_truncated = dom.blocks_truncated
    page.links_truncated = dom.links_truncated
    if page.parse_warnings:
        log.info("parse.limited", extra={"url": url, "warnings": page.parse_warnings})
    log.info(
        "parse.completed",
        extra={
            "url": url,
            "blocks": len(page.blocks),
            "links": len(page.links),
            "json_ld_nodes": len(page.json_ld_nodes),
            "json_ld_errors": len(page.json_ld_errors),
            "microdata_items": len(page.microdata),
        },
    )
    return page


def _meta_language(meta: dict[str, list[str]]) -> str | None:
    for key in ("content-language", "og:locale"):
        if meta.get(key):
            return meta[key][0].strip().lower().replace("_", "-")
    return None


def primary_language(tag: str | None) -> str | None:
    """'en-US' -> 'en'; None for a missing or malformed tag."""
    primary = (tag or "").strip().lower().replace("_", "-").split("-")[0]
    return primary if primary.isalpha() and 2 <= len(primary) <= 3 else None
