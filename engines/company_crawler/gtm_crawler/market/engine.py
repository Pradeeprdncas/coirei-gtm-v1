"""One market-research run: plan, discover, collect, extract, assess, analyze, report.

``run_research`` never raises for a source or provider failure: each is an
``Issue`` on the report, and the run finishes ``PARTIAL`` with what it has.
A run that collected no evidence at all is ``FAILED`` (the report still
lists what was tried); so is one whose analysis raised (the report keeps its sources and
evidence, with the error as an issue). ``stop`` (cancellation) and ``Budget.max_duration_seconds``
end discovery and collection early; analysis then runs over what was collected.

When discovery yields nothing (the search provider is down or not set up)
and an earlier run of the same subject exists (``prior``), its sources and
evidence are reused and marked ``CACHED``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..parse import ParsedPage, TextBlock
from ..rawstore import RawStoreBackend
from ..search import SearchError, SearchResult, rank_candidates
from ..urls import InvalidUrl, normalize_url
from . import analyze, pdf
from .ads import (
    CAPABILITIES,
    AdEvidence,
    AdView,
    ad_evidence,
    ad_section,
    dossier_competitors,
)
from .analyze import ANALYSIS_VERSION
from .assessment import (
    classify,
    domain_of,
    find_contradictions,
    independent_domains,
    metric_summaries,
    source_quality,
)
from .collect import Collected, Collector
from .extract import (
    EXTRACTOR_VERSION,
    dimension_patterns,
    extract_evidence,
    parse_metrics,
    pdf_locators,
    pdf_table_evidence,
    relevance,
    subject_terms,
)
from .models import (
    Budget,
    Dimension,
    EvidenceKind,
    EvidenceRecord,
    Issue,
    Locator,
    ProductInput,
    Report,
    ResearchMode,
    ResearchRequest,
    RetrievalStatus,
    RunStatus,
    SourceRecord,
    SourceSelection,
    SourceType,
)
from .ocr import Ocr
from .plan import Plan, build_plan
from .scope import acronyms
from .scoring import score_opportunities
from .select import MIN_VALUE, WAVE, Candidate, Needs, evaluate, pick_wave
from .sizing import evidence_total, size_market

log = logging.getLogger(__name__)

MIN_RELEVANCE = 0.5
MIN_TERMS_MATCHED = 3
# Not fetched: the engine reads HTML only.
_DOCUMENT = re.compile(r"\.(docx?|xlsx?|pptx?|zip|csv|epub)(?:$|[?#])", re.I)
# PDFs are read when market.pdf_enabled and pdfplumber is installed (market/pdf.py). The
# URL only decides what is skipped: what is read is decided by the file's signature.
_PDF_URL = re.compile(r"\.pdf(?:$|[?#])", re.I)
OCR_CERTAINTY_FACTOR = 0.6
# Dossier fields that speak to the product market, and what they speak to.
DOSSIER_DIMENSIONS: dict[str, Dimension] = {
    "price": Dimension.PRICING, "plan_name": Dimension.PRICING,
    "key_feature": Dimension.COMPETITION, "business_model": Dimension.COMPETITION,
    "competitor": Dimension.COMPETITION, "market_domain": Dimension.SEGMENT,
    "main_use_case": Dimension.PAIN_POINT, "customer": Dimension.ADOPTION,
    "product_positioning": Dimension.COMPETITION,
}  # fmt: skip

Progress = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class Search(Protocol):
    async def search(
        self, query: str, num_results: int, include_domains: tuple[str, ...] = ()
    ) -> tuple[str, list[SearchResult]]: ...


class _Run:
    def __init__(self, req: ResearchRequest, progress: Progress | None) -> None:
        self.req = req
        self.issues: list[Issue] = []
        self.stats: dict[str, Any] = {"queries_sent": 0, "queries_failed": 0}
        self.plan: Plan | None = None
        self._progress = progress

    def issue(self, stage: str, severity: str, message: str) -> None:
        self.issues.append(Issue(stage=stage, severity=severity, message=message[:500]))
        log.info("market.issue", extra={"stage": stage, "severity": severity,
                                        "detail": message[:300]})  # fmt: skip

    async def progress(self, stage: str, **detail: Any) -> None:
        if self._progress is not None:
            result = self._progress(stage, detail)
            if inspect.isawaitable(result):
                await result


def _terms(req: ResearchRequest) -> set[str]:
    if req.mode is ResearchMode.PRODUCT and req.product is not None:
        p = req.product
        return subject_terms(req.industry) or subject_terms(p.problem or p.description or p.name)
    return subject_terms(req.subject, req.industry)


def _vocabulary(req: ResearchRequest) -> set[str]:
    """Words that may name the subject market: subject and industry, and their acronyms."""
    texts = [t for t in (req.subject, req.industry) if t]
    return {w for t in texts for w in re.findall(r"[a-z0-9]+", t.lower())} | {
        a for t in texts for a in acronyms(t)}  # fmt: skip


def _is_relevant(page: ParsedPage, terms: set[str]) -> bool:
    share = relevance(page, terms)
    return share >= MIN_RELEVANCE or share * len(terms) >= MIN_TERMS_MATCHED


def _competitor_urls(
    product: ProductInput, settings: Settings, budget: Budget
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(pages to fetch, pages over the budget) of the known competitors.

    Per competitor: the given page, pricing under its path, pricing at the site,
    the home page, at most ``max_pages_per_domain`` of them; all competitors
    together at most ``max(max_sources, max_pages_per_domain)`` (they are not
    counted in ``max_sources`` otherwise)."""
    out: list[tuple[str, str]] = []
    over: list[tuple[str, str]] = []
    total = max(budget.max_sources, budget.max_pages_per_domain)
    for c in product.known_competitors:
        if not c.url:
            continue
        try:
            root = normalize_url(c.url, settings.urls)
        except InvalidUrl:
            continue
        origin = "/".join(root.split("/", 3)[:3])
        # Pricing under the given path, unless the given page is the pricing page.
        under = root if root.rstrip("/").endswith("/pricing") else f"{root.rstrip('/')}/pricing"
        # One URL per page: "/pricing/" and "/pricing" are the same page.
        urls: list[str] = []
        for u in (root, under, f"{origin}/pricing", f"{origin}/"):
            if u.rstrip("/") not in {x.rstrip("/") for x in urls}:
                urls.append(u)
        for n, url in enumerate(urls):
            within = n < budget.max_pages_per_domain and len(out) < total
            (out if within else over).append((url, f"known_competitor:{c.name}"))
    return out, over


