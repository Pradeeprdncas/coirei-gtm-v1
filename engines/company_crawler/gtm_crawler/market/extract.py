"""Rule-based evidence extraction from a parsed page (no LLM).

A page's content blocks (navigation, header, footer and sidebars excluded)
are split into sentences. A sentence becomes an ``EvidenceRecord`` when it
speaks to a research ``Dimension`` (keyword rules below) and is about the
subject: it names a subject term, or the page's title does. The record keeps
the sentence verbatim with its block's XPath and character offsets, so it can
be re-checked against the stored raw page (``evidence.resolve``).

Numbers are parsed into ``Metric`` values (market sizes and forecasts, CAGR,
price points, percentage shares); the sentence stays the authority, and a
number the rules cannot place is simply not parsed. Prices and their terms
are read by ``market/pricing.py``, from sentences, pricing-card elements
(with the heading above and the unit elements next to them), table cells
(with their row and column labels) and JSON-LD offers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from ..evidence import QuoteError, quote_block, quote_json
from ..parse import ParsedPage, TextBlock
from ..urls import InvalidUrl, registrable_domain, url_host
from . import pricing, scope
from .models import (
    Attribution,
    Dimension,
    EvidenceRecord,
    Locator,
    Metric,
    MetricKind,
    Price,
    PriceBound,
    PriceContext,
    PricingType,
    ScopeBasis,
)

# 3: market scope per figure; 4: price terms (market/pricing.py), cards, tables, JSON-LD;
# 5: plan/product attribution (brands, item names, add-on headings, uncertain plans);
# 6: table cells whose text is in elements inside the cell (<td><p>$29</p></td>);
# 7: market figures: never the tail of a longer number; a company's valuation, funding,
# deal, market cap or revenue is not a market figure.
EXTRACTOR_VERSION = "market-rules-7"
MIN_SENTENCE, MAX_SENTENCE = 40, 600
MAX_EVIDENCE_PER_SOURCE = 40
_BOILERPLATE = frozenset({"nav", "header", "footer", "aside"})

# A statement ends like one; fragments without figures that do not are skipped.
_CLOSED = re.compile(r"[.!?][\"”')\]]?$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
_WORD = re.compile(r"[a-z0-9][a-z0-9&+-]*")
_STOPWORDS = frozenset(
    "the and for with from that this into over market markets industry industries "
    "software solution solutions service services platform product products tool tools "
    "global company companies business businesses".split()
)
# Cookie banners, newsletter prompts and similar page furniture.
_JUNK = re.compile(
    r"\b(cookies?|subscribe|newsletter|sign up|log ?in|all rights reserved|privacy policy|"
    r"terms of (use|service)|javascript|download (the )?(free )?sample|request (a )?sample|"
    r"buy now|add to cart)\b",
    re.I,
)

_DIMENSION_RULES: tuple[tuple[Dimension, re.Pattern[str]], ...] = tuple(
    (dim, re.compile(pattern, re.I))
    for dim, pattern in (
        (Dimension.MARKET_SIZE, r"\bmarket (size|value|revenue)|\bvalued at\b|\bworth\b.{0,40}"
                                r"\b(billion|million|trillion)\b|\b(tam|total addressable)\b"),
        (Dimension.GROWTH, r"\bcagr\b|compound annual growth|\bgrow(th|ing|s)?\b.{0,40}%|"
                           r"\bexpected to reach\b|\bprojected to\b|\bforecast"),
        (Dimension.DEFINITION, r"\b(is defined as|refers to|(market|industry|sector) (includes|"
                               r"covers|spans)|consists of|comprises|encompasses)\b"),
        (Dimension.DEMAND_DRIVER, r"\b(driven by|drivers?|fuel(l?ed|ing)|propel|boost(ed|ing)?|"
                                  r"rising demand|growing demand|increasing demand|"
                                  r"demand for|growing adoption|increasing adoption)\b"),
        (Dimension.PAIN_POINT, r"\b(struggle|pain points?|frustrat\w*|time[- ]consuming|"
                               r"inefficien\w*|bottleneck|costly|tedious|error[- ]prone|"
                               r"manual process\w*|challenge[sd]? (for|of|in) (customers|users|"
                               r"buyers|teams|companies|businesses))\b"),
        (Dimension.ADOPTION, r"\b(adopt(ed|ion|ing)?|uptake|penetration|"
                             r"\d+(\.\d+)?\s?% of [a-z -]{0,25}?(companies|businesses|"
                             r"organizations|firms|respondents|users|consumers|enterprises|"
                             r"households))\b"),
        (Dimension.WILLINGNESS_TO_PAY, r"\b(willing(ness)? to pay|budgets?|spend(ing)? on|"
                                       r"price sensitiv\w*|pay (more|for))\b"),
        (Dimension.SEGMENT, r"\b(segment(s|ed|ation)?|smbs?|small and medium|mid-?market|"
                            r"enterprises?|end[- ]users?|verticals?|by (region|application|"
                            r"end use|type|deployment))\b"),
        (Dimension.COMPETITION, r"\b(market share|key players|leading (players|vendors|"
                                r"companies|providers)|major players|competitors?|competitive|"
                                r"dominat\w+|incumbents?|consolidat\w+|fragmented)\b"),
        (Dimension.PRICING, r"\b(pric(e|es|ing|ed)|per (month|user|seat|year|license)|"
                            r"subscription|freemium|free tier|/mo\b|plans? start)\b"),
        (Dimension.SUBSTITUTE, r"\b(spreadsheets?|excel|workarounds?|in-house|homegrown|"
                               r"substitutes?|alternatives? to|instead of|manual(ly)?)\b"),
        (Dimension.BARRIER, r"\b(barriers?|hinder\w*|restrain\w*|obstacles?|lack of|high cost|"
                            r"limit(s|ed|ing) (the )?(growth|adoption)|challenges?)\b"),
        (Dimension.REGULATION, r"\b(regulat\w+|compliance|legislation|directive|gdpr|hipaa|"
                               r"licen[cs]e requirements?|policy|policies|mandate[sd]?|"
                               r"act of \d{4}|government)\b"),
        (Dimension.TECHNOLOGY, r"\b(artificial intelligence|\bai\b|machine learning|automation|"
                               r"cloud|digitali[sz]ation|technolog\w+|iot|blockchain)\b"),
        (Dimension.TREND, r"\b(trends?|shift(s|ing)?|emerging|increasingly|growing number|"
                          r"rise of|surge)\b"),
        (Dimension.RISK, r"\b(risks?|threats?|uncertaint\w+|declin\w+|downturn|headwinds?|"
                         r"volatil\w+|disrupt\w*)\b"),
        (Dimension.DISTRIBUTION, r"\b(distribution|channels?|resellers?|partners(hip)?s?|"
                                 r"marketplaces?|direct sales|go-to-market|self-serve)\b"),
    )
)  # fmt: skip

_CURRENCIES = {
    "usd": "USD", "us$": "USD", "$": "USD", "eur": "EUR", "€": "EUR", "gbp": "GBP",
    "£": "GBP", "inr": "INR", "₹": "INR", "rs": "INR", "rs.": "INR", "jpy": "JPY",
    "¥": "JPY", "cny": "CNY", "aud": "AUD", "a$": "AUD", "cad": "CAD", "c$": "CAD",
    "dollars": "USD", "euros": "EUR",
}  # fmt: skip
_SCALES = {
    "trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3,
    "crore": 1e7, "lakh": 1e5,
}  # fmt: skip
# Never the tail of a longer number ("1 234,5", "4,5", "2'500"): locale-written market figures
# are not read here (market/pricing.py reads prices only), and their last group is not one.
_NUM = r"(?<!\d)(?<!\d[.,'’\s])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_SCALE = r"(?:\s?(?P<scale>trillion|billion|million|thousand|crore|lakh|tn|bn|mn|mm|[tbmk])\b)?"
# A currency token never follows a letter: "HK$" / "S$" are not USD, "hours 5" is not INR.
_MONEY = re.compile(
    r"(?<![A-Za-z])(?P<cur>US\$|A\$|C\$|USD|EUR|GBP|INR|JPY|CNY|AUD|CAD|Rs\.?|[$€£₹¥])\s?"
    + _NUM + _SCALE
    + r"|" + _NUM.replace("num", "num2") + _SCALE.replace("scale", "scale2")
    + r"\s(?P<cur2>USD|EUR|GBP|INR|dollars|euros)\b"
    # "2581.22 USD Million" (seen live on market-research pages).
    + r"(?:\s(?P<scale3>trillion|billion|million|thousand|crore|lakh|bn|mn)\b)?",
    re.I,
)  # fmt: skip
# One company's money, not a market's: a valuation, funding, a deal, market cap, ARR. Read
# just before the amount or right after it ("at a $10 billion valuation").
_COMPANY_MONEY = re.compile(
    r"\b(valuations?|rais(e|ed|es|ing)|funding|financing|series [a-h]|investors?|acqui(red|res|"
    r"sition)|buyouts?|ipo|market cap(itali[sz]ation)?|(post|pre)-money|arr|annual recurring "
    r"revenue)\b", re.I)  # fmt: skip
_COMPANY_MONEY_AFTER = re.compile(r"\s*((in|of|an?|the)\s+)?" + _COMPANY_MONEY.pattern, re.I)
# "valued at" / "worth" size a market only when the market is what is valued: "the CRM market
# (size) was valued at"; "Notion, valued at" and "in this market, Notion is worth" do not.
_VALUED = re.compile(r"\b(valued at|worth)\b", re.I)
_MARKET_VALUED = re.compile(r"\b(market|industry|sector|segment|spending)\b(\s+[\w-]+){0,4}?\s+"
                            r"(valued at|worth)\b", re.I)  # fmt: skip
# One company's revenue ("Notion's annual revenue", "its sales"), not an industry's.
_COMPANY_REVENUE = re.compile(r"(\b(its|their)|\b[Tt]he company['’]s|\b[A-Z][\w&.-]*['’]s)\s+"
                              r"([\w-]+\s+){0,2}(revenues?|sales|turnover)\b")  # fmt: skip


def _company_money(before: str, after: str) -> bool:
    """Whether the amount between ``before`` and ``after`` is one company's (valuation,
    funding, deal, market cap, revenue): such a figure never sizes a market."""
    near = before[-60:]
    if _COMPANY_MONEY.search(near) or _COMPANY_MONEY_AFTER.match(after):
        return True
    if _VALUED.search(near) and not _MARKET_VALUED.search(near):
        return True
    return _COMPANY_REVENUE.search(near) is not None and not re.search(
        r"\b(industry|market|sector)['’]s\b", near, re.I
    )


_YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_BY_YEAR = re.compile(r"\b(by|in|through|until)\s(?:the\s(?:end\sof\s)?)?(?P<y>20\d\d)\b", re.I)
_FUTURE = re.compile(r"\b(reach|projected|expected|forecast\w*|will|estimated to (reach|grow)|"
                     r"anticipated|poised)\b", re.I)  # fmt: skip
_CAGR = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s?%\s?(?:\(?cagr\)?|compound annual growth rate)"
    r"|(?:cagr|compound annual growth rate|annual growth rate)\)?\s(?:of\s)?(?:about\s|around\s|"
    r"approximately\s|nearly\s|over\s|roughly\s)?(?P<b>\d+(?:\.\d+)?)\s?%",
    re.I,
)
_PERIOD = re.compile(
    r"(?:from|between|over)?\s?(?P<a>20\d\d)\s?(?:to|and|-|–|—|through)\s?(?P<b>20\d\d)", re.I
)
_SHARE = re.compile(
    r"(?P<v>\d+(?:\.\d+)?)\s?%\s(?:of\s)(?:all\s|the\s)?(?P<who>[a-z][a-z -]{2,40}?)\b"
    r"(?=\s(?:say|said|use|used|have|had|are|were|plan|report|reported|cite|adopt)|[,.;])",
    re.I,
)


# A market figure ("the ... market totaled $8.1 billion") is found by parse_metrics, not
# by the market-size wording rule: page targeting looks for it too.
_MARKET_FIGURE = (r"\bmarket\b[^.]{0,80}?(?:[$€£₹¥]|usd|eur)\s?\d[\d.,]*\s?(?:billion|million|"
                  r"bn|mn|trillion)\b")  # fmt: skip


def dimension_patterns(dims: Iterable[Dimension]) -> list[str]:
    """The rule (regular expression) of each dimension, as text (page targeting)."""
    wanted = set(dims)
    out = [rx.pattern for dim, rx in _DIMENSION_RULES if dim in wanted]
    # Listed three times: a stated figure weighs three times a wording match (seen on a
    # real report: a glossary's "CAGR, forecast" outranked the page stating the market).
    return out + [_MARKET_FIGURE] * 3 if Dimension.MARKET_SIZE in wanted else out


def states_money(text: str) -> bool:
    """Whether ``text`` states an amount with a currency (e.g. a price)."""
    return pricing.has_amount(text)


def subject_terms(*texts: str | None) -> set[str]:
    """Distinctive lower-case words of the subject (stopwords and short words dropped)."""
    out: set[str] = set()
    for text in texts:
        if text:
            out.update(w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in
                       _STOPWORDS)  # fmt: skip
    return out


def relevance(page: ParsedPage, terms: set[str]) -> float:
    """Share of the subject terms that the page's title and content mention."""
    if not terms:
        return 1.0
    words = set(_WORD.findall(f"{page.title or ''}\n{page.main_text}".lower()))
    return len(terms & words) / len(terms)


