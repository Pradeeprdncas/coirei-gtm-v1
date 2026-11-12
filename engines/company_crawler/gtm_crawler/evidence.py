"""Evidence locators and source classification.

A ``Quote`` is the exact text an observation rests on plus where it sits:
a text block's XPath with character offsets into that block's text, a
JSON-LD value's JSON path, or a ``<meta>`` tag's key and index
(``og:site_name[0]``). ``resolve`` re-derives the quote from a parsed page,
so any stored quote can be checked against the raw document it came from.

``classify_source`` places a page in the source hierarchy relative to the
organization an observation is about: the same page is first-party for its
own company and third-party for everyone else. Tier describes source
quality only; it never makes a statement true.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import SourceSettings
from .parse import ParsedPage, TextBlock
from .urls import registrable_domain, url_host

_META_LOCATOR = re.compile(r"^(.+)\[(\d+)\]$")
_JSON_STEP = re.compile(r'\[(\d+|"(?:[^"\\]|\\.)*")\]')
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# First-party page types, checked in order against host labels and path segments.
_FIRST_PARTY_TYPES = (
    ("official_investor_information", {"investors", "investor-relations", "ir"}),
    ("official_press_release", {"press", "newsroom", "news", "press-releases", "media"}),
    ("official_pricing", {"pricing", "plans", "price", "prices"}),
    ("official_documentation", {"docs", "documentation", "developers", "developer", "api"}),
    ("official_product_page", {"product", "products", "platform", "features", "solutions"}),
)


# Observation confidence, approved 2026-09-29 (plan section 11):
#   extraction_certainty x source weight x kind factor.
# It scores one observation from one source. Corroboration, conflicts, freshness
# and the CORROBORATED / SUPPORTED thresholds are assessed per field, not here.
REGULATORY_WEIGHT = 0.95
FIRST_PARTY_WEIGHT = 0.90
TIER_WEIGHTS = {1: 0.95, 2: 0.80, 3: 0.65, 4: 0.45}
KIND_FACTORS = {"stated": 1.0, "derived": 0.9}


class QuoteError(ValueError):
    """A quote that is not exactly present at its claimed location."""


@dataclass(frozen=True)
class Quote:
    locator_type: str  # "xpath", "json_path", "meta" or "page"
    locator: str
    text: str
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True)
class Source:
    tier: int
    type: str
    first_party: bool


def quote_block(block: TextBlock, start: int, end: int) -> Quote:
    """The text of ``block`` between character offsets ``start`` and ``end``."""
    if not 0 <= start < end <= len(block.text):
        raise QuoteError(f"offsets {start}:{end} outside block of {len(block.text)} chars")
    text = block.text[start:end]
    if not text.strip():
        raise QuoteError("quote is blank")
    return Quote("xpath", block.xpath, text, start, end)


def find_quote(page: ParsedPage, needle: str) -> Quote | None:
    """The first exact occurrence of ``needle`` in the page's text blocks."""
    for block in page.blocks:
        start = block.text.find(needle)
        if start >= 0 and needle.strip():
            return quote_block(block, start, start + len(needle))
    return None


def quote_json(page: ParsedPage, path: str) -> Quote:
    """A scalar JSON-LD value, e.g. ``$.jsonld[0]["foundingDate"]``.

    Strings are quoted as-is; numbers and booleans as their JSON text.
    """
    value = _json_value(page.json_ld, path)
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool | int | float):
        text = json.dumps(value)
    else:
        raise QuoteError(f"{path}: not a scalar value")
    if not text.strip():
        raise QuoteError(f"{path}: value is blank")
    return Quote("json_path", path, text)


def quote_meta(page: ParsedPage, key: str, index: int = 0) -> Quote:
    """The content of the ``index``-th ``<meta>`` tag named ``key`` (lowercase)."""
    values = page.meta.get(key, [])
    if not 0 <= index < len(values):
        raise QuoteError(f"no meta {key}[{index}]")
    return Quote("meta", f"{key}[{index}]", values[index])


def quote_page(page: ParsedPage, prop: str) -> Quote:
    """The page's own ``title`` (its <title>) or ``url`` (the address it was served at)."""
    text = _page_props(page).get(prop)
    if not text:
        raise QuoteError(f"no page {prop}")
    return Quote("page", prop, text)


def _page_props(page: ParsedPage) -> dict[str, str | None]:
    return {"title": page.title, "url": page.url}


def resolve(page: ParsedPage, quote: Quote) -> str | None:
    """Re-derive ``quote``'s text from ``page``; None if its location no longer exists."""
    if quote.locator_type == "json_path":
        try:
            return quote_json(page, quote.locator).text
        except QuoteError:
            return None
    if quote.locator_type == "page":
        return _page_props(page).get(quote.locator)
    if quote.locator_type == "meta":
        m = _META_LOCATOR.match(quote.locator)
        values = page.meta.get(m.group(1), []) if m else []
        index = int(m.group(2)) if m else -1
        return values[index] if 0 <= index < len(values) else None
    # An element with mixed content yields several blocks sharing its XPath
    # (the text runs between its child blocks); the quote sits in one of them.
    found: str | None = None
    for block in page.blocks:
        if block.xpath == quote.locator:
            if quote.char_start is None or quote.char_end is None:
                found = block.text
            else:
                found = block.text[quote.char_start : quote.char_end]
            if found == quote.text:
                break
    return found


def _json_value(scripts: list[Any], path: str) -> Any:
    if not path.startswith("$.jsonld"):
        raise QuoteError(f"{path}: must start with $.jsonld")
    rest = path[len("$.jsonld") :]
    steps = _JSON_STEP.findall(rest)
    if "".join(f"[{s}]" for s in steps) != rest or not steps:
        raise QuoteError(f"{path}: malformed JSON path")
    value: Any = scripts
    for step in steps:
        key: str | int = int(step) if step.isdigit() else json.loads(step)
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise QuoteError(f"{path}: no value at {step}") from exc
    return value


def classify_source(url: str, subject_domain: str | None, settings: SourceSettings) -> Source:
    """Where a page at ``url`` sits in the source hierarchy for ``subject_domain``'s company."""
    host = url_host(url)
    domain = registrable_domain(host)
    if urlsplit(url).scheme == "upload":
        # A document the company gave about itself (documents.py): its own word.
        return Source(1, "company_document", domain == subject_domain)
    if subject_domain is not None and domain == subject_domain:
        labels = host.removesuffix(domain).split(".")
        tokens = set(labels) | set(_TOKEN.findall(urlsplit(url).path.lower()))
        for source_type, terms in _FIRST_PARTY_TYPES:
            if tokens & terms:
                return Source(1, source_type, True)
        return Source(1, "official_website", True)

    def listed(domains: tuple[str, ...]) -> bool:
        return any(host == d or host.endswith("." + d) for d in domains)

    if listed(settings.regulatory_domains):
        return Source(1, "regulatory_filing", False)
    if listed(settings.business_database_domains):
        return Source(2, "business_database", False)
    if listed(settings.major_publication_domains):
        return Source(2, "major_publication", False)
    if listed(settings.industry_publication_domains):
        return Source(3, "industry_publication", False)
    if listed(settings.professional_profile_domains):
        return Source(3, "professional_profile", False)
    return Source(4, "unverified_third_party", False)


def observation_confidence(extraction_certainty: float, source: Source, kind: str) -> float:
    weight = FIRST_PARTY_WEIGHT if source.first_party else TIER_WEIGHTS[source.tier]
    return round(extraction_certainty * weight * KIND_FACTORS[kind], 2)