async def _ads(
    run: _Run,
    product: ProductInput,
    crawled: list[dict[str, Any]],
    ads: Callable[[list[str]], list[AdView]] | None,
    settings: Settings,
    budget: Budget,
    fixed: list[tuple[str, str]],
    over: list[tuple[str, str]],
) -> AdEvidence:
    """The captured ads of the known competitors and of the crawled company's dossier
    competitors (those with a domain; co-listed candidates have none) as evidence
    (``market.ads``). Their landing pages join the competitor pages to fetch,
    ``max_pages_per_domain`` per competitor and ``max(max_sources, max_pages_per_domain)``
    in all. A known competitor without captured ads is a warning; a dossier one is not
    (it was not asked for): the checklist (``gtm-market --ad-checklist``) lists it."""
    names: dict[str, str] = {}
    for c in product.known_competitors:
        if c.url:
            try:
                names.setdefault(domain_of(normalize_url(c.url, settings.urls)), c.name)
            except InvalidUrl:
                continue
    asked = set(names)
    for r in crawled:
        if r["role"] == "competitor" and r["domain"]:
            names.setdefault(r["domain"], r["name"])
    if not names:
        run.issue("ads", "warning", "include_ads needs competitors with a domain: known "
                  "competitors with a URL, or a company_id whose dossier names some")  # fmt: skip
        return AdEvidence()
    if ads is None:
        run.issue("ads", "warning", "include_ads given but no ad store here")
        return AdEvidence()
    try:
        views = await asyncio.to_thread(ads, list(names))
    except Exception as exc:  # noqa: BLE001 - captured ads are optional input, like the dossier
        run.issue("ads", "warning", f"captured ads not usable: {type(exc).__name__}: {exc}")
        return AdEvidence()
    have = {v.domain for v in views}
    missing = sorted(n for d, n in names.items() if d in asked and d not in have)
    if missing:
        run.issue("ads", "warning", f"no captured ads for: {', '.join(missing)}")
    others = sorted(n for d, n in names.items() if d not in asked and d not in have)
    if others:
        run.issue("ads", "info", f"dossier competitors without captured ads: "
                  f"{', '.join(others)}")  # fmt: skip
    found = ad_evidence(views, names)
    known = {u.rstrip("/") for u, _ in fixed + over}
    total = max(budget.max_sources, budget.max_pages_per_domain)
    per: Counter[str] = Counter()
    for url, why in found.landing:
        if url.rstrip("/") in known:
            continue
        per[why] += 1
        within = per[why] <= budget.max_pages_per_domain and per.total() <= total
        (fixed if within else over).append((url, why))
    run.stats["ads"] = {
        "competitors": len(names),
        "ads": len(found.sources),
        "evidence": len(found.evidence),
        "landing_pages": sum(per.values()),
    }
    return found


def dossier_evidence(
    dossier: dict[str, Any],
) -> tuple[list[SourceRecord], list[EvidenceRecord]]:
    """The crawled company's and its competitors' cited facts, as market evidence.

    Each citation keeps its URL, quote, locator and raw-artifact hash, so the
    chain back to the fetched page is the crawler's own (``/lineage``)."""
    sources: dict[str, SourceRecord] = {}
    evidence: list[EvidenceRecord] = []

    def add(owner: str, fields: dict[str, Any]) -> None:
        for name, dim in DOSSIER_DIMENSIONS.items():
            entries = fields.get(name)
            for entry in entries if isinstance(entries, list) else [entries] if entries else []:
                if "value" not in entry:
                    continue  # claims and unknowns are not facts
                for c in entry.get("citations", []):
                    url = c.get("url")
                    quote = (c.get("quote") or "").strip()
                    if not url or not quote:
                        continue
                    src = sources.get(url)
                    if src is None:
                        retrieved = c.get("retrieved_at")
                        published = c.get("published_at")
                        src = sources[url] = SourceRecord(
                            url=url, domain=domain_of(url),
                            source_type=SourceType.CRAWLED_DOSSIER,
                            tier=int(c.get("source_tier") or 4), raw_sha256=c.get(
                                "artifact_sha256"),
                            retrieved_at=datetime.fromisoformat(retrieved) if retrieved else None,
                            published_at=datetime.fromisoformat(published) if published else None,
                            status=RetrievalStatus.CACHED, discovered_by="dossier",
                            extraction_method=c.get("extractor"), publisher=owner,
                        )  # fmt: skip
                    loc = c.get("locator") or {}
                    evidence.append(EvidenceRecord(
                        source_id=src.source_id, claim=f"{owner} {name}: {entry['value']}"[:2000],
                        excerpt=quote[:2000], dimensions=[dim], extraction_certainty=0.8,
                        locator=Locator(type="dossier", value=f"{loc.get('type')}:"
                                        f"{loc.get('value')}"[:500], char_start=loc.get(
                                            "char_start"), char_end=loc.get("char_end")),
                        segment=None,
                    ))  # fmt: skip

    company = dossier.get("company", {})
    add(str(company.get("domain") or "company"), company.get("fields", {}))
    for product in dossier.get("products", []):
        add(str(product.get("name") or "product"), product.get("fields", {}))
    for rival in dossier.get("competitors", []):
        add(str(rival.get("name") or rival.get("domain")), rival.get("fields", {}))
    return list(sources.values()), evidence