_ANY = pricing.Context()


def sentences(text: str) -> Iterable[tuple[int, int]]:
    """(start, end) offsets of the sentences of ``text``, whitespace-trimmed."""
    start = 0
    for m in _SENTENCE_END.finditer(text):
        yield from _trimmed(text, start, m.start())
        start = m.end()
    yield from _trimmed(text, start, len(text))


def _trimmed(text: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end > start:
        yield start, end


def dimensions_of(sentence: str) -> list[Dimension]:
    return [dim for dim, rule in _DIMENSION_RULES if rule.search(sentence)]


def parse_metrics(
    sentence: str,
    today: date | None = None,
    terms: set[str] | None = None,
    geography: str | None = None,
    vocabulary: set[str] | None = None,
    price_context: pricing.Context | None = _ANY,
) -> list[Metric]:
    """The numbers a sentence states, normalized (see module docstring); market figures get
    the scope their own clause states (``scope.assign``; ``terms``: the subject's words,
    ``geography``: the requested one, matched even when the gazetteer lacks it). Prices:
    ``pricing.prices`` with ``price_context`` (None: not read, e.g. a table cell, whose
    prices are read with its labels)."""
    this_year = (today or date.today()).year
    out: list[Metric] = []
    scoped: list[tuple[int, int, Metric]] = []
    is_market = re.search(r"\bmarket\b|\bindustry\b|\bspending\b|\brevenue", sentence, re.I)
    moneys = list(_MONEY.finditer(sentence))
    for i, m in enumerate(moneys):
        cur = _CURRENCIES.get((m.group("cur") or m.group("cur2") or "").lower())
        num = m.group("num") or m.group("num2")
        scale_word = (m.group("scale") or m.group("scale2") or m.group("scale3") or "").lower()
        if cur is None or num is None:
            continue
        value = round(float(num.replace(",", "")) * _SCALES.get(scale_word, 1.0), 2)
        nxt = moneys[i + 1].start() if i + 1 < len(moneys) else len(sentence)
        after = sentence[m.end() : nxt]
        before = sentence[moneys[i - 1].end() if i else 0 : m.start()]
        if scale_word in ("", "k", "thousand") and value < 1e6:
            continue  # a price, if anything (market/pricing.py)
        if not is_market or _company_money(before, after):
            continue
        year_m = _BY_YEAR.search(after[:80]) or _BY_YEAR.search(before[-60:])
        year = int(year_m.group("y")) if year_m else None
        if year is None:
            years = [int(y) for y in _YEAR.findall(after[:80])]
            year = years[0] if years else None
        # "projected to grow from X in 2025 to Y": X is the base value, not a forecast.
        base_value = re.search(r"\bfrom\s*$", before, re.I) is not None
        future = (bool(_FUTURE.search(before)) and not base_value) or (
            year is not None and year > this_year
        )
        kind = MetricKind.MARKET_FORECAST if future else MetricKind.MARKET_SIZE
        metric = Metric(kind=kind, value=value, unit=cur, year=year,
                        text=sentence[m.start() : m.end()])  # fmt: skip
        out.append(metric)
        scoped.append((m.start(), m.end(), metric))
    for c in _CAGR.finditer(sentence):
        rate = float(c.group("a") or c.group("b"))
        if not 0 < rate < 200:
            continue
        period = _PERIOD.search(sentence)
        start, end = (int(period.group("a")), int(period.group("b"))) if period else (None, None)
        if start is not None and end is not None and end <= start:
            start = end = None
        metric = Metric(kind=MetricKind.GROWTH_RATE, value=rate, unit="percent_per_year",
                        period_start=start, period_end=end, text=c.group(0))  # fmt: skip
        out.append(metric)
        scoped.append((c.start(), c.end(), metric))
    for s in _SHARE.finditer(sentence):
        value = float(s.group("v"))
        if value <= 100:
            out.append(Metric(kind=MetricKind.SHARE, value=value, unit="percent",
                              text=s.group(0).strip()))  # fmt: skip
    scope.assign(
        sentence, sorted(scoped, key=lambda f: f[0]), terms or set(), geography, vocabulary
    )
    if price_context is not None:
        out += pricing.prices(sentence, price_context)
    return out


def extract_evidence(
    page: ParsedPage,
    source_id: str,
    terms: set[str],
    *,
    geography: str | None = None,
    today: date | None = None,
    always_relevant: bool = False,
    vocabulary: set[str] | None = None,
    vendor: str | None = None,
) -> list[EvidenceRecord]:
    """Evidence records for the sentences of ``page`` that pass the rules, and for its
    prices (pricing-card elements, table cells, JSON-LD offers: ``_price_evidence``).

    ``always_relevant`` skips the subject-term test (a competitor's own page is
    about that competitor whether or not it names the research subject); ``vendor``:
    that competitor's name, recorded on its prices.
    """
    title_hit = always_relevant or bool(terms & set(_WORD.findall((page.title or "").lower())))
    page_hit = title_hit or not terms or bool(terms & _page_words(page))
    base = _price_base(page, geography, vendor)
    out: list[EvidenceRecord] = []
    seen: set[str] = set()
    for i, block in enumerate(page.blocks):
        # A heading is read only for a price it states ("Could we ... $329/month ...?").
        if block.region in _BOILERPLATE or block.heading_level and not pricing.has_amount(
                block.text):  # fmt: skip
            continue
        # A table cell's prices are read with its row and column labels (_table_prices).
        ctx = None if _cell(block) else _block_context(page, i, base)
        for start, end in sentences(block.text):
            if len(out) >= MAX_EVIDENCE_PER_SOURCE + MAX_PRICE_EVIDENCE:
                break
            text = block.text[start:end]
            # Past the cap, only prices (a long comparison page states dozens).
            full = len(out) >= MAX_EVIDENCE_PER_SOURCE
            if full and not pricing.has_amount(text):
                continue
            # A short sentence of a longer block is read for a price only ("The Studio plan
            # is $300/year. ..."); a short block is a pricing card's (_card_prices).
            short = len(text) < MIN_SENTENCE
            if len(text) > MAX_SENTENCE or short and (not pricing.has_amount(text) or len(
                    block.text.strip()) < MIN_SENTENCE) or _JUNK.search(text):  # fmt: skip
                continue
            key = " ".join(text.lower().split())
            if key in seen:
                continue
            dims = dimensions_of(text)
            metrics = parse_metrics(text, today, terms, geography, vocabulary, ctx)
            priced = any(m.kind in _PRICES for m in metrics)
            if not dims and not metrics or (full or short or block.heading_level) and not priced:
                continue
            if short or block.heading_level:
                metrics = [m for m in metrics if m.kind in _PRICES]
                dims = [Dimension.PRICING]
            if not metrics and not _CLOSED.search(text):
                continue  # a heading-like fragment ("Restraints Impact Analysis of ...")
            words = set(_WORD.findall(text.lower()))
            named = bool(terms & words) or not terms
            # A price need not name the subject on a page that does (a vendor's plan).
            if not (named or title_hit or priced and page_hit):
                continue
            try:
                quote = quote_block(block, start, end)
            except QuoteError:
                continue
            seen.add(key)
            out.append(_record(source_id, text, text, _xpath(quote), dims, metrics, geography,
                               0.8 if metrics else 0.6 if named else 0.4))  # fmt: skip
    if page_hit:
        out += _price_evidence(page, source_id, base, seen)
    if not base.third_party:
        _unattributed(out)
    return out


_PLAN_PRICES = (PricingType.SUBSCRIPTION, PricingType.UNKNOWN)


def _unattributed(records: list[EvidenceRecord]) -> None:
    """On a page that names several plans, a plan price that nothing ties to one of them
    is kept with its attribution said to be uncertain; no plan is guessed."""
    prices = [(e, m.price) for e in records for m in e.metrics if m.price is not None]
    plans = sorted({p.plan for _, p in prices if p.plan})
    if len(plans) < 2:
        return
    for e, p in prices:
        if p.plan is None and p.product is None and p.pricing_type in _PLAN_PRICES:
            p.attribution = Attribution.UNCERTAIN
            p.ambiguities.append(
                f"plan not identified: the page names {len(plans)} plans "
                f"({', '.join(plans[:4])}{', ...' if len(plans) > 4 else ''})"
            )
            e.extraction_certainty = min(e.extraction_certainty, 0.6)


def _record(source_id: str, claim: str, excerpt: str, locator: Locator, dims: list[Dimension],
            metrics: list[Metric], geography: str | None,
            certainty: float) -> EvidenceRecord:  # fmt: skip
    for metric in metrics:
        if metric.kind in (MetricKind.MARKET_SIZE, MetricKind.MARKET_FORECAST):
            if Dimension.MARKET_SIZE not in dims:
                dims.insert(0, Dimension.MARKET_SIZE)
        elif metric.kind is MetricKind.GROWTH_RATE and Dimension.GROWTH not in dims:
            dims.insert(0, Dimension.GROWTH)
        elif metric.kind in _PRICES and Dimension.PRICING not in dims:
            dims.append(Dimension.PRICING)
        elif metric.kind is MetricKind.SHARE and Dimension.ADOPTION not in dims:
            dims.append(Dimension.ADOPTION)
    # Documented in docs/MARKET_ENGINE_IMPLEMENTATION.md ("Evidence quality"): a price
    # whose terms the source leaves open is less certain.
    if any(m.price is not None and m.price.ambiguities for m in metrics):
        certainty = min(certainty, 0.6)
    return EvidenceRecord(
        source_id=source_id, claim=claim[:2000], excerpt=excerpt, locator=locator,
        dimensions=dims, metrics=metrics, extraction_certainty=certainty,
        geography=geography if geography and geography.lower() in excerpt.lower() else None,
        period=_period(metrics),
    )  # fmt: skip


# --- prices outside sentences ------------------------------------------------------------

_PRICES = (MetricKind.PRICE_POINT, MetricKind.PRICE_ON_REQUEST)
MAX_PRICE_EVIDENCE = 60
# A heading that names a section, not a plan or product.
_SECTION_HEADING = re.compile(
    r"\b(?:pricing|prices?|plans?|compare|comparison|faqs?|frequently|questions?|features?|why|"
    r"how|what|choose|simple|transparent|billing|overview|get started|all|our|your|included|"
    r"everything|testimonials?|customers?|table of contents|summary|conclusion|introduction|"
    r"best|top|vs\.?|review|alternatives?)\b", re.I)  # fmt: skip
# The element after a pricing card's amount that states its unit or billing.
_CARD_TAIL = re.compile(
    r"^\s*(?:/|per\b|a (?:month|year)\b|billed|paid|monthly|annually|yearly|each\b|every\b|"
    r"(?:user|seat|member|month|mo|year|yr)\b|USD|EUR|GBP|INR|excl|incl|\+|plus\b|vat\b|gst\b|"
    r"tax|one[- ]time|set-?up|\(|when\b|for the first)", re.I)  # fmt: skip
_JSON_OFFERS = frozenset({"Offer", "AggregateOffer"})
_JSON_PRODUCTS = frozenset({"Product", "SoftwareApplication", "WebApplication",
                            "MobileApplication", "Service"})  # fmt: skip
_JSON_PERIODS = {"P1M": "month", "P1Y": "year", "P1W": "week", "P1D": "day", "MON": "month",
                 "ANN": "year", "WEE": "week", "DAY": "day", "MONTH": "month", "YEAR": "year",
                 "MONTHLY": "month", "YEARLY": "year", "ANNUAL": "year"}  # fmt: skip
_JSON_GOODS = {"LBR": "lb", "KGM": "kg", "ONZ": "oz", "GRM": "g", "C62": "unit", "H87": "piece"}


def _xpath(quote: Any) -> Locator:
    return Locator(type="xpath", value=quote.locator, char_start=quote.char_start,
                   char_end=quote.char_end)  # fmt: skip


def _page_words(page: ParsedPage) -> set[str]:
    return set(_WORD.findall(page.main_text.lower()))


def _price_base(page: ParsedPage, geography: str | None, vendor: str | None) -> pricing.Context:
    """What holds for every price of the page: its vendor, whether its "$" may be another
    dollar, its "prices as of" date, whether it is a pricing page."""
    own_page = pricing.pricing_page(page.url, page.title)
    try:
        domain = registrable_domain(url_host(page.url)) if page.url else ""
    except InvalidUrl:
        domain = ""  # a page without a host (a test's or a stored extraction's): no brand
    names = [vendor, domain.split(".")[0] if domain else None, *page.meta.get("og:site_name", [])]
    return pricing.Context(
        vendor=vendor,
        dollar=pricing.dollar_place(page.url, page.title, geography),
        effective=pricing.page_effective(
            [b.text for b in page.blocks], own_page or vendor is not None
        ),  # fmt: skip
        card=own_page,
        third_party=vendor is None and not own_page,
        brands=frozenset(k for n in names if n and (k := pricing.brand_key(n))),
    )


def _heading(page: ParsedPage, i: int) -> TextBlock | None:
    """The nearest heading above block ``i`` in the same container (a card, a section)."""
    b = page.blocks[i]
    for j in range(i - 1, max(-1, i - 15), -1):
        h = page.blocks[j]
        if h.heading_level is not None and b.xpath.startswith(h.xpath.rsplit("/", 1)[0] + "/"):
            return h
    return None


def _named(heading: TextBlock | None, brands: frozenset[str] = frozenset()) -> str | None:
    """A heading that names one thing (a plan, a product), not a section or the page's
    own brand."""
    if heading is None:
        return None
    text = heading.text.strip()
    if len(text) > 40 or len(text.split()) > 4 or pricing.has_amount(text) \
            or _SECTION_HEADING.search(text) or pricing.brand_key(text) in brands:  # fmt: skip
        return None
    return re.sub(r"\s+(?:plan|tier)$", "", text, flags=re.I)


def _tier(name: str | None) -> bool:
    return bool(name) and name is not None and name[0].isdigit()


def _context(role: str, block: TextBlock) -> PriceContext:
    """A quote of the element that gives a price its terms, located like an excerpt."""
    text = block.text[:500]
    return PriceContext(role=role, text=text, locator=Locator(
        type="xpath", value=block.xpath, char_start=0, char_end=len(text)))  # fmt: skip


def _block_context(page: ParsedPage, i: int, base: pricing.Context) -> pricing.Context:
    """The heading above a block names its plan on the vendor's own or a pricing page, and
    the product elsewhere (a comparison article's section on one tool)."""
    heading = _heading(page, i)
    if heading is not None and pricing.add_on_heading(heading.text):
        # Items under an "Add-ons" heading are add-ons, named by their own label.
        return replace(base, labels=heading.text, context=(_context("heading", heading),))
    name = _named(heading, base.brands)
    if name is None or heading is None or heading.heading_level == 1 and base.card:
        return base  # a pricing page's h1 names the page, not one price
    found = _context("heading", heading)
    # The page's title heading (h1) names what the page is about (a shop's product), never
    # a plan: a pricing page's plans are named by their own cards.
    if (base.vendor or base.card) and heading.heading_level != 1:
        return replace(base, plan=name, context=(found,), plan_from=Attribution.HEADING)
    return replace(base, product=name, context=(found,), plan_from=Attribution.HEADING)


def _furniture(block: TextBlock, base: pricing.Context) -> bool:
    """Page furniture; a pricing page's cards may sit in a "header" or "aside" container
    (seen live), its nav and footer are still skipped."""
    return block.region in ("nav", "footer") or block.region in _BOILERPLATE and not base.card


def _price_evidence(page: ParsedPage, source_id: str, base: pricing.Context,
                    seen: set[str]) -> list[EvidenceRecord]:  # fmt: skip
    out = _card_prices(page, source_id, base, seen)
    out += _table_prices(page, source_id, base)
    out += _json_ld_prices(page, source_id, base)
    return out[:MAX_PRICE_EVIDENCE]


def _card_prices(page: ParsedPage, source_id: str, base: pricing.Context,
                 seen: set[str]) -> list[EvidenceRecord]:  # fmt: skip
    """Short elements that state an amount ("$29") with the elements after it that state
    its unit ("per user / month", "Billed annually") and the heading above (its plan)."""
    out: list[EvidenceRecord] = []
    blocks = page.blocks
    for i, block in enumerate(blocks):
        text = block.text.strip()
        if (_furniture(block, base) or _cell(block)
                or len(text) >= MIN_SENTENCE or not text):  # fmt: skip
            continue
        if not pricing.has_amount(text) and not pricing.quote_based(text):
            continue
        ctx = _block_context(page, i, base)
        tail: list[TextBlock] = []
        parent = block.xpath.rsplit("/", 1)[0]
        for n in blocks[i + 1 : i + 4]:
            if (n.heading_level is not None or _cell(n) or len(n.text) > 80
                    or not n.xpath.startswith(parent + "/") or pricing.has_amount(n.text)
                    or not _CARD_TAIL.match(n.text)):  # fmt: skip
                break
            tail.append(n)
        found = tuple(_context("card", n) for n in tail)
        # An amount alone is a price on a pricing page or under a plan's heading only.
        named = ctx.plan is not None or ctx.vendor is not None and ctx.product is not None
        ctx = replace(ctx, own=len(block.text), context=ctx.context + found,
                      card=ctx.card or named)  # fmt: skip
        joined = " ".join([block.text, *(n.text for n in tail)])
        metrics = pricing.prices(joined, ctx)
        if not metrics:
            continue
        key = " ".join(joined.lower().split())
        if key in seen:
            continue
        seen.add(key)
        claim = f"{ctx.plan or ctx.product or ''}: {joined}".lstrip(": ")
        out.append(_record(source_id, claim, block.text, Locator(
            type="xpath", value=block.xpath, char_start=0, char_end=len(block.text)),
            [Dimension.PRICING], metrics, None, 0.7))  # fmt: skip
    return out


_CELL = re.compile(r"/t[dh](?:\[\d+\])?(?=/|$)")


def _cell(block: TextBlock) -> str | None:
    """The XPath of the table cell ``block`` is, or is inside (the innermost)."""
    found = list(_CELL.finditer(block.xpath))
    return block.xpath[: found[-1].end()] if found else None


def _table_prices(page: ParsedPage, source_id: str,
                  base: pricing.Context) -> list[EvidenceRecord]:  # fmt: skip
    """Each table cell that states an amount, read with its row label (first cell) and
    column header (first row): "Monthly price" / "Pro" -> the Pro plan, billed monthly."""
    out: list[EvidenceRecord] = []
    for table in page.tables:
        # A cell's blocks: the cell itself, or the elements inside it (<td><p>$29</p></td>).
        groups: dict[str, list[TextBlock]] = {}
        for b in page.blocks:
            if b.xpath.startswith(table.xpath + "/") and (c := _cell(b)):
                groups.setdefault(c, []).append(b)
        cells = list(groups.values())
        grid: list[list[tuple[str, list[TextBlock] | None]]] = []
        at = 0
        for row in table.rows:
            line: list[tuple[str, list[TextBlock] | None]] = []
            for cell in row:
                found = None
                for k in range(at, min(len(cells), at + 6)):
                    if cell.strip() and " ".join(x.text for x in cells[k]).split() == cell.split():
                        found, at = cells[k], k + 1
                        break
                line.append((cell, found))
            grid.append(line)
        if not grid:
            continue
        header = grid[0] if not any(pricing.has_amount(c) for c, _ in grid[0]) else None
        for line in grid:
            if line is header:
                continue
            label, label_cell = line[0] if line and not pricing.has_amount(line[0][0]) \
                else ("", None)  # fmt: skip
            label_block = label_cell[0] if label_cell else None
            for j, (cell, group) in enumerate(line):
                if group is None or (j == 0 and label):
                    continue
                # The element that states the amount is the excerpt; the cell's others
                # ("$29" / "per month") are its context.
                block = next((b for b in group if pricing.has_amount(b.text)
                              or pricing.quote_based(b.text)), group[0])  # fmt: skip
                if _furniture(block, base):
                    continue
                if len(cell) > MAX_SENTENCE or not pricing.has_amount(cell) and \
                        not pricing.quote_based(cell):  # fmt: skip
                    continue
                col, col_cell = header[j] if header and j < len(header) else ("", None)
                col_block = col_cell[0] if col_cell else None
                col_name = _named(TextBlock(xpath="", tag="th", text=col),
                                  base.brands) if col else None  # fmt: skip
                row_name = _named(TextBlock(xpath="", tag="td", text=label),
                                  base.brands) if label else None  # fmt: skip
                if not base.third_party:
                    # A volume tier ("1 - 1,000", "500 contacts") is not a plan.
                    col_name = None if _tier(col_name) else col_name
                    row_name = None if _tier(row_name) else row_name
                product = None
                row_named = bool(row_name) and not pricing.price_label(label) and \
                    not _tier(row_name)  # fmt: skip
                col_named = bool(col_name) and not pricing.price_label(col) and \
                    not _tier(col_name)  # fmt: skip
                if base.third_party and (row_named or col_named):
                    # A comparison: one axis names vendors, the other plans or tiers
                    # ("500 contacts", "1 person" are tiers, not names).
                    if row_named:
                        product, row_name = row_name, None
                        col_name = None if _tier(col_name) else col_name
                    else:
                        product, col_name = col_name, None
                        row_name = None if _tier(row_name) else row_name
                elif not base.third_party and row_named and col_named:
                    # A vendor's plan columns, a named row ("Advanced Payments"): an extra
                    # priced on each plan (pricing._price: add_on).
                    product, row_name = row_name, None
                plan = col_name if col_name and not pricing.price_label(col) else (
                    row_name if row_name and not pricing.price_label(label) else None)  # fmt: skip
                quotes = tuple(_context(role, b) for role, b in (("row", label_block),
                                                                 ("column", col_block))
                               if b is not None)  # fmt: skip
                rest = group[group.index(block) + 1 :]
                quotes += tuple(_context("card", b) for b in rest)
                ctx = replace(base, plan=plan or base.plan, product=product or base.product,
                              labels=f"{label} {col}".strip(), card=True, context=quotes,
                              plan_from=Attribution.TABLE, own=len(block.text))  # fmt: skip
                # Amounts of the element only; its unit may follow in the cell's next one.
                metrics = pricing.prices(" ".join([block.text, *(b.text for b in rest)]), ctx)
                if not metrics:
                    continue
                claim = " / ".join(x for x in (label, col) if x) + f": {cell}"
                out.append(_record(source_id, claim, block.text, Locator(
                    type="xpath", value=block.xpath, char_start=0, char_end=len(block.text)),
                    [Dimension.PRICING], metrics, None, 0.7))  # fmt: skip
    return out


def _json_ld_prices(page: ParsedPage, source_id: str,
                    base: pricing.Context) -> list[EvidenceRecord]:  # fmt: skip
    """Schema.org offers: price (lowPrice / highPrice: a range), priceCurrency as stated
    (never assumed), the billing period of a priceSpecification, the product's name."""
    out: list[EvidenceRecord] = []
    products = {n.path: n for n in page.json_ld_nodes if _JSON_PRODUCTS & set(n.types)}
    for node in page.json_ld_nodes:
        if not _JSON_OFFERS & set(node.types):
            continue
        owner = max((p for path, p in products.items() if node.path.startswith(path)),
                    key=lambda p: len(p.path), default=None)  # fmt: skip
        product = owner.data.get("name") if owner is not None else None
        spec = node.data.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        spec = spec if isinstance(spec, dict) else {}
        for key, bound in (("price", PriceBound.EXACT), ("lowPrice", PriceBound.FROM),
                           ("highPrice", PriceBound.UP_TO)):  # fmt: skip
            path = f'{node.path}["{key}"]'
            try:
                quote = quote_json(page, path)
                value = float(quote.text.replace(",", ""))
            except (QuoteError, ValueError):
                continue
            cur = node.data.get("priceCurrency") or spec.get("priceCurrency")
            raw_spec = node.data.get("priceSpecification")
            cur_path = (f'{node.path}["priceCurrency"]' if node.data.get("priceCurrency") else
                        f'{node.path}["priceSpecification"]'
                        + ("[0]" if isinstance(raw_spec, list) else "") + '["priceCurrency"]'
                        if spec.get("priceCurrency") else None)  # fmt: skip
            if cur_path and not isinstance(cur, str):
                cur_path = None
            currency = cur.upper() if isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur) \
                else None  # fmt: skip
            codes = [str(spec.get(k) or "").upper() for k in ("billingDuration", "unitCode",
                                                              "unitText")]  # fmt: skip
            period = next((_JSON_PERIODS[c] for c in codes if c in _JSON_PERIODS), None)
            goods = next((_JSON_GOODS[c] for c in codes if c in _JSON_GOODS), None)
            kind = (PricingType.SUBSCRIPTION if period else PricingType.UNIT if goods else
                    PricingType.ONE_TIME if owner is not None and "Product" in owner.types
                    else PricingType.UNKNOWN)  # fmt: skip
            ambiguities = [] if currency else ["no priceCurrency stated"]
            if kind is PricingType.UNKNOWN:
                ambiguities.append("what the amount pays for is not stated")
            if bound is PriceBound.FROM:
                ambiguities.append("a starting price, not every customer's price")
            elif bound is PriceBound.UP_TO:
                ambiguities.append("an upper bound")
            name = node.data.get("name")
            context = [PriceContext(role="json", text=str(cur)[:500], locator=Locator(
                type="json_path", value=cur_path))] if cur_path else []  # fmt: skip
            unit = "/".join([currency or "unknown", *([goods] if goods else []),
                             *([period] if period else [])])  # fmt: skip
            item = product if isinstance(product, str) else None
            plan = name if isinstance(name, str) and name != product else None
            if item is None and plan is not None and kind is not PricingType.SUBSCRIPTION:
                item, plan = plan, None  # an offer named alone: the item sold
            plan = pricing.debrand(plan, base.brands)
            item = None if item and pricing.brand_key(item) in base.brands else item
            metric = Metric(kind=MetricKind.PRICE_POINT, value=value, unit=unit, text=quote.text,
                            price=Price(
                                pricing_type=kind, currency=currency, period=period,
                                basis=goods, bound=bound, plan=plan, product=item,
                                vendor=base.vendor, effective=base.effective, context=context,
                                attribution=Attribution.STRUCTURED_DATA if plan or item
                                else None, ambiguities=ambiguities))  # fmt: skip
            label = product if isinstance(product, str) else "offer"
            out.append(_record(source_id, f"{label} (structured data): {key} {quote.text} "
                               f"{cur or ''}".strip(), quote.text,
                               Locator(type="json_path", value=path), [Dimension.PRICING],
                               [metric], None, 0.8))  # fmt: skip
    return out


