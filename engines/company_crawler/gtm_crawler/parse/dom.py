"""HTML DOM extraction: text blocks, links, lists, tables, metadata.

Cleaning is non-destructive: navigation, headers and footers are kept (a
footer is often where a company's address or legal name appears) and each
text block is tagged with the page region it came from, so later stages can
weigh it rather than never seeing it. Every block records its XPath so any
quote can be located in the raw document again.

Only script, style and similar non-content elements are skipped.

Regions come from semantic tags and ARIA roles first, then from class and id
hints for layouts built from plain ``<div>``s (``class="site-menu"``,
``id="sidebar"``, a cookie banner, ...). Regions decide what counts as a
page's main text (``ParsedPage.main_text``, used for duplicate detection);
evidence extraction still sees every block.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

import lxml.html
from lxml import etree

from ..config import ParseSettings, UrlSettings
from ..urls import InvalidUrl, normalize_url

SKIP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "math", "iframe", "object",
        "embed", "canvas", "head", "select", "option", "textarea", "input",
    }
)  # fmt: skip
# Elements rendered inline: their text belongs to the enclosing block.
INLINE_TAGS = frozenset(
    {
        "a", "abbr", "b", "bdi", "bdo", "big", "br", "button", "cite", "code", "data",
        "del", "dfn", "em", "font", "i", "img", "ins", "kbd", "label", "mark", "picture",
        "q", "s", "samp", "small", "source", "span", "strike", "strong", "sub", "sup",
        "time", "tt", "u", "var", "wbr",
    }
)  # fmt: skip
REGION_TAGS = {
    "main": "main",
    "nav": "nav",
    "header": "header",
    "footer": "footer",
    "aside": "aside",
}
REGION_ROLES = {
    "main": "main", "navigation": "nav", "banner": "header",
    "contentinfo": "footer", "complementary": "aside", "menu": "nav", "menubar": "nav",
}  # fmt: skip
# Whole tokens of class / id values (split on non-alphanumerics) naming boilerplate.
REGION_HINTS = {
    **dict.fromkeys(("nav", "navbar", "navigation", "menu", "menubar", "megamenu",
                     "breadcrumb", "breadcrumbs", "pagination", "skiplink"), "nav"),
    **dict.fromkeys(("header", "masthead", "topbar"), "header"),
    **dict.fromkeys(("footer", "colophon"), "footer"),
    **dict.fromkeys(("sidebar", "widget", "cookie", "cookies", "consent", "gdpr",
                     "newsletter", "subscribe", "social", "share", "sharing", "advert",
                     "advertisement", "promo"), "aside"),
}  # fmt: skip
_HINT_TOKEN = re.compile(r"[a-z0-9]+")
_SCHEMA_PREFIX = re.compile(r"^https?://schema\.org/", re.I)

_WHITESPACE = re.compile(r"\s+")
# ``ParseSettings.inline_spacing``: an inline element boundary is a sentinel until the
# whole text is joined, then a space where the two sides read as separate words
# ("EmailOctopus" + "Pro", "Pro" + "$29"), nothing where they continue one ("$" + "29",
# "4" + ".60", "1" + ",000", "Mail" + "chimp"). Superscripts and subscripts attach.
_BOUNDARY = "\x00"
_BOUNDARIES = re.compile(r"\x00+")
_ATTACHED = frozenset({"sup", "sub", "wbr"})
# Class tokens that lay an inline element out as its own box (Tailwind/Bootstrap): a
# heading split into ``<span class="block">`` lines reads "URL" / "to", not "URLto".
_BOX_CLASSES = frozenset({"block", "flex", "grid", "d-block", "d-flex", "d-grid"})
_CURRENCY_SYMBOLS = frozenset("$€£₹¥")
_META_CHARSET = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
# WHATWG: these labels are decoded as windows-1252 by browsers.
_ENCODING_ALIASES = {"iso-8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252",
                     "us-ascii": "cp1252", "ascii": "cp1252"}  # fmt: skip
_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass(frozen=True)
class TextBlock:
    xpath: str
    tag: str
    text: str
    region: str | None = None
    heading_level: int | None = None


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    rel: str | None
    context: str


@dataclass(frozen=True)
class ListBlock:
    xpath: str
    ordered: bool
    items: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    xpath: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class MicrodataItem:
    """A Schema.org microdata item (``itemscope``): its type and property values."""

    xpath: str
    types: tuple[str, ...]
    properties: dict[str, list[str]]


@dataclass
class DomExtract:
    encoding: str
    title: str | None = None
    language: str | None = None
    base_url: str | None = None
    canonical_url: str | None = None
    meta: dict[str, list[str]] = field(default_factory=dict)
    blocks: list[TextBlock] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    lists: list[ListBlock] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    microdata: list[MicrodataItem] = field(default_factory=list)
    invalid_links: int = 0
    blocks_truncated: bool = False
    links_truncated: bool = False


def detect_encoding(body: bytes, content_type: str | None) -> str:
    """BOM, then the HTTP charset, then a <meta> declaration, then UTF-8."""
    for bom, encoding in _BOMS:
        if body.startswith(bom):
            return encoding
    candidates: list[str] = []
    if content_type:
        match = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", content_type, re.I)
        if match:
            candidates.append(match.group(1))
    meta = _META_CHARSET.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for label in candidates:
        label = _ENCODING_ALIASES.get(label.strip().lower(), label.strip().lower())
        try:
            return codecs.lookup(label).name
        except LookupError:
            continue
    return "utf-8"


def parse_document(
    body: bytes, content_type: str | None, warnings: list[str] | None = None
) -> tuple[lxml.html.HtmlElement, str]:
    """Decode and parse ``body``. Raises ``etree.ParserError`` for empty documents.

    libxml2's own safety limits apply (nesting deeper than 256 elements, huge
    text nodes): content beyond them is dropped, and each limit hit is
    appended to ``warnings`` so the loss is recorded, never silent.
    """
    encoding = detect_encoding(body, content_type)
    text = body.decode(encoding, "replace").lstrip("﻿")
    # lxml rejects str input that still carries an encoding declaration.
    text = _XML_DECLARATION.sub("", text, count=1)
    parser = lxml.html.HTMLParser(remove_comments=True, remove_pis=True, no_network=True)
    doc = lxml.html.document_fromstring(text, parser=parser)
    if warnings is not None:
        for entry in parser.error_log:
            message = (entry.message or "").strip()
            if "depth" in message.lower() or "huge" in message.lower():
                warnings.append(message[:200])
    return doc, encoding


def extract_dom(
    doc: lxml.html.HtmlElement,
    page_url: str,
    encoding: str,
    settings: ParseSettings,
    url_settings: UrlSettings,
) -> DomExtract:
    tree = doc.getroottree()
    out = DomExtract(encoding=encoding)
    out.language = (doc.get("lang") or "").strip().lower() or None
    out.meta = _meta(doc)
    out.title = _title(doc)

    base = page_url
    for element in doc.iter("base"):
        href = element.get("href")
        if href:
            try:
                base = normalize_url(href, url_settings, base=page_url)
            except InvalidUrl:
                pass
            break
    out.base_url = base

    for element in doc.iter("link"):
        rels = (element.get("rel") or "").lower().split()
        if "canonical" in rels and element.get("href"):
            try:
                out.canonical_url = normalize_url(element.get("href", ""), url_settings, base=base)
                break
            except InvalidUrl:
                continue

    body = doc.find("body")
    _Walker(tree, out, settings).walk(body if body is not None else doc, None)
    _links(doc, out, base, settings, url_settings)
    spacing, hidden = settings.inline_spacing, settings.skip_aria_hidden
    out.lists = [
        ListBlock(tree.getpath(el), el.tag == "ol",
                  tuple(t for li in el if li.tag == "li"
                        and (t := element_text(li, spacing, hidden))))
        for el in doc.iter("ul", "ol")
    ]  # fmt: skip
    out.lists = [lst for lst in out.lists if lst.items]
    out.tables = [_table(tree, el, spacing, hidden) for el in doc.iter("table")]
    out.tables = [t for t in out.tables if t.rows]
    out.microdata = _microdata(tree, doc, settings.max_microdata_items)
    return out


def normalize_space(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def element_text(element: etree._Element, spacing: bool = False, skip_hidden: bool = False) -> str:
    """Visible text of ``element``, with block boundaries turned into spaces."""
    parts: list[str] = []
    _collect(element, parts, " ", spacing, skip_hidden)
    return _joined(parts, spacing)


def _inline_text(element: etree._Element, spacing: bool = False, skip_hidden: bool = False) -> str:
    parts: list[str] = []
    # A block inside an inline element (<a><div>Pro</div><div>$29</div></a>) is a line
    # of its own when spacing; legacy text joins it to its neighbours.
    _collect(element, parts, " " if spacing else "", spacing, skip_hidden)
    return _joined(parts, spacing)


def _joined(parts: list[str], spacing: bool) -> str:
    text = "".join(parts)
    return normalize_space(_resolve_boundaries(text) if spacing else text)


def _resolve_boundaries(text: str) -> str:
    def sep(m: re.Match[str]) -> str:
        a = text[m.start() - 1] if m.start() else ""
        b = text[m.end()] if m.end() < len(text) else ""
        if not a or not b or a.isspace() or b.isspace():
            return ""
        words = (
            (a.islower() and b.isupper())  # "EmailOctopus" | "Pro"
            or (a.isalnum() and b.isalnum() and a.isdigit() != b.isdigit())  # "Pro" | "29"
            or (a.isalnum() and b in _CURRENCY_SYMBOLS)  # "Pro" | "$29"
            or (a in ".!?:;,)]" and b.isupper())  # "billed annually." | "Pro"
        )
        return " " if words else ""

    return _BOUNDARIES.sub(sep, text)


def _collect(element: etree._Element, parts: list[str], block_separator: str,
             spacing: bool = False, skip_hidden: bool = False) -> None:  # fmt: skip
    tag = _tag(element)
    if tag in SKIP_TAGS:
        return
    if tag == "br":
        parts.append(" ")
    if tag not in INLINE_TAGS:
        separator = block_separator
    elif spacing and _BOX_CLASSES.intersection((element.get("class") or "").split()):
        separator = " "
    else:
        separator = _BOUNDARY if spacing and tag not in _ATTACHED else ""
    parts.append(separator)
    parts.append(_clean(element.text, spacing))
    for child in element:
        if skip_hidden and _decoration(child):
            parts.append(_BOUNDARY if spacing else " ")  # a word boundary in its place
        elif _tag(child) is not None:
            _collect(child, parts, block_separator, spacing, skip_hidden)
        parts.append(_clean(child.tail, spacing))
    parts.append(separator)


def _decoration(element: etree._Element) -> bool:
    """An inline element screen readers skip (ParseSettings.skip_aria_hidden)."""
    hidden = (element.get("aria-hidden") or "").strip().lower() == "true"
    return hidden and _tag(element) in INLINE_TAGS


def _clean(text: str | None, spacing: bool) -> str:
    return (text or "").replace(_BOUNDARY, "") if spacing else text or ""


def _tag(element: etree._Element) -> str | None:
    return element.tag.lower() if isinstance(element.tag, str) else None


def _region(element: etree._Element, inside: str | None = None) -> str | None:
    role = (element.get("role") or "").strip().lower()
    if inside == "main":  # ParseSettings.landmark_regions
        tag = _tag(element) or ""
        return REGION_ROLES.get(role) or (REGION_TAGS.get(tag) if tag in ("aside", "nav") else None)
    region = REGION_ROLES.get(role) or REGION_TAGS.get(_tag(element) or "")
    if region is None and _tag(element) not in ("html", "body"):
        hints = f"{element.get('class') or ''} {element.get('id') or ''}".lower()
        for token in _HINT_TOKEN.findall(hints):
            if token in REGION_HINTS:
                return REGION_HINTS[token]
    return region


class _Walker:
    def __init__(self, tree: etree._ElementTree, out: DomExtract, settings: ParseSettings) -> None:
        self.tree = tree
        self.out = out
        self.settings = settings

    def walk(self, element: etree._Element, region: str | None) -> None:
        tag = _tag(element)
        if tag is None:
            return
        region = _region(element, region if self.settings.landmark_regions else None) or region
        children = [c for c in element if _tag(c) is not None]
        spacing, hidden = self.settings.inline_spacing, self.settings.skip_aria_hidden
        if all(_tag(c) in INLINE_TAGS or _tag(c) in SKIP_TAGS for c in children):
            self._emit(element, tag, _inline_text(element, spacing, hidden), region)
            return

        # Mixed content: loose text between child blocks is a block of its own,
        # emitted in document order. Legacy text puts a space around every inline
        # child ("$<b>29</b>" -> "$ 29"); spacing reads each boundary (_resolve_boundaries).
        pending: list[str] = [_clean(element.text, spacing)]
        for child in children:
            child_tag = _tag(child)
            if child_tag in SKIP_TAGS:
                pass
            elif hidden and _decoration(child):
                pending.append(_BOUNDARY if spacing else "")
            elif child_tag in INLINE_TAGS:
                if spacing:
                    parts: list[str] = []
                    _collect(child, parts, " ", spacing, hidden)
                    pending.append("".join(parts))
                else:
                    pending.append(_inline_text(child, skip_hidden=hidden))
            else:
                self._emit(element, tag, self._pending(pending), region)
                self.walk(child, region)
                pending = []
            pending.append(_clean(child.tail, spacing))
        self._emit(element, tag, self._pending(pending), region)

    def _pending(self, pending: list[str]) -> str:
        if self.settings.inline_spacing:
            return normalize_space(_resolve_boundaries("".join(pending)))
        return normalize_space(" ".join(pending))

    def _emit(self, element: etree._Element, tag: str, text: str, region: str | None) -> None:
        if not text:
            return
        if len(self.out.blocks) >= self.settings.max_blocks:
            self.out.blocks_truncated = True
            return
        level = int(tag[1]) if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456" else None
        self.out.blocks.append(TextBlock(self.tree.getpath(element), tag, text, region, level))


def _meta(doc: lxml.html.HtmlElement) -> dict[str, list[str]]:
    meta: dict[str, list[str]] = {}
    for element in doc.iter("meta"):
        key = next(
            (
                element.get(a)
                for a in ("property", "name", "http-equiv", "itemprop")
                if element.get(a)
            ),
            None,
        )
        content = element.get("content")
        if key and content is not None and content.strip():
            meta.setdefault(key.strip().lower(), []).append(content.strip())
    return meta


def _title(doc: lxml.html.HtmlElement) -> str | None:
    for path in ("//head/title", "//title"):
        found = doc.xpath(path)
        if found:
            return normalize_space(found[0].text_content()) or None
    return None


def _links(
    doc: lxml.html.HtmlElement,
    out: DomExtract,
    base: str,
    settings: ParseSettings,
    url_settings: UrlSettings,
) -> None:
    # Many links share one container (a nav list, a footer); compute its text once.
    context_cache: dict[etree._Element, str] = {}
    for element in doc.iter("a", "area"):
        href = element.get("href")
        if not href:
            continue
        if len(out.links) >= settings.max_links:
            out.links_truncated = True
            return
        try:
            url = normalize_url(href, url_settings, base=base)
        except InvalidUrl:
            out.invalid_links += 1
            continue
        text = element_text(element, settings.inline_spacing, settings.skip_aria_hidden)
        if not text:
            image = element.find(".//img")
            text = normalize_space(
                element.get("aria-label") or element.get("title")
                or (image.get("alt") if image is not None else None) or ""
            )  # fmt: skip
        container = element.getparent()
        while container is not None and _tag(container) in INLINE_TAGS:
            container = container.getparent()
        context = ""
        if container is not None:
            if container not in context_cache:
                around = element_text(container, settings.inline_spacing, settings.skip_aria_hidden)
                context_cache[container] = around[: settings.link_context_chars]
            context = context_cache[container]
        rel = normalize_space(element.get("rel") or "").lower() or None
        out.links.append(Link(url, text, rel, context))


def _microdata(
    tree: etree._ElementTree, doc: lxml.html.HtmlElement, limit: int
) -> list[MicrodataItem]:
    """Schema.org microdata: each ``itemscope`` with its own ``itemprop`` values.

    A property belongs to the nearest enclosing item; a nested item's properties
    are its own (the nested item is a separate entry).
    """
    items: list[MicrodataItem] = []
    for scope in doc.iter():
        if len(items) >= limit:
            break
        if not isinstance(scope.tag, str) or scope.get("itemscope") is None:
            continue
        types = tuple(_SCHEMA_PREFIX.sub("", t) for t in (scope.get("itemtype") or "").split() if t)
        props: dict[str, list[str]] = {}
        for element in scope.iterdescendants():
            if not isinstance(element.tag, str) or element.get("itemprop") is None:
                continue
            owner = next(
                (a for a in element.iterancestors() if a.get("itemscope") is not None), None
            )
            if owner is not scope:
                continue
            value = (element.get("content") or element.get("href") or element.get("src")
                     or element.get("datetime") or element_text(element))  # fmt: skip
            for name in element.get("itemprop", "").split():
                if value:
                    props.setdefault(name, []).append(value[:1000])
        items.append(MicrodataItem(tree.getpath(scope), types, props))
    return items


def _table(tree: etree._ElementTree, table: etree._Element, spacing: bool = False,
           skip_hidden: bool = False) -> Table:  # fmt: skip
    rows = []
    for row in table.iter("tr"):
        # Rows of a nested table belong to that table.
        if next(row.iterancestors("table"), None) is not table:
            continue
        cells = tuple(element_text(cell, spacing, skip_hidden)
                      for cell in row if _tag(cell) in ("td", "th"))  # fmt: skip
        if any(cells):
            rows.append(cells)
    return Table(tree.getpath(table), tuple(rows))
