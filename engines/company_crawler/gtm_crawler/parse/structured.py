"""Structured data: JSON-LD and publication dates.

Structured data is evidence, not truth: it is kept verbatim (each JSON-LD
script as parsed) and every typed node is addressable by a JSON path such as
``$.jsonld[0]['@graph'][2]`` for use as an evidence locator. Malformed scripts
are recorded as errors, never guessed at.

OpenGraph, Twitter and other ``<meta>`` values are collected by the DOM
extractor into ``meta``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import lxml.html

_JSON_LD_TYPE = re.compile(r"^\s*application/ld\+json\s*(;.*)?$", re.I)
_WRAPPERS = re.compile(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)|(-->|//\s*\]\]>|\]\]>)\s*$")
_SCHEMA_PREFIX = re.compile(r"^https?://schema\.org/", re.I)
_MAX_NODE_DEPTH = 64

ARTICLE_TYPES = frozenset(
    {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle", "ScholarlyArticle"}
)
# Checked in order; the first parseable value wins.
PUBLISHED_META_KEYS = (
    "article:published_time", "og:published_time", "datepublished", "date",
    "dc.date.issued", "dc.date", "dcterms.issued", "pubdate", "publish-date", "publish_date",
)  # fmt: skip


@dataclass(frozen=True)
class JsonLdNode:
    path: str
    types: tuple[str, ...]
    data: dict[str, Any]


@dataclass
class JsonLdExtract:
    scripts: list[Any]
    nodes: list[JsonLdNode]
    errors: list[str]


def extract_json_ld(doc: lxml.html.HtmlElement) -> JsonLdExtract:
    out = JsonLdExtract([], [], [])
    for script in doc.iter("script"):
        if not _JSON_LD_TYPE.match(script.get("type") or ""):
            continue
        index = len(out.scripts) + len(out.errors)
        raw = _WRAPPERS.sub("", (script.text or "").strip()).strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            out.errors.append(f"jsonld[{index}]: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        out.scripts.append(data)
        out.nodes.extend(_typed_nodes(data, f"$.jsonld[{len(out.scripts) - 1}]", 0))
    return out


def node_types(node: dict[str, Any]) -> tuple[str, ...]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return tuple(_SCHEMA_PREFIX.sub("", v) for v in values if isinstance(v, str) and v)


def _typed_nodes(value: Any, path: str, depth: int) -> Iterator[JsonLdNode]:
    if depth > _MAX_NODE_DEPTH:
        return
    if isinstance(value, dict):
        types = node_types(value)
        if types:
            yield JsonLdNode(path, types, value)
        for key, child in value.items():
            yield from _typed_nodes(child, f"{path}[{json.dumps(key)}]", depth + 1)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _typed_nodes(child, f"{path}[{i}]", depth + 1)


def published_at(
    nodes: list[JsonLdNode], meta: dict[str, list[str]], doc: lxml.html.HtmlElement
) -> tuple[datetime | None, str | None]:
    """The page's own publication date and where it was read from.

    Only explicit publication markers are used; dates found in body text and
    modification dates are not treated as publication dates.
    """
    for node in nodes:
        if ARTICLE_TYPES.intersection(node.types):
            parsed = parse_datetime(node.data.get("datePublished"))
            if parsed:
                return parsed, f"json-ld:{node.path}.datePublished"
    for key in PUBLISHED_META_KEYS:
        for value in meta.get(key, []):
            parsed = parse_datetime(value)
            if parsed:
                return parsed, f"meta:{key}"
    for element in doc.xpath("//time[@itemprop='datePublished' or @pubdate][@datetime]"):
        parsed = parse_datetime(element.get("datetime"))
        if parsed:
            return parsed, "time[datetime]"
    return None, None


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601 dates and datetimes. A value without a timezone is taken as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# Properties whose value names a page (spec section 6: structured-data URLs).
URL_PROPERTIES = ("url", "@id", "mainEntityOfPage", "sameAs")


def structured_urls(nodes: list[JsonLdNode], limit: int = 200) -> list[str]:
    """URLs that typed JSON-LD nodes point to, in document order, unnormalized.

    A value may be a string, a list of strings, or a node reference
    (``{"@id": ...}`` / ``{"url": ...}``). Only http(s) values are returned.
    """
    out: dict[str, None] = {}

    def add(value: Any) -> None:
        if isinstance(value, str):
            if value.lower().startswith(("http://", "https://")):
                out.setdefault(value.strip())
        elif isinstance(value, list):
            for item in value[:limit]:
                add(item)
        elif isinstance(value, dict):
            add(value.get("@id") or value.get("url"))

    for node in nodes:
        for prop in URL_PROPERTIES:
            add(node.data.get(prop))
        if len(out) >= limit:
            break
    return list(out)[:limit]