def _period(metrics: list[Metric]) -> str | None:
    for m in metrics:
        if m.period_start and m.period_end:
            return f"{m.period_start}-{m.period_end}"
        if m.year:
            return str(m.year)
    return None


# --- PDF evidence (market/pdf.py extractions) --------------------------------------------

_PDF_BLOCK = re.compile(r"^pdf:page=(\d+):at=(\d+)$")
_YEAR_CELL = re.compile(r"^(19[5-9]\d|20\d\d)$")
_NUMBER_CELL = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+(?:\.\d+)?$")
_TOTAL_LABEL = re.compile(r"^(?:total|overall|all regions|market total)$", re.I)
_MARKET_CAPTION = re.compile(r"\b(?:market|industry|sector|spending)\b", re.I)
_CAPTION_LABEL = re.compile(r"^\s*(?:table|figure|fig\.|exhibit|chart)\s*[\w.-]*\s*[:.\-–]\s*",
                            re.I)  # fmt: skip


def pdf_page(extraction: dict[str, Any], url: str) -> ParsedPage:
    """A PDF extraction's headings and paragraphs as a ``ParsedPage`` for
    ``extract_evidence``; each block's XPath names its page and offset
    (``pdf_locators`` turns them into page locators). Tables: ``pdf_table_evidence``."""
    blocks = []
    for page in extraction["pages"]:
        for b in page["blocks"]:
            if b["kind"] == "table":
                continue
            heading = b["kind"] == "heading"
            blocks.append(TextBlock(xpath=f"pdf:page={page['number']}:at={b['start']}",
                                    tag="h2" if heading else "p",
                                    text=page["text"][b["start"] : b["end"]],
                                    heading_level=2 if heading else None))  # fmt: skip
    meta = extraction.get("metadata", {})
    first = next((b.text for b in blocks if b.heading_level), None)
    created = meta.get("created")
    return ParsedPage(url=url, title=meta.get("title") or first, blocks=blocks,
                      published_at=datetime.fromisoformat(created) if created else None,
                      published_at_source="pdf_metadata" if created else None)  # fmt: skip


