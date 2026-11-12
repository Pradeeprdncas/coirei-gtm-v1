"""Prometheus metrics (production LLD sections 23 and 24; prometheus_client, approved 2026-09-29).

Metrics live in the process's default registry. The worker serves them on
``worker.metrics_port``; the API at ``/metrics``.
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import Engine, event, text

_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

CRAWL_JOBS = Counter("crawl_jobs_total", "Crawl jobs finished, by resulting status", ["status"])
CRAWL_DURATION = Histogram(
    "crawl_duration_seconds", "Duration of one site crawl (one run of it)",
    buckets=(10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)  # fmt: skip
URLS_DISCOVERED = Counter("urls_discovered_total", "URLs accepted into a frontier")
URLS_FETCHED = Counter("urls_fetched_total", "URLs fetched (site and external pages)")
HTTP_REQUESTS = Counter("http_requests_total", "HTTP fetches by outcome", ["outcome"])
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP fetch latency, redirects and retries included",
    buckets=_SECONDS,
)  # fmt: skip
HTTP_RETRIES = Counter("http_retries_total", "Fetch retries (in-fetch and scheduled)", ["kind"])
BLOCKED_REQUESTS = Counter(
    "blocked_requests_total", "Requests refused by the network guard or robots.txt", ["reason"]
)
BYTES_DOWNLOADED = Counter("bytes_downloaded_total", "Response body bytes downloaded")
BROWSER_FALLBACK = Counter(
    "browser_fallback_total", "Browser renders attempted, by trigger and outcome",
    ["trigger", "outcome"],
)  # fmt: skip
RENDER_DURATION = Histogram(
    "browser_render_duration_seconds", "Browser render wall time", buckets=_SECONDS
)
BROWSER_RESTARTS = Counter(
    "browser_restarts_total", "Chromium relaunches, by reason (recycle, crash)", ["reason"]
)
PAGES_PARSED = Counter("pages_parsed_total", "Pages parsed and stored")
EVIDENCE_CREATED = Counter("evidence_created_total", "Evidence records created")
OBSERVATIONS_CREATED = Counter("observations_created_total", "Observations created")
IDENTITY_DECISIONS = Counter(
    "identity_decisions_total", "Identity resolution decisions recorded", ["outcome", "kind"]
)
FIELD_UNKNOWN = Counter("field_unknown_total", "Field assessments resolved as UNKNOWN")
FIELD_CONFLICT = Counter("field_conflict_total", "Field assessments resolved as CONFLICTING")
FRONTIER_DEPTH = Gauge("frontier_depth", "URLs queued in the frontier of running crawls")
EXTRACTION_DURATION = Histogram(
    "extraction_duration_seconds", "Duration of one extraction run over a crawl",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)  # fmt: skip
DB_DURATION = Histogram(
    "db_query_duration_seconds", "Database statement latency",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
)  # fmt: skip

DEAD_LETTERS = Counter(
    "dead_letter_items_total", "Work moved to the dead-letter queue", ["entity", "error_class"]
)

# Cost accounting (spec section 27; cost.py).
COST_USD = Counter("cost_usd", "Cost in USD at the configured unit prices", ["kind"])
RESOURCE_USAGE = Counter("resource_usage", "Resource usage by kind and unit", ["kind", "unit"])
# Statement seconds of the current job run: set by cost.metered, summed by the listener.
DB_SECONDS: ContextVar[list[float] | None] = ContextVar("db_seconds", default=None)
# Crawl quality (spec section 27; quality.py), observed once per finished job.
_RATIO = (0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1)
CRAWL_URLS = Counter(
    "crawl_urls_total",
    "URLs of finished jobs by outcome (discovered, eligible, fetched, blocked, deferred, trapped)",
    ["outcome"],
)
PAGES_RELEVANT = Counter("pages_relevant_total", "Pages cited as evidence, of finished jobs")
EXTERNAL_SOURCES = Counter(
    "external_sources_total", "External domains cited as evidence, of finished jobs"
)
DUPLICATE_RATIO = Histogram(
    "crawl_duplicate_ratio", "Duplicate pages / parsed pages, per finished job", buckets=_RATIO
)
UNIQUE_CONTENT_YIELD = Histogram(
    "crawl_unique_content_yield", "Unique pages / HTTP fetches, per finished job", buckets=_RATIO
)
GAPS_UNANSWERED = Histogram(
    "crawl_gaps_unanswered", "Company fields left open, per finished job",
    buckets=(0, 1, 2, 5, 10, 20, 40, 80),
)  # fmt: skip
FIELD_STATES = Counter("field_states_total", "Field assessments by resulting state", ["state"])
SEARCH_REQUESTS = Counter(
    "search_requests_total", "Search provider calls by outcome", ["provider", "outcome"]
)
# Alerting (spec section 26): worker liveness, crashes, dependency outages.
WORKER_LAST_SEEN = Gauge(
    "worker_last_seen_timestamp_seconds", "When this worker last polled or renewed a lease"
)
JOB_LEASES_EXPIRED = Counter(
    "job_leases_expired_total", "Jobs reclaimed because their worker's lease ran out"
)
CIRCUIT_OPENED = Counter(
    "circuit_opened_total", "Circuit breaker trips, by breaker (host, search, browser)",
    ["breaker"],
)  # fmt: skip

_instrumented: set[int] = set()


class _QueueCollector(Collector):
    """Queue depth read from PostgreSQL at scrape time (spec section 26): jobs by
    state, open URLs of running crawls by state, open dead-letter items."""

    engine: Engine | None = None

    def collect(self) -> Any:
        if self.engine is None:
            return
        try:
            with self.engine.connect() as conn:
                jobs = conn.execute(
                    text("SELECT status, count(*) FROM crawl_job GROUP BY status")
                ).all()
                urls = conn.execute(text(
                    "SELECT u.state, count(*) FROM crawl_url u JOIN crawl c ON c.id = u.crawl_id"
                    " WHERE c.status = 'running' AND u.state IN"
                    " ('eligible', 'retry', 'leased', 'fetching', 'fetched', 'parsed')"
                    " GROUP BY u.state"
                )).all()  # fmt: skip
                dead = conn.execute(text(
                    "SELECT entity, count(*) FROM dead_letter_item WHERE resolved_at IS NULL"
                    " GROUP BY entity"
                )).all()  # fmt: skip
        except Exception:  # noqa: BLE001 - a scrape must not fail because the DB is down
            yield GaugeMetricFamily("database_up", _DB_UP_DOC, value=0)
            return
        yield GaugeMetricFamily("database_up", _DB_UP_DOC, value=1)
        for name, doc, label, rows in (
            ("queue_jobs", "Crawl jobs by state", "state", jobs),
            ("queue_urls", "Open URLs of running crawls by state", "state", urls),
            ("dead_letter_open", "Unresolved dead-letter items", "entity", dead),
        ):
            family = GaugeMetricFamily(name, doc, labels=[label])
            for key, count in rows:
                family.add_metric([key], count)
            yield family

    def describe(self) -> Any:
        # Names only, so the registry knows them before an engine is set.
        yield GaugeMetricFamily("database_up", _DB_UP_DOC)
        for name in ("queue_jobs", "queue_urls", "dead_letter_open"):
            yield GaugeMetricFamily(name, name)


_DB_UP_DOC = "1 if the database answered the last scrape's queue queries, else 0"
_QUEUE = _QueueCollector()
REGISTRY.register(_QUEUE)


def descendant_rss_bytes(pid: int, proc: Path = Path("/proc")) -> int | None:
    """Resident memory of ``pid``'s descendant processes, from Linux ``/proc``; None elsewhere.

    A worker's only child processes are Playwright's driver and the Chromium it
    launches, so for a worker this is the browser's memory.
    """
    if not proc.is_dir():
        return None
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:  # "pid (comm) state ppid ...": comm may hold spaces and ")"
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue  # exited meanwhile
            children.setdefault(ppid, []).append(int(entry.name))
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    total, todo = 0, list(children.get(pid, []))
    while todo:
        child = todo.pop()
        todo.extend(children.get(child, []))
        try:
            total += int((proc / str(child) / "statm").read_text().split()[1]) * page
        except (OSError, IndexError, ValueError):
            continue
    return total


class _BrowserMemoryCollector(Collector):
    """Browser memory pressure (spec section 26), read at scrape time."""

    def collect(self) -> Any:
        rss = descendant_rss_bytes(os.getpid())
        if rss is not None:
            yield GaugeMetricFamily(
                "browser_processes_resident_memory_bytes",
                "Resident memory of this process's children (Playwright driver and Chromium)",
                value=rss,
            )

    def describe(self) -> Any:
        yield GaugeMetricFamily("browser_processes_resident_memory_bytes", "")


REGISTRY.register(_BrowserMemoryCollector())


def instrument_engine(engine: Engine) -> None:
    """Time every statement the engine executes (once per engine)."""
    _QUEUE.engine = engine  # ponytail: one engine per process; the latest one is scraped
    if id(engine) in _instrumented:
        return
    _instrumented.add(id(engine))

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn: Any, *_: Any) -> None:
        conn.info.setdefault("query_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _stop(conn: Any, *_: Any) -> None:
        elapsed = time.perf_counter() - conn.info["query_start"].pop()
        DB_DURATION.observe(elapsed)
        if (job_seconds := DB_SECONDS.get()) is not None:
            job_seconds[0] += elapsed


def serve_metrics(host: str, port: int, engine: Engine | None = None) -> None:
    """Serve /metrics on ``host:port`` (port 0: don't) and time the engine's statements."""
    if engine is not None:
        instrument_engine(engine)
    if port:
        start_http_server(port, addr=host)