def _ocr_evidence(collected: Collected, terms: set[str], req: ResearchRequest,
                  today: date | None) -> list[EvidenceRecord]:  # fmt: skip
    out: list[EvidenceRecord] = []
    for url, text in collected.ocr:
        page = ParsedPage(url=url, title=collected.source.title,
                          blocks=[TextBlock(xpath="/ocr", tag="p", text=text)])  # fmt: skip
        for e in extract_evidence(page, collected.source.source_id, terms,
                                  geography=req.geography, today=today,
                                  vocabulary=_vocabulary(req)):  # fmt: skip
            e.locator = Locator(type="image", value=url, char_start=e.locator.char_start if
                                e.locator else None, char_end=e.locator.char_end if e.locator
                                else None)  # fmt: skip
            e.extraction_certainty = round(e.extraction_certainty * OCR_CERTAINTY_FACTOR, 2)
            out.append(e)
    return out


def _extract(
    c: Collected, terms: set[str], req: ResearchRequest, today: date | None, vendor: bool
) -> list[EvidenceRecord]:
    """Evidence of one collected page or PDF (paragraphs, table rows, OCR text)."""
    assert c.page is not None
    src = c.source
    # A known competitor's page: its prices are that competitor's.
    found_by = src.discovered_by or ""
    name = found_by.removeprefix("known_competitor:") if vendor and \
        found_by.startswith("known_competitor:") else None  # fmt: skip
    found = extract_evidence(c.page, src.source_id, terms, geography=req.geography,
                             today=today, always_relevant=vendor,
                             vocabulary=_vocabulary(req), vendor=name)  # fmt: skip
    if c.pdf is not None:
        libraries = ", ".join(f"{k} {v}" for k, v in c.pdf["libraries"].items())
        src.extraction_method = f"rules over PDF text ({libraries}; {c.pdf['version']})"
        pdf_locators(found, c.pdf["version"])
        found += pdf_table_evidence(c.pdf, src.source_id, terms, geography=req.geography,
                                    today=today, vocabulary=_vocabulary(req))  # fmt: skip
    return found + _ocr_evidence(c, terms, req, today)


async def _discover(
    run: _Run, search: Search | None, settings: Settings, stop: asyncio.Event | None,
    deadline: float,
) -> list[Candidate]:  # fmt: skip
    """The plan's queries, sent in order; one candidate per URL with what search said
    about it (title, date, position) and the queries and questions that found it,
    ordered as ``search.rank_candidates`` would (more queries, then position)."""
    req = run.req
    plan = build_plan(req, settings.market)
    run.stats["plan"] = {"questions": [q.key for q in plan.questions],
                         "queries": [q.text for q in plan.queries],
                         "over_budget": [q.text for q in plan.over_budget]}  # fmt: skip
    run.plan = plan
    if search is None:
        run.issue(
            "discovery",
            "warning",
            "no search provider configured (set "
            "GTM__SEARCH__API_KEY); only known competitor pages and cached evidence used",
        )
        return []
    found: dict[str, Candidate] = {}
    for n, q in enumerate(plan.queries):
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            run.stats["queries_not_sent"] = len(plan.queries) - n
            if not (stop is not None and stop.is_set()):
                run.issue("discovery", "warning", f"duration budget reached: "
                          f"{len(plan.queries) - n} query(ies) not sent")  # fmt: skip
            break
        await run.progress("discovery", query=q.text)
        run.stats["queries_sent"] += 1
        try:
            provider, results = await search.search(q.text, req.limits.results_per_query,
                                                    q.include_domains)  # fmt: skip
        except SearchError as exc:
            run.stats["queries_failed"] += 1
            run.issue("discovery", "warning", f"search failed for {q.text!r}: {exc}")
            continue
        for r in results:
            try:
                url = normalize_url(r.url, settings.urls)
            except InvalidUrl:
                continue
            c = found.get(url)
            if c is None:
                tier = classify(url, settings.sources, settings.market)[1]
                c = found[url] = Candidate(url=url, domain=domain_of(url), tier=tier,
                                           position=r.position)  # fmt: skip
            c.title = c.title or r.title
            c.published = c.published or r.published
            c.position = min(c.position, r.position)
            if q.text not in c.queries:
                c.queries.append(q.text)
            if q.question not in c.questions:
                c.questions.append(q.question)
    order = {
        url: i
        for i, (url, _) in enumerate(
            rank_candidates((u, q, c.position) for u, c in found.items() for q in c.queries)
        )
    }
    return sorted(found.values(), key=lambda c: order[c.url])


