"""Shared helpers for rule-based extractors.

Extractors are deterministic functions of a parsed page and a ``Context``;
they return ``ObservationDraft``s and never touch the database. Rules favour
precision: a statement is attributed to the company only when its sentence
refers to the company itself ("we", "our", or one of its names), because an
UNKNOWN field is better than a fact about somebody else.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..db.evidence import ObservationDraft, RelatedOrg
from ..evidence import Quote, quote_block
from ..parse import ParsedPage, TextBlock

# Sentence ends: terminal punctuation followed by whitespace and an uppercase
# letter, digit or quote, unless the word before is a common abbreviation.
_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*\s+(?=[A-Z0-9\"'(])")
_ABBREVIATIONS = re.compile(
    r"(?:\b(?:Inc|Ltd|Co|Corp|LLC|St|Mr|Mrs|Ms|Dr|Jr|Sr|vs|etc|e\.g|i\.e|U\.S|No|Mt|Ave|approx)"
    r"|\b[A-Z])\.$"
)
_FIRST_PERSON = re.compile(r"\b(?:we|we're|we've|our|us)\b", re.IGNORECASE)
_CAPITALIZED = re.compile(r"\b[A-Z][A-Za-z0-9&'’-]+")
# Capitalized words that do not name another party.
NOT_PROPER_NOUNS = frozenset(
    {"Inc", "LLC", "Ltd", "Corp", "Corporation", "Co", "GmbH", "PLC", "Limited", "Today",
     "Series", "Seed", "Pre-seed", "Pre-Seed", "Round", "Funding", "The", "In", "On", "This",
     "January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December", "It", "Its", "Their", "And", "As"}
)  # fmt: skip


@dataclass(frozen=True)
class Sentence:
    block: TextBlock
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.block.text[self.start : self.end]

    def quote(self) -> Quote:
        return quote_block(self.block, self.start, self.end)


@dataclass
class Context:
    """What is known about the company before a page is examined."""

    domain: str
    # Names the company goes by on its own site (JSON-LD, og:site_name).
    names: set[str] = field(default_factory=set)
    # Product slug -> product row id, once products have been identified.
    product_ids: dict[str, int] = field(default_factory=dict)
    # False on another site's page: there "we" / "our" is the publisher, and
    # only the company's name refers to the company.
    first_party: bool = True

    def refers_to_company(self, text: str) -> bool:
        if self.first_party and _FIRST_PERSON.search(text):
            return True
        lowered = text.lower()
        return any(_contains_word(lowered, name.lower()) for name in self.names)

    def is_subject_of(self, text: str, pos: int) -> bool:
        """The company is what the claim at ``pos`` is about.

        A self-reference must come before the claim, with no other proper noun
        between them: "we raised $5M" passes, "we congratulate our customer
        Globex on raising $5M" does not.
        """
        refs = [m.end() for m in _FIRST_PERSON.finditer(text, 0, pos)] if self.first_party else []
        for name in self.names:
            refs += [m.end() for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)",
                                                  text[:pos], re.IGNORECASE)]  # fmt: skip
        if not refs:
            return False
        between = text[max(refs) : pos]
        return not any(w not in NOT_PROPER_NOUNS for w in _CAPITALIZED.findall(between))


def sentences(page: ParsedPage) -> Iterator[Sentence]:
    for block in page.blocks:
        start = 0
        for m in _BOUNDARY.finditer(block.text):
            if _ABBREVIATIONS.search(block.text[start : m.start() + 1]):
                continue
            yield from _sentence(block, start, m.start() + len(m.group().rstrip()))
            start = m.end()
        yield from _sentence(block, start, len(block.text))


def _sentence(block: TextBlock, start: int, end: int) -> Iterator[Sentence]:
    while start < end and block.text[start].isspace():
        start += 1
    while end > start and block.text[end - 1].isspace():
        end -= 1
    if end > start:
        yield Sentence(block, start, end)


def _contains_word(text: str, word: str) -> bool:
    return bool(word) and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def draft(
    extractor: str,
    version: str,
    field_name: str,
    value: str,
    quote: Quote,
    certainty: float,
    normalized: dict[str, Any] | None = None,
    kind: str = "stated",
    qualifier: str | None = None,
    related: RelatedOrg | None = None,
) -> ObservationDraft:
    return ObservationDraft(
        related=related,
        field=field_name,
        value_verbatim=value,
        quote=quote,
        extractor=extractor,
        extractor_version=version,
        extraction_certainty=certainty,
        value_normalized=normalized,
        kind=kind,
        qualifier=qualifier,
    )


# Spec section 16: a figure the page attributes to someone else ("LinkedIn
# lists", "according to Crunchbase") keeps that attribution; it is the
# page's report of a third party's claim, not the company's own statement.
_NAME = r"[A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*)*"
_REPORTED = re.compile(
    rf"\b(?i:according\s+to|as\s+(?:reported|estimated|listed)\s+by|data\s+from)\s+(?P<a>{_NAME})"
    rf"|\b(?P<b>{_NAME})\s+(?:lists|listed|estimates|estimated|reports|reported|ranks|"
    r"ranked|puts|places|placed)\b"  # not "says": a quoted executive speaks for the company
)


def reported(text: str, ctx: Context) -> dict[str, str]:
    """``{"status": "reported", "reported_by": name}`` if the sentence attributes
    its claims to a third party, else ``{}``. The company citing itself is not a report."""
    for m in _REPORTED.finditer(text):
        words = (m.group("a") or m.group("b")).rstrip(".").split()
        while words and words[0] in NOT_PROPER_NOUNS:
            words.pop(0)  # "Today LinkedIn lists" -> "LinkedIn"
        name = " ".join(words)
        if name and not _FIRST_PERSON.fullmatch(name) and not ctx.refers_to_company(name):
            return {"status": "reported", "reported_by": name}
    return {}


CURRENCIES = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
              "USD": "USD", "EUR": "EUR", "GBP": "GBP", "INR": "INR", "JPY": "JPY",
              "CAD": "CAD", "AUD": "AUD"}  # fmt: skip
_MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
                "b": 1e9, "bn": 1e9, "billion": 1e9, "lakh": 1e5, "crore": 1e7}  # fmt: skip
# Words before an amount that make it a bound or an estimate, not an exact figure.
AMOUNT_QUALIFIERS = {
    "more than": "gt", "over": "gt", "above": "gt", "in excess of": "gt", "upwards of": "gt",
    "at least": "gte", "nearly": "approx", "almost": "approx", "about": "approx",
    "approximately": "approx", "around": "approx", "roughly": "approx", "some": "approx",
    "~": "approx", "up to": "lte", "at most": "lte", "less than": "lt", "fewer than": "lt",
    "under": "lt",
}  # fmt: skip
QUALIFIER = r"(?:(?P<q>(?i:" + "|".join(re.escape(k) for k in AMOUNT_QUALIFIERS) + r"))\s+)?"
MONEY = (
    r"(?P<cur>US\$|[$€£¥₹]|\b(?:USD|EUR|GBP|INR|JPY|CAD|AUD)\s?)"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<mult>thousand|million|billion|lakh|crore|mm|mn|bn|[KMB])\b)?"
)


def money(m: re.Match[str]) -> dict[str, Any]:
    """Normalized form of a ``QUALIFIER + MONEY`` match; the stated figure, scaled, nothing more."""
    amount = float(m.group("num").replace(",", ""))
    mult = (m.group("mult") or "").lower()
    return {
        "qualifier": AMOUNT_QUALIFIERS.get((m.group("q") or "").lower(), "eq"),
        "amount": amount * _MULTIPLIERS.get(mult, 1),
        "currency": CURRENCIES[m.group("cur").strip().upper()],
    }
