"""Traction extraction (GTM Parameters V1.0, section 4).

Traction is high-risk, so these rules only record what a sentence states
about the company itself, and never compute one figure from another (plan
section 28): funding is never valuation, a round is never total funding,
customers are never an exact count unless stated, and nothing is estimated
from prices, traffic or headcount.

Fields: arr, mrr, annual_revenue, customer_count, customer_concentration,
funding_round, total_funding, valuation, investor, technology_partnership,
strategic_partnership, partnership, product_sales.

``total_funding`` is recorded only when the source says the figure is a
total ("to date", "in total", "total funding of"); otherwise a raised amount
is a ``funding_round``. Investors and explicitly typed partnerships are also
stored as relationships. A partnership whose kind is not stated ("partnered
with X") is recorded as ``partnership`` with no relationship type.

A statement counts only if the company is its subject: a self-reference
("we", "our", its name) must precede the claim with no other proper noun in
between (see ``Context.is_subject_of``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from ..db.evidence import ObservationDraft, RelatedOrg
from ..evidence import quote_block, quote_json
from ..parse import ParsedPage
from .common import (
    AMOUNT_QUALIFIERS,
    MONEY,
    QUALIFIER,
    Context,
    Sentence,
    money,
    reported,
    sentences,
)
from .common import draft as _draft
from .company import _org_nodes
from .product import BOILERPLATE_REGIONS, _page_product

EXTRACTOR = "traction"
VERSION = "4"

M = QUALIFIER + MONEY
_COUNT = r"(?<![\d,.])(?P<n>\d{1,3}(?:,\d{3})+|\d+)(?P<plus>\+)?"
_YEAR = re.compile(r"\b(?:in|during|for|as of|by(?: the end of)?|FY)\s?((?:19|20)\d{2})\b")

_ARR = [
    re.compile(rf"{M}\s+(?:in\s+)?(?:ARR|annual recurring revenue)\b"),
    re.compile(rf"\b(?:ARR|annual recurring revenue)\s+(?:of|reached|exceeded|surpassed|topped|"
               rf"grew to|to|passed)\s+{M}"),
]  # fmt: skip
_MRR = [
    re.compile(rf"{M}\s+(?:in\s+)?(?:MRR|monthly recurring revenue)\b"),
    re.compile(rf"\b(?:MRR|monthly recurring revenue)\s+(?:of|reached|exceeded|surpassed|"
               rf"topped|grew to|to|passed)\s+{M}"),
]  # fmt: skip
_REVENUE = [
    re.compile(rf"{M}\s+in\s+(?:annual\s+|total\s+|yearly\s+)?revenues?\b"),
    re.compile(rf"(?<!recurring\s)\b(?:annual\s+|yearly\s+|total\s+)?revenues?\s+(?:of|reached|"
               rf"exceeded|surpassed|topped|grew to|was|were|totaled|totalled|passed)\s+{M}"),
]  # fmt: skip
_CUSTOMERS = re.compile(
    rf"{QUALIFIER}{_COUNT}\s+(?:paying\s+)?(?:customers|clients|businesses|companies|"
    r"organizations|organisations|brands|teams|enterprises)\b"
)
_USED_BY = re.compile(
    r"\s+(?:use|uses|trust|trusts|rely\s+on|relies\s+on|choose|chose|run\s+on|love)\s+"
    r"(?:us\b|our\b|[A-Z])"
)
_CONCENTRATION = re.compile(
    r"\b(?:(?:our\s+)?(?:single\s+)?(?:largest|biggest)\s+customer|top\s+(?:\d+|ten|five|three|"
    r"twenty)\s+customers|no\s+(?:single\s+)?customer|any\s+single\s+customer)\b[^.;]{0,60}?"
    rf"\b(?:accounted|accounts|account|represented|represents|represent|contributed|made up|"
    rf"makes up|comprised)\s+(?:for\s+)?{QUALIFIER}(?P<pct>\d+(?:\.\d+)?)\s?%\s+of\s+(?:our\s+)?"
    r"(?:total\s+|net\s+)?(?:revenues?|sales)\b",
    re.IGNORECASE,
)
_ROUND = r"(?P<round>pre-seed|seed|series\s+[a-h](?:-?\d)?(?:\s+extension)?|growth|bridge)"
_RAISED = re.compile(
    r"\b(?:raised|raises|raising|closed|closes|secured|secures|announced|announces|completed)"
    rf"\s+(?:an?\s+|its\s+|our\s+|a\s+total\s+of\s+)?{M}"
    rf"(?:\s+(?:in\s+)?{_ROUND}\b)?"
    r"(?:\s+(?:funding|financing|investment|venture)\b)?(?:\s+round\b)?",
    re.IGNORECASE,
)
_ROUND_OF = re.compile(
    rf"\b{_ROUND}\s+(?:funding\s+|financing\s+|investment\s+)?round\s+of\s+{M}", re.IGNORECASE
)
_TOTAL_MARKER = re.compile(
    r"\s*(?:in\s+total|to\s+date|in\s+total\s+funding|total|since\s+(?:our\s+)?(?:founding|"
    r"inception|launch))\b",
    re.IGNORECASE,
)
_TOTAL = [
    re.compile(
        rf"\btotal\s+(?:funding|capital\s+raised|investment|raised)\s+(?:of|to|is|"
        rf"stands\s+at|to\s+date\s+(?:of|is))?\s*:?\s*{M}",
        re.IGNORECASE,
    ),
    re.compile(rf"\braised\s+a\s+total\s+of\s+{M}", re.IGNORECASE),
]
_VALUATION = [
    re.compile(rf"\b(?:valued\s+at|(?P<basis>post-money|pre-money)?\s*valuation\s+of|at\s+an?\s+"
               rf"valuation\s+of)\s+{M}", re.IGNORECASE),
    re.compile(rf"{M}\s+(?:(?P<basis>post-money|pre-money)\s+)?valuation\b", re.IGNORECASE),
]  # fmt: skip

_NAME = r"[A-Z0-9][\w&.'’-]*(?:\s+(?:[A-Z0-9&][\w&.'’-]*|of|de|du|la|von))*"
_NAME_LIST = rf"(?P<list>{_NAME}(?:\s*,\s*(?:and\s+)?{_NAME}|\s+and\s+{_NAME})*)"
_INVESTORS = re.compile(
    r"\b(?:(?:co-)?led\s+by|backed\s+by|participation\s+(?:from|by)|investors?\s+(?:include|"
    r"including|are)|investments?\s+from|funded\s+by|joined\s+by|funding\s+from)\s+" + _NAME_LIST
)
_STRATEGIC = re.compile(
    r"\bstrategic\s+(?:partnership|alliance)\s+with\s+" + _NAME_LIST
    + r"|\bstrategic\s+partners\s+(?:include|including|are|such\s+as)\s+" + _NAME_LIST.replace(
        "?P<list>", "?P<list2>")
)  # fmt: skip
_TECHNOLOGY = re.compile(
    r"\b(?:technology|integration|tech)\s+(?:partnership\s+with|partners\s+(?:include|including|"
    r"are|such\s+as))\s+" + _NAME_LIST
)
_PARTNER_BADGE = re.compile(r"\b(?:an?|as\s+an?)\s+(?P<badge>" + _NAME + r")\s+(?i:partner)\b")
_GENERIC_PARTNER = re.compile(
    r"(?<!strategic\s)(?<!technology\s)\b(?:partnered|partnering|partners|partnership)\s+with\s+"
    + _NAME_LIST
)
# "We have no partnership with X", "we are not partnered with X".
_NEGATED = re.compile(r"\b(?:no|not|never|without|\w+n't)\s+(?:[\w-]+\s+){0,3}$", re.IGNORECASE)
_BADGE_TIERS = frozenset(
    {"Advanced", "Select", "Premier", "Gold", "Silver", "Platinum", "Certified", "Official",
     "ISV", "Technology", "Solution", "Solutions", "Registered", "Preferred", "Elite",
     "Consulting", "Integration", "Tech", "Partner", "Network"}
)  # fmt: skip
_GLUED = re.compile(r"\.(?=[A-Z])\S*\s.*$")
_NUMBER = re.compile(r"[\d,.]+\+?(?:\s|$)")  # "500+ teams", not "3M" or "1Password"
_NOT_ORGS = frozenset(
    {"We", "Our", "Us", "The", "This", "Series", "Seed", "Existing", "New", "Leading", "Other",
     "Several", "Many", "Top", "Both", "All", "Trusted", "Proud", "Key", "Valued", "Reliable",
     "Strategic", "Global", "Local",
     # Quantities and kinds of people, not names: "Trusted by Thousands of Students".
     "Thousands", "Hundreds", "Millions", "Dozens", "Over", "More", "Teams", "Companies",
     "Students", "Customers", "Businesses", "Developers", "Professionals", "Everyone", "People"}
)  # fmt: skip
_UNITS_SOLD = [
    re.compile(rf"{QUALIFIER}{_COUNT}\s+(?:units|licenses|licences|copies|devices|robots)\s+"
               r"(?:sold|shipped)\b"),
    re.compile(rf"\b(?:sold|shipped)\s+{QUALIFIER}{_COUNT}\s+(?:units|licenses|licences|"
               r"copies|devices|robots)\b"),
]  # fmt: skip
_PARTNER_SECTION = re.compile(
    r"\b(?P<kind>technology|integration|tech|strategic|channel|alliance)\s+partners?\b", re.I
)


def extract_traction(page: ParsedPage, ctx: Context) -> list[ObservationDraft]:
    sections = _partner_sections(page) if ctx.first_party else iter(())
    return [*_from_json_ld(page, ctx), *_from_text(page, ctx), *sections]


def draft(field_name: str, value: str, s: Any, certainty: float, **kw: Any) -> ObservationDraft:
    quote = s.quote() if isinstance(s, Sentence) else s
    return _draft(EXTRACTOR, VERSION, field_name, value, quote, certainty, **kw)


def _from_json_ld(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for node in _org_nodes(page, ctx.domain, ctx.first_party):
        funders = node.data.get("funder")
        items = funders if isinstance(funders, list) else [funders]
        for i, funder in enumerate(items):
            if not isinstance(funder, dict) or not isinstance(funder.get("name"), str):
                continue
            index = f"[{i}]" if isinstance(funders, list) else ""
            quote = quote_json(page, f'{node.path}["funder"]{index}["name"]')
            yield draft("investor", quote.text, quote, 0.9,
                        related=RelatedOrg("investor", quote.text, "json_ld_funder"))  # fmt: skip


def _from_text(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    named = _page_product(page, ctx)
    product = ctx.product_ids.get(named[0]) if named else None
    for s in sentences(page):
        text = s.text
        if not ctx.refers_to_company(text):
            continue
        year = _YEAR.search(text)
        # Figures the sentence attributes to a third party keep that attribution.
        extra = {**({"year": int(year.group(1))} if year else {}), **reported(text, ctx)}

        def ok(m: re.Match[str], text: str = text) -> bool:
            return ctx.is_subject_of(text, m.start())

        def partner(m: re.Match[str], text: str = text) -> bool:
            return ok(m) and not _NEGATED.search(text[: m.start()])

        for field_name, patterns in (("arr", _ARR), ("mrr", _MRR), ("annual_revenue", _REVENUE)):
            for pattern in patterns:
                for m in pattern.finditer(text):
                    if ok(m):
                        yield _money(s, m, field_name, 0.8, extra)
        for m in _CUSTOMERS.finditer(text):
            # "2,000 companies use Acme": the company is named after the claim.
            if ok(m) or _USED_BY.match(text, m.end()) and ctx.refers_to_company(text[m.end() :]):
                yield _count(s, m, "customer_count", 0.7, extra)
        for m in _CONCENTRATION.finditer(text):
            if ctx.refers_to_company(text[: m.end()]):
                yield draft("customer_concentration", m.group(), s, 0.8)
        yield from _funding(s, ok, extra)
        for pattern in _VALUATION:
            for m in pattern.finditer(text):
                if ok(m):
                    basis = (m.group("basis") or "").lower() or None
                    yield _money(s, m, "valuation", 0.8, {**extra, "basis": basis})
        # "led by X with participation from Y": once one investor clause is about
        # the company, the following ones in the sentence are too.
        chained = False
        for m in _INVESTORS.finditer(text):
            if chained or ok(m):
                chained = True
                yield from _related(s, m, "list", "investor", "investor", "stated_investor")
        for m in _STRATEGIC.finditer(text):
            if partner(m):
                group = "list" if m.group("list") else "list2"
                yield from _related(s, m, group, "strategic_partnership", "strategic_partner",
                                    "stated_strategic_partnership")  # fmt: skip
        for m in _TECHNOLOGY.finditer(text):
            if partner(m):
                yield from _related(s, m, "list", "technology_partnership", "tech_partner",
                                    "stated_technology_partnership")  # fmt: skip
        for m in _PARTNER_BADGE.finditer(text):
            name = _strip_tiers(m.group("badge"))
            if name and partner(m):
                yield draft("technology_partnership", name, s, 0.8,
                            related=RelatedOrg("tech_partner", name, "partner_badge"))  # fmt: skip
        for m in _GENERIC_PARTNER.finditer(text):
            if partner(m):
                for name in _names(m.group("list")):
                    yield draft(
                        "partnership",
                        name,
                        s,
                        0.7,
                        normalized={"qualifier": "eq", "type": "unspecified"},
                    )
        for pattern in _UNITS_SOLD:
            for m in pattern.finditer(text):
                if ok(m):
                    d = _count(s, m, "product_sales", 0.7, extra)
                    yield d if product is None else replace(d, product_id=product)


def _funding(s: Sentence, ok: Any, extra: dict[str, Any]) -> Iterator[ObservationDraft]:
    text = s.text
    totals = [m for pattern in _TOTAL for m in pattern.finditer(text) if ok(m)]
    for m in totals:
        yield _money(s, m, "total_funding", 0.8, extra)
    covered = [(m.start(), m.end()) for m in totals]
    for pattern in (_RAISED, _ROUND_OF):
        for m in pattern.finditer(text):
            if not ok(m) or any(a <= m.start() < b for a, b in covered):
                continue
            if _TOTAL_MARKER.match(text, m.end()):
                # "raised $50M to date": a total, stated as such.
                end = _TOTAL_MARKER.match(text, m.end())
                assert end is not None
                yield _money(s, m, "total_funding", 0.8, extra, end=end.end())
                continue
            round_name = m.group("round")
            normalized = {**extra, "round": _round(round_name) if round_name else None}
            yield _money(s, m, "funding_round", 0.8, normalized)


def _money(
    s: Sentence, m: re.Match[str], field_name: str, certainty: float, extra: dict[str, Any],
    end: int | None = None,
) -> ObservationDraft:  # fmt: skip
    """The stated amount with its qualifier (and, for a round, the round's name)."""
    start = m.start("q") if m.group("q") else m.start("cur")
    stop = m.end("mult") if m.group("mult") else m.end("num")
    if field_name == "funding_round" and m.group("round"):
        start, stop = min(start, m.start("round")), max(stop, m.end("round"))
    value = s.text[start : end or stop].rstrip(" .,")
    return draft(field_name, value, s, certainty, normalized={**money(m), **extra})


def _count(
    s: Sentence, m: re.Match[str], field_name: str, certainty: float, extra: dict[str, Any]
) -> ObservationDraft:
    start = m.start("q") if m.group("q") else m.start("n")
    value = s.text[start : m.end()]
    qualifier = "gte" if m.group("plus") else _qualifier(m)
    normalized = {"qualifier": qualifier, "value": int(m.group("n").replace(",", "")), **extra}
    return draft(field_name, value, s, certainty, normalized=normalized)


def _qualifier(m: re.Match[str]) -> str:
    return AMOUNT_QUALIFIERS.get((m.group("q") or "").lower(), "eq")


def _round(name: str) -> str:
    return re.sub(r"[\s-]+", "_", name.strip().lower())


def _related(
    s: Sentence, m: re.Match[str], group: str, field_name: str, rel_type: str, basis: str
) -> Iterator[ObservationDraft]:
    for name in _names(m.group(group)):
        yield draft(field_name, name, s, 0.8, related=RelatedOrg(rel_type, name, basis))


def _names(listed: str) -> list[str]:
    parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", listed)
    out = []
    for part in parts:
        # ponytail: "WordPress.Compare Ghost" is two elements the page ran together;
        # cut at a full stop glued to the next capitalized word when more words
        # follow ("Curie.Bio" stays). Space the blocks apart in the parser instead
        # if this ever cuts a real name.
        name = _GLUED.sub("", part.strip()).rstrip(".")
        if name and name.split()[0] not in _NOT_ORGS and not _NUMBER.match(name):
            out.append(name)
    return out


def _strip_tiers(badge: str) -> str:
    words = badge.split()
    while words and words[-1] in _BADGE_TIERS:
        words.pop()
    return " ".join(words) if words and words[0] not in _NOT_ORGS else ""


def _partner_sections(page: ParsedPage) -> Iterator[ObservationDraft]:
    """Names listed under "Technology partners" / "Strategic partners" headings."""
    level: int | None = None
    kind = ""
    for block in page.blocks:
        if block.heading_level is not None:
            if level is not None and block.heading_level <= level:
                level = None
            if level is None:
                m = _PARTNER_SECTION.search(block.text)
                # A heading, not a sentence that mentions partners.
                if m and len(block.text) <= 40:
                    level, kind = block.heading_level, m.group("kind").lower()
                continue
        if level is None or block.region in BOILERPLATE_REGIONS:
            continue
        if (block.heading_level is not None or block.tag == "li") and len(block.text) <= 60:
            quote = quote_block(block, 0, len(block.text))
            name = quote.text
            if kind == "strategic":
                yield draft(
                    "strategic_partnership",
                    name,
                    quote,
                    0.75,
                    related=RelatedOrg("strategic_partner", name, "partner_page"),
                )
            elif kind in ("technology", "integration", "tech"):
                yield draft(
                    "technology_partnership",
                    name,
                    quote,
                    0.75,
                    related=RelatedOrg("tech_partner", name, "partner_page"),
                )
            else:  # channel / alliance: a partnership of unstated kind
                yield draft(
                    "partnership", name, quote, 0.7, normalized={"qualifier": "eq", "type": kind}
                )