def pdf_locators(evidence: list[EvidenceRecord], version: str) -> None:
    """Block-relative XPath locators of PDF paragraphs -> page locators (offsets in the
    page's normalized text)."""
    for e in evidence:
        m = _PDF_BLOCK.match(e.locator.value) if e.locator is not None else None
        if e.locator is None or m is None or e.locator.char_start is None \
                or e.locator.char_end is None:  # fmt: skip
            continue
        page, at = int(m.group(1)), int(m.group(2))
        e.locator = Locator(type="pdf", value=f"page={page}", page=page, version=version,
                            char_start=at + e.locator.char_start,
                            char_end=at + e.locator.char_end)  # fmt: skip
        # A price's heading quote, from the same synthetic page: a page locator too.
        for c in (c for metric in e.metrics if metric.price for c in metric.price.context):
            cm = _PDF_BLOCK.match(c.locator.value)
            if cm is not None and c.locator.char_start is not None and c.locator.char_end \
                    is not None:  # fmt: skip
                p, a = int(cm.group(1)), int(cm.group(2))
                c.locator = Locator(type="pdf", value=f"page={p}", page=p, version=version,
                                    char_start=a + c.locator.char_start,
                                    char_end=a + c.locator.char_end)  # fmt: skip


def _table_scale(unit: dict[str, str]) -> float:
    return _SCALES.get(unit["scale"].lower(), 1.0)


