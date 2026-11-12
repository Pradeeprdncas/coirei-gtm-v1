"""Product information extraction (GTM Parameters V1.0, section 3).

Product identification (approved 2026-09-29):

1. A product is a JSON-LD ``Product`` / ``SoftwareApplication`` (or web /
   mobile application) node the site publishes, or a page at its own product
   URL (``/products/<slug>``, ``/product/<slug>``, ``/apps/<slug>``) whose
   ``<h1>`` or ``og:title`` names it. Feature and pricing pages alone are not
   products.
2. Products are never merged because they share a site. Two sightings are the
   same product only when their slugs match (name lowercased, the company's
   own name dropped from the front, non-alphanumerics collapsed to "-").
3. Prices are kept exactly as written, with currency and period normalized;
   "Contact sales" is recorded as such, never as a number.
4. Recent updates come from changelog / release-note / news pages: dated
   headings, or a dated page's ``<h1>``. Undated entries stay undated.
5. Features, use cases, positioning and category come from the product's own
   pages (feature lists, the text right after the ``<h1>``, meta description)
   and JSON-LD ``featureList`` / ``applicationCategory``. The home page's
   headline is the company's own positioning, for no product in particular.

Observations on a product's own page (or from its JSON-LD node) carry that
product's id. Company-wide pages (``/features``, ``/pricing``, ``/changelog``)
produce observations with no product: which product they describe is not
stated, so it is not assumed. The number of products is a count of product
rows, not an observation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from ..db.evidence import ObservationDraft
from ..evidence import Quote, QuoteError, quote_block, quote_json, quote_meta
from ..identity import normalize_name
from ..parse import ParsedPage, TextBlock
from ..parse.structured import JsonLdNode
from ..urls import registrable_domain
from .common import CURRENCIES, Context
from .common import draft as _draft

EXTRACTOR = "product"
VERSION = "6"

_PRODUCT_TYPES = frozenset(
    {"Product", "SoftwareApplication", "WebApplication", "MobileApplication", "SoftwareSourceCode"}
)
_TOP_LEVEL = re.compile(r'^\$\.jsonld\[\d+\](?:\["@graph"\]\[\d+\])?$')
_PRODUCT_PATH = re.compile(r"^/(?:products?|apps?)/([a-z0-9]+(?:-[a-z0-9]+)*)(?:/.*)?$")
_TITLE_SEPARATORS = re.compile(r"\s+[|–—·:-]\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Not "/price": that is as often a page about a price (a docs page for a "price" helper).
_PRICING_PAGE = re.compile(r"(?:^|/)(?:pricing|plans|prices)(?:/|$)")
_UPDATES_PAGE = re.compile(
    r"(?:^|/)(?:changelog|release-notes|releases|updates|whats-new|what-s-new|news|newsroom|"
    r"press|press-releases)(?:/|$)"
)
# A section's heading ("Key features", "Features of the platform"), not a sentence
# that mentions features ("Get notified when we ship new features").
_HEADING_WORD = r"[\w&'’()-]+"
_FEATURE_HEADING = re.compile(
    rf"^(?:{_HEADING_WORD}\s+){{0,3}}(?:features?|capabilities)(?:\s+{_HEADING_WORD}){{0,3}}\W*$"
    r"|\bwhat you get\b", re.I
)  # fmt: skip
_USE_CASE_HEADING = re.compile(r"\buse[- ]cases?\b", re.I)
# An article's "features" are its subject's (a post about another product's features).
_ARTICLE_PAGE = re.compile(r"(?:^|/)(?:blog|resources|tutorials|guides|articles|posts|learn)/.")
# Page furniture: its lists are menus, not what a section above them lists.
BOILERPLATE_REGIONS = frozenset({"nav", "header", "footer", "aside"})

_PERIODS = {"mo": "month", "month": "month", "monthly": "month", "yr": "year", "year": "year",
            "annually": "year", "annual": "year", "week": "week", "day": "day",
            "user": "user", "seat": "seat"}  # fmt: skip
_PRICE = re.compile(
    r"(?:(?P<from>(?:starting (?:at|from)|starts? at|as low as|from)\s+))?"
    r"(?P<cur>US\$|[$€£¥₹]|\b(?:USD|EUR|GBP|INR|JPY|CAD|AUD)\s?)"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    # Any unit ("per truck per month"), so a price never loses what it is per.
    r"(?:\s*(?:/|per)\s*(?P<per1>[a-z]+)\b)?"
    r"(?:\s*(?:/|per)\s*(?P<per2>[a-z]+)\b)?"
    r"(?:\s+(?P<billed>billed\s+(?:monthly|annually)))?",
    re.IGNORECASE,
)
_NON_NUMERIC_PRICE = re.compile(
    r"\b(?:contact (?:sales|us)|custom pricing|talk to sales|get a quote|request a quote)\b"
    r"|^free$|\bfree\s*forever\b",  # "Freeforever": two spans read as one word
    re.IGNORECASE,
)
_NOT_PLAN_HEADING = re.compile(r"\?\s*$|\bcompar", re.IGNORECASE)
# A badge after a plan's name in its heading ("Essentials Recommended").
_PLAN_BADGE = re.compile(r"\s+(?:most popular|popular|recommended|best value|best deal)$", re.I)
# A price card that puts each part in its own element: "$", "18", "USD / mo", "Billed yearly".
_CURRENCY_ONLY = re.compile(r"US\$|[$€£¥₹]")
_AMOUNT_ONLY = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?")
_PERIOD_ONLY = re.compile(r"(?:(?P<cur>[A-Z]{3})\s*)?(?:/|per)\s*(?P<per>[a-z]+)", re.IGNORECASE)
_BILLED_ONLY = re.compile(r"billed\s+(?P<cycle>monthly|yearly|annually)", re.IGNORECASE)
_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
    r"|\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b"
    r"|\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b"
)


@dataclass(frozen=True)
class ProductCandidate:
    slug: str
    name: str
    basis: str


def slugify(name: str, company_names: Collection[str] = ()) -> str:
    """Lowercased, hyphenated name without a leading company name.

    The company's first word counts as its name too: "Acme Robotics" sells
    "Acme Stock", which is the product "stock".
    """
    lowered = name.strip().lower()
    prefixes = set(company_names) | {c.split()[0] for c in company_names if c.split()}
    for company in sorted(prefixes, key=len, reverse=True):
        prefix = company.lower()
        if lowered.startswith(prefix + " ") and len(lowered) > len(prefix) + 1:
            lowered = lowered[len(prefix) + 1 :]
            break
    return _NON_ALNUM.sub("-", lowered).strip("-")


def identify_products(page: ParsedPage, ctx: Context) -> list[ProductCandidate]:
    if not ctx.first_party:
        return []  # another site's /products/ pages are its own products
    found = [
        ProductCandidate(slug, q.text, basis) for slug, q, basis, _ in _json_products(page, ctx)
    ]
    named = _page_product(page, ctx)
    if named:
        found.append(ProductCandidate(named[0], named[1], "product_page"))
    return found


def extract_products(page: ParsedPage, ctx: Context) -> list[ObservationDraft]:
    if not ctx.first_party:
        return []
    return [
        *_from_json_ld(page, ctx),
        *_from_product_page(page, ctx),
        *_pricing(page, ctx),
        *_updates(page),
        *_company_sections(page, ctx),
        *_home_positioning(page, ctx),
    ]


def draft(
    field_name: str, value: str, quote: Quote, certainty: float, product: int | None, **kw: Any
) -> ObservationDraft:
    d = _draft(EXTRACTOR, VERSION, field_name, value, quote, certainty, **kw)
    return d if product is None else replace(d, product_id=product)


# --- identification ----------------------------------------------------------------


def _json_products(page: ParsedPage, ctx: Context) -> Iterator[tuple[str, Quote, str, JsonLdNode]]:
    for node in page.json_ld_nodes:
        types = _PRODUCT_TYPES.intersection(node.types)
        if not types or not _TOP_LEVEL.match(node.path):
            continue
        url = node.data.get("url")
        if isinstance(url, str) and url.strip():
            host = urlsplit(url.strip()).hostname
            if not host or registrable_domain(host) != ctx.domain:
                continue  # someone else's product (a partner, an integration)
        try:
            quote = quote_json(page, f'{node.path}["name"]')
        except QuoteError:
            continue
        slug = slugify(quote.text, ctx.names)
        if slug:
            yield slug, quote, f"json_ld:{sorted(types)[0]}", node


def _page_product(page: ParsedPage, ctx: Context) -> tuple[str, str, Quote] | None:
    """(slug, name, quote) if ``page`` is a product's own page and names the product."""
    m = _PRODUCT_PATH.match(urlsplit(page.url).path.rstrip("/") or "/")
    if not m:
        return None
    slug = m.group(1)
    for block in page.blocks:
        if block.heading_level == 1:
            if slugify(block.text, ctx.names) == slug:
                return slug, block.text, quote_block(block, 0, len(block.text))
            break
    # "Stock | Acme": the part matching the slug is the name; the quote is the
    # whole og:title, since a meta value is cited whole.
    for i, title in enumerate(page.meta.get("og:title", [])):
        for part in _TITLE_SEPARATORS.split(title):
            if part.strip() and slugify(part, ctx.names) == slug:
                return slug, part.strip(), quote_meta(page, "og:title", i)
    return None