def _screen(
    candidates: list[Candidate], settings: Settings
) -> tuple[list[Candidate], list[SourceRecord]]:
    """Candidates that can be read at all; the others SKIPPED with the reason (document
    types not read, PDFs while PDF support is off or not installed)."""
    kept: list[Candidate] = []
    skipped: list[SourceRecord] = []
    for c in candidates:
        reason = None
        if _DOCUMENT.search(c.url):
            reason = "document type not fetched (HTML and PDF only)"
        elif c.is_pdf and (why_not := _pdf_unreadable(settings)):
            reason = why_not
        if reason is None:
            kept.append(c)
        else:
            s = _skipped(c.url, c.queries[0], settings, reason)
            s.search_title = c.title
            skipped.append(s)
    return kept, skipped


def _pdf_unreadable(settings: Settings) -> str | None:
    """Why PDFs are not read in this configuration, or None."""
    if not settings.market.pdf_enabled:
        return "PDF not read: market.pdf_enabled = false"
    ok, reason = pdf.available()
    return None if ok else f"PDF not read: {reason}"


def _skipped(
    url: str, why: str, settings: Settings, reason: str, vendor: bool = False
) -> SourceRecord:
    st, tier = classify(url, settings.sources, settings.market, vendor=vendor)
    return SourceRecord(url=url, domain=domain_of(url), source_type=st, tier=tier,
                        status=RetrievalStatus.SKIPPED, discovered_by=why,
                        status_detail=reason)  # fmt: skip


def _question_report(
    run: _Run, evidence: list[EvidenceRecord], sources: list[SourceRecord],
    by_id: dict[str, SourceRecord], no_search: bool,
) -> list[dict[str, Any]]:  # fmt: skip
    """Per plan question: answered or not, by how many domains, which sources were
    collected or left for it, and why it is still open."""
    out: list[dict[str, Any]] = []
    for q in run.plan.questions if run.plan else []:
        dims = set(q.dimensions)
        mine = [e for e in evidence if dims & set(e.dimensions)]
        domains = {by_id[e.source_id].domain for e in mine if e.source_id in by_id}
        aimed = [s for s in sources if s.selection and q.key in s.selection.targets]
        tried = [s for s in aimed if s.status is not RetrievalStatus.SKIPPED]
        untried = [s for s in aimed if s.status is RetrievalStatus.SKIPPED]
        reason = ""
        if not domains:
            outcomes = Counter("no evidence for it" if s.status is RetrievalStatus.FETCHED
                               else s.status.value for s in tried)  # fmt: skip
            parts = []
            if tried:
                parts.append(
                    f"{len(tried)} source(s) collected for it gave none ("
                    + ", ".join(f"{n} {k}" for k, n in sorted(outcomes.items()))
                    + ")"
                )
            if untried:
                why = Counter(s.status_detail or "" for s in untried).most_common(1)[0][0]
                parts.append(f"{len(untried)} candidate(s) not collected: {why}")
            if not parts:
                plan = run.plan
                assert plan is not None
                sent = {x.question for x in plan.queries[: run.stats["queries_sent"]]}
                if no_search:
                    parts.append("no search provider")
                elif q.key not in sent:
                    parts.append("its searches were not sent (budget.max_queries or deadline)")
                else:
                    parts.append("its searches found nothing new for it")
            reason = "; ".join(parts)
        out.append({"key": q.key, "text": q.text, "answered": bool(domains),
                    "domains": len(domains), "evidence": len(mine), "tried": len(tried),
                    "untried": len(untried), "reason": reason})  # fmt: skip
    return out


def _total_missing(req: ResearchRequest, evidence: list[EvidenceRecord],
                   by_id: dict[str, SourceRecord]) -> bool:  # fmt: skip
    """A requested evidence-derived total that the evidence so far cannot give."""
    s = req.sizing
    if s is None or s.top_down is not None or not s.use_evidence_total:
        return False
    scoped = s.model_copy(update={k: v for k, v in (("geography", req.geography),
                                                    ("segment", req.segment))
                                  if getattr(s, k) is None})  # fmt: skip
    total, _, _ = evidence_total(metric_summaries(evidence, by_id, []), scoped)
    return total is None


@dataclass
class _Collection:
    """What collection produced, and how the budgets were spent."""

    sources: list[SourceRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    pages: dict[str, ParsedPage] = field(default_factory=dict)
    used: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("useful", "attempts", "html_pages", "pdfs", "pdf_pages", "bytes", "waves"), 0
        )
    )
    stopped: str = "no candidates"
    timed_out: int = 0  # sources cut off by the duration budget mid-wave
    untried: list[tuple[Candidate, SourceSelection | None, str]] = field(default_factory=list)


def pdf_focus(needs: Needs, terms: set[str], geography: str | None) -> pdf.Focus:
    """What a long PDF's pages are chosen for: the rules of the questions still open (all
    of them when none is), the subject's terms and the place."""
    questions = needs.open_questions() or needs.plan.questions
    words = sorted(terms | set((geography or "").lower().split()))
    return pdf.Focus(
        terms=words[:40], questions={q.key: dimension_patterns(q.dimensions) for q in questions}
    )