def table_cell_value(cell: str, unit: dict[str, str]) -> float:
    """A numeric table cell in the table's stated unit, as an absolute amount."""
    return round(float(cell.replace(",", "")) * _table_scale(unit), 2)


def pdf_table_evidence(
    extraction: dict[str, Any],
    source_id: str,
    terms: set[str],
    *,
    geography: str | None = None,
    today: date | None = None,
    vocabulary: set[str] | None = None,
) -> list[EvidenceRecord]:
    """One record per data row of each regular table: the row's exact text as the
    excerpt, located by page, table and row. Its figures are parsed only when the
    table states a currency unit ("EUR billion"), has year columns and a caption that
    names a market: each numeric cell under a year is a figure for that year, scoped
    by the row label (a place, "Total" = the caption's place, a known segment, or a
    qualifier: the label is a part of the market). Nothing else is inferred."""
    this_year = (today or date.today()).year
    vocab = (vocabulary or set()) | terms
    title_words = set(_WORD.findall(str(extraction.get("metadata", {}).get("title") or "").lower()))
    out: list[EvidenceRecord] = []
    for page in extraction["pages"]:
        for b in page["blocks"]:
            table = b.get("table")
            if b["kind"] != "table" or table is None or not table["regular"]:
                continue
            rows: list[list[str]] = table["rows"]
            caption = table.get("caption") or ""
            unit = table.get("unit")
            years = {j: int(c) for j, c in enumerate(rows[0]) if _YEAR_CELL.match(c)}
            figures = bool(unit and years and _MARKET_CAPTION.search(caption))
            caption_places = scope.places(caption, geography)
            # "Table 1: European ... market" -> "European ... market": the label is no market word.
            market_words = _CAPTION_LABEL.sub("", caption)
            caption_off, caption_qualifier = scope.definition(market_words, terms, vocab,
                                                              geography)  # fmt: skip
            named = terms & (set(_WORD.findall(caption.lower())) | title_words)
            offset = b["start"] + len(" | ".join(rows[0])) + 1
            for r, row in enumerate(rows[1:], start=1):
                text = " | ".join(row)
                start, offset = offset, offset + len(text) + 1
                if len(out) >= MAX_EVIDENCE_PER_SOURCE:
                    return out
                if len(text) > MAX_SENTENCE:  # prose in cells, not a row of figures (seen live)
                    continue
                metrics = []
                place: str | None
                segment: str | None
                qualifier: str | None
                if figures and unit is not None:
                    label = row[0]
                    whole = [p for p in scope.places(label, geography)
                             if p[0] == 0 and p[1] == len(label)]  # fmt: skip
                    if whole:
                        place, segment, qualifier = whole[0][2], None, caption_qualifier
                    elif _TOTAL_LABEL.match(label.strip()):
                        place = ", ".join(sorted({p[2] for p in caption_places})) or None
                        segment, qualifier = None, caption_qualifier
                    else:
                        place = ", ".join(sorted({p[2] for p in caption_places})) or None
                        segment = scope.canonical_segment(label, terms)
                        known = segment != label.strip()
                        qualifier = caption_qualifier if known else label.strip().lower()
                        segment = segment if known else None
                    for j, year in years.items():
                        cell = row[j].strip()
                        if not _NUMBER_CELL.match(cell):
                            continue
                        metrics.append(Metric(
                            kind=(MetricKind.MARKET_FORECAST if year > this_year
                                  else MetricKind.MARKET_SIZE),
                            value=table_cell_value(cell, unit), unit=unit["currency"], year=year,
                            text=cell, geography=place, segment=segment,
                            measure="spending" if "spend" in caption.lower() else "market",
                            market_phrase=caption[:200], off_subject=caption_off,
                            qualifier=qualifier,
                            scope_basis=ScopeBasis.STATED if place else ScopeBasis.NOT_STATED,
                        ))  # fmt: skip
                dims = [Dimension.MARKET_SIZE] if metrics else dimensions_of(f"{caption} {text}")
                if not dims or not (named or not terms):
                    continue
                out.append(EvidenceRecord(
                    source_id=source_id, claim=f"{caption}: {text}"[:2000] if caption else text,
                    excerpt=text,
                    locator=Locator(type="pdf", value=f"page={page['number']};table="
                                    f"{table['index']};row={r}", page=page["number"],
                                    char_start=start, char_end=start + len(text),
                                    version=extraction["version"], table=table["index"], row=r),
                    dimensions=dims, metrics=metrics, extraction_certainty=0.7 if metrics else 0.5,
                    geography=geography if geography and geography.lower() in
                    f"{caption} {text}".lower() else None, period=_period(metrics),
                ))  # fmt: skip
    return out
