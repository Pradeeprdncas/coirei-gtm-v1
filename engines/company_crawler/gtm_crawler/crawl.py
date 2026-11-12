"""Crawl orchestration for one company site.

Lifecycle: robots.txt -> sitemaps -> frontier loop (robots check, per-host
throttle, fetch, raw store, parse, browser fallback, duplicate check, persist,
expand links) -> finish with a status and statistics.

The browser fallback renders a page only when its HTML has too little text
and runs scripts; the rendered page replaces the HTTP one only if it has more
text. Both fetches are recorded.

After the site, a bounded external phase (``ExternalSettings``) fetches single
pages elsewhere, only for what the first-party evidence left open (plan
section 14): the ``gaps`` callback extracts and assesses the site and returns
the fields still UNKNOWN, UNSUPPORTED, STALE or CONFLICTING. Each ``SEARCH_PLAN``
query runs only if one of its fields is such a gap; reference profiles the site
links to are fetched only for profile gaps. Competitor discovery runs every
time, under its own budgets: queries built from the company's names, products
and categories (``search.competitor_queries``), then a second round about the
strongest competitors found. External pages are recorded like any other page;
their links are recorded but never followed.

* ``completed``: the frontier was exhausted.
* ``partial``: a budget (pages, bytes, time, error rate) stopped the crawl first.
* ``paused``: the ``stop`` event was set (pause requested); resumable.
* ``failed``: robots.txt was unreachable (nothing may be fetched) or no page
  could be fetched and parsed.

The frontier is durable (``crawl_url`` rows claimed under leases), and the
rest of the in-memory state is checkpointed, so ``crawl(..., resume=id)``
continues a crawl whose worker stopped or died. Per-URL problems never stop
the crawl; they are recorded, and retryable ones (timeouts, 429, 5xx) are
retried later (``retry_wait``) up to ``max_url_attempts``. A database failure
or cancellation stops it without marking it failed, so it can be resumed;
any other unexpected error marks it failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from .breaker import CircuitBreaker
from .config import ConfigError, Secret, Settings
from .db.store import CrawlStore, ResumeState, Validators
from .domain import FINAL_URL_STATES, OPEN_URL_STATES, PolicyDecision, UrlState
from .extract.company import company_names
from .extract.product import slugify
from .fetch.browser import BrowserFetcher, browser_trigger
from .fetch.http import (
    RETRYABLE_ERRORS,
    FetchError,
    FetchResult,
    HttpFetcher,
    retry_after_seconds,
    retry_class,
)
from .frontier import Frontier, FrontierEntry, Rejection
from .identity import external_id
from .log import log_context
from .metrics import (
    BLOCKED_REQUESTS,
    BROWSER_FALLBACK,
    CRAWL_DURATION,
    FRONTIER_DEPTH,
    HTTP_RETRIES,
    PAGES_PARSED,
    URLS_DISCOVERED,
    URLS_FETCHED,
)
from .parse import ParsedPage, parse_page
from .parse.fingerprint import hamming_distance
from .parse.structured import structured_urls
from .policy import Decision, PolicyEngine
from .priority import Prioritizer
from .rawstore import RawStoreError, make_rawstore
from .robots import RobotsCache, RobotsStatus
from .search import (
    FailoverSearch,
    SearchError,
    competitor_queries,
    make_search,
    rank_candidates,
    second_hop_queries,
)
from .sitemaps import SitemapFetchRecord, discover_sitemaps
from .throttle import HostThrottle, Outcome
from .tracing import span
from .urls import InvalidUrl, normalize_url, registrable_domain, url_host
from .versions import current_versions

log = logging.getLogger(__name__)

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# The seed is fetched first: it is the root of link discovery.
SEED_SCORE = 1_000.0

# Facts a company profile (LinkedIn, Crunchbase, ...) typically states.
PROFILE_FIELDS = frozenset(
    {"legal_name", "headquarters", "company_location", "company_country", "founded_year",
     "employee_count", "company_size", "ownership_type", "funding_round", "total_funding",
     "investor", "founder"}
)  # fmt: skip
# (query, restricted to reference domains, fields it can fill). A query runs
# only if one of its fields is a gap after the first-party pass.
# Competitor queries are not here: they run every time (search.COMPETITOR_TEMPLATES).
SEARCH_PLAN: tuple[tuple[str, bool, frozenset[str]], ...] = (
    ('"{name}" funding', False,
     frozenset({"funding_round", "total_funding", "valuation", "investor"})),
    ('"{name}" revenue customers', False,
     frozenset({"arr", "mrr", "annual_revenue", "customer_count", "customer_concentration"})),
    ('"{name}"', True, PROFILE_FIELDS),
)  # fmt: skip


@dataclass
class CrawlSummary:
    crawl_id: int
    organization_id: int
    status: str
    stats: dict[str, Any]


FINISHED = frozenset({"completed", "partial", "failed"})


@dataclass
class _Stats:
    fetches: int = 0
    not_modified: int = 0
    bytes: int = 0
    retries_scheduled: int = 0
    blocked: int = 0
    fetch_errors: Counter[str] = field(default_factory=Counter)
    pages_parsed: int = 0
    duplicates: int = 0
    robots_disallowed: int = 0
    # Non-ALLOW policy decisions, "decision:reason" -> count.
    policy: Counter[str] = field(default_factory=Counter)
    redirect_off_site: int = 0
    external_links: int = 0
    reference_links: int = 0
    renders: int = 0
    renders_used: int = 0
    render_errors: Counter[str] = field(default_factory=Counter)
    renders_over_budget: int = 0
    # Render cost (spec section 11): wall time, and what the pages requested.
    render_ms: int = 0
    render_requests: int = 0
    render_bytes: int = 0
    render_refused: int = 0
    # URLs moved to the dead-letter queue (Phase 13).
    dead_lettered: int = 0
    # URLs handed back for later because their host's circuit was open.
    circuit_deferrals: int = 0
    # Queued URLs dropped at claim time: their shape stopped yielding new content.
    low_yield_skipped: int = 0
    # Queued URLs dropped at claim time: under a section found to be a translated copy.
    other_language_skipped: int = 0


async def crawl(
    seed_url: str,
    settings: Settings,
    engine: Engine,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    search_transport: httpx.AsyncBaseTransport | None = None,
    gaps: Callable[[int, int], set[str]] | None = None,
    known: Callable[[int, int], dict[str, list[str]]] | None = None,
    job_id: int | None = None,
    resume: int | None = None,
    stop: asyncio.Event | None = None,
    owner: str | None = None,
    progress: Callable[[int, int], Any] | None = None,
) -> CrawlSummary:
    """Crawl the site of ``seed_url`` and persist everything fetched.

    ``resume`` continues that crawl instead of starting a new one (a finished
    crawl is returned as it is). ``stop`` pauses the crawl when set. ``owner``
    names the lease holder of claimed URLs (default: a fresh id).
    ``progress(crawl_id, organization_id)`` runs (in a thread, one at a time) every
    ``crawl.progress_every_pages`` parsed pages while the site is crawled, so what was
    found so far can be assessed.

    ``gaps(crawl_id, organization_id)`` runs (in a thread) after the site and
    before the external phase; it returns the fields the site left open. Without
    it every field counts as open. ``known(crawl_id, organization_id)`` returns
    the values found so far for the fields that seed competitor queries
    (product names, categories, competitors); without it only the company's
    names are used and there is no second round.

    Raises ``ConfigError`` (e.g. missing user-agent contact) or ``InvalidUrl``
    before anything is written.
    """
    seed = normalize_url(seed_url, settings.urls)
    async with HttpFetcher(
        settings.fetch, settings.identity, settings.urls, transport=transport
    ) as f:
        search = (
            make_search(settings.search, search_transport) if settings.external.enabled else None
        )
        run = _Crawl(seed, settings, CrawlStore(engine), f, clock, search, gaps)
        run.known = known
        run.progress = progress
        run.job_id, run.resume_id, run.stop = job_id, resume, stop
        run.owner = owner or f"crawl-{uuid.uuid4().hex[:12]}"
        try:
            with log_context(**({"job_id": job_id} if job_id else {})):
                return await run.run()
        finally:
            if run.browser is not None:
                await run.browser.aclose()
            if search is not None:
                await search.aclose()


def config_snapshot(settings: Settings) -> dict[str, Any]:
    """Settings recorded with the crawl, without credentials or deployment details."""
    snapshot = dataclasses.asdict(settings)
    for section in ("database", "worker", "api", "market"):
        snapshot.pop(section)
    for values in snapshot.values():  # any other credential, wherever it is
        for key in [k for k, v in values.items() if isinstance(v, Secret)]:
            values.pop(key)
    return snapshot


# Settings whose change would make stored pages, locators or admission
# decisions inconsistent with what a resumed crawl would now produce.
_RESUME_SENSITIVE = ("urls", "parse", "frontier")


def _check_resumable(stored: dict[str, Any], current: dict[str, Any]) -> None:
    current = json.loads(json.dumps(current))  # as stored: tuples become lists
    changed = [s for s in _RESUME_SENSITIVE if stored.get(s) != current.get(s)]
    if changed:
        raise ConfigError(
            f"cannot resume: settings changed since the crawl started ({', '.join(changed)})"
        )


def _outcome(result: FetchResult) -> Outcome | None:
    """How a response reflects on its server, for rate control and the circuit
    breaker; None if it says nothing about the server (refused by our guard)."""
    if result.error is FetchError.BLOCKED:
        return None
    if result.error is None and result.status in (429, 503):
        return "overloaded"
    if result.error in RETRYABLE_ERRORS or (result.error is None and (result.status or 0) >= 500):
        return "failed"
    return "ok"


def _listed(domain: str, domains: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


async def fetch_until(
    urls: list[str], limit: int, concurrency: int, fetch: Callable[[str], Awaitable[int]]
) -> int:
    """Fetch ``urls`` in order, ``concurrency`` at once, until ``limit`` of them were
    stored (``fetch`` returns 1, or 0 for a page refused or failed); never more at once
    than could still count. Returns how many were stored."""
    got, i = 0, 0
    while got < limit and i < len(urls):
        batch = urls[i : i + min(max(1, concurrency), limit - got)]
        i += len(batch)
        got += sum(await asyncio.gather(*(fetch(u) for u in batch)))
    return got


class _Crawl:
    def __init__(
        self,
        seed: str,
        settings: Settings,
        store: CrawlStore,
        fetcher: HttpFetcher,
        clock: Callable[[], float],
        search: FailoverSearch | None = None,
        gaps: Callable[[int, int], set[str]] | None = None,
    ) -> None:
        self.seed = seed
        self.site = registrable_domain(url_host(seed))
        self.settings = settings
        self.store = store
        self.fetcher = fetcher
        self.clock = clock
        self.rawstore = make_rawstore(settings.rawstore)
        self.robots = RobotsCache(
            fetcher, settings.robots, settings.identity.product_token, self._record_robots
        )
        self.policy = PolicyEngine(self.robots, self._in_site)
        self.frontier = Frontier(self.site, settings.frontier)
        self.prioritizer = Prioritizer(settings.priority)
        c = settings.crawl
        self.throttle = HostThrottle(
            c.per_host_concurrency, c.min_delay_seconds, slow_seconds=c.slow_response_seconds,
            recover_after=c.rate_recover_after, max_extra_delay=c.max_extra_delay_seconds,
        )  # fmt: skip
        self.breaker = CircuitBreaker(
            "host", c.circuit_failure_threshold, c.circuit_cooldown_seconds,
            c.circuit_max_cooldown_seconds, clock,
        )  # fmt: skip
        self.browser = (
            BrowserFetcher(settings.browser, settings.urls, fetcher, self.robots, self.throttle)
            if settings.browser.enabled
            else None
        )
        self.stats = _Stats()
        self.inlinks: Counter[str] = Counter()
        # URLs whose refusal is stored (bounded by frontier.max_recorded_rejections).
        self.recorded_rejections: set[str] = set()
        # Fingerprint -> page id of the first page with that content. A future
        # while that page is still being written, so concurrent duplicates
        # resolve to the same original.
        self.exact: dict[str, asyncio.Future[int]] = {}
        self.near: list[tuple[int, asyncio.Future[int]]] = []
        self.reference_domains = settings.crawl.reference_domains
        self.search = search
        self.gaps = gaps
        self.known: Callable[[int, int], dict[str, list[str]]] | None = None
        self.progress: Callable[[int, int], Any] | None = None
        self.organization_id = 0
        self.progress_task: asyncio.Task[Any] | None = None
        self.progress_pages = 0
        # Names the site uses for itself, and reference profiles it links to.
        self.names: set[str] = set()
        self.reference_urls: dict[str, None] = {}
        self.extra: dict[str, Any] = {}
        self.crawl_id = 0
        self.started = 0.0
        self.job_id: int | None = None
        self.resume_id: int | None = None
        self.stop: asyncio.Event | None = None
        self.owner = ""
        self.last_checkpoint = 0.0

    async def run(self) -> CrawlSummary:
        elapsed = 0.0
        if self.resume_id is None:
            org_id, self.crawl_id = await asyncio.to_thread(
                self.store.start_crawl, self.seed, config_snapshot(self.settings), self.job_id,
                current_versions(self.settings),
            )  # fmt: skip
        else:
            row = await asyncio.to_thread(self.store.crawl_row, self.resume_id)
            org_id, self.crawl_id = row.organization_id, row.id
            if row.status in FINISHED:
                return CrawlSummary(row.id, org_id, row.status, row.stats)
            _check_resumable(row.config, config_snapshot(self.settings))
            # URLs a dead predecessor held come back, counting a lost lease each.
            await asyncio.to_thread(self.store.recover, self.crawl_id,
                                    self.settings.crawl.max_url_lease_expiries)  # fmt: skip
            elapsed = self._restore(await asyncio.to_thread(self.store.resume_state, row.id))
        self.started = self.clock() - elapsed
        self.last_checkpoint = self.started
        resumed = self.resume_id is not None
        with span("crawl.site", crawl_id=self.crawl_id, domain=self.site, resumed=resumed) as sp:
            summary = await self._run(org_id)
            sp["status"] = summary.status
            return summary

    async def _run(self, org_id: int) -> CrawlSummary:
        self.organization_id = org_id
        event = "crawl.resumed" if self.resume_id else "crawl.started"
        log.info(event, extra={"seed": self.seed})
        stop_reason: str | None
        try:
            policy = await self.robots.policy_for(self.seed)
            self.extra["robots_status"] = str(policy.status)
            if policy.status is RobotsStatus.UNREACHABLE:
                status, stop_reason = "failed", "robots_unreachable"
            else:
                if self.resume_id is None:
                    await self._enqueue(
                        [
                            FrontierEntry(
                                self.seed, 0, SEED_SCORE, "seed", signals={"seed": SEED_SCORE}
                            )
                        ]
                    )
                    self.extra["sitemaps"] = await self._seed_from_sitemaps()
                stop_reason = await self._loop()
                if stop_reason == "paused":
                    status = "paused"
                elif self.stats.pages_parsed == 0:
                    status = "failed"
                    stop_reason = stop_reason or "no_pages"
                else:
                    status = "partial" if stop_reason else "completed"
                    if self.settings.external.enabled:
                        self.extra["external"] = await self._external(org_id)
        except (asyncio.CancelledError, KeyboardInterrupt, DBAPIError):
            # Resumable: leave the crawl running for whoever resumes it.
            await self._suspend()
            raise
        except BaseException:
            await asyncio.to_thread(
                self.store.finish_crawl, self.crawl_id, "failed", self._stats(None, self.extra)
            )
            log.exception("crawl.aborted", extra={"crawl_id": self.crawl_id})
            raise

        stats = self._stats(stop_reason, self.extra)
        CRAWL_DURATION.observe(self.clock() - self.started)
        if status == "paused":
            await self._suspend()
        await asyncio.to_thread(self.store.finish_crawl, self.crawl_id, status, stats)
        log.info("crawl.completed", extra={"crawl_id": self.crawl_id, "status": status, **stats})
        return CrawlSummary(self.crawl_id, org_id, status, stats)

    def _restore(self, state: ResumeState) -> float:
        """Rebuild in-memory state from the database. Returns time already spent."""
        for url, url_state in state.urls:
            if url_state in (UrlState.TRAP, UrlState.DEFERRED):
                self.recorded_rejections.add(url)
                if url_state == UrlState.TRAP:
                    self.frontier.mark_seen(url)  # a trap stays refused
            else:
                self.frontier.restore(url, queued=url_state in OPEN_URL_STATES)
        loop = asyncio.get_running_loop()
        for page_id, digest, sim in state.pages:
            done: asyncio.Future[int] = loop.create_future()
            done.set_result(page_id)
            if digest is not None:
                self.exact.setdefault(digest, done)
            if sim is not None:
                self.near.append((sim, done))
        self.inlinks.update(state.inlinks)
        self.reference_urls.update(dict.fromkeys(state.reference_urls))
        cp = state.checkpoint
        stats = cp.get("stats", {})
        for name, value in stats.items():
            if isinstance(getattr(self.stats, name, None), Counter):
                getattr(self.stats, name).update(value)
            elif hasattr(self.stats, name):
                setattr(self.stats, name, value)
        self.names.update(cp.get("names", []))
        self.extra.update(cp.get("extra", {}))
        languages = self.extra.get("languages", {})
        self.frontier.main_language = languages.get("main")
        self.frontier.translated.update(languages.get("translated", []))
        return float(cp.get("elapsed", 0.0))

    def _checkpoint(self) -> dict[str, Any]:
        # Not dataclasses.asdict: it rebuilds a Counter from (key, count) pairs.
        stats = {f.name: getattr(self.stats, f.name) for f in dataclasses.fields(self.stats)}
        stats = {k: dict(v) if isinstance(v, Counter) else v for k, v in stats.items()}
        return {"stats": stats, "names": sorted(self.names), "extra": self.extra,
                "elapsed": self.clock() - self.started}  # fmt: skip

    async def _save_checkpoint(self) -> None:
        self.last_checkpoint = self.clock()
        await asyncio.to_thread(self.store.save_checkpoint, self.crawl_id, self._checkpoint())

    async def _suspend(self) -> None:
        """Save state and hand leased URLs back, best effort (the database may be down)."""
        try:
            await asyncio.to_thread(self.store.save_checkpoint, self.crawl_id, self._checkpoint())
            await asyncio.to_thread(self.store.release, self.crawl_id)
        except Exception:  # noqa: BLE001 - leases expire on their own if this fails
            log.warning("crawl.suspend_failed", extra={"crawl_id": self.crawl_id}, exc_info=True)

    async def _seed_from_sitemaps(self) -> dict[str, Any]:
        found = await discover_sitemaps(
            self.seed, self.fetcher, self.robots, self.settings.sitemaps, self.settings.urls,
            throttle=self.throttle, on_fetch=self._record_sitemap,
        )  # fmt: skip
        entries = []
        for u in found.urls:
            signals = self.prioritizer.signals(
                u.url, 1, sitemap_priority=u.priority, in_sitemap=True, lastmod=u.lastmod
            )
            entries.append(FrontierEntry(u.url, 1, sum(signals.values()), "sitemap",
                                         signals=signals))  # fmt: skip
        await self._enqueue(entries)
        return {
            "fetched": len(found.fetched),
            "failed": len(found.failed),
            "urls": len(found.urls),
            "dropped": found.dropped,
        }

    async def _loop(self) -> str | None:
        s = self.settings.crawl
        in_flight: set[asyncio.Task[None]] = set()
        stop_reason: str | None = None
        try:
            while True:
                if stop_reason is None:
                    stop_reason = self._budget_exhausted(len(in_flight))
                if stop_reason is None and len(in_flight) < s.concurrency:
                    room = min(s.concurrency - len(in_flight),
                               s.max_pages - self.stats.fetches - len(in_flight))  # fmt: skip
                    claimed = await asyncio.to_thread(
                        self.store.claim, self.crawl_id, self.owner, s.lease_seconds, room,
                        s.max_url_lease_expiries,
                    )  # fmt: skip
                    for entry, attempt in claimed:
                        self.frontier.claimed(entry.url)
                        in_flight.add(asyncio.create_task(self._process(entry, attempt)))
                    FRONTIER_DEPTH.set(len(self.frontier))
                if not in_flight:
                    if stop_reason:
                        return stop_reason
                    wait = await asyncio.to_thread(self.store.seconds_to_next_retry, self.crawl_id)
                    if wait is None:
                        return None
                    left = s.max_duration_seconds - (self.clock() - self.started)
                    if wait > left:
                        return "max_duration"
                    await asyncio.sleep(wait)
                    continue
                done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # re-raise failures (e.g. database errors)
                self._maybe_progress()
                if self.clock() - self.last_checkpoint >= s.checkpoint_seconds:
                    await self._save_checkpoint()
        finally:
            for task in in_flight:
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            if self.progress_task is not None:
                await asyncio.gather(self.progress_task, return_exceptions=True)

    def _maybe_progress(self) -> None:
        """Start the progress callback when enough new pages were parsed and the last
        one is done. It only reads what is stored: a failure is logged, never fatal."""
        every = self.settings.crawl.progress_every_pages
        if (self.progress is None or every <= 0
                or self.stats.pages_parsed - self.progress_pages < every
                or (self.progress_task is not None and not self.progress_task.done())):  # fmt: skip
            return
        self.progress_pages = self.stats.pages_parsed

        async def call(progress: Callable[[int, int], Any] = self.progress) -> None:
            try:
                await asyncio.to_thread(progress, self.crawl_id, self.organization_id)
            except Exception:
                log.exception("crawl.progress_failed", extra={"crawl_id": self.crawl_id})

        self.progress_task = asyncio.create_task(call())

    def _budget_exhausted(self, in_flight: int) -> str | None:
        s = self.settings.crawl
        if self.stop is not None and self.stop.is_set():
            return "paused"
        if self.stats.fetches + in_flight >= s.max_pages:
            return "max_pages"
        if self.stats.bytes >= s.max_bytes:
            return "max_bytes"
        if self.clock() - self.started >= s.max_duration_seconds:
            return "max_duration"
        errors = sum(self.stats.fetch_errors.values())
        if (
            self.stats.fetches >= s.error_rate_min_fetches
            and errors / self.stats.fetches > s.max_error_rate
        ):
            return "error_rate"
        return None

    async def _process(self, entry: FrontierEntry, attempt: int = 1) -> None:
        with span("url.process", url=entry.url, domain=url_host(entry.url), attempt=attempt):
            try:
                await self._process_url(entry, attempt)
            except (asyncio.CancelledError, DBAPIError):
                raise  # the crawl stops (resumable); not this URL's fault
            except Exception as exc:  # noqa: BLE001 - one poison URL must not stop the crawl
                log.exception("url.dead_lettered", extra={"url": entry.url})
                self.stats.dead_lettered += 1
                await asyncio.to_thread(
                    self.store.dead_letter_url, self.crawl_id, entry.url, type(exc).__name__,
                    f"{type(exc).__name__}: {exc}", attempt, {"stage": "process"},
                )  # fmt: skip

    async def _process_url(self, entry: FrontierEntry, attempt: int) -> None:
        url = entry.url
        host = url_host(url)
        if entry.depth > 0 and self.frontier.low_yield(url):
            # Queued before its shape stopped yielding new content.
            self.stats.low_yield_skipped += 1
            await asyncio.to_thread(self.store.set_state, self.crawl_id, url, UrlState.TRAP,
                                    str(Rejection.LOW_YIELD))  # fmt: skip
            return
        if entry.discovered_via != "seed" and self.frontier.other_language(url):
            # Queued (often from a sitemap) before its section was seen to be a translation.
            self.stats.other_language_skipped += 1
            await asyncio.to_thread(self.store.set_state, self.crawl_id, url, UrlState.DEFERRED,
                                    str(Rejection.OTHER_LANGUAGE))  # fmt: skip
            return
        decision = await self.policy.check(url)
        if not decision.allowed:
            await self._refused(url, decision)
            return
        wait = self.breaker.check(host)
        if wait:
            self.stats.circuit_deferrals += 1
            self.stats.policy[f"{PolicyDecision.DEFER}:circuit_open"] += 1
            await asyncio.to_thread(self.store.postpone, self.crawl_id, url, "circuit_open", wait)
            return

        await asyncio.to_thread(self.store.advance, self.crawl_id, url, UrlState.FETCHING)
        prior = None
        if self.settings.fetch.conditional_requests:
            prior = await asyncio.to_thread(self.store.validators, url)
            if prior and not await asyncio.to_thread(self.rawstore.exists, prior.raw_sha256):
                prior = None  # the stored copy was pruned: fetch it whole
        redirects: dict[str, Decision] = {}

        async def follow(target: str) -> bool:
            # Every redirect hop is a new fetch: it gets its own policy decision.
            redirects[target] = await self.policy.check(target)
            return redirects[target].allowed

        async with self.throttle.slot(host, decision.crawl_delay):
            with span("fetch.http") as sp:
                result = await self.fetcher.fetch(
                    url, HTML_TYPES, follow=follow, validators=prior.validators if prior else None
                )
                sp.update(http_status=result.status, fetch_error=result.error)
        outcome = _outcome(result)
        if outcome is not None:
            self.throttle.observe(host, outcome, (result.duration_ms or 0) / 1000)
            self.breaker.record(host, outcome == "ok")
        if prior is not None and result.status == 304 and result.error is None:
            await self._reuse(result, prior)
        self.stats.fetches += 1
        self.stats.bytes += len(result.body or b"")
        URLS_FETCHED.inc()
        if not result.ok:
            self.stats.fetch_errors[str(result.error or f"http_{result.status}")] += 1

        raw_sha = None
        if result.ok and result.body is not None:
            raw_sha = await asyncio.to_thread(self.rawstore.put, result.body)
        fetch_id = await asyncio.to_thread(
            self.store.record_fetch, self.crawl_id, result, "http", raw_sha
        )

        final = result.final_url
        if final != url:
            self.frontier.mark_seen(final)
            refused = redirects.get(final)
            if refused is not None and not refused.allowed:
                # The redirect was not followed: the target was refused.
                if refused.reason == "out_of_scope":
                    self.stats.redirect_off_site += 1
                reason = ("redirect_off_site" if refused.reason == "out_of_scope"
                          else f"redirect_{refused.reason}")  # fmt: skip
                await self._refused(url, Decision(refused.decision, reason,
                                                  {**refused.detail, "target": final}))  # fmt: skip
                return
        if not result.ok or result.body is None:
            await self._failed(url, result, attempt)
            return
        await asyncio.to_thread(self.store.advance, self.crawl_id, url, UrlState.FETCHED)

        with span("parse"):
            parsed = await self._parse(result.body, final, result.headers.get("content-type"))
        if parsed is None:
            await asyncio.to_thread(
                self.store.set_state, self.crawl_id, url, UrlState.FAILED, "parse_timeout"
            )
            return
        page = parsed
        trigger = browser_trigger(page, result.body, self.settings.browser)
        if self.browser is not None and trigger is not None:
            rendered = await self._render(self.browser, result, page, trigger)
            if rendered is not None:
                fetch_id, page = rendered
        self.names |= company_names(page, self.site)
        self.frontier.learn_language(final, page.language, seed=entry.discovered_via == "seed")
        self.extra["languages"] = {"main": self.frontier.main_language,
                                   "translated": sorted(self.frontier.translated)}  # fmt: skip
        with span("store"):
            await self._store_page(entry, result, fetch_id, page)

    async def _reuse(self, result: FetchResult, prior: Validators) -> None:
        """A 304: use the body stored from the earlier fetch, if it is still intact."""
        try:
            body = await asyncio.to_thread(self.rawstore.get, prior.raw_sha256)
        except RawStoreError as exc:  # pruned or damaged: treat as a failed fetch
            result.error, result.error_detail = FetchError.DECODE, f"stored copy: {exc}"
            return
        result.body = body
        result.not_modified = True
        result.content_type = result.content_type or prior.content_type
        result.headers.setdefault("content-type", prior.content_type or "text/html")
        self.stats.not_modified += 1

    async def _failed(self, url: str, result: FetchResult, attempt: int) -> None:
        """Record a failed fetch: blocked, retried later, or failed for good."""
        reason = str(result.error or f"http_{result.status}")
        if result.error is FetchError.BLOCKED:
            self.stats.blocked += 1
            await asyncio.to_thread(
                self.store.set_state, self.crawl_id, url, UrlState.BLOCKED, reason
            )
            return
        kind = retry_class(result)
        if kind is None:
            if result.status is not None:
                self.frontier.record_yield(url, useful=False)  # 404s and the like
            await asyncio.to_thread(
                self.store.set_state, self.crawl_id, url, UrlState.FAILED, reason
            )
            return
        c = self.settings.crawl
        if attempt >= c.max_url_attempts:
            # Retryable yet failing every time: dead-letter it for a later requeue.
            self.stats.dead_lettered += 1
            await asyncio.to_thread(
                self.store.dead_letter_url, self.crawl_id, url, reason,
                f"{reason}: {result.error_detail or ''}".strip(": "), attempt,
                {"retry_class": kind, "status": result.status},
            )  # fmt: skip
            return
        retry_after = retry_after_seconds(result.headers.get("retry-after"))
        if kind == "rate_limited":
            wait = retry_after if retry_after is not None else c.retry_delay_rate_limited_seconds
        elif kind == "dns":
            wait = c.retry_delay_dns_seconds
        else:
            wait = retry_after or 0.0
        delay = max(c.retry_delay_seconds, wait)
        if kind == "rate_limited":
            # The host asked us to slow down: every request to it waits, not just this URL.
            self.throttle.backoff(url_host(url), delay)
        self.stats.retries_scheduled += 1
        HTTP_RETRIES.labels("scheduled").inc()
        await asyncio.to_thread(
            self.store.set_state, self.crawl_id, url, UrlState.RETRY, reason, delay
        )

    async def _render(
        self, browser: BrowserFetcher, result: FetchResult, page: ParsedPage, trigger: str
    ) -> tuple[int, ParsedPage] | None:
        """Render ``result`` in the browser; the rendered page if it is more useful."""
        kind = trigger.split(":", 1)[0]
        if self.stats.renders >= self.settings.browser.max_renders:
            self.stats.renders_over_budget += 1
            BROWSER_FALLBACK.labels(kind, "over_budget").inc()
            return None
        self.stats.renders += 1
        started = time.perf_counter()
        with span("fetch.browser", trigger=trigger) as sp:
            rendered = await browser.render(result)
            sp.update(http_status=rendered.status, fetch_error=rendered.error)
        rendered.duration_ms = round((time.perf_counter() - started) * 1000)
        self.stats.render_ms += rendered.duration_ms
        cost = rendered.render or {}
        self.stats.render_requests += cost.get("requests", 0)
        self.stats.render_bytes += cost.get("bytes", 0)
        self.stats.render_refused += cost.get("refused", 0)
        raw_sha = None
        if rendered.ok and rendered.body is not None:
            raw_sha = await asyncio.to_thread(self.rawstore.put, rendered.body)
        fetch_id = await asyncio.to_thread(
            self.store.record_fetch, self.crawl_id, rendered, "browser", raw_sha, trigger
        )
        if not rendered.ok or rendered.body is None:
            self.stats.render_errors[str(rendered.error or f"http_{rendered.status}")] += 1
            BROWSER_FALLBACK.labels(kind, "error").inc()
            return None
        BROWSER_FALLBACK.labels(kind, "rendered").inc()
        final = rendered.final_url
        if not self._in_site(final):
            return None  # script navigated off site; its content is not this company's
        with span("parse", rendered=True):
            better = await self._parse(rendered.body, final, rendered.headers["content-type"])
        if better is None:
            return None
        if len(better.cleaned_text) <= len(page.cleaned_text):
            return None
        self.frontier.mark_seen(final)
        self.stats.renders_used += 1
        return fetch_id, better

    async def _parse(self, body: bytes, url: str, content_type: str | None) -> ParsedPage | None:
        """The parsed page, or None if parsing took longer than ``parse.timeout_seconds``."""
        # ponytail: the crawl moves on but the thread runs to its end (bounded by
        # fetch.max_body_bytes and the parse caps); parse in a killable process if
        # abandoned parses ever occupy the thread pool.
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    parse_page, body, url, content_type, self.settings.parse, self.settings.urls
                ),
                self.settings.parse.timeout_seconds,
            )
        except TimeoutError:
            log.warning("parse.timeout", extra={"url": url, "bytes": len(body)})
            return None

    async def _store_page(
        self, entry: FrontierEntry, result: FetchResult, fetch_id: int, page: ParsedPage
    ) -> None:
        # Claim the fingerprint before any await so concurrent twins see each other.
        original: asyncio.Future[int] | None = None
        mine: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        text = page.cleaned_text
        if text:
            digest, sim = page.text_sha256, page.simhash
            original = self.exact.get(digest) or self._near_duplicate(sim)
            if original is None:
                self.exact[digest] = mine
                self.near.append((sim, mine))

        duplicate_of = await original if original is not None else None
        try:
            page_id = await asyncio.to_thread(self.store.record_page, fetch_id, page, duplicate_of)
        except BaseException as exc:
            # Later twins must not inherit this failure: they store themselves.
            if self.exact.get(page.text_sha256) is mine:
                del self.exact[page.text_sha256]
            self.near = [(sim, f) for sim, f in self.near if f is not mine]
            if isinstance(exc, asyncio.CancelledError):
                mine.cancel()
            elif not mine.done():
                mine.set_exception(exc)  # twins already waiting get it
                mine.exception()  # observed here (raised below): no "never retrieved"
            raise
        mine.set_result(page_id)
        self.stats.pages_parsed += 1
        PAGES_PARSED.inc()
        await asyncio.to_thread(self.store.advance, self.crawl_id, entry.url, UrlState.PARSED)
        self.frontier.record_yield(entry.url, useful=duplicate_of is None and bool(text))
        if duplicate_of is not None:
            await asyncio.to_thread(self.store.set_state, self.crawl_id, entry.url,
                                    UrlState.PROCESSED, "duplicate")  # fmt: skip
            self.stats.duplicates += 1
            log.info(
                "page.duplicate", extra={"url": result.final_url, "duplicate_of": duplicate_of}
            )
            return
        await self._expand(entry, page, page_id)
        await asyncio.to_thread(self.store.set_state, self.crawl_id, entry.url, UrlState.PROCESSED)

    def _in_site(self, url: str) -> bool:
        return registrable_domain(url_host(url)) == self.site

    def _near_duplicate(self, sim: int) -> asyncio.Future[int] | None:
        # ponytail: linear scan, fine for thousands of pages per crawl; use
        # banded LSH buckets if crawls grow to hundreds of thousands of pages.
        limit = self.settings.crawl.near_duplicate_bits
        for other, future in self.near:
            if hamming_distance(sim, other) <= limit:
                return future
        return None

    async def _expand(self, entry: FrontierEntry, page: ParsedPage, page_id: int) -> None:
        depth = entry.depth + 1
        rows: list[tuple[str, str, str | None, bool, bool]] = []
        candidates: list[FrontierEntry] = []
        boosts: dict[str, float] = {}
        # What the linking page is about: its links inherit that relevance.
        topic = " ".join([page.title or "", *page.meta.get("description", [])[:1]])
        for link in page.links:
            domain = registrable_domain(url_host(link.url))
            external = domain != self.site
            reference = external and _listed(domain, self.reference_domains)
            rows.append((link.url, link.text, link.rel, external, reference))
            if external:
                self.stats.external_links += 1
                self.stats.reference_links += reference
                if reference:
                    self.reference_urls[link.url] = None
                continue
            self.inlinks[link.url] += 1
            boost = self.prioritizer.inlink_boost(self.inlinks[link.url])
            if self.frontier.is_queued(link.url):
                boosts[link.url] = boosts.get(link.url, 0.0) + boost
                continue
            signals = self.prioritizer.signals(
                link.url, depth, link.text, link.context, parent_topic=topic
            )
            signals["inlinks"] = round(boost, 6)
            candidates.append(
                FrontierEntry(link.url, depth, sum(signals.values()), "link", page.url,
                              link.text[:500] or None, signals)
            )  # fmt: skip
        canonical = page.canonical_url
        if canonical and canonical != page.url and self._in_site(canonical):
            signals = self.prioritizer.signals(canonical, entry.depth)
            candidates.append(FrontierEntry(canonical, entry.depth, sum(signals.values()),
                                            "canonical", page.url, signals=signals))  # fmt: skip
        for raw in structured_urls(page.json_ld_nodes, self.settings.parse.max_links):
            # JSON-LD url / @id / mainEntityOfPage / sameAs (spec section 6).
            try:
                url = normalize_url(raw, self.settings.urls, base=page.url)
            except InvalidUrl:
                continue
            domain = registrable_domain(url_host(url))
            external = domain != self.site
            reference = external and _listed(domain, self.reference_domains)
            rows.append((url, "", "structured-data", external, reference))
            if reference:
                self.reference_urls[url] = None
            if not external and not self.frontier.is_queued(url):
                signals = self.prioritizer.signals(url, depth, parent_topic=topic)
                candidates.append(
                    FrontierEntry(url, depth, sum(signals.values()), "structured_data",
                                  page.url, signals=signals)
                )  # fmt: skip

        await asyncio.to_thread(self.store.record_links, page_id, rows)
        await asyncio.to_thread(self.store.boost, self.crawl_id, boosts)
        await self._enqueue(candidates)

    async def _external(self, org_id: int) -> dict[str, Any]:
        """Fetch the bounded set of external pages the policy allows, for open fields only."""
        ext = self.settings.external
        subject = min(self.names, key=len) if self.names else self.site
        stats: dict[str, Any] = {"subject": subject, "gaps": None, "queries": {},
                                 "search_error": None, "queries_over_budget": 0,
                                 "providers": {}, "reference_pages": 0,
                                 "search_pages": 0}  # fmt: skip
        missing = await asyncio.to_thread(self.gaps, self.crawl_id, org_id) if self.gaps else None
        stats["gaps"] = sorted(missing) if missing is not None else "all"

        def needed(fields: frozenset[str]) -> bool:
            return missing is None or not missing.isdisjoint(fields)

        references = list(self.reference_urls) if needed(PROFILE_FIELDS) else []
        hits: list[tuple[str, str, int]] = []
        plan = [(q, q.format(name=subject), self.reference_domains if profiles else ())
                for q, profiles, fields in SEARCH_PLAN if needed(fields)]  # fmt: skip
        if self.search is None:
            stats["search_error"] = "disabled"
        elif plan:
            slug = slugify(subject)
            budget = self.settings.search.max_queries_per_crawl
            sent = plan[:budget]
            results = await self._search_all(
                [(template, query, domains, ()) for template, query, domains in sent], stats
            )
            for template, query, domains in plan[budget:]:
                # Query budget (spec section 14): recorded, not sent.
                stats["queries_over_budget"] += 1
                await self._over_budget(template, query, domains)
            for (_, query, domains), found in zip(sent, results, strict=True):
                for url, position in found:
                    if domains:
                        # A profile counts only if its URL names this exact company.
                        if slug and slug in url.lower():
                            references.append(url)
                    else:
                        hits.append((url, query, position))

        done: set[str] = set()
        refs = list(dict.fromkeys(references))[: ext.max_reference_pages]
        done.update(refs)
        stats["reference_pages"] += await fetch_until(
            refs, len(refs), ext.concurrency, lambda url: self._fetch_external(url, "link", None)
        )
        stats["search_pages"] += await self._fetch_results(
            hits, ext.max_search_pages - stats["search_pages"], done
        )
        if self.search is not None:
            await self._competitor_search(org_id, subject, stats, done)
        return stats

    async def _competitor_search(
        self, org_id: int, subject: str, stats: dict[str, Any], done: set[str]
    ) -> None:
        """Competitor discovery: always, since competitors are an open-ended list.

        Round one asks about the company, its products and categories; round two
        about the strongest competitors known after extracting round one's pages.
        """
        ext = self.settings.external
        c: dict[str, Any] = {"queries": 0, "queries_over_budget": 0, "search_pages": 0,
                             "second_hop": []}  # fmt: skip
        stats["competitors"] = c
        known = await asyncio.to_thread(self.known, self.crawl_id, org_id) if self.known else {}
        queries = competitor_queries(
            [subject, *sorted(self.names - {subject})],
            known.get("product_name", ()),
            [*known.get("product_category", ()), *known.get("market_domain", ())],
        )
        await self._competitor_round(queries, stats, done)
        if not (ext.second_hop_competitors and self.known):
            return
        if c["search_pages"] and self.gaps:
            # Extract and assess round one's pages (the callback does both).
            await asyncio.to_thread(self.gaps, self.crawl_id, org_id)
            known = await asyncio.to_thread(self.known, self.crawl_id, org_id)
        c["second_hop"] = list(known.get("competitor", ()))[: ext.second_hop_competitors]
        await self._competitor_round(second_hop_queries(subject, c["second_hop"]), stats, done)

    async def _competitor_round(
        self, queries: list[tuple[str, str]], stats: dict[str, Any], done: set[str]
    ) -> None:
        ext, c = self.settings.external, stats["competitors"]
        room = max(0, ext.max_competitor_queries - c["queries"])
        sent = queries[:room]
        c["queries"] += len(sent)
        # The company's own pages are crawled anyway: keep result slots for others.
        results = await self._search_all(
            [(template, query, (), (self.site,)) for template, query in sent], stats
        )
        for template, query in queries[room:]:
            c["queries_over_budget"] += 1
            await self._over_budget(template, query, ())
        hits = [(url, query, position) for (_, query), found in zip(sent, results, strict=True)
                for url, position in found]  # fmt: skip
        c["search_pages"] += await self._fetch_results(
            hits, ext.max_competitor_search_pages - c["search_pages"], done
        )

    async def _search_all(
        self,
        queries: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]],
        stats: dict[str, Any],
    ) -> list[list[tuple[str, int]]]:
        """Send queries (template, query, include, exclude), ``external.concurrency`` at
        once, then record them in the order given; each one's results."""
        gate = asyncio.Semaphore(max(1, self.settings.external.concurrency))

        async def one(q: tuple[str, str, tuple[str, ...], tuple[str, ...]]) -> Any:
            async with gate:
                return await self._send(*q)

        answers = await asyncio.gather(*(one(q) for q in queries))
        return [await self._record(*q[:3], answer, stats)
                for q, answer in zip(queries, answers, strict=True)]  # fmt: skip

    async def _fetch_results(
        self, hits: list[tuple[str, str, int]], limit: int, done: set[str]
    ) -> int:
        """Fetch the best-ranked result pages not fetched yet, until ``limit`` are stored."""
        todo = [(url, query) for url, query in rank_candidates(hits) if url not in done]
        queries = dict(todo)

        def fetch(url: str) -> Awaitable[int]:
            done.add(url)  # tried: no other round fetches it again
            return self._fetch_external(url, "search", queries[url])

        return await fetch_until(list(queries), limit, self.settings.external.concurrency, fetch)

    async def _over_budget(self, template: str, query: str, domains: tuple[str, ...]) -> None:
        await asyncio.to_thread(self.store.record_search, self.crawl_id, template, query,
                                domains, "over_budget")  # fmt: skip

    async def _send(
        self, template: str, query: str, include: tuple[str, ...], exclude: tuple[str, ...]
    ) -> tuple[str | None, Any, SearchError | None, int]:
        """One query sent: (provider, results, error, duration in ms)."""
        assert self.search is not None
        started = time.perf_counter()
        try:
            with span("search.query", template=template) as sp:
                provider, results = await self.search.search(
                    query, self.settings.search.results_per_query, include, exclude
                )
                sp.update(provider=provider, results=len(results))
        except SearchError as exc:
            return None, [], exc, round((time.perf_counter() - started) * 1000)
        return provider, results, None, round((time.perf_counter() - started) * 1000)

    async def _record(
        self,
        template: str,
        query: str,
        include: tuple[str, ...],
        answer: tuple[str | None, Any, SearchError | None, int],
        stats: dict[str, Any],
    ) -> list[tuple[str, int]]:
        """Record one sent query: its off-site result URLs with their positions."""
        provider, results, exc, duration_ms = answer
        if exc is not None or provider is None:
            stats["search_error"] = str(exc)
            log.warning("search.failed", extra={"query": query, "error": str(exc)})
            await asyncio.to_thread(
                self.store.record_search, self.crawl_id, template, query, include,
                "failed", error=str(exc)[:1000], duration_ms=duration_ms,
            )  # fmt: skip
            return []
        stats["queries"][query] = len(results)
        stats["providers"][provider] = stats["providers"].get(provider, 0) + 1
        normalized: list[tuple[str, str | None, str | None, int]] = []
        for r in results:
            try:
                normalized.append((normalize_url(r.url, self.settings.urls), r.title,
                                   r.published, r.position))  # fmt: skip
            except InvalidUrl:
                continue
        await asyncio.to_thread(
            self.store.record_search, self.crawl_id, template, query, include, "ok",
            provider=provider, results=normalized, duration_ms=duration_ms,
        )  # fmt: skip
        return [(url, pos) for url, _, _, pos in normalized if not self._in_site(url)]

    async def _fetch_external(self, url: str, via: str, query: str | None) -> int:
        """Fetch one external page (no link following). Returns 1 if a page was stored."""
        with span("url.external", url=url, domain=url_host(url), stage="external", via=via):
            return await self._fetch_external_page(url, via, query)

    async def _fetch_external_page(self, url: str, via: str, query: str | None) -> int:
        state = await asyncio.to_thread(self.store.url_state, self.crawl_id, url)
        if state in FINAL_URL_STATES:
            return int(state == UrlState.PROCESSED)  # done before a restart
        entry = FrontierEntry(url, 1, 0.0, via, None, f"search: {query}" if query else None)
        await asyncio.to_thread(self.store.record_queued, self.crawl_id, [entry], True)
        profile = external_id(url)
        if (self.search is not None and profile and profile.source_type == "linkedin"
                and profile.external_id.startswith("company/")):  # fmt: skip
            # LinkedIn's robots.txt refuses crawlers: its company page (size,
            # headcount) is never fetched from linkedin.com, only taken from
            # the search provider's copy, recorded as such (method below).
            fetched = await self._contents(url, f"https://www.linkedin.com/{profile.external_id}")
            if fetched is None:
                return 0
            result, method = fetched, "search_contents"
        else:
            decision = await self.policy.check(url, external=True)
            if not decision.allowed:
                await self._refused(url, decision)
                return 0
            async with self.throttle.slot(url_host(url), decision.crawl_delay):
                with span("fetch.http") as sp:
                    result = await self.fetcher.fetch(url, HTML_TYPES)
                    sp.update(http_status=result.status, fetch_error=result.error)
            outcome = _outcome(result)
            if outcome is not None:
                self.throttle.observe(url_host(url), outcome, (result.duration_ms or 0) / 1000)
            method = "http"
        self.stats.bytes += len(result.body or b"")
        URLS_FETCHED.inc()
        raw_sha = None
        if result.ok and result.body is not None:
            raw_sha = await asyncio.to_thread(self.rawstore.put, result.body)
        fetch_id = await asyncio.to_thread(
            self.store.record_fetch, self.crawl_id, result, method, raw_sha
        )
        if not result.ok or result.body is None:
            state = UrlState.BLOCKED if result.error is FetchError.BLOCKED else UrlState.FAILED
            reason = str(result.error or f"http_{result.status}")
            await asyncio.to_thread(self.store.set_state, self.crawl_id, url, state, reason)
            return 0
        with span("parse"):
            page = await self._parse(
                result.body, result.final_url, result.headers.get("content-type")
            )
        if page is None:
            await asyncio.to_thread(
                self.store.set_state, self.crawl_id, url, UrlState.FAILED, "parse_timeout"
            )
            return 0
        page_id = await asyncio.to_thread(self.store.record_page, fetch_id, page, None)
        rows = [
            (link.url, link.text, link.rel, not self._in_site(link.url), False)
            for link in page.links
        ]
        await asyncio.to_thread(self.store.record_links, page_id, rows)
        await asyncio.to_thread(self.store.set_state, self.crawl_id, url, UrlState.PROCESSED)
        return 1

    async def _contents(self, url: str, canonical: str) -> FetchResult | None:
        """The search provider's copy of ``canonical`` as a fetched page of
        ``url``, or None (URL state FAILED) if the provider has none."""
        assert self.search is not None
        started = time.perf_counter()
        try:
            found = await self.search.contents(canonical)
        except SearchError as exc:
            log.warning("contents.failed", extra={"url": canonical, "error": str(exc)})
            found = None
        if found is None:
            await asyncio.to_thread(self.store.set_state, self.crawl_id, url, UrlState.FAILED,
                                    "contents_unavailable")  # fmt: skip
            return None
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            headers={"content-type": "text/html; charset=utf-8"}, body=found.to_html(),
            attempts=1, duration_ms=round((time.perf_counter() - started) * 1000),
        )  # fmt: skip

    async def _record_robots(
        self, origin: str, status: RobotsStatus, result: FetchResult
    ) -> int | None:
        raw_sha = None
        if result.ok and result.body is not None:
            raw_sha = await asyncio.to_thread(self.rawstore.put, result.body)
        snapshot_id: int = await asyncio.to_thread(
            self.store.record_robots, self.crawl_id, origin, str(status), result, raw_sha
        )
        return snapshot_id

    async def _record_sitemap(self, record: SitemapFetchRecord) -> None:
        raw_sha = None
        result = record.result
        if result is not None and result.ok and result.body is not None:
            raw_sha = await asyncio.to_thread(self.rawstore.put, result.body)
        await asyncio.to_thread(self.store.record_sitemap, self.crawl_id, record, raw_sha)

    async def _refused(self, url: str, decision: Decision) -> None:
        """Record a non-ALLOW policy decision: the URL's state and an audit event."""
        state = (
            UrlState.DEFERRED if decision.decision == PolicyDecision.DEFER else (UrlState.BLOCKED)
        )
        reason = decision.reason or str(decision.decision)
        self.stats.policy[f"{decision.decision}:{reason}"] += 1
        if reason in ("robots_disallowed", "redirect_robots_disallowed"):
            self.stats.robots_disallowed += 1
        BLOCKED_REQUESTS.labels(reason).inc()
        log.info("url.refused", extra={"url": url, "decision": str(decision.decision),
                                       "reason": reason})  # fmt: skip
        await asyncio.to_thread(self.store.set_state, self.crawl_id, url, state, reason)
        await asyncio.to_thread(
            self.store.record_decision, self.crawl_id, self.job_id, url, decision.decision,
            reason, decision.detail,
        )  # fmt: skip

    async def _enqueue(self, entries: list[FrontierEntry]) -> None:
        """Admit ``entries``; record the accepted as eligible and the refused with reasons.

        Refusals other than duplicates and external URLs are stored so the
        crawl's scope is explainable (spec sections 8, 13): traps as ``trap``,
        depth and breadth budgets as ``deferred``. At most
        ``max_recorded_rejections`` per crawl are stored; all are counted.
        """
        accepted: list[FrontierEntry] = []
        refused: list[tuple[FrontierEntry, UrlState, str]] = []
        for e in entries:
            reason = self.frontier.add(e)
            if reason is None:
                accepted.append(e)
            elif reason not in (Rejection.DUPLICATE, Rejection.EXTERNAL) and (
                e.url not in self.recorded_rejections
            ):
                state = UrlState.TRAP if reason.startswith("trap:") else UrlState.DEFERRED
                refused.append((e, state, str(reason)))
        URLS_DISCOVERED.inc(len(accepted))
        FRONTIER_DEPTH.set(len(self.frontier))
        await asyncio.to_thread(self.store.record_queued, self.crawl_id, accepted)
        room = self.settings.frontier.max_recorded_rejections - len(self.recorded_rejections)
        if refused and room > 0:
            refused = refused[:room]
            self.recorded_rejections.update(e.url for e, _, _ in refused)
            await asyncio.to_thread(self.store.record_refused, self.crawl_id, refused)

    def _stats(self, stop_reason: str | None, extra: dict[str, Any]) -> dict[str, Any]:
        s = self.stats
        return {
            "stop_reason": stop_reason,
            "fetches": s.fetches,
            "not_modified": s.not_modified,
            "bytes": s.bytes,
            "retries_scheduled": s.retries_scheduled,
            "blocked": s.blocked,
            "fetch_errors": dict(s.fetch_errors),
            "pages_parsed": s.pages_parsed,
            "duplicates": s.duplicates,
            "robots_disallowed": s.robots_disallowed,
            "policy_decisions": dict(s.policy),
            "redirect_off_site": s.redirect_off_site,
            "external_links": s.external_links,
            "reference_links": s.reference_links,
            "browser": {
                "renders": s.renders,
                "used": s.renders_used,
                "errors": dict(s.render_errors),
                "over_budget": s.renders_over_budget,
                "render_ms": s.render_ms,
                "requests": s.render_requests,
                "bytes": s.render_bytes,
                "requests_refused": s.render_refused,
                "restarts": dict(self.browser.restarts) if self.browser else {},
                "circuit": self.browser.breaker.snapshot() if self.browser else {},
                "unavailable": self.browser.unavailable if self.browser else None,
            },
            "frontier_rejections": {str(k): v for k, v in self.frontier.rejections.items()},
            "low_yield_skipped": s.low_yield_skipped,
            "other_language_skipped": s.other_language_skipped,
            "dead_lettered": s.dead_lettered,
            "circuit_deferrals": s.circuit_deferrals,
            # Hosts the crawl slowed down for, and hosts whose circuit opened.
            "rate_control": self.throttle.snapshot(),
            "circuits": self.breaker.snapshot(),
            "frontier_remaining": len(self.frontier),
            "duration_seconds": round(self.clock() - self.started, 3),
            **extra,
        }
