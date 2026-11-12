"""GTM signals extraction (production spec section 17): customers and case
studies, integrations, hiring, office geography and technology.

Same precision rules as the other extractors: a sentence counts only when
the company is its subject (``Context.is_subject_of``); page-level signals
(case study and integration pages, job postings, the site's generator tag)
count only on the company's own site.

Fields:

* ``customer``: organizations the company names as customers ("our customers
  include A, B and C"; on its own site, a sentence opening "Trusted by A and
  B"). A list followed by a generic noun ("Fortune 500 companies") names no
  one and is skipped.
* ``case_study``: the ``<h1>`` of a page at ``/customers/<slug>``,
  ``/case-studies/<slug>`` and similar.
* ``integration``: "integrates with A, B and C", and the product named by the
  ``<h1>`` of an ``/integrations/<slug>`` page ("Acme + Salesforce" is
  Salesforce).
* ``job_opening``: JSON-LD ``JobPosting`` titles (date posted as ``as_of``).
* ``open_roles``: "we have 12 open positions" (qualifiers kept).
* ``office_location``: "we have offices in London, New York and Berlin".
* ``website_technology``: the site's ``<meta name="generator">``, which
  states what the site is built with. Nothing is inferred from script URLs,
  headers or markup patterns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ..db.evidence import ObservationDraft
from ..evidence import QuoteError, quote_block, quote_json, quote_meta
from ..parse import ParsedPage
from .common import AMOUNT_QUALIFIERS, NOT_PROPER_NOUNS, QUALIFIER, Context, Sentence, sentences
from .common import draft as _draft
from .traction import _NAME_LIST, _names

VERSION = "5"

_CUSTOMERS = re.compile(
    r"\b(?:customers|clients)\s+(?:include|including|such\s+as|like|are)\s+" + _NAME_LIST
)
_TRUSTED = re.compile(
    r"^(?:Trusted|Used|Loved|Chosen)\s+by\s+(?:(?:teams|companies|brands|leaders)\s+"
    r"(?:at|like|such\s+as)\s+)?" + _NAME_LIST
)
_INTEGRATES = re.compile(
    r"\b(?:integrates|integrate|integrating|integrations?|connects|connect)\s+"
    r"(?:natively\s+)?(?:with|to)\s+" + _NAME_LIST
)
# "offices in", and a training institute's or clinic's "branches / centres in".
_OFFICES = re.compile(
    r"\b(?:offices?|branch(?:es)?|cent(?:re|er)s|campuses)\s+(?:in|across|at)\s+" + _NAME_LIST
)
_OPEN_ROLES = re.compile(
    rf"{QUALIFIER}(?P<n>\d{{1,4}})(?P<plus>\+)?\s+open\s+(?:positions|roles|jobs|vacancies"
    r"|opportunities)\b"
)
# A generic noun right after the list: "Trusted by Fortune 500 companies".
_GENERIC_TAIL = re.compile(r"\s+(?!and\b)[a-z]")
_CASE_STUDY_PATH = re.compile(
    r"^/(?:customers|case-studies|case-study|customer-stories|success-stories)/[^/]+/?$"
)
_INTEGRATION_PATH = re.compile(r"^/(?:integrations|apps|marketplace)/[^/]+/?$")
_CONNECTORS = re.compile(
    r"\s*(?:[+&|/×]|\b(?:x|with|for|and|integrations?|connector)\b)\s*", re.IGNORECASE
)


def extract_signals(page: ParsedPage, ctx: Context) -> list[ObservationDraft]:
    out = list(_from_text(page, ctx))
    if ctx.first_party:
        out += [*_case_study(page), *_integration_page(page, ctx), *_job_postings(page, ctx),
                *_generator(page)]  # fmt: skip
    return out


def draft(field_name: str, value: str, quote: Any, certainty: float, **kw: Any) -> ObservationDraft:
    q = quote.quote() if isinstance(quote, Sentence) else quote
    return _draft("signals", VERSION, field_name, value, q, certainty, **kw)


def _from_text(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for s in sentences(page):
        text = s.text
        if ctx.first_party:
            m = _TRUSTED.match(text)
            if m and not _GENERIC_TAIL.match(text, m.end()):
                for name in _names(m.group("list")):
                    yield draft("customer", name, s, 0.7)
        if not ctx.refers_to_company(text):
            continue
        for pattern, field_name, certainty in ((_CUSTOMERS, "customer", 0.8),
                                               (_INTEGRATES, "integration", 0.8),
                                               (_OFFICES, "office_location", 0.8)):  # fmt: skip
            for m in pattern.finditer(text):
                if ctx.is_subject_of(text, m.start()) and not _GENERIC_TAIL.match(text, m.end()):
                    split = _places if field_name == "office_location" else _names
                    for name in split(m.group("list")):
                        yield draft(field_name, name, s, certainty)
        for m in _OPEN_ROLES.finditer(text):
            if ctx.is_subject_of(text, m.start()):
                qualifier = "gte" if m.group("plus") else AMOUNT_QUALIFIERS.get(
                    (m.group("q") or "").lower(), "eq")  # fmt: skip
                normalized = {"qualifier": qualifier, "value": int(m.group("n"))}
                yield draft("open_roles", m.group().strip(), s, 0.8, normalized=normalized)


def _places(listed: str) -> list[str]:
    """Place names from a list ("London, New York and Berlin").

    "out of the office in December": a month is not a place.
    """
    parts = (p.strip().rstrip(".") for p in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", listed))
    return [p for p in parts if p and p.split()[0] not in NOT_PROPER_NOUNS]


def _h1(page: ParsedPage) -> Any:
    return next((b for b in page.blocks if b.heading_level == 1 and b.text.strip()), None)


def _case_study(page: ParsedPage) -> Iterator[ObservationDraft]:
    h1 = _h1(page)
    if h1 is not None and _CASE_STUDY_PATH.match(urlsplit(page.url).path):
        yield draft("case_study", h1.text.strip(), quote_block(h1, 0, len(h1.text)), 0.85)


def _integration_page(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    h1 = _h1(page)
    if h1 is None or len(h1.text) > 60 or not _INTEGRATION_PATH.match(urlsplit(page.url).path):
        return
    remainder = h1.text
    for name in sorted(ctx.names, key=len, reverse=True):
        remainder = re.sub(rf"(?i)(?<!\w){re.escape(name)}(?!\w)", " ", remainder)
    parts = [p for p in _CONNECTORS.split(remainder) if p.strip()]
    if len(parts) == 1 and parts[0].strip() in h1.text:
        yield draft("integration", parts[0].strip(), quote_block(h1, 0, len(h1.text)), 0.8)


def _job_postings(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for node in page.json_ld_nodes:
        if "JobPosting" not in node.types:
            continue
        hiring = node.data.get("hiringOrganization")
        if isinstance(hiring, dict):
            org_url = str(hiring.get("sameAs") or hiring.get("url") or "")
            org_name = str(hiring.get("name") or "")
            if org_url and ctx.domain not in org_url and org_name not in ctx.names:
                continue  # a posting for another organization
        try:
            quote = quote_json(page, f'{node.path}["title"]')
        except QuoteError:
            continue
        posted = _date(node.data.get("datePosted"))
        d = draft("job_opening", quote.text, quote, 0.9)
        yield d if posted is None else replace(d, as_of=posted)


def _generator(page: ParsedPage) -> Iterator[ObservationDraft]:
    for index, value in enumerate(page.meta.get("generator", [])[:3]):
        if value.strip():
            yield draft("website_technology", value, quote_meta(page, "generator", index), 0.9)


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