async def _collect(
    run: _Run,
    collector: Collector,
    fixed: list[tuple[str, str]],
    candidates: list[Candidate],
    terms: set[str],
    today: date | None,
    stop: asyncio.Event | None,
    deadline: float,
) -> _Collection:
    """Known competitor pages first (not counted in the budgets); then search results
    in waves picked by ``select`` from the questions still open, each wave processed
    (duplicates, relevance, extraction) in pick order before the next is chosen."""
    req, limits, settings = run.req, run.req.limits, collector.settings
    out = _Collection()
    first_text: dict[str, str] = {}  # main-text hash -> the first source with it
    by_id: dict[str, SourceRecord] = {}
    failed_domains: set[str] = set()
    per_domain: dict[str, int] = {}
    now = today or date.today()

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    def process(c: Collected) -> list[EvidenceRecord]:
        src = c.source
        out.sources.append(src)
        by_id[src.source_id] = src
        if src.status is not RetrievalStatus.FETCHED or c.page is None:
            return []
        if src.content_sha256:
            original = first_text.setdefault(src.content_sha256, src.source_id)
            if original != src.source_id:
                src.status = RetrievalStatus.DUPLICATE
                src.status_detail = f"same text as {original}"
                return []
        vendor = src.source_type is SourceType.COMPANY_WEBSITE
        if not vendor and not _is_relevant(c.page, terms):
            src.status = RetrievalStatus.IRRELEVANT
            src.status_detail = f"mentions {relevance(c.page, terms):.0%} of the subject terms"
            return []
        src.extraction_method, src.extraction_version = "rules", EXTRACTOR_VERSION
        out.pages[src.source_id] = c.page
        try:
            found = _extract(c, terms, req, today, vendor)
        except Exception as exc:  # noqa: BLE001 - one source's content must not end the run
            src.limitations.append(f"extraction failed: {type(exc).__name__}"[:200])
            run.issue("extraction", "warning", f"{src.url}: extraction failed "
                      f"({type(exc).__name__})")  # fmt: skip
            return []
        if not found:
            src.limitations.append("no sentence matched the extraction rules")
        out.evidence.extend(found)
        return found

    async def gather(jobs: list[Any]) -> list[Collected | None]:
        """Run ``jobs`` until all finish, the deadline passes or ``stop`` is set; what did
        not finish is cancelled (and comes back None). Cancellation of this coroutine
        cancels them too, so nothing keeps fetching after the collector is closed."""
        tasks = [asyncio.ensure_future(j) for j in jobs]
        watcher = asyncio.ensure_future(stop.wait()) if stop is not None else None
        try:
            waiting = set(tasks)
            while waiting and not stopping():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait(waiting | ({watcher} if watcher else set()),
                                             timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                waiting -= done
        finally:
            for t in [*tasks, *([watcher] if watcher else [])]:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, *([watcher] if watcher else []),
                                 return_exceptions=True)  # fmt: skip
        return [t.result() if t.done() and not t.cancelled() and t.exception() is None
                else None for t in tasks]  # fmt: skip

    def lost(url: str, why: str, vendor: bool = False) -> Collected:
        out.timed_out += 0 if stopping() else 1
        reason = "cancelled" if stopping() else "duration budget"
        return Collected(_skipped(url, why, settings, reason, vendor))

    for i in range(0, len(fixed), WAVE):  # competitor pages
        chunk = fixed[i : i + WAVE]
        if stopping() or time.monotonic() >= deadline:
            for u, why in chunk:
                process(lost(u, why, vendor=True))
            continue
        await run.progress("collection", url=chunk[0][0], done=len(out.sources),
                           total=len(fixed) + len(candidates))  # fmt: skip
        got = await gather([collector.collect(u, why, vendor=True) for u, why in chunk])
        for (u, why), col in zip(chunk, got, strict=True):
            process(col or lost(u, why, vendor=True))

    needs = Needs(run.plan) if run.plan is not None else None
    pending = list(candidates)
    used = out.used

    def blocked(c: Candidate, chosen: list[Candidate]) -> str | None:
        """Which budget stops ``c`` given the picks of this wave so far (None: none)."""
        pdfs = used["pdfs"] + sum(x.is_pdf for x in chosen)
        html = used["html_pages"] + sum(not x.is_pdf for x in chosen)
        domain = per_domain.get(c.domain, 0) + sum(x.domain == c.domain for x in chosen)
        pages = used["pdf_pages"] + limits.max_pdf_pages * sum(x.is_pdf for x in chosen)
        if c.is_pdf and pdfs >= limits.max_pdfs:
            return f"over the PDF allowance ({limits.max_pdfs}, budget.max_pdfs)"
        if c.is_pdf and pages >= limits.max_extraction_pages:
            return "PDF page budget used (budget.max_extraction_pages)"
        if not c.is_pdf and html >= limits.html_pages:
            return f"over the HTML page allowance ({limits.html_pages})"
        if domain >= limits.max_pages_per_domain:
            return "over the per-domain budget"
        return None

    def rank() -> list[tuple[Candidate, SourceSelection]]:
        assert needs is not None
        needs.update(out.evidence, by_id, _total_missing(req, out.evidence, by_id))
        return sorted(((c, evaluate(c, needs, terms, req.geography, failed_domains, now))
                       for c in pending), key=lambda cs: -cs[1].value)  # fmt: skip

    while pending and needs is not None:
        ranked = rank()
        stopped = None
        if stopping():
            stopped = "cancelled"
        elif time.monotonic() >= deadline:
            stopped = "duration budget"
        elif used["useful"] >= limits.max_sources:
            stopped = f"source budget: {used['useful']} useful source(s) (budget.max_sources)"
        elif used["attempts"] >= limits.attempts:
            stopped = f"attempt budget: {used['attempts']} attempt(s) (budget.max_attempts)"
        elif used["bytes"] >= limits.max_total_bytes:
            stopped = f"byte budget: {used['bytes']} bytes (budget.max_total_bytes)"
        if stopped is not None:
            out.stopped = stopped
            break
        room = min(WAVE, limits.max_sources - used["useful"], limits.attempts - used["attempts"])
        wave = pick_wave(ranked, needs, lambda c, chosen: blocked(c, chosen) is None, room)
        if not wave:
            free = [sel.value for c, sel in ranked if blocked(c, []) is None]
            open_ = ", ".join(q.key for q in needs.open_questions()) or "none"
            out.stopped = ("no remaining candidate passes the budgets" if not free else
                           f"remaining candidates add little (best value {max(free):.3f} < "
                           f"{MIN_VALUE}); open questions: {open_}")  # fmt: skip
            break
        used["waves"] += 1
        picks = [ranked[i] for i in wave]
        for c, _ in picks:
            pending.remove(c)
        await run.progress("collection", wave=used["waves"], done=len(out.sources),
                           total=len(fixed) + len(candidates))  # fmt: skip
        # PDF pages are handed out in pick order, so a wave cannot overrun the run's budget;
        # a URL without ".pdf" that serves a PDF may read one only while PDFs remain.
        pages_left = limits.max_extraction_pages - used["pdf_pages"]
        pdfs_left = limits.max_pdfs - used["pdfs"] - sum(c.is_pdf for c, _ in picks)
        allowance: list[int] = []
        for c, _ in picks:
            grant = min(limits.max_pdf_pages, max(0, pages_left))
            if not c.is_pdf and pdfs_left <= 0:
                grant = 0
            allowance.append(grant)
            pages_left -= grant
        focus = pdf_focus(needs, terms, req.geography)
        got = await gather([collector.collect(c.url, c.queries[0], max_pdf_pages=grant,
                                              pdf_focus=focus)
                            for (c, _), grant in zip(picks, allowance, strict=True)])  # fmt: skip
        for (c, sel), col in zip(picks, got, strict=True):
            col = col or lost(c.url, c.queries[0])
            src = col.source
            src.search_title = c.title
            src.selection = sel.model_copy(update={"wave": used["waves"]})
            # Counted when the page was requested (not when robots.txt, the policy or the
            # deadline stopped it first); a PDF downloaded but not read still counts.
            if src.retrieved_at is not None:
                used["attempts"] += 1
                per_domain[c.domain] = per_domain.get(c.domain, 0) + 1
                used["pdfs" if col.pdf_body or c.is_pdf else "html_pages"] += 1
                used["bytes"] += src.size_bytes or 0
                used["pdf_pages"] += pdf.pages_read(col.pdf) if col.pdf else 0
                if col.pdf_body and not c.is_pdf:
                    pdfs_left -= 1
            if src.status in (RetrievalStatus.FAILED, RetrievalStatus.BLOCKED,
                              RetrievalStatus.UNREADABLE):  # fmt: skip
                failed_domains.add(c.domain)
            if process(col):
                used["useful"] += 1
    else:
        out.stopped = "every candidate was considered" if needs is not None else "no plan"
    if out.timed_out and not out.stopped.startswith(("cancelled", "duration budget")):
        out.stopped = "duration budget"  # the last wave was cut short
    final = {c.url: sel for c, sel in rank()} if pending and needs is not None else {}
    for c in pending:
        last_sel = final.get(c.url)
        why = blocked(c, []) or out.stopped
        out.untried.append((c, last_sel, why))
    return out


