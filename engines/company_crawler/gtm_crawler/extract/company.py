"""Company information extraction (GTM Parameters V1.0, section 2).

Sources, in order of reliability:

* JSON-LD Organization markup the site publishes about itself: name,
  legalName, url, foundingDate, numberOfEmployees, address, tickerSymbol,
  parentOrganization, naics / isicV4, description.
* ``og:site_name``, and the home page's meta description; without a site
  name, the part of the home page's <title> that spells the domain
  ("coirei — The AI GTM Strategist" on coirei.com). The home page's own
  address is the website.
* A legal name ("Coirei Innovations Pvt Ltd") that starts with the
  company's name, and the address in the block under it (a footer's).
* Sentences that refer to the company itself ("we", "our", or its name):
  founding year, headquarters, location, employee count and size band,
  ownership, business model and market.

* Public contact details on the company's own pages: e-mail addresses at its
  domain, phone numbers after a phone cue ("Phone:", "Call us"), JSON-LD
  ``email`` / ``telephone``, and the contact page itself.
* Founders named in JSON-LD (``founder``).

Fields: company_name, legal_name, website, founded_year, employee_count,
company_size, headquarters, company_location, company_country,
ownership_type, business_model, market_domain, company_description,
contact_email, contact_phone, contact_page, founder.

Not here: partnerships (traction extraction, as relationships), and the
site's domain and subdomains, which come from the crawl's own fetch records
rather than from anything a page states.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

from ..db.evidence import ObservationDraft
from ..evidence import QuoteError, quote_block, quote_json, quote_meta, quote_page
from ..parse import ParsedPage
from ..parse.structured import JsonLdNode
from .common import AMOUNT_QUALIFIERS, QUALIFIER, Context, Sentence, reported, sentences
from .common import draft as _draft

EXTRACTOR = "company"
VERSION = "9"

_ORG_TYPES = re.compile(r"Organization|Corporation|Business|Company|NGO")
# A top-level JSON-LD node: a script's root or a direct member of its @graph.
# schema.org PostalAddress keys, as an office's normalized parts.
_ADDRESS_PARTS = (("street", "streetAddress"), ("locality", "addressLocality"),
                  ("region", "addressRegion"), ("postal_code", "postalCode"),
                  ("country", "addressCountry"))  # fmt: skip
_TOP_LEVEL = re.compile(r'^\$\.jsonld\[\d+\](?:\["@graph"\]\[\d+\])?$')

_PLACE = r"[A-Z][\w'’-]*\.?(?:(?:,\s*|\s+)(?:[A-Z][\w'’-]*\.?))*"
_YEAR = r"(?:19|20)\d{2}"
_NUMBER = r"\d{1,3}(?:,\d{3})+|\d+"

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@((?:[\w-]+\.)+[a-z]{2,})(?![\w-])", re.I)
_NOT_EMAIL = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|avif)$", re.I)
# A phone number after a cue: a bare digit run (an order number, a price) is not one.
_PHONE = re.compile(
    r"\b(?:phone|tel(?:ephone)?|call(?:\s+us)?|mobile|whatsapp|contact)\b[\s:.-]{0,3}(?:(?:at|on)\s+)?"
    r"(\+?\d[\d\s().-]{6,18}\d)",
    re.I,
)
_CONTACT_PATH = re.compile(r"/contact(?:[-_]?us)?/?$", re.I)

_FOUNDED = re.compile(rf"\b(?:founded|established|incorporated|started)\s+in\s+({_YEAR})\b")
_HEADQUARTERS = re.compile(
    rf"\b(?:headquartered|HQ'd|head office is|headquarters (?:is|are))\s+(?:located\s+)?"
    rf"(?:in|at)\s+(?:the\s+city\s+of\s+)?({_PLACE})"
    rf"|\b(?:Headquarters|HQ|Head office)\s*[:\-–]\s*({_PLACE})"
)
_BASED_IN = re.compile(rf"\bbased\s+(?:out\s+of|in)\s+({_PLACE})")
# Qualifiers as for every other count (common.AMOUNT_QUALIFIERS): "up to 250", "fewer than 50".
_EMPLOYEES = re.compile(
    rf"(?<!\w){QUALIFIER}(?<![\d,\-–])(?P<n>{_NUMBER})(?P<plus>\+)?\s+(?:full-time\s+)?"
    r"(?:employees|staff members|team members|FTEs)\b",
    re.IGNORECASE,
)
_SIZE_BAND = re.compile(rf"\b({_NUMBER})\s*[-–]\s*({_NUMBER})\s+employees\b", re.IGNORECASE)
# A LinkedIn company page, as the search provider gives it (crawl._contents).
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_PROFILE_BAND = re.compile(
    rf"^(?:-\s*)?Company size:\s*(?P<band>(?P<low>{_NUMBER})(?:\s*[-–]\s*(?P<high>{_NUMBER})"
    rf"|(?P<plus>\+))\s+employees)\s*$",
    re.IGNORECASE,
)
_PROFILE_HEADCOUNT = re.compile(rf"^(?:-\s*)?Employees:\s*(?P<n>{_NUMBER})\s*$", re.IGNORECASE)
_OWNERSHIP = (
    (re.compile(r"\bpublicly[- ](?:traded|listed|held)\b", re.I), "public", 0.85),
    (re.compile(r"\b(?:NYSE|NASDAQ|Nasdaq|LSE|TSX|ASX|NSE|BSE|Euronext)\s*:\s*[A-Z][A-Z.]{0,5}\b"),
     "public", 0.9),
    (re.compile(r"\bprivately[- ](?:held|owned)\b", re.I), "private", 0.85),
    (re.compile(r"\b(?:venture|VC)[- ]backed\b", re.I), "venture_backed", 0.75),
    (re.compile(rf"\b(?:wholly[- ]owned\s+)?subsidiary\s+of\s+{_PLACE}"), "subsidiary", 0.85),
    (re.compile(r"\bemployee[- ]owned\b", re.I), "employee_owned", 0.85),
    (re.compile(r"\b501\(c\)\(3\)", re.I), "nonprofit", 0.8),
)  # fmt: skip
# "Nonprofit" is the company's ownership only as what it is ("we are a nonprofit"):
# elsewhere it is a customer group or a discount ("pricing for nonprofit organizations").
_NONPROFIT = re.compile(r"\bnon-?profit\b", re.I)
# "We are / we're / <name> is" followed, in the same clause, by a model keyword.
_SUBJECT = r"\b(?:we\s+are|we're|{name}\s+is)\s+an?\b"
_CLAUSE_END = re.compile(r"\b(?:for|that|which|who|helping|helps|to|serving|used by)\b|[,;:]")
_BUSINESS_MODELS = (
    (re.compile(r"\bB2B2C\b"), "b2b2c"),
    (re.compile(r"\bB2B\b"), "b2b"),
    (re.compile(r"\bB2C\b"), "b2c"),
    (re.compile(r"\b(?:D2C|DTC|direct-to-consumer)\b", re.I), "direct_to_consumer"),
    (re.compile(r"\bSaaS\b|\bsoftware[- ]as[- ]a[- ]service\b", re.I), "saas"),
    (re.compile(r"\bmarketplace\b", re.I), "marketplace"),
    (re.compile(r"\bsubscription\b", re.I), "subscription"),
    (re.compile(r"\bopen[- ]source\b", re.I), "open_source"),
    (re.compile(r"\bfreemium\b", re.I), "freemium"),
    (re.compile(r"\b(?:usage-based|pay-as-you-go)\b", re.I), "usage_based"),
)
# "Acme | Robots for warehouses": a site name with its tagline.
_TAGLINE = re.compile(r"\s+[|–—·:-]\s+")
# A name with its legal form: "Coirei Innovations Pvt Ltd", "Acme Robotics, Inc" (a
# final "." is left out: it may end the sentence).
_LEGAL_NAME = re.compile(
    r"\b([A-Z][\w&'’-]*(?:\s+[A-Z][\w&'’-]*){0,4}),?\s+"
    r"(?:Pvt\.?\s*Ltd|Private\s+Limited|Pty\.?\s+Ltd|Co\.,?\s+Ltd|Ltd|Limited|Inc|"
    r"Incorporated|LLC|LLP|GmbH|AG|PLC|plc|Corp|Corporation|S\.A\.S|S\.A|B\.V|N\.V)(?!\w)"
)
# A one-line postal address: three or more comma-separated parts, no email, URL or phone.
_ADDRESS = re.compile(r"[\w#.'’ -]+(?:,\s*[\w#.'’ -]+){2,}")
_MARKET = re.compile(r"\b(?:in|for|across)\s+the\s+([a-z][a-z&/ -]{2,40}?)\s+(?:industry|sector)\b")


def company_names(page: ParsedPage, domain: str) -> set[str]:
    """Names the site uses for its own company, for recognising self-references."""
    names = {
        n.data["name"] for n in _org_nodes(page, domain) if isinstance(n.data.get("name"), str)
    }
    if _is_home(page, domain):
        names.update(_site_name(v) for v in page.meta.get("og:site_name", [])[:1])
        if title := _title_name(page, domain):
            names.add(title)
    return {n.strip() for n in names if len(n.strip()) >= 2}


def _is_home(page: ParsedPage, domain: str) -> bool:
    """The company's home page. A section or sub-site (docs, forum, changelog)
    names itself in ``og:site_name`` and describes itself, not the company."""
    parts = urlsplit(page.url)
    return parts.hostname in (domain, f"www.{domain}") and parts.path in ("", "/")


def _site_name(value: str) -> str:
    return _TAGLINE.split(value.strip())[0]


def _title_name(page: ParsedPage, domain: str) -> str | None:
    """The part of the home page's <title> that spells the site's domain name:
    "coirei" in "coirei — The AI GTM Strategist" on coirei.com, "Acme" in
    "Home | Acme". A title that does not ("Welcome") names nothing."""
    if not _is_home(page, domain) or not page.title or page.meta.get("og:site_name"):
        return None
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    parts = (p.strip() for p in _TAGLINE.split(page.title.strip()))
    return next((p for p in parts if re.sub(r"[^a-z0-9]", "", p.lower()) == label), None)


def _is_company_name(name: str, ctx: Context) -> bool:
    return any(name.lower().startswith(n.lower()) for n in ctx.names)


def extract_company(page: ParsedPage, ctx: Context) -> list[ObservationDraft]:
    if not ctx.first_party:
        # The page's own site name and description describe its publisher.
        return [*_from_json_ld(page, ctx), *_from_text(page, ctx), *_profile_lines(page)]
    return [*_from_json_ld(page, ctx), *_from_meta(page, ctx), *_from_text(page, ctx),
            *_legal_address(page, ctx), *_branches(page, ctx), *_contact(page, ctx)]  # fmt: skip


def draft(field_name: str, value: str, quote: Any, certainty: float, **kw: Any) -> ObservationDraft:
    return _draft(EXTRACTOR, VERSION, field_name, value, quote, certainty, **kw)


# --- JSON-LD -------------------------------------------------------------------


def _org_nodes(page: ParsedPage, domain: str, first_party: bool = True) -> Iterator[JsonLdNode]:
    """Organization nodes that describe the company whose site is ``domain``.

    Only top-level nodes count (a nested publisher, customer or sponsor may be
    anyone), and a node naming a URL on another domain is someone else, as is
    one naming a sub-site ("Acme Developer Docs" at docs.acme.com). On another
    site's page a node must name the company's URL, since without one it
    describes the publisher.
    """
    for node in page.json_ld_nodes:
        if not any(_ORG_TYPES.search(t) for t in node.types) or not _TOP_LEVEL.match(node.path):
            continue
        url = node.data.get("url")
        if isinstance(url, str) and url.strip():
            if urlsplit(url.strip()).hostname not in (domain, f"www.{domain}"):
                continue
        elif not first_party:
            continue
        yield node


def _from_json_ld(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for node in _org_nodes(page, ctx.domain, ctx.first_party):

        def q(*keys: str | int, node: JsonLdNode = node) -> Any:
            path = node.path + "".join(f"[{json.dumps(k)}]" for k in keys)
            try:
                return quote_json(page, path)
            except QuoteError:
                return None

        for key, name in (("name", "company_name"), ("legalName", "legal_name")):
            if quote := q(key):
                yield draft(name, quote.text, quote, 1.0)
        if quote := q("url"):
            yield draft("website", quote.text, quote, 1.0)
        if quote := q("description"):
            yield draft("company_description", quote.text, quote, 1.0)
        for key, name in (("email", "contact_email"), ("telephone", "contact_phone")):
            if quote := q(key):
                yield draft(name, quote.text.removeprefix("mailto:"), quote, 1.0)
        founders = node.data.get("founder")
        listed = isinstance(founders, list)
        named: list[Any] = founders if isinstance(founders, list) else [founders]
        for i, founder in enumerate(named):
            keys: tuple[str | int, ...] = ("founder", i) if listed else ("founder",)
            if isinstance(founder, dict) and (quote := q(*keys, "name")):
                yield draft("founder", quote.text, quote, 1.0)
            elif isinstance(founder, str) and (quote := q(*keys)):
                yield draft("founder", quote.text, quote, 1.0)
        if (quote := q("foundingDate")) and (year := re.match(rf"^\s*({_YEAR})\b", quote.text)):
            yield draft("founded_year", quote.text, quote, 1.0,
                        normalized={"qualifier": "eq", "value": int(year.group(1))})  # fmt: skip
        yield from _employees_json(node.data.get("numberOfEmployees"), q)
        yield from _address_json(node.data.get("address"), q)
        if quote := q("tickerSymbol"):
            yield draft("ownership_type", quote.text, quote, 0.9,
                        normalized={"qualifier": "eq", "value": "public"})  # fmt: skip
        parent = node.data.get("parentOrganization")
        if isinstance(parent, dict) and (quote := q("parentOrganization", "name")):
            yield draft("ownership_type", quote.text, quote, 0.9,
                        normalized={"qualifier": "eq", "value": "subsidiary",
                                    "parent": quote.text})  # fmt: skip
        for scheme in ("naics", "isicV4"):
            if quote := q(scheme):
                yield draft("market_domain", quote.text, quote, 1.0,
                            normalized={"qualifier": "eq", "scheme": scheme,
                                        "value": quote.text})  # fmt: skip


def _employees_json(value: Any, q: Any) -> Iterator[ObservationDraft]:
    if isinstance(value, int | str):
        quote = q("numberOfEmployees")
        number = _int(quote.text) if quote else None
        if quote and number is not None:
            yield draft("employee_count", quote.text, quote, 1.0,
                        normalized={"qualifier": "eq", "value": number})  # fmt: skip
    elif isinstance(value, dict):
        # A QuantitativeValue: an exact value or a range. Each bound is its own
        # statement ("at least 51", "at most 200"); field assessment pairs them.
        for key, qualifier, name in (("value", "eq", "employee_count"),
                                     ("minValue", "gte", "company_size"),
                                     ("maxValue", "lte", "company_size")):  # fmt: skip
            quote = q("numberOfEmployees", key)
            number = _int(quote.text) if quote else None
            if quote and number is not None:
                yield draft(name, quote.text, quote, 1.0,
                            normalized={"qualifier": qualifier, "value": number})  # fmt: skip


def _address_json(value: Any, q: Any) -> Iterator[ObservationDraft]:
    # An Organization's address is where it is, not necessarily its headquarters.
    addresses = value if isinstance(value, list) else [value]
    for i, address in enumerate(addresses):
        if not isinstance(address, dict):
            continue
        prefix: tuple[str | int, ...] = ("address", i) if isinstance(value, list) else ("address",)
        if quote := q(*prefix, "addressLocality"):
            yield draft("company_location", quote.text, quote, 1.0)
        country = address.get("addressCountry")
        keys = ("addressCountry", "name") if isinstance(country, dict) else ("addressCountry",)
        if quote := q(*prefix, *keys):
            yield draft("company_country", quote.text, quote, 1.0)


def _branches(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    """Each address in the company's own JSON-LD, as an office: the main
    site's and every branch sub-site's (porur.acme.com, a LocalBusiness).
    A sub-site's node is not the company itself (``_org_nodes``), but where
    it is, is one of the company's places."""
    # ponytail: JSON-LD addresses only; an address only in page text (a
    # "Contact" block) or in Organization.location/department is not read.
    for node in page.json_ld_nodes:
        if not any(_ORG_TYPES.search(t) for t in node.types) or not _TOP_LEVEL.match(node.path):
            continue
        url = node.data.get("url")
        site = url.strip() if isinstance(url, str) and url.strip() else page.url
        host = urlsplit(site).hostname or ""
        if host != ctx.domain and not host.endswith(f".{ctx.domain}"):
            continue
        name = node.data.get("name")
        value = node.data.get("address")
        addresses = value if isinstance(value, list) else [value]
        for i, address in enumerate(addresses):
            if not isinstance(address, dict):
                continue
            prefix: tuple[str | int, ...] = (
                ("address", i) if isinstance(value, list) else ("address",)
            )
            parts = {part: address[key].strip() for part, key in _ADDRESS_PARTS
                     if isinstance(address.get(key), str) and address[key].strip()}  # fmt: skip
            if isinstance(name, str) and name.strip():
                parts["name"] = name.strip()
            for key in ("streetAddress", "addressLocality"):
                path = node.path + "".join(f"[{json.dumps(k)}]" for k in (*prefix, key))
                try:
                    quote = quote_json(page, path)
                except QuoteError:
                    continue
                yield draft("office_location", quote.text, quote, 0.9,
                            normalized={"qualifier": "eq", **parts})  # fmt: skip
                break


