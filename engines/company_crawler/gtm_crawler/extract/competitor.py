"""Competitor discovery (approved 2026-09-29, methodology A + B).

A company is recorded as a competitor only on explicit evidence, stored as a
``competitor`` observation plus a ``competitor`` relationship whose ``basis``
says what kind of evidence it was:

A. The company's own site
   * ``comparison_page``: a page at ``/vs/x``, ``/compare/x``, ``/a-vs-b``
     whose ``<h1>`` (or ``og:title``) names the other company, e.g.
     "Acme vs Globex"; the name must match the URL. At ``/a-vs-b`` one side
     must be the company itself: "Data Analyst vs Data Engineer" on a
     training company's blog compares two jobs, not two companies.
   * ``alternative_page``: ``/x-alternative(s)``, ``/alternatives/x`` with an
     ``<h1>`` like "The best Globex alternative".
   * ``stated_competition``: "we compete with X", "our competitors include X".
   * ``stated_alternative``: "Acme is the leading alternative to X".
   * ``stated_contrast``: a sentence opening "Unlike X, we ..." (weaker).

B. Other sites (found by search), where only the company's name refers to it
   * the same statements with the company's name as subject ("Acme competes
     with X", "alternatives to Acme include X");
   * ``third_party_alternatives_list``: items under a page titled "Acme
     alternatives" / "Acme competitors".
   * ``third_party_comparison``: a page titled "Acme vs Globex".
   * ``listed_as_alternative``: a page titled "Globex alternatives" that
     lists Acme as one.

Never evidence of competition: a co-mention, a customer, partner,
integration or investor.

Candidate tier (approved 2026-11-06): the other items of a list that has the
company as one of its items ("Best CRM software", "Globex alternatives") are
recorded as ``candidate_competitor`` with basis ``category_list``. Being
listed together is a co-mention, so a candidate is never a competitor: it is
not assessed as one and its site is not crawled.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from ..db.evidence import ObservationDraft, RelatedOrg
from ..evidence import Quote, quote_block, quote_meta
from ..parse import BOILERPLATE_REGIONS, ParsedPage
from .common import Context, sentences
from .common import draft as _draft
from .product import slugify
from .traction import _NAME, _NAME_LIST, _names

EXTRACTOR = "competitor"
VERSION = "4"
CANDIDATE = "candidate_competitor"

_COMPARE_PATH = re.compile(
    r"/(?:vs|versus|compare|comparison|comparisons)/(?P<slug>[a-z0-9-]+)/?$"
    r"|/(?P<a>[a-z0-9-]+)-(?:vs|versus)-(?P<b>[a-z0-9-]+)/?$"
)
_ALTERNATIVE_PATH = re.compile(
    r"/alternatives?/(?P<slug>[a-z0-9-]+)/?$|/(?P<slug2>[a-z0-9-]+)-alternatives?/?$"
)
_COMPARE_HEADING = re.compile(
    rf"(?P<a>{_NAME})\s+(?:vs\.?|versus|or)\s+(?P<b>{_NAME})"
    rf"|\b(?i:compare|comparing)\s+(?P<c>{_NAME})\s+(?:and|with|to)\s+(?P<d>{_NAME})"
    rf"|\b(?i:switch(?:ing)?|migrat(?:e|ing)|mov(?:e|ing))\s+from\s+(?P<e>{_NAME})"
)
_ALTERNATIVE_HEADING = re.compile(
    rf"(?P<a>{_NAME})\s+(?i:alternatives?|competitors)\b"
    rf"|\b(?i:alternatives?\s+to)\s+(?P<b>{_NAME})"
)
# "The 10 Best CRM Software for 2026", "Top project management tools".
_CATEGORY_TITLE = re.compile(
    r"\b(?:best|top|leading)\b.{0,60}?\b(?:software|tools?|platforms?|apps?|applications"
    r"|solutions?|companies|vendors|providers|products|services)\b",
    re.I,
)
_ITEM = re.compile(r"^(?:\d+[.)]\s*)?(?P<name>" + _NAME + r")")
_ITEM_STOPWORDS = frozenset(
    {"FAQ", "FAQs", "Conclusion", "Summary", "Pricing", "Methodology", "Why", "What", "How",
     "Frequently", "Final", "Our", "The", "Related", "Comparison", "Overview", "Table",
     "Introduction", "Bottom", "Other", "More", "Best", "Top", "Verdict", "Share", "About"}
)  # fmt: skip

_STATEMENTS = (
    (re.compile(r"\b(?:compete|competes|competing|competed)\s+(?:directly\s+)?(?:with|against)\s+"
                + _NAME_LIST), "stated_competition", 0.8),
    (re.compile(r"\bcompetitors?\s+(?:include|including|are|such\s+as|like)\s+" + _NAME_LIST),
     "stated_competition", 0.8),
    (re.compile(r"\balternatives?\s+to\s+" + _NAME_LIST), "stated_alternative", 0.7),
)  # fmt: skip
# "Unlike X, we ...": the company is named after the contrast.
_UNLIKE = re.compile(r"^Unlike\s+" + _NAME_LIST + r",\s+")
# Third-party phrasing with the company after the claim: "alternatives to Acme include X".
_ALTERNATIVES_TO = re.compile(
    r"\b(?i:alternatives|competitors)\s+(?:to|of)\s+(?P<subject>" + _NAME + r")\s+"
    r"(?:include|including|are)\s+" + _NAME_LIST
)


def extract_competitors(page: ParsedPage, ctx: Context) -> list[ObservationDraft]:
    found = [*_statements(page, ctx)]
    if ctx.first_party:
        found += [*_comparison_page(page, ctx)]
    elif title := next(_titles(page), None):
        found += [*_alternatives_list(page, ctx, title[0])]
        found += [*_third_party_comparison(ctx, *title)]
        found += [*_category_list(page, ctx, *title)]
    own = {slugify(n, ()) for n in ctx.names} | {slugify(n, ctx.names) for n in ctx.names}
    return [d for d in found if d.related and slugify(d.related.name, ()) not in own]


def _draft_competitor(
    name: str, quote: Quote, basis: str, certainty: float, kind: str = "competitor"
) -> ObservationDraft:
    return _draft(EXTRACTOR, VERSION, kind, name, quote, certainty,
                  related=RelatedOrg(kind, name, basis))  # fmt: skip


def _comparison_page(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    path = urlsplit(page.url).path.lower()
    own_side = False
    if m := _COMPARE_PATH.search(path):
        slugs = {s for s in m.groups() if s}
        pattern, basis = _COMPARE_HEADING, "comparison_page"
        own_side = m.group("a") is not None
    elif m := _ALTERNATIVE_PATH.search(path):
        slugs = {s for s in m.groups() if s}
        pattern, basis = _ALTERNATIVE_HEADING, "alternative_page"
    else:
        return
    # "acme-vs-globex" also yields the parts on either side of "vs".
    slugs |= {part for s in slugs for part in re.split(r"-(?:vs|versus)-", s)}
    for text, quote in _titles(page):
        for hm in pattern.finditer(text):
            if own_side and not any(ctx.refers_to_company(g) for g in hm.groups() if g):
                continue
            for name in (g for g in hm.groups() if g):
                name = name.strip()
                if slugify(name, ctx.names) in slugs or slugify(name) in slugs:
                    yield _draft_competitor(name, quote, basis, 0.85)
        break  # only the page's main title


def _titles(page: ParsedPage) -> Iterator[tuple[str, Quote]]:
    for block in page.blocks:
        if block.heading_level == 1:
            yield block.text, quote_block(block, 0, len(block.text))
            return
    for i, title in enumerate(page.meta.get("og:title", [])):
        yield title, quote_meta(page, "og:title", i)
        return


def _statements(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for s in sentences(page):
        text = s.text
        if not ctx.refers_to_company(text):
            continue
        for pattern, basis, certainty in _STATEMENTS:
            for m in pattern.finditer(text):
                if ctx.is_subject_of(text, m.start()):
                    for name in _names(m.group("list")):
                        yield _draft_competitor(name, s.quote(), basis, certainty)
        unlike = _UNLIKE.match(text)
        if unlike and ctx.refers_to_company(text[unlike.end() : unlike.end() + 60]):
            for name in _names(unlike.group("list")):
                yield _draft_competitor(name, s.quote(), "stated_contrast", 0.6)
        for m in _ALTERNATIVES_TO.finditer(text):
            if ctx.refers_to_company(m.group("subject")):
                for name in _names(m.group("list")):
                    yield _draft_competitor(name, s.quote(), "stated_alternative", 0.7)


def _list_subject(title: str) -> str | None:
    """X of a title "X alternatives" / "alternatives to X", without "Best", "Top 5"."""
    heading = _ALTERNATIVE_HEADING.search(title)
    subject = heading and (heading.group("a") or heading.group("b"))
    if not subject:
        return None
    words = subject.split()
    while words and (words[0] in _ITEM_STOPWORDS or words[0].isdigit()):
        words.pop(0)
    return " ".join(words) or None


def _list_items(page: ParsedPage) -> Iterator[tuple[str, Quote]]:
    """Names heading a list item (``<h2>``, ``<h3>``, ``<li>``), with their quote."""
    for block in page.blocks:
        is_item = block.heading_level in (2, 3) or block.tag == "li"
        # The article's list, not the site's menus and footer ("Affiliate", "Advertise").
        if not is_item or len(block.text) > 80 or block.region in BOILERPLATE_REGIONS:
            continue
        im = _ITEM.match(block.text)
        if im is None:
            continue
        name = _trim(im.group("name"))
        if not name or name.split()[0] in _ITEM_STOPWORDS or len(name.split()) > 5:
            continue
        start = block.text.index(name)
        yield name, quote_block(block, start, start + len(name))


def _alternatives_list(page: ParsedPage, ctx: Context, title: str) -> Iterator[ObservationDraft]:
    """Items under a page titled "Acme alternatives" / "Acme competitors" on another site."""
    subject = _list_subject(title)
    if not subject or not ctx.refers_to_company(subject):
        return
    for name, quote in _list_items(page):
        if not ctx.refers_to_company(name):
            yield _draft_competitor(name, quote, "third_party_alternatives_list", 0.5)


def _third_party_comparison(ctx: Context, title: str, quote: Quote) -> Iterator[ObservationDraft]:
    """A page on another site titled "Acme vs Globex" / "Compare Globex and Acme"."""
    m = _COMPARE_HEADING.search(title)
    if m is None:
        return
    for one, other in ((m.group("a"), m.group("b")), (m.group("c"), m.group("d"))):
        if one and other and ctx.refers_to_company(one) != ctx.refers_to_company(other):
            rival = other if ctx.refers_to_company(one) else one
            yield _draft_competitor(rival.strip(), quote, "third_party_comparison", 0.6)


def _category_list(
    page: ParsedPage, ctx: Context, title: str, quote: Quote
) -> Iterator[ObservationDraft]:
    """A list on another site that has the company as an item: "Best CRM software",
    "Globex alternatives". The other items are only candidates (co-listed)."""
    subject = _list_subject(title)
    if subject and ctx.refers_to_company(subject):
        return  # the company's own alternatives: _alternatives_list
    if not subject and not _CATEGORY_TITLE.search(title):
        return
    items = list(_list_items(page))
    if not any(ctx.refers_to_company(name) for name, _ in items):
        return
    if subject:
        # The page names the company as an alternative to the subject.
        yield _draft_competitor(subject, quote, "listed_as_alternative", 0.5)
    for name, item_quote in items:
        if not ctx.refers_to_company(name) and name != subject:
            yield _draft_competitor(name, item_quote, "category_list", 0.35, CANDIDATE)


def _trim(name: str) -> str:
    # "Globex Pro Plan" is fine; "Globex Review" or trailing punctuation is not part of it.
    name = re.sub(r"\s+(?:Review|Reviews|Pricing|Overview)$", "", name.strip())
    return name.rstrip(".,:;")