async def run_research(
    req: ResearchRequest,
    settings: Settings,
    *,
    search: Search | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    rawstore: RawStoreBackend | None = None,
    dossier: Callable[[int], dict[str, Any]] | None = None,
    ads: Callable[[list[str]], list[AdView]] | None = None,
    prior: Callable[[str], Report | None] | None = None,
    narrator: Callable[[Report], Awaitable[None]] | None = None,
    progress: Progress | None = None,
    stop: asyncio.Event | None = None,
    today: date | None = None,
) -> Report:
    started = time.monotonic()
    deadline = started + req.limits.max_duration_seconds
    run = _Run(req, progress)
    terms = _terms(req)
    await run.progress("planning")
    found = await _discover(run, search, settings, stop, deadline)
    fixed, over = _competitor_urls(req.product, settings, req.limits) if req.product else ([], [])
    # The crawled company's dossier, read once: its facts are evidence, its competitors'
    # captured ads too (include_ads). A failure is kept and reported with the evidence.
    crawled: dict[str, Any] | Exception | None = None
    if req.product is not None and req.product.company_id is not None and dossier is not None:
        try:
            crawled = await asyncio.to_thread(dossier, req.product.company_id)
        except Exception as exc:  # noqa: BLE001 - the dossier is optional input
            crawled = exc
    found_ads = AdEvidence()
    if req.product is not None and req.product.include_ads:
        listed = dossier_competitors(crawled) if isinstance(crawled, dict) else []
        found_ads = await _ads(run, req.product, listed, ads, settings, req.limits, fixed, over)
    candidates, skipped = _screen(found, settings)
    skipped += [_skipped(u, why, settings, "over the competitor page budget", vendor=True)
                for u, why in over]  # fmt: skip

    sources: list[SourceRecord] = []
    evidence: list[EvidenceRecord] = []
    pages: dict[str, ParsedPage] = {}
    collection = _Collection()
    if (candidates or fixed) and not (stop is not None and stop.is_set()):
        ocr = Ocr(settings.market) if settings.market.ocr_enabled else None
        if ocr is not None and not ocr.available:
            run.issue("collection", "info", f"OCR off: {ocr.reason}")
        collector = Collector(settings, req.limits, transport=transport, rawstore=rawstore,
                              ocr=ocr)  # fmt: skip
        try:
            collection = await _collect(run, collector, fixed, candidates, terms, today, stop,
                                        deadline)  # fmt: skip
        finally:
            await collector.aclose()
        sources, evidence, pages = collection.sources, collection.evidence, collection.pages
    elif candidates or fixed:
        collection.untried = [(c, None, "cancelled") for c in candidates]
        collection.stopped = "cancelled"
        skipped += [_skipped(u, why, settings, "cancelled", vendor=True) for u, why in fixed]
    for c, sel, why in collection.untried:
        s = _skipped(c.url, c.queries[0], settings, why)
        s.search_title, s.selection = c.title, sel
        skipped.append(s)
    lost = collection.timed_out + (
        len(collection.untried) if collection.stopped == "duration budget" else 0
    )
    if lost:
        run.issue("collection", "warning", f"duration budget reached: {lost} source(s) not "
                  "collected")  # fmt: skip
    run.stats["selection"] = {
        "method": "adaptive waves: value = need_fit x quality (market/select.py)",
        "stopped": collection.stopped, "used": collection.used,
        "limits": {"max_sources": req.limits.max_sources, "max_attempts": req.limits.attempts,
                   "max_html_pages": req.limits.html_pages, "max_pdfs": req.limits.max_pdfs,
                   "max_pdf_pages": req.limits.max_pdf_pages,
                   "max_extraction_pages": req.limits.max_extraction_pages,
                   "max_total_bytes": req.limits.max_total_bytes,
                   "max_pages_per_domain": req.limits.max_pages_per_domain},
    }  # fmt: skip
    sources += skipped

    if req.product is not None and req.product.company_id is not None:
        if dossier is None:
            run.issue("competitors", "warning", "company_id given but no dossier access here")
        else:
            try:
                if isinstance(crawled, Exception):
                    raise crawled
                assert crawled is not None
                d_sources, d_evidence = dossier_evidence(crawled)
                sources += d_sources
                evidence += d_evidence
            except Exception as exc:  # noqa: BLE001 - the dossier is optional input
                run.issue("competitors", "warning", f"dossier {req.product.company_id} not "
                          f"usable: {type(exc).__name__}: {exc}")  # fmt: skip
    sources += found_ads.sources
    evidence += found_ads.evidence

    if not any(s.status is RetrievalStatus.FETCHED for s in sources) and prior is not None:
        try:
            earlier = await asyncio.to_thread(prior, req.subject_key())
        except Exception as exc:  # noqa: BLE001 - the cache is optional input, like the dossier
            earlier = None
            run.issue("discovery", "warning", f"earlier report not usable as a cache: "
                      f"{type(exc).__name__}: {exc}")  # fmt: skip
        if earlier is not None:
            run.issue("discovery", "warning", f"no new sources; reusing evidence from run "
                      f"{earlier.research_id} ({earlier.generated_at:%Y-%m-%d})")  # fmt: skip
            for s in earlier.sources:
                if s.status in (RetrievalStatus.FETCHED, RetrievalStatus.CACHED):
                    sources.append(s.model_copy(update={"status": RetrievalStatus.CACHED}))
            # Figures are re-read from the excerpt (the authority) with today's rules, so
            # evidence from an earlier extractor version gets its market scope too.
            evidence += [e.model_copy(update={
                "cached_from": e.cached_from or earlier.research_id, "contradicted_by": [],
                "metrics": parse_metrics(e.excerpt, today, terms, req.geography,
                                     _vocabulary(req)) if e.metrics
                else []}) for e in earlier.evidence if e.kind is EvidenceKind.DIRECT]  # fmt: skip

    await run.progress("assessment", evidence=len(evidence))
    analysis_failed = False
    try:
        by_id = {s.source_id: s for s in sources}
        per_source: dict[str, list[EvidenceRecord]] = {}
        for e in evidence:
            per_source.setdefault(e.source_id, []).append(e)
        for s in sources:
            if s.status in (RetrievalStatus.FETCHED, RetrievalStatus.CACHED):
                s.quality = source_quality(s, pages.get(s.source_id),
                                           per_source.get(s.source_id, []),
                                           req.geography)  # fmt: skip
        contradictions = find_contradictions(evidence, by_id)
        metrics = metric_summaries(evidence, by_id, contradictions)

        await run.progress("analysis")
        run.stats["questions"] = _question_report(run, evidence, sources, by_id, search is None)
        # Unanswered questions say why ("unanswered: <question> (<reason>)").
        gaps = [f"unanswered: {q['text']} ({q['reason']})" for q in run.stats["questions"]
                if not q["answered"]]  # fmt: skip
        if collection.stopped.startswith(("source budget", "attempt budget", "byte budget")):
            waiting = sorted({k for q in run.stats["questions"] if not q["answered"]
                              for k in [q["key"]] if q["untried"]})  # fmt: skip
            if waiting:
                run.issue("budget", "warning", f"collection stopped ({collection.stopped}) with "
                          "untried candidates for open question(s): "
                          f"{', '.join(waiting)}")  # fmt: skip
        sections = (analyze.industry_sections(evidence, by_id, metrics, req.geography)
                    if req.mode is ResearchMode.INDUSTRY
                    else analyze.product_sections(req, evidence, by_id, metrics))  # fmt: skip
        if req.product is not None and req.product.include_ads:
            rivals = [req.product.name, *(c.name for c in req.product.known_competitors)]
            if isinstance(crawled, dict):
                rivals += [r["name"] for r in dossier_competitors(crawled)]
            sections["competitor_ads"] = ad_section(evidence, by_id, pages, rivals,
                                                    found_ads.notes)  # fmt: skip
        # Sections' own "no evidence" gaps repeat the unanswered questions; keep the rest.
        for section in sections.values():
            gaps += [f"{section.title}: {g}" for g in section.gaps
                     if not g.startswith(analyze.NO_EVIDENCE)]  # fmt: skip

        def corroborated(ids: list[str]) -> bool:
            records = [e for e in evidence if e.evidence_id in ids]
            return independent_domains(records, by_id) >= 2 and not any(
                e.contradicted_by for e in records
            )

        sizing = None
        if req.sizing is not None:
            # The research scope applies unless the sizing names its own.
            scope = {k: v for k, v in (("geography", req.geography), ("segment", req.segment))
                     if getattr(req.sizing, k) is None}  # fmt: skip
            sizing = size_market(req.sizing.model_copy(update=scope), metrics, corroborated,
                                 {e.evidence_id for e in evidence})  # fmt: skip
        candidates_to_score = [req.subject, *req.segments]
        scoring = None
        try:
            scoring = score_opportunities(candidates_to_score, evidence, by_id, req.scoring,
                                          req.subject)  # fmt: skip
        except ValueError as exc:
            run.issue("scoring", "error", str(exc))
        summary = analyze.executive_summary(req, sections, sources, evidence,
                                            len(contradictions), gaps)  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - what was collected must not be lost
        log.exception("market.analysis_failed", extra={"research_id": req.research_id})
        run.issue("analysis", "error", f"analysis failed: {type(exc).__name__}: {exc}")
        analysis_failed = True
        contradictions, metrics, gaps, summary = [], [], [], []
        sections, sizing, scoring = {}, None, None

    cancelled = stop is not None and stop.is_set()
    if not evidence and not cancelled:
        tried = ", ".join(f"{n} {k}" for k, n in sorted(Counter(s.status.value for s in sources)
                                                       .items())) or "no sources"  # fmt: skip
        run.issue("extraction", "error", f"no evidence collected ({tried})")
    # A report is not success by itself: unmet research requirements make the run partial.
    questions = run.plan.questions if run.plan else []
    unanswered = [g.removeprefix("unanswered: ") for g in gaps if g.startswith("unanswered: ")]
    if evidence and unanswered:
        run.issue("coverage", "warning", f"{len(unanswered)} of {len(questions)} research "
                  f"question(s) have no evidence: {'; '.join(unanswered)}")  # fmt: skip
    if sizing is not None and not sizing.results:
        run.issue("sizing", "warning", "market sizing requested but not calculated: "
                  + "; ".join(sizing.insufficient))  # fmt: skip
    used = [s for s in sources if s.status in (RetrievalStatus.FETCHED, RetrievalStatus.CACHED)]
    run.stats["coverage"] = {
        "questions": len(questions), "questions_answered": len(questions) - len(unanswered),
        "sources_tried": sum(s.status is not RetrievalStatus.SKIPPED for s in sources),
        "sources_used": len(used), "domains": len({s.domain for s in used}),
        "evidence": len(evidence),
    }  # fmt: skip
    status = RunStatus.COMPLETED
    if cancelled:
        status = RunStatus.CANCELLED
    elif analysis_failed or (not evidence and any(i.severity in ("warning", "error")
                                                  for i in run.issues)):  # fmt: skip
        status = RunStatus.FAILED
    elif any(i.severity in ("warning", "error") for i in run.issues) or any(
        s.status in (RetrievalStatus.SKIPPED, RetrievalStatus.FAILED, RetrievalStatus.UNREADABLE)
        for s in sources
    ):
        status = RunStatus.PARTIAL
    statuses = dict(Counter(s.status.value for s in sources))
    # Why the run is not COMPLETED, in one place (each also an issue or a source status).
    reasons = [f"{i.stage}: {i.message}"[:300] for i in run.issues
               if i.severity in ("warning", "error")]  # fmt: skip
    reasons += [f"{n} source(s) {k}" for k, n in sorted(statuses.items())
                if k in (RetrievalStatus.SKIPPED.value, RetrievalStatus.FAILED.value,
                         RetrievalStatus.UNREADABLE.value)]  # fmt: skip
    run.stats.update(sources=statuses, evidence=len(evidence), status_reasons=reasons,
                     duration_seconds=round(time.monotonic() - started, 1))  # fmt: skip
    limitations = [
        "Evidence is extracted by keyword and number rules (no language model): sentences can be "
        "misfiled or missed, and figures are only as reliable as their sources.",
        "Open-web pages are fetched one at a time from search results; links are not followed, "
        "and paywalled or login-only content is never read.",
        "Source quality, confidence and scores are ordinal judgements by documented rules, not "
        "probabilities.",
    ]  # fmt: skip
    if req.product is not None and req.product.include_ads:
        limitations += [c["limitation"] for c in CAPABILITIES.values()]
    report = Report(
        analysis_version=ANALYSIS_VERSION, research_id=req.research_id, mode=req.mode,
        subject=req.subject, request=req, status=status,
        executive_summary=summary, sections=sections, metrics=metrics,
        contradictions=contradictions, sizing=sizing,
        scoring=scoring, assumptions=list(req.assumptions), gaps=gaps, limitations=limitations,
        issues=run.issues, sources=sources, evidence=evidence, stats=run.stats,
    )  # fmt: skip
    if narrator is not None and evidence and status in (RunStatus.COMPLETED, RunStatus.PARTIAL):
        try:
            await narrator(report)
        except Exception as exc:  # noqa: BLE001 - narrative is optional
            report.issues.append(Issue(stage="narrative", severity="warning",
                                       message=f"narrative skipped: {exc}"[:500]))  # fmt: skip
    if not analysis_failed:  # else "analysis" stays the last stage: the one that failed
        await run.progress("done", status=status.value)
    return report