def _contact(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    """E-mail addresses at the company's domain, cued phone numbers and the contact page."""
    for block in page.blocks:
        for m in _EMAIL.finditer(block.text):
            host = m.group(1).lower()
            if _NOT_EMAIL.search(m.group(0)) or not (
                host == ctx.domain or host.endswith(f".{ctx.domain}")
            ):
                continue
            yield draft("contact_email", m.group(0), quote_block(block, *m.span()), 0.9)
        for m in _PHONE.finditer(block.text):
            if 8 <= sum(c.isdigit() for c in m.group(1)) <= 15:
                yield draft("contact_phone", m.group(1), quote_block(block, *m.span(1)), 0.8)
    if _CONTACT_PATH.search(urlsplit(page.url).path):
        quote = quote_page(page, "url")
        yield draft("contact_page", quote.text, quote, 1.0)


def _int(text: str) -> int | None:
    digits = text.strip().replace(",", "")
    return int(digits) if digits.isdigit() else None


# --- meta ----------------------------------------------------------------------


def _from_meta(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    if not _is_home(page, ctx.domain):
        return
    named = True
    if page.meta.get("og:site_name"):
        quote = quote_meta(page, "og:site_name")
        # The site name is often the company's name, but can be a product's.
        yield draft("company_name", _site_name(quote.text), quote, 0.7)
    elif name := _title_name(page, ctx.domain):
        yield draft("company_name", name, quote_page(page, "title"), 0.6)
    else:
        named = False
    # The address is the company's website once the page there names the company
    # (a parked or "coming soon" page is not yet anyone's site).
    if named:
        quote = quote_page(page, "url")
        yield draft("website", quote.text, quote, 0.9)
    for key in ("description", "og:description"):
        if page.meta.get(key):
            quote = quote_meta(page, key)
            yield draft("company_description", quote.text, quote, 0.8)
            break


# --- text ----------------------------------------------------------------------


def _from_text(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    for s in sentences(page):
        if not ctx.refers_to_company(s.text):
            continue
        # A sentence attributed to a third party ("LinkedIn lists Acme in the
        # 51-200 employees band") is recorded as that party's reported claim.
        by = reported(s.text, ctx)
        for d in _sentence(s, ctx):
            yield replace(d, value_normalized={**(d.value_normalized or {"qualifier": "eq"}),
                                               **by}) if by else d  # fmt: skip


def _sentence(s: Sentence, ctx: Context) -> Iterator[ObservationDraft]:
    text = s.text
    for m in _FOUNDED.finditer(text):
        yield _span(s, m, 1, "founded_year", 0.8,
                    {"qualifier": "eq", "value": int(m.group(1))})  # fmt: skip
    for m in _HEADQUARTERS.finditer(text):
        yield _span(s, m, 1 if m.group(1) else 2, "headquarters", 0.8)
    for m in _BASED_IN.finditer(text):
        yield _span(s, m, 1, "company_location", 0.7)
    for m in _EMPLOYEES.finditer(text):
        qualifier = "gte" if m.group("plus") else AMOUNT_QUALIFIERS.get(
            (m.group("q") or "").lower(), "eq")  # fmt: skip
        number = int(m.group("n").replace(",", ""))
        yield _span(s, m, 0, "employee_count", 0.8, {"qualifier": qualifier, "value": number})
    for m in _SIZE_BAND.finditer(text):
        low, high = (int(g.replace(",", "")) for g in m.groups())
        if low < high:
            yield _span(s, m, 0, "company_size", 0.8,
                        {"qualifier": "range", "min": low, "max": high})  # fmt: skip
    for pattern, category, certainty in _OWNERSHIP:
        for m in pattern.finditer(text):
            # The company must be what is listed: in "Facebook is a publicly listed company
            # that only wants our money" (a blog post) the "our" comes after the claim.
            if not ctx.is_subject_of(text, m.start()):
                continue
            yield _span(s, m, 0, "ownership_type", certainty,
                        {"qualifier": "eq", "value": category})  # fmt: skip
    yield from _business_model(s, ctx)
    for m in _MARKET.finditer(text):
        yield _span(s, m, 1, "market_domain", 0.6)
    for m in _LEGAL_NAME.finditer(text):
        if _is_company_name(m.group(1), ctx):
            yield draft("legal_name", m.group(), s.quote(), 0.7)


def _profile_lines(page: ParsedPage) -> Iterator[ObservationDraft]:
    """A LinkedIn company page's "Company Size: 51-200 employees" and
    "Employees: 77" lines. Such a page is read only once identity resolution
    has made it this company's profile, so the lines need not name it."""
    if urlsplit(page.url).hostname not in _LINKEDIN_HOSTS:
        return
    for block in page.blocks:
        if m := _PROFILE_BAND.match(block.text):
            low, high = int(m["low"].replace(",", "")), m["high"]
            if high is not None and low < int(high.replace(",", "")):
                normalized = {"qualifier": "range", "min": low, "max": int(high.replace(",", ""))}
            elif high is None and m["plus"]:
                normalized = {"qualifier": "gte", "value": low}
            else:
                continue
            yield draft("company_size", m["band"], quote_block(block, 0, len(block.text)), 0.8,
                        normalized=normalized)  # fmt: skip
        elif m := _PROFILE_HEADCOUNT.match(block.text):
            yield draft(
                "employee_count",
                m["n"],
                quote_block(block, 0, len(block.text)),
                0.7,
                normalized={"qualifier": "eq", "value": int(m["n"].replace(",", ""))},
            )


def _legal_address(page: ParsedPage, ctx: Context) -> Iterator[ObservationDraft]:
    """The address in the block under a block that is only the company's legal
    name (a footer's "Acme Ltd," / "7th Floor, Mount Road, Chennai, India").
    An office's address, not necessarily the headquarters."""
    # ponytail: next-block adjacency, no address parser; a footer laid out otherwise is missed.
    for block, below in pairwise(page.blocks):
        m = _LEGAL_NAME.fullmatch(block.text.rstrip(" ,."))
        text = below.text
        if (
            m
            and _is_company_name(m.group(1), ctx)
            and len(text) <= 160
            and _ADDRESS.fullmatch(text)
        ):
            yield draft("company_location", text, quote_block(below, 0, len(text)), 0.6)


def _business_model(s: Sentence, ctx: Context) -> Iterator[ObservationDraft]:
    names = "|".join(re.escape(n) for n in sorted(ctx.names, key=len, reverse=True))
    subject = re.compile(_SUBJECT.format(name=f"(?:{names})" if names else "(?!)"), re.I)
    text = s.text
    for m in subject.finditer(text):
        end = _CLAUSE_END.search(text, m.end())
        clause_end = end.start() if end else len(text)
        for pattern, category in _BUSINESS_MODELS:
            if k := pattern.search(text, m.end(), clause_end):
                yield _span(s, k, 0, "business_model", 0.6,
                            {"qualifier": "eq", "value": category})  # fmt: skip
        if k := _NONPROFIT.search(text, m.end(), clause_end):
            yield _span(s, k, 0, "ownership_type", 0.8, {"qualifier": "eq", "value": "nonprofit"})


def _span(
    s: Sentence,
    m: re.Match[str],
    group: int,
    field_name: str,
    certainty: float,
    normalized: dict[str, Any] | None = None,
) -> ObservationDraft:
    """A draft whose value is ``m``'s group and whose quote is the whole sentence."""
    value = m.group(group).rstrip(".,")
    return draft(field_name, value, s.quote(), certainty, normalized=normalized)