# --- product-specific observations ---------------------------------------------------


def _from_json_ld(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for slug, name_quote, _, node in _json_products(page, ctx):
        product = ctx.product_ids.get(slug)
        yield draft("product_name", name_quote.text, name_quote, 1.0, product)

        def q(*keys: str | int, node: JsonLdNode = node) -> Quote | None:
            try:
                return quote_json(page, node.path + "".join(f"[{json.dumps(k)}]" for k in keys))
            except QuoteError:
                return None

        if quote := q("description"):
            yield draft("product_description", quote.text, quote, 1.0, product)
        for key in ("applicationCategory", "category", "applicationSubCategory"):
            if quote := q(key):
                yield draft("product_category", quote.text, quote, 1.0, product)
        features = node.data.get("featureList")
        for keys in ([("featureList",)] if isinstance(features, str) else
                     [("featureList", i) for i in range(len(features))]
                     if isinstance(features, list) else []):  # fmt: skip
            if quote := q(*keys):
                yield draft("key_feature", quote.text, quote, 1.0, product)
        offers = node.data.get("offers")
        for keys in ([("offers",)] if isinstance(offers, dict) else
                     [("offers", i) for i in range(len(offers))]
                     if isinstance(offers, list) else []):  # fmt: skip
            yield from _json_offer(page, node, keys, q, product)


def _json_offer(
    page: ParsedPage, node: JsonLdNode, keys: tuple[str | int, ...], q: Any, product: int | None
) -> Iterator[ObservationDraft]:
    price = q(*keys, "price")
    if price is None:
        return
    currency = q(*keys, "priceCurrency")
    try:
        amount: float | None = float(price.text.replace(",", ""))
    except ValueError:
        amount = None
    normalized: dict[str, Any] = {"qualifier": "eq", "amount": amount,
                                  "currency": currency.text if currency else None}  # fmt: skip
    plan = q(*keys, "name")
    yield draft("price", price.text, price, 1.0, product,
                normalized=normalized if amount is not None else None,
                qualifier=plan.text if plan else None)  # fmt: skip


def _from_product_page(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    named = _page_product(page, ctx)
    if not named:
        return
    slug, name, name_quote = named
    product = ctx.product_ids.get(slug)
    yield draft("product_name", name, name_quote, 0.9, product)
    for key in ("description", "og:description"):
        if page.meta.get(key):
            quote = quote_meta(page, key)
            yield draft("product_description", quote.text, quote, 0.8, product)
            break
    # Positioning: the product page's own opening statement, right after its <h1>.
    blocks = page.blocks
    h1 = next((i for i, b in enumerate(blocks) if b.heading_level == 1), None)
    if h1 is not None:
        for block in blocks[h1 + 1 : h1 + 4]:
            if block.heading_level is not None:
                continue
            if block.tag in ("p", "div") and 20 <= len(block.text) <= 400:
                quote = quote_block(block, 0, len(block.text))
                yield draft("product_positioning", quote.text, quote, 0.6, product)
                break
    yield from _sections(page, product)


def _home_positioning(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    """The home page's headline: the company's one-line value proposition, for
    no product in particular. A headline that is only the company's name says
    nothing, so the heading right after it is taken instead. Only the main
    site's: a help centre or developer portal home is not the company's pitch."""
    parts = urlsplit(page.url)
    if parts.path not in ("", "/") or (parts.hostname or "").removeprefix("www.") != ctx.domain:
        return
    brands = {normalize_name(n).replace(" ", "") for n in {*ctx.names, ctx.domain.split(".")[0]}}
    blocks = page.blocks
    h1 = next((i for i, b in enumerate(blocks) if b.heading_level == 1), None)
    if h1 is None:
        return
    for block in blocks[h1 : h1 + 3]:
        if block.heading_level is None or normalize_name(block.text).replace(" ", "") in brands:
            continue
        if 10 <= len(block.text) <= 200:
            quote = quote_block(block, 0, len(block.text))
            yield draft("product_positioning", quote.text, quote, 0.6, None)
        return


def _company_sections(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    """Feature and use-case sections on pages that are not a product's own page."""
    article = _ARTICLE_PAGE.search(urlsplit(page.url).path.lower())
    if _page_product(page, ctx) is None and not article:
        yield from _sections(page, None)


def _sections(page: ParsedPage, product: int | None) -> Iterator[ObservationDraft]:
    """Items under a "Features" or "Use cases" heading, until the next heading at that level."""
    level: int | None = None
    field_name = ""
    for block in page.blocks:
        if block.heading_level is not None:
            if level is not None and block.heading_level <= level:
                level = None
            if level is None:
                if _FEATURE_HEADING.search(block.text):
                    level, field_name = block.heading_level, "key_feature"
                elif _USE_CASE_HEADING.search(block.text):
                    level, field_name = block.heading_level, "main_use_case"
                continue
        if level is None or block.region in BOILERPLATE_REGIONS:
            continue
        # Sub-headings and list items are the items; paragraphs describe them.
        if (block.heading_level is not None or block.tag == "li") and len(block.text) <= 200:
            quote = quote_block(block, 0, len(block.text))
            yield draft(field_name, quote.text, quote, 0.7, product)


# --- pricing -----------------------------------------------------------------------


def _pricing(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    path = urlsplit(page.url).path.lower()
    if not _PRICING_PAGE.search(path):
        return
    named = _PRODUCT_PATH.match(path.rstrip("/"))
    product = ctx.product_ids.get(named.group(1)) if named else None
    # The nearest heading above a price is taken as its plan's name, and is
    # recorded as a plan only once a price is found under it.
    plan: TextBlock | None = None
    plan_recorded = False
    # Under a question ("Still need help choosing?") a "Contact us" is help, and
    # under "Compare plans" a "Free" is a column header: neither is a price.
    not_plans = False
    blocks = page.blocks
    for i, block in enumerate(blocks):
        if block.heading_level is not None:
            not_plans = bool(_NOT_PLAN_HEADING.search(block.text))
            if 2 <= block.heading_level <= 4 and len(block.text) <= 60 and not not_plans:
                plan, plan_recorded = block, False
            else:
                plan = None
            continue
        if block.tag in ("td", "th"):
            plan = None  # a heading above a table names the table, not a plan
        prices = list(_PRICE.finditer(block.text))
        # A term (<dt>) labels a value: in a comparison grid, "Free" is a plan's column.
        labels = not_plans or block.tag == "dt"
        others = [] if labels else list(_NON_NUMERIC_PRICE.finditer(block.text))
        plan_name = _PLAN_BADGE.sub("", plan.text) if plan is not None else None
        split = _split_price(blocks, i, plan_name, product)
        if (prices or others or split) and plan is not None and not plan_recorded:
            plan_recorded = True
            quote = quote_block(plan, 0, len(plan_name or ""))
            yield draft("plan_name", quote.text, quote, 0.7, product)
        if split:
            yield split
        for m in prices:
            yield _price(block, m, plan_name, product)
        for m in others:
            value = m.group()
            quote = quote_block(block, m.start(), m.end())
            free = value.lower().startswith("free")
            yield draft("price", value, quote, 0.7, product, qualifier=plan_name,
                        normalized={"qualifier": "eq", "amount": 0} if free else
                        {"qualifier": "eq", "value": "contact_sales"})  # fmt: skip


def _split_price(
    blocks: list[TextBlock], i: int, plan: str | None, product: int | None
) -> ObservationDraft | None:
    """The price of a card whose currency sign (``blocks[i]``), amount, period and
    billing cycle are separate elements. The amount is the value and the quote; the
    rest, read from the elements next to it, is in the normalized value."""
    if not _CURRENCY_ONLY.fullmatch(blocks[i].text) or i + 1 >= len(blocks):
        return None
    amount = blocks[i + 1]
    if not _AMOUNT_ONLY.fullmatch(amount.text):
        return None
    normalized: dict[str, Any] = {
        "qualifier": "eq",
        "amount": float(amount.text.replace(",", "")),
        "currency": CURRENCIES[blocks[i].text.upper()],
        "per": [],
    }
    for block in blocks[i + 2 : i + 4]:
        if m := _PERIOD_ONLY.fullmatch(block.text):
            normalized["per"] = [_PERIODS.get(m.group("per").lower(), m.group("per").lower())]
        elif m := _BILLED_ONLY.fullmatch(block.text):
            cycle = m.group("cycle").lower()
            normalized["billing"] = "monthly" if cycle == "monthly" else "annual"
    quote = quote_block(amount, 0, len(amount.text))
    return draft("price", quote.text, quote, 0.7, product, normalized=normalized, qualifier=plan)


def _price(block: TextBlock, m: re.Match[str], plan: str | None, product: int | None) -> Any:
    periods = [_PERIODS.get(p.lower(), p.lower()) for p in (m.group("per1"), m.group("per2")) if p]
    text = m.group().rstrip()
    normalized = {
        "qualifier": "gte" if m.group("from") else "eq",
        "amount": float(m.group("amount").replace(",", "")),
        "currency": CURRENCIES[m.group("cur").strip().upper()],
        "per": periods,
    }
    # The billing cycle is not a unit: "$249/mo billed annually" is per month, billed annually.
    if billed := (m.group("billed") or "").lower():
        normalized["billing"] = "monthly" if billed.endswith("monthly") else "annual"
    quote = quote_block(block, m.start(), m.start() + len(text))
    # The plan (nearest heading above) is context, recorded as the qualifier.
    return draft("price", text, quote, 0.8, product, normalized=normalized, qualifier=plan)


# --- recent updates ------------------------------------------------------------------


def _updates(page: ParsedPage) -> Iterator[ObservationDraft]:
    if not _UPDATES_PAGE.search(urlsplit(page.url).path.lower()):
        return
    headings = [b for b in page.blocks if b.heading_level is not None]
    if page.published_at is not None:
        # A dated article or release note: its <h1> is the update; the page's
        # publication date is recorded with the observation.
        h1 = next((b for b in headings if b.heading_level == 1), None)
        if h1 is not None:
            quote = quote_block(h1, 0, len(h1.text))
            yield draft("recent_update", quote.text, quote, 0.7, None)
        return
    # A changelog index: each heading that carries its own date is an entry.
    for block in headings:
        if block.heading_level == 1:
            continue
        m = _DATE.search(block.text)
        day = _parse_date(next(g for g in m.groups() if g)) if m else None
        if day is None:
            continue
        quote = quote_block(block, 0, len(block.text))
        yield draft("recent_update", quote.text, quote, 0.7, None,
                    normalized={"qualifier": "eq", "date": day.isoformat()})  # fmt: skip


def _parse_date(text: str) -> date | None:
    text = re.sub(r"\bSept\b", "Sep", text.replace(",", "").replace(".", ""))
    for fmt in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
