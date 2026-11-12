"""Runs the rule-based extractors over a finished crawl and records observations.

Every page is re-parsed from its raw document (so evidence locators match
what ``EvidenceStore`` will verify). Duplicate pages are skipped. A
statement repeated across one site (a footer, ``og:site_name`` on every page)
is recorded once per run: repeating it is not additional evidence. The same
statement on another site is kept; that is corroboration.

Each call is a new extraction run that supersedes the crawl's earlier runs,
so improved rules can be re-run over a stored crawl without fetching again.
``plan_extraction`` computes a run without writing anything (products are then
keyed by slug only), so ``replay.py`` can compare a dry run with a stored run.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import aliased

from ..config import Settings
from ..db.evidence import EvidenceError, EvidenceStore, ObservationDraft, check_draft
from ..db.identity import attributable_pages
from ..db.models import Crawl, Fetch, Observation, Organization, Page, Url, is_current
from ..identity import PROFILE_DOMAINS, external_id
from ..metrics import EXTRACTION_DURATION
from ..parse import ParsedPage, primary_language
from ..rawstore import make_rawstore
from ..urls import registrable_domain, url_host
from ..versions import current_versions
from . import company, competitor, llm, product, signals, traction
from .common import Context
from .company import company_names, extract_company
from .competitor import extract_competitors
from .product import extract_products, identify_products
from .signals import extract_signals
from .traction import extract_traction

log = logging.getLogger(__name__)

EXTRACTORS = (extract_company, extract_products, extract_traction, extract_competitors,
              extract_signals)  # fmt: skip
# A community forum on the company's domain is its users talking: neither the
# company's own statement nor an independent source. Nothing is extracted from it.
# ponytail: recognised by host name only; add a setting if a site needs another name.
_USER_CONTENT_HOST = re.compile(r"^(?:forums?|community|discuss|discussions?|answers)\.")
EXTRACTOR_VERSIONS = {"company": company.VERSION, "product": product.VERSION,
                      "traction": traction.VERSION, "competitor": competitor.VERSION,
                      "signals": signals.VERSION, "llm": llm.VERSION}  # fmt: skip


def extract_crawl(
    engine: Engine, settings: Settings, crawl_id: int, trigger: str = "crawl"
) -> dict[str, Any]:
    """Extract observations from every page of ``crawl_id``. Returns statistics."""
    with EXTRACTION_DURATION.time():
        return _extract_crawl(engine, settings, crawl_id, trigger)


@dataclass
class ExtractionPlan:
    """An extraction run computed but not yet recorded."""

    organization_id: int
    # (page id, page URL, drafts) for every readable page, in page order.
    pages: list[tuple[int, str, list[ObservationDraft]]] = field(default_factory=list)
    products: dict[str, int] = field(default_factory=dict)  # slug -> product id
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # Pages on other sites not shown to be about this company: kept, not read.
    unverified: list[dict[str, Any]] = field(default_factory=list)
    # The company's own pages in another language than its site's main one: not read.
    other_language: list[dict[str, Any]] = field(default_factory=list)
    company_names: set[str] = field(default_factory=set)


def _extract_crawl(
    engine: Engine, settings: Settings, crawl_id: int, trigger: str
) -> dict[str, Any]:
    store = EvidenceStore(engine, make_rawstore(settings.rawstore), settings.sources)
    plan = plan_extraction(store, crawl_id, llm_model=settings.llm.model)
    return record_extraction(store, settings, crawl_id, plan, trigger)


def record_extraction(
    store: EvidenceStore, settings: Settings, crawl_id: int, plan: ExtractionPlan, trigger: str
) -> dict[str, Any]:
    """Record ``plan`` as a new extraction run of ``crawl_id``. Returns statistics."""
    extraction_id = store.start_extraction(crawl_id, trigger, current_versions(settings))
    fields: Counter[str] = Counter()
    for page_id, _, drafts in plan.pages:
        if drafts:
            store.record(extraction_id, plan.organization_id, page_id, drafts)
            fields.update(d.field for d in drafts)
    stats: dict[str, Any] = {
        "extraction_id": extraction_id, "pages": len(plan.pages),
        "unreadable_pages": len(plan.unreadable), "rejected": len(plan.rejected),
        "identity_unverified_pages": len(plan.unverified),
        "other_language_pages": len(plan.other_language),
        "observations": dict(sorted(fields.items())),
        "company_names": sorted(plan.company_names), "products": sorted(plan.products),
    }  # fmt: skip
    log.info("extract.completed", extra={"crawl_id": crawl_id, **stats})
    return stats


def plan_extraction(
    store: EvidenceStore, crawl_id: int, *, write_products: bool = True, llm_model: str = ""
) -> ExtractionPlan:
    """Run the extractors over ``crawl_id``'s stored pages without recording observations.

    With ``write_products=False`` nothing at all is written: products get
    placeholder ids (negative, one per slug) instead of ``product`` rows.
    With ``llm_model``, that model's stored page readings (``llm.read_crawl``)
    gate product fields by page type and add their quoted facts; no call is made.
    """
    final = aliased(Url)
    with store.engine.connect() as conn:
        org_id, domain, seed = conn.execute(
            select(Crawl.organization_id, Organization.registrable_domain, Crawl.seed_url)
            .join(Organization, Organization.id == Crawl.organization_id)
            .where(Crawl.id == crawl_id)
        ).one()
        page_rows = conn.execute(
            select(Page.id, func.coalesce(final.url, Url.url), Url.url, Page.language)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .join(Url, Url.id == Fetch.url_id)
            .outerjoin(final, final.id == Fetch.final_url_id)
            .where(Fetch.crawl_id == crawl_id, Page.duplicate_of_page_id.is_(None))
            .order_by(Page.id)
        ).all()  # fmt: skip
        profiles, site_linked, linking = attributable_pages(conn, crawl_id)
        # A documents crawl (documents.py) has no home page to learn the company's
        # names from: use the names its other crawls found, so "Acme was founded
        # in 2019" in an uploaded deck is recognised as being about the company.
        known_names: set[str] = set()
        if seed.startswith("upload://"):
            known_names = set(conn.execute(
                select(Observation.value_verbatim).distinct()
                .where(Observation.organization_id == org_id,
                       Observation.field.in_(("company_name", "legal_name")),
                       Observation.product_id.is_(None), is_current())
            ).scalars())  # fmt: skip

    plan = ExtractionPlan(organization_id=org_id)
    readings = llm.stored_results(store.engine, crawl_id, llm_model)
    # ponytail: all parsed pages held in memory (two passes); fine for max_pages
    # in the low thousands, stream the second pass if crawls grow much larger.
    pages: list[tuple[int, str, ParsedPage]] = []
    # The site's main language is its first page's (the seed). Its translations
    # restate the same facts, often older or worded otherwise: not read, so a
    # French copy is neither a second source nor a conflict. Unknown: all read.
    main_language = next((primary_language(lang) for _, u, _, lang in page_rows
                          if registrable_domain(url_host(u)) == domain), None)  # fmt: skip
    for page_id, url, requested, language in page_rows:
        if _USER_CONTENT_HOST.match(url_host(url)):
            continue
        site = registrable_domain(url_host(url))
        translated = primary_language(language) not in (None, main_language)
        if site == domain and main_language and translated:
            plan.other_language.append({"page_id": page_id, "url": url, "language": language})
            continue
        if site != domain:
            # Another company can have the same name: the name on a page, or in
            # its URL, does not make the page about this one. A link does.
            # ponytail: one link either way is enough, so a page about two
            # same-name companies that links to this one is still read; weigh
            # such pages down in assess.py if they show up.
            ext = external_id(url) if site in PROFILE_DOMAINS else None
            if ext is not None:
                known = ext in profiles
            else:
                known = page_id in linking or not site_linked.isdisjoint((url, requested))
            if not known:
                plan.unverified.append({"page_id": page_id, "url": url})
                log.info("extract.identity_unverified", extra={"page_id": page_id})
                continue
        try:
            pages.append((page_id, url, store.load_page(page_id)[1]))
        except EvidenceError as exc:
            plan.unreadable.append({"page_id": page_id, "url": url, "error": str(exc)})
            log.warning("extract.page_unreadable", extra={"page_id": page_id, "error": str(exc)})

    ctx = Context(domain=domain)
    ctx.names |= {n.strip() for n in known_names if len(n.strip()) >= 2}
    third_party = replace(ctx, first_party=False)  # shares names and product ids
    own = [p for _, _, p in pages if registrable_domain(url_host(p.url)) == domain]
    for page in own:
        ctx.names |= company_names(page, domain)

    def page_type(page_id: int) -> str | None:
        return (readings.get(page_id) or {}).get("page_type")

    def product_id(slug: str, name: str, basis: str = llm.EXTRACTOR) -> int:
        if slug not in ctx.product_ids:
            ctx.product_ids[slug] = (
                store.ensure_product(org_id, slug, name, basis)
                if write_products else -1 - len(ctx.product_ids)
            )  # fmt: skip
        return ctx.product_ids[slug]

    # Products are identified across the whole site first, so every page's
    # observations can refer to them. The first sighting names the product.
    for page_id, _, page in pages:
        own_page = registrable_domain(url_host(page.url)) == domain
        if own_page and page_type(page_id) not in llm.NOT_PRODUCT_PAGES:
            for candidate in identify_products(page, ctx):
                product_id(candidate.slug, candidate.name, candidate.basis)

    seen: set[tuple[str, str, int | None, str | None, str | None, str, str]] = set()
    for page_id, url, page in pages:
        drafts: list[ObservationDraft] = []
        site = registrable_domain(url_host(page.url))
        page_ctx = ctx if site == domain else third_party
        # A blog post or a syllabus is not the company describing what it sells.
        gated = page_type(page_id) in llm.NOT_PRODUCT_PAGES
        found = [d for extractor in EXTRACTORS for d in extractor(page, page_ctx)
                 if not (gated and d.field in llm.GATED_FIELDS)]  # fmt: skip
        if page_id in readings and site == domain:
            found += llm.page_drafts(page, readings[page_id], page_ctx, product_id)
        for d in found:
            # A different kind of evidence for the same statement is kept
            # (e.g. a competitor both stated and compared on its own page).
            basis = d.related.basis if d.related else None
            key = (site, d.field, d.product_id, d.qualifier, basis, d.value_verbatim,
                   json.dumps(d.value_normalized, sort_keys=True))  # fmt: skip
            if key in seen:
                continue
            try:
                check_draft(d, page)
            except EvidenceError as exc:
                # An extractor produced something its own evidence does not
                # support: a bug to fix, never something to store.
                plan.rejected.append({"page_id": page_id, "url": url,
                                      "field": d.field, "error": str(exc)})  # fmt: skip
                log.warning("extract.rejected", extra={"page_id": page_id, "error": str(exc)})
                continue
            seen.add(key)
            drafts.append(d)
        plan.pages.append((page_id, url, drafts))

    plan.company_names = ctx.names
    plan.products = dict(ctx.product_ids)
    return plan
