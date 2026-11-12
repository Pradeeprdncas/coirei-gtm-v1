"""Load harness (spec sections 29, 32 and 35; Appendix B phase 16).

Runs the real worker and pipeline code (``run_worker``, ``run_pipeline``, the
crawl loop, extraction, assessment, the job API) against a simulated web, in a
scratch database, and reports measurements with PASS/FAIL acceptance checks::

    python -m gtm_crawler.loadtest deep_single_domain --scale 2000 --out results.json
    python -m gtm_crawler.loadtest all --out results.json   # every scenario, default scale

Nothing leaves the machine:

* sites are generated and served through ``httpx.MockTransport`` with
  configurable latency, jitter and error rate (HTTP 500s);
* Chromium is replaced by a timed stub of ``BrowserFetcher._render``; the
  render budget, semaphore and breaker around it are the real ones;
* search is a scripted Exa-compatible endpoint that can be slow, time out,
  answer 5xx or 429.

Without ``--database-url`` the database is ``gtm_crawler_load_scratch`` on the local
server; it is dropped, recreated and migrated first (only names containing
"load" are accepted). What the simulation does not capture, and measured
results, are in ``docs/load-results.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import itertools
import json
import logging
import os
import platform
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import httpx
from prometheus_client import REGISTRY
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from . import crawl as crawl_module
from . import pipeline as pipeline_module
from .assess import assess_organization
from .breaker import CircuitBreaker
from .config import (
    BrowserSettings,
    CrawlSettings,
    DatabaseSettings,
    ExternalSettings,
    FetchSettings,
    IdentitySettings,
    RawStoreSettings,
    SearchSettings,
    Secret,
    Settings,
    WorkerSettings,
)
from .db import make_engine
from .db.migrate import upgrade_head
from .extract import extract_crawl
from .fetch.browser import BrowserFetcher
from .fetch.http import FetchResult
from .jobs import JobStore
from .metrics import instrument_engine
from .parse import parse_page
from .pipeline import run_pipeline
from .search import ExaSearch, FailoverSearch
from .throttle import HostThrottle
from .worker import run_worker

DEFAULT_DATABASE_URL = os.environ.get(
    "GTM_LOAD_DATABASE_URL", "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_load_scratch"
)
PRESS_HOST = "press.load.example"  # third-party pages search results point to
BACKUP_SEARCH_HOST = "backup-search.load.example"
FINAL_JOBS = ("completed", "failed", "cancelled")
IN_FLIGHT = ["leased", "fetching", "fetched", "parsed"]
FINAL_URLS = ["processed", "blocked", "trap", "failed", "dlq"]

# --- the simulated web ----------------------------------------------------------------

_SYLLABLES = ("ka", "lo", "mi", "ne", "ru", "ta", "vo", "shi", "pe", "dra", "zu", "fen",
              "gal", "hor", "bix", "qua")  # fmt: skip
_CALENDAR = re.compile(r"/calendar/(\d{4})/(\d{2})")


def word(n: int) -> str:
    """One of 4096 pronounceable words."""
    return "".join(_SYLLABLES[(n >> s) & 15] for s in (0, 4, 8))


def _html(title: str, body: str, site_name: str = "") -> bytes:
    head = f'<meta property="og:site_name" content="{site_name}">' if site_name else ""
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>".encode()


def _words(seed: str, n: int = 60) -> str:
    rng = random.Random(zlib.crc32(seed.encode()))  # noqa: S311 - page text, not security
    return " ".join(word(rng.randrange(4096)) for _ in range(n))


@dataclass
class Site:
    """A generated company site: a tree of ``pages`` pages (``branching`` children
    each), a cross link per page, optionally an endless calendar (a crawler trap)
    and JavaScript shells (``shell_ratio``) that only a render fills."""

    host: str
    name: str
    pages: int
    branching: int = 4
    shell_ratio: float = 0.0
    trap: bool = False
    index: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.index = {self.path(i): i for i in range(self.pages)}

    @property
    def root(self) -> str:
        return f"https://{self.host}/"

    def path(self, i: int) -> str:
        sections = max(1, -(-self.pages // 400))  # frontier.max_urls_per_prefix is 500
        return "/" if i == 0 else f"/s{i % sections}/{word(i)}-{i}"

    def is_shell(self, i: int) -> bool:
        return i > 0 and zlib.crc32(f"{self.host}#{i}".encode()) % 100 < self.shell_ratio * 100

    def full(self, i: int) -> bytes:
        b = self.branching
        linked = [*range(i * b + 1, min(self.pages, i * b + b + 1)), (i - 1) // b if i else 0,
                  zlib.crc32(f"x{i}".encode()) % self.pages]  # fmt: skip
        nav = "".join(f'<a href="{self.path(j)}">{word(j)} guide</a>' for j in linked)
        if i == 0 and self.trap:
            nav += '<a href="/calendar/2026/01">Events</a>'
        facts = (f"{self.name} is a B2B software company founded in 2015 and headquartered in "
                 f"Austin, Texas. {self.name} has 120 employees. ") if i == 0 else ""  # fmt: skip
        return _html(
            f"{self.name} {word(i)}",
            f"<nav>{nav}</nav><main><h1>{self.name} {word(i)}</h1><p>{facts}"
            f"{_words(f'{self.host}/{i}')}.</p></main><footer>© 2026 {self.name}, Inc.</footer>",
            self.name,
        )

    def shell(self, i: int) -> bytes:
        return _html(
            f"{self.name} {word(i)}", '<div id="root"></div><script src="/app.js"></script>'
        )

    def calendar(self, year: int, month: int) -> bytes:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return _html(
            "Events",
            f'<a href="/calendar/{year}/{month:02d}">Next month</a><a href="/">Home</a>'
            f"<p>{_words(f'{self.host}/cal/{year}/{month}')}</p>",
        )


class SimWeb:
    """``httpx.MockTransport`` handler serving generated sites, with latency, jitter
    and an error rate; records per-host concurrency and gaps between requests."""

    def __init__(
        self, sites: list[Site], latency: float = 0.005, jitter: float = 0.005,
        error_rate: float = 0.0,
    ) -> None:  # fmt: skip
        self.sites = {s.host: s for s in sites}
        self.latency, self.jitter, self.error_rate = latency, jitter, error_rate
        self.hits: Counter[str] = Counter()
        self.active: Counter[str] = Counter()
        self.max_active: Counter[str] = Counter()
        self.first_start: dict[str, float] = {}
        self.last_start: dict[str, float] = {}
        self.min_gap: dict[str, float] = {}
        self.requests: Counter[str] = Counter()
        self.errors = 0
        self._rng = random.Random(7)  # noqa: S311 - simulated latency, not security

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url, host = str(request.url), request.url.host
        self.hits[url] += 1
        if request.url.path == "/robots.txt":  # fetched outside the throttle, once per host
            return _response(200, b"User-agent: *\nAllow: /\n", "text/plain")
        now = time.perf_counter()
        if host in self.last_start:
            self.min_gap[host] = min(self.min_gap.get(host, 1e9), now - self.last_start[host])
        self.first_start.setdefault(host, now)
        self.last_start[host] = now
        self.requests[host] += 1
        self.active[host] += 1
        self.max_active[host] = max(self.max_active[host], self.active[host])
        try:
            await asyncio.sleep(self.latency + self._rng.random() * self.jitter)
        finally:
            self.active[host] -= 1
        failing = zlib.crc32(f"{url}#{self.hits[url]}".encode()) % 10_000
        if host in self.sites and failing < self.error_rate * 10_000:
            self.errors += 1
            return _response(500, b"", "text/plain")
        body = self.page(url)
        return _response(200, body, "text/html") if body else _response(404, b"", "text/plain")

    def page(self, url: str, rendered: bool = False) -> bytes | None:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path or "/"
        if host == PRESS_HOST:
            company = path.strip("/").split("/")[0]
            site = self.sites.get(company)
            if site is None:
                return None
            return _html(f"{site.name} news", (
                f"<h1>{site.name} raises $20M Series A</h1><p>{site.name} raised a $20M Series A"
                f" led by Example Ventures. {_words(url)}</p>"))  # fmt: skip
        site = self.sites.get(host)
        if site is None:
            return None
        if site.trap and (m := _CALENDAR.fullmatch(path)):
            return site.calendar(int(m[1]), int(m[2]))
        i = site.index.get(path)
        if i is None:
            return None
        return site.shell(i) if site.is_shell(i) and not rendered else site.full(i)

    def fetched(self, host: str, prefix: str) -> int:
        return sum(1 for url in self.hits if url.startswith(f"https://{host}{prefix}"))


def _response(status: int, body: bytes, content_type: str) -> httpx.Response:
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class SimSearch:
    """Exa-compatible search at ``api.exa.ai`` and a backup provider at
    ``BACKUP_SEARCH_HOST``. A provider in mode "down" fails every call with a
    5xx, a timeout or a 429, in rotation."""

    def __init__(self, web: SimWeb, latency: float = 0.01, timeout: float = 0.2) -> None:
        self.web, self.latency, self.timeout = web, latency, timeout
        self.modes = {"exa": "ok", "backup": "ok"}
        self.calls: Counter[str] = Counter()
        self._failures = itertools.count()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        provider = "backup" if request.url.host == BACKUP_SEARCH_HOST else "exa"
        await asyncio.sleep(self.latency)
        if self.modes[provider] == "down":
            kind = ("5xx", "timeout", "429")[next(self._failures) % 3]
            self.calls[f"{provider}:{kind}"] += 1
            if kind == "timeout":
                await asyncio.sleep(self.timeout)
                raise httpx.ReadTimeout("simulated provider timeout", request=request)
            if kind == "429":
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(503, text="unavailable")
        self.calls[f"{provider}:ok"] += 1
        query = json.loads(request.content)["query"]
        hosts = [h for h, s in self.web.sites.items() if s.name in query or h in query]
        urls = [f"https://{PRESS_HOST}/{h}/{k}" for h in hosts[:1] for k in range(3)]
        return httpx.Response(200, json={"results": [{"url": u, "title": "t"} for u in urls]})


def two_providers(
    settings: SearchSettings, transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FailoverSearch:  # fmt: skip
    """``make_search`` with a second, backup provider (only Exa exists today): the
    real ``FailoverSearch`` and breaker over two Exa-protocol endpoints."""
    primary = ExaSearch(settings, transport, sleep)
    backup = ExaSearch(replace(settings, endpoint=f"https://{BACKUP_SEARCH_HOST}/search"),
                       transport, sleep)  # fmt: skip
    backup.name = "backup"
    breaker = CircuitBreaker(
        "search", settings.circuit_failure_threshold, settings.circuit_cooldown_seconds,
        settings.circuit_cooldown_seconds * 8,
    )  # fmt: skip
    return FailoverSearch([primary, backup], breaker)


class FakeBrowser:
    """Stands in for Chromium: a render takes ``delay`` seconds and returns the
    page's full HTML. Records how many renders ran at once."""

    def __init__(self, web: SimWeb, delay: float) -> None:
        self.web, self.delay = web, delay
        self.active = self.max_active = self.renders = 0

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        fake = self

        async def render(_: BrowserFetcher, fetched: FetchResult, result: FetchResult) -> Any:
            fake.renders += 1
            fake.active += 1
            fake.max_active = max(fake.max_active, fake.active)
            try:
                await asyncio.sleep(fake.delay)
            finally:
                fake.active -= 1
            body = fake.web.page(fetched.final_url, rendered=True) or b"<html></html>"
            result.status, result.content_type, result.body = 200, "text/html", body
            result.headers = {"content-type": "text/html; charset=utf-8"}
            result.render = {"requests": 3, "bytes": len(body), "refused": 0}
            return result

        with mock.patch.object(BrowserFetcher, "_render", render):
            yield


@contextlib.contextmanager
def throttle_starts(released: dict[str, list[float]]) -> Iterator[None]:
    """Record when ``HostThrottle`` releases each request, per host."""
    slot = HostThrottle.slot

    @contextlib.asynccontextmanager
    async def recorded(self: HostThrottle, host: str, crawl_delay: float | None = None) -> Any:
        async with slot(self, host, crawl_delay):
            released.setdefault(host, []).append(time.perf_counter())
            yield

    with mock.patch.object(HostThrottle, "slot", recorded):
        yield


# --- measurement ----------------------------------------------------------------------


def percentiles(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)

    def at(q: float) -> float | None:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2) if ordered else None

    return {"n": len(ordered), "p50": at(0.5), "p90": at(0.9), "p99": at(0.99),
            "max": round(ordered[-1], 2) if ordered else None}  # fmt: skip


def memory() -> dict[str, Any]:
    """Peak and current resident memory of this process, from what the OS offers."""
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                        ("peak", ctypes.c_size_t), ("current", ctypes.c_size_t),
                        *((f"f{k}", ctypes.c_size_t) for k in range(6))]  # fmt: skip

        c = Counters()
        c.cb = ctypes.sizeof(c)
        get = ctypes.windll.psapi.GetProcessMemoryInfo
        get.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        get(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return {
            "method": "process peak working set (Win32 GetProcessMemoryInfo)",
            "peak_mb": round(c.peak / 2**20, 1),
            "current_mb": round(c.current / 2**20, 1),
        }
    import resource  # POSIX only

    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB elsewhere
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    return {"method": "process peak RSS (getrusage ru_maxrss)", "peak_mb": round(peak / 2**20, 1)}


def machine(engine: Engine) -> dict[str, Any]:
    ram = None
    if sys.platform == "win32":

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong), *((f"f{k}", ctypes.c_ulonglong)
                                                         for k in range(6))]  # fmt: skip

        s = Status()
        s.length = ctypes.sizeof(s)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
            ram = s.total
    elif hasattr(os, "sysconf"):
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    with engine.connect() as conn:
        pg = conn.execute(text("SELECT version()")).scalar()
    cpu = platform.processor() or platform.machine()
    return {"platform": platform.platform(), "processor": cpu,
            "logical_cpus": os.cpu_count(), "ram_gb": round(ram / 2**30, 1) if ram else None,
            "python": platform.python_version(), "postgres": pg}  # fmt: skip


def _db_totals() -> tuple[float, float]:
    count = REGISTRY.get_sample_value("db_query_duration_seconds_count") or 0.0
    total = REGISTRY.get_sample_value("db_query_duration_seconds_sum") or 0.0
    return count, total


def _circuit_trips(breaker: str) -> float:
    return REGISTRY.get_sample_value("circuit_opened_total", {"breaker": breaker}) or 0.0


class StageTimer:
    """Wall and thread-CPU seconds of parsing, extraction and assessment."""

    def __init__(self) -> None:
        self.stages: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def _wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def timed(*args: Any, **kwargs: Any) -> Any:
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    s = self.stages.setdefault(name, {"calls": 0, "wall_s": 0.0, "cpu_s": 0.0})
                    s["calls"] += 1
                    s["wall_s"] += time.perf_counter() - wall
                    s["cpu_s"] += time.thread_time() - cpu

        return timed

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        with (
            mock.patch.object(crawl_module, "parse_page",
                              self._wrap("parse", parse_page)),
            mock.patch.object(pipeline_module, "extract_crawl",
                              self._wrap("extract", extract_crawl)),
            mock.patch.object(pipeline_module, "assess_organization",
                              self._wrap("assess", assess_organization)),
        ):  # fmt: skip
            yield

    def report(self) -> dict[str, dict[str, float]]:
        return {k: {n: round(v, 3) for n, v in s.items()} for k, s in self.stages.items()}


class _LogCounter(logging.Handler):
    """Counts the crawler's warnings and errors by event name."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.counts: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        self.counts[f"{record.levelname.lower()}:{record.getMessage()}"] += 1

    @contextlib.contextmanager
    def attached(self) -> Iterator[None]:
        logger = logging.getLogger("gtm_crawler")
        saved = logger.level, logger.propagate
        logger.addHandler(self)
        logger.setLevel(logging.WARNING)  # INFO logging is not part of what is measured
        logger.propagate = False
        try:
            yield
        finally:
            logger.removeHandler(self)
            logger.setLevel(saved[0])
            logger.propagate = saved[1]


class Probes:
    """Event-loop lag and queue depths, sampled while a scenario runs."""

    def __init__(self, observer: Engine, pattern: str) -> None:
        self.observer, self.pattern = observer, pattern
        self.lag_ms: list[float] = []
        self.max_queued = self.max_running = self.max_open_urls = 0
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> Probes:
        self._tasks = [asyncio.create_task(self._lag()), asyncio.create_task(self._queues())]
        return self

    async def __aexit__(self, *_: object) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _lag(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(0.05)
            self.lag_ms.append((time.perf_counter() - started - 0.05) * 1000)

    async def _queues(self) -> None:
        while True:
            jobs, urls = await asyncio.to_thread(self._sample)
            self.max_queued = max(self.max_queued, jobs.get("queued", 0))
            self.max_running = max(self.max_running,
                                   jobs.get("running", 0) + jobs.get("finalizing", 0))  # fmt: skip
            self.max_open_urls = max(self.max_open_urls, urls)
            await asyncio.sleep(0.5)

    def _sample(self) -> tuple[dict[str, int], int]:
        with self.observer.connect() as conn:
            jobs = conn.execute(text(
                "SELECT status, count(*) FROM crawl_job WHERE root_url LIKE :p GROUP BY status"
            ), {"p": self.pattern}).all()  # fmt: skip
            urls = conn.execute(text(
                "SELECT count(*) FROM crawl_url u JOIN crawl c ON c.id = u.crawl_id"
                " JOIN crawl_job j ON j.id = c.job_id WHERE j.root_url LIKE :p"
                " AND u.state IN ('eligible', 'retry', 'leased', 'fetching', 'fetched', 'parsed')"
            ), {"p": self.pattern}).scalar_one()  # fmt: skip
        return {status: n for status, n in jobs}, int(urls)


# --- scenario plumbing ----------------------------------------------------------------


def tuned(settings: Settings, **sections: dict[str, Any]) -> Settings:
    """``settings`` with some fields of some sections replaced."""
    return replace(settings, **{name: replace(getattr(settings, name), **values)
                                for name, values in sections.items()})  # fmt: skip


def base_settings(database_url: str, raw: str) -> Settings:
    """Production defaults, except what would make a simulated run take hours
    (politeness delay, retry waits) or reach out (no competitor crawls)."""
    return Settings(
        database=DatabaseSettings(url=Secret(database_url)),
        identity=IdentitySettings(user_agent_contact="https://load-test.invalid/contact"),
        fetch=FetchSettings(max_retries=0),
        rawstore=RawStoreSettings(root=raw),
        crawl=CrawlSettings(
            min_delay_seconds=0.0,
            retry_delay_seconds=0.2,
            checkpoint_seconds=2.0,
            max_pages=100_000,
        ),  # fmt: skip
        browser=BrowserSettings(enabled=False),
        search=SearchSettings(
            api_key=Secret("load-test-key"), max_retries=1, retry_backoff_seconds=0.01
        ),  # fmt: skip
        external=ExternalSettings(
            enabled=False, max_competitor_crawls=0, max_search_pages=3, max_reference_pages=0
        ),  # fmt: skip
        worker=WorkerSettings(lease_seconds=3.0, poll_seconds=0.1, metrics_port=0),
    )


@dataclass
class Ctx:
    name: str
    scale: int
    url: str
    raw: str
    observer: Engine
    prefix: str = field(default_factory=lambda: f"lt{uuid.uuid4().hex[:8]}")
    checks: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    engines: list[Engine] = field(default_factory=list)
    timeout: float = 1800.0

    @property
    def pattern(self) -> str:
        return f"https://{self.prefix}-%"

    def host(self, i: int) -> str:
        return f"{self.prefix}-c{i}.example"

    def sites(self, n: int, pages: int, **kwargs: Any) -> list[Site]:
        return [Site(self.host(i), f"{word(i * 37 + 5).title()} Systems {i}", pages, **kwargs)
                for i in range(n)]  # fmt: skip

    def settings(self) -> Settings:
        return base_settings(self.url, self.raw)

    def check(self, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def engine(self, settings: Settings, pool_size: int | None = None) -> Engine:
        engine = make_engine(settings.database, pool_size=pool_size)
        # Before any statement, as gtm-worker does (serve_metrics): listeners added
        # while another thread's statement is in flight break its timing.
        instrument_engine(engine)
        self.engines.append(engine)
        return engine

    def jobs(self) -> dict[str, int]:
        with self.observer.connect() as conn:
            rows = conn.execute(text(
                "SELECT status, count(*) FROM crawl_job WHERE root_url LIKE :p GROUP BY status"
            ), {"p": self.pattern}).all()  # fmt: skip
        return {status: n for status, n in rows}

    async def submit(self, engine: Engine, settings: Settings, sites: list[Site]) -> None:
        store = JobStore(engine)
        for site in sites:
            await asyncio.to_thread(store.create, site.root, settings.urls)

    async def drain(
        self, settings: Settings, engine: Engine, workers: int, expected: int, web: SimWeb,
        search: SimSearch | None = None,
    ) -> float:  # fmt: skip
        """Run ``workers`` worker loops until ``expected`` jobs are final. Returns seconds."""
        shutdown = asyncio.Event()
        started = time.perf_counter()
        tasks = [
            asyncio.create_task(run_worker(
                settings, engine, owner=f"{self.prefix}-w{k}", shutdown=shutdown,
                transport=httpx.MockTransport(web),
                search_transport=httpx.MockTransport(search) if search else None,
            ))
            for k in range(workers)
        ]  # fmt: skip
        try:
            while True:
                jobs = await asyncio.to_thread(self.jobs)
                if sum(jobs.values()) >= expected and all(s in FINAL_JOBS for s in jobs):
                    break
                if all(t.done() for t in tasks):
                    break  # every worker loop died
                await asyncio.sleep(0.25)
        finally:
            shutdown.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        crashed = [repr(r) for r in results if isinstance(r, BaseException)]
        self.check("worker loops survived", not crashed, crashed or f"{workers} workers")
        return time.perf_counter() - started

    def crawl_stats(self) -> list[dict[str, Any]]:
        with self.observer.connect() as conn:
            return [row[0] or {} for row in conn.execute(text(
                "SELECT c.stats FROM crawl c JOIN crawl_job j ON j.id = c.job_id"
                " WHERE j.root_url LIKE :p ORDER BY c.id"
            ), {"p": self.pattern})]  # fmt: skip


def summary(observer: Engine, pattern: str) -> dict[str, Any]:
    """What the run stored, from the database: pages, fetches, latency, integrity."""
    with observer.connect() as conn:
        ids = list(conn.execute(text(
            "SELECT c.id FROM crawl c JOIN crawl_job j ON j.id = c.job_id WHERE j.root_url LIKE :p"
        ), {"p": pattern}).scalars())  # fmt: skip
        q = {"ids": ids}
        pages, unique = conn.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE p.duplicate_of_page_id IS NULL)"
            " FROM page p JOIN \"fetch\" f ON f.id = p.fetch_id WHERE f.crawl_id = ANY(:ids)"
        ), q).one()  # fmt: skip
        fetches = {k: v for k, v in conn.execute(text(
            "SELECT method, count(*) FROM \"fetch\" WHERE crawl_id = ANY(:ids) GROUP BY method"
        ), q)}  # fmt: skip
        latency = [float(v) for v in conn.execute(text(
            "SELECT duration_ms FROM \"fetch\" WHERE crawl_id = ANY(:ids) AND method = 'http'"
            " AND duration_ms IS NOT NULL"
        ), q).scalars()]  # fmt: skip
        urls = {k: v for k, v in conn.execute(text(
            "SELECT state, count(*) FROM crawl_url WHERE crawl_id = ANY(:ids) GROUP BY state"
        ), q)}  # fmt: skip
        # The same URL stored twice as an original page in one crawl.
        duplicate_pages = conn.execute(text(
            "SELECT count(*) FROM (SELECT 1 FROM page p JOIN \"fetch\" f ON f.id = p.fetch_id"
            " WHERE f.crawl_id = ANY(:ids) AND p.duplicate_of_page_id IS NULL"
            " GROUP BY f.crawl_id, f.url_id HAVING count(*) > 1) d"
        ), q).scalar_one()  # fmt: skip
        # The same fact cited from two stored copies of the same URL (current extraction).
        duplicate_evidence = conn.execute(text(
            "SELECT count(*) FROM (SELECT 1 FROM observation o"
            " JOIN evidence e ON e.id = o.evidence_id JOIN page p ON p.id = e.page_id"
            " JOIN \"fetch\" f ON f.id = p.fetch_id WHERE o.crawl_id = ANY(:ids)"
            " AND o.extraction_id = (SELECT max(x.id) FROM extraction x"
            "                        WHERE x.crawl_id = o.crawl_id)"
            " GROUP BY o.crawl_id, o.field, o.value_verbatim, f.url_id"
            " HAVING count(DISTINCT p.id) > 1) d"
        ), q).scalar_one()  # fmt: skip
        observations = conn.execute(text(
            "SELECT count(*) FROM observation WHERE crawl_id = ANY(:ids)"
        ), q).scalar_one()  # fmt: skip
    return {"crawls": len(ids), "pages": pages, "unique_pages": unique, "fetches": fetches,
            "fetch_latency_ms": percentiles(latency), "url_states": urls,
            "duplicate_pages": duplicate_pages, "duplicate_evidence": duplicate_evidence,
            "observations": observations}  # fmt: skip


def scratch_database(url: str, marker: str = "load", *, migrate: bool = True) -> None:
    """Drop, recreate and (if ``migrate``) migrate ``url``'s database; only a
    database whose name contains ``marker`` (the restore drill uses "drill")."""
    u = make_url(url)
    name = u.database or ""
    if marker not in name or not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError(f"refusing to recreate database {name!r}: its name must contain "
                         f"'{marker}'")  # fmt: skip
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    if migrate:
        upgrade_head(url)


# --- scenarios ------------------------------------------------------------------------


async def deep_single_domain(ctx: Ctx) -> None:
    """A site of ``scale`` pages plus an endless calendar, with a page budget above
    its size (the trap is all that is left at the end); then a second site of
    ``scale`` pages with max_pages at 10% of it (the budget binds)."""
    site = Site(ctx.host(0), "Deepco Systems", ctx.scale, branching=4, trap=True)
    small = Site(ctx.host(1), "Budgetco Systems", ctx.scale, branching=4)
    web = SimWeb([site, small], latency=0.005, jitter=0.005, error_rate=0.01)
    roomy, budget = ctx.scale + 500, max(10, ctx.scale // 10)
    settings = tuned(ctx.settings(), crawl={"max_pages": roomy})
    engine = ctx.engine(settings, settings.crawl.concurrency + 2)
    out = await run_pipeline(site.root, settings, engine, transport=httpx.MockTransport(web))
    stats = out["stats"]
    calendar = web.fetched(site.host, "/calendar/")
    covered = sum(1 for u in web.hits if urlsplit(u).path in site.index
                  and urlsplit(u).hostname == site.host)  # fmt: skip
    limited = tuned(settings, crawl={"max_pages": budget})
    out2 = await run_pipeline(small.root, limited, engine, transport=httpx.MockTransport(web))
    stats2 = out2["stats"]
    ctx.extra.update(site_pages=ctx.scale, max_pages=roomy, status=out["status"],
                     stop_reason=stats["stop_reason"], fetches=stats["fetches"],
                     site_pages_fetched=covered, crawl_seconds=stats["duration_seconds"],
                     calendar_fetched=calendar, frontier_rejections=stats["frontier_rejections"],
                     retries_scheduled=stats["retries_scheduled"],
                     dead_lettered=stats["dead_lettered"], injected_http_500=web.errors,
                     budget_run={"max_pages": budget, "fetches": stats2["fetches"],
                                 "stop_reason": stats2["stop_reason"],
                                 "status": out2["status"]})  # fmt: skip
    ctx.check("jobs completed", out["job_status"] == out2["job_status"] == "completed",
              [out["status"], out2["status"]])  # fmt: skip
    ctx.check("deep site covered", covered >= 0.95 * ctx.scale,
              f"{covered} of {ctx.scale} pages fetched")  # fmt: skip
    ctx.check("crawler trap bounded", calendar <= settings.frontier.max_urls_per_pattern,
              f"{calendar} calendar pages fetched, "
              f"max_urls_per_pattern {settings.frontier.max_urls_per_pattern}")  # fmt: skip
    ctx.check("page budget respected", stats["fetches"] <= roomy and stats2["fetches"] <= budget
              and stats2["stop_reason"] == "max_pages",
              f"{stats2['fetches']} fetches, max_pages {budget}, "
              f"stop {stats2['stop_reason']}")  # fmt: skip


async def many_domain(ctx: Ctx, workers: int = 4) -> None:
    """``scale`` companies of 25 pages each, 4 concurrent workers, search on."""
    sites = ctx.sites(ctx.scale, 25)
    web = SimWeb(sites, latency=0.02, jitter=0.02, error_rate=0.02)
    search = SimSearch(web)
    settings = tuned(ctx.settings(), crawl={"min_delay_seconds": 0.05},
                     external={"enabled": True})  # fmt: skip
    engine = ctx.engine(settings, workers * (settings.crawl.concurrency + 2))
    await ctx.submit(engine, settings, sites)
    released: dict[str, list[float]] = {}
    with throttle_starts(released):
        seconds = await ctx.drain(settings, engine, workers, len(sites), web, search)
    jobs = await asyncio.to_thread(ctx.jobs)
    c = settings.crawl
    own = [s.host for s in sites]
    slot_gap = min((b - a for h in own for a, b in itertools.pairwise(released.get(h, []))),
                   default=1e9)  # fmt: skip
    max_active = max(web.max_active[h] for h in own)
    min_gap = min(web.min_gap.get(h, 1e9) for h in own)
    # The throttle spaces the release of requests (slot_gap); a released request
    # can still be held up by the event loop before it reaches the transport,
    # so gaps seen by the server (min_gap) can be shorter. Mean spacing bounds
    # the rate the server sees.
    mean_gap = min((web.last_start[h] - web.first_start[h]) / (web.requests[h] - 1)
                   for h in own if web.requests[h] > 1)  # fmt: skip
    ctx.extra.update(companies=len(sites), workers=workers, jobs=jobs,
                     jobs_per_minute=round(len(sites) / seconds * 60, 1),
                     site_host_max_concurrency=max_active,
                     site_host_min_gap_ms=round(min_gap * 1000, 1),
                     throttle_min_release_gap_ms=round(slot_gap * 1000, 1),
                     site_host_min_mean_gap_ms=round(mean_gap * 1000, 1),
                     shared_host_max_concurrency=web.max_active[PRESS_HOST],
                     search_calls=dict(search.calls), injected_http_500=web.errors)  # fmt: skip
    ctx.check("all jobs completed", jobs == {"completed": len(sites)}, jobs)
    ctx.check("per-host concurrency bounded", max_active <= c.per_host_concurrency,
              f"max {max_active} in flight per site, per_host_concurrency "
              f"{c.per_host_concurrency}")  # fmt: skip
    # Windows: time.monotonic ticks ~16 ms and asyncio fires timers up to one
    # tick early, so a release can come up to ~32 ms before its due time.
    ctx.check("throttle releases requests min_delay apart (less 32 ms timer slack)",
              slot_gap >= c.min_delay_seconds - 0.032,
              f"min gap between releases {slot_gap * 1000:.1f} ms")  # fmt: skip
    ctx.check(
        "per-host request rate within min_delay",
        mean_gap >= c.min_delay_seconds,
        f"busiest host: mean gap {mean_gap * 1000:.1f} ms, min gap "
        f"{min_gap * 1000:.1f} ms, min_delay {c.min_delay_seconds * 1000:.0f} ms",
    )


async def browser_heavy(ctx: Ctx) -> None:
    """A site where ~90% of pages are JavaScript shells; render budget = ``scale``."""
    site = Site(ctx.host(0), "Shellco Systems", ctx.scale + ctx.scale // 2, shell_ratio=0.9)
    web = SimWeb([site])
    browser = FakeBrowser(web, delay=0.05)
    settings = tuned(ctx.settings(), browser={"enabled": True, "max_renders": ctx.scale})
    engine = ctx.engine(settings, settings.crawl.concurrency + 2)
    renders_before = REGISTRY.get_sample_value("browser_render_duration_seconds_count") or 0.0
    with browser.installed():
        out = await run_pipeline(site.root, settings, engine, transport=httpx.MockTransport(web))
    b = out["stats"]["browser"]
    observed = (REGISTRY.get_sample_value("browser_render_duration_seconds_count") or 0.0) - (
        renders_before
    )
    limit = settings.browser.concurrency
    ctx.extra.update(max_renders=ctx.scale, renders=b["renders"], used=b["used"],
                     over_budget=b["over_budget"], render_ms_total=b["render_ms"],
                     render_ms_mean=round(b["render_ms"] / max(1, b["renders"]), 1),
                     max_concurrent_renders=browser.max_active, browser_concurrency=limit,
                     render_duration_metric_count=observed)  # fmt: skip
    ctx.check("job completed", out["job_status"] == "completed", out["status"])
    ctx.check("render budget respected", b["renders"] <= ctx.scale and browser.renders <= ctx.scale,
              f"{b['renders']} renders, max_renders {ctx.scale}")  # fmt: skip
    ctx.check("render budget was binding", b["over_budget"] > 0, b["over_budget"])
    ctx.check("render concurrency bounded", browser.max_active <= limit,
              f"max {browser.max_active} at once, browser.concurrency {limit}")  # fmt: skip
    ctx.check("browser usage measured", b["render_ms"] > 0 and observed == b["renders"],
              f"render_ms {b['render_ms']}, histogram count {observed}")  # fmt: skip


async def provider_degradation(ctx: Ctx, workers: int = 2) -> None:
    """Phase A: the primary search provider fails (5xx, timeouts, 429) and a backup
    answers. Phase B: both fail. ``scale`` companies per phase."""
    n = ctx.scale
    sites = ctx.sites(2 * n, 8)
    web = SimWeb(sites)
    search = SimSearch(web)
    settings = tuned(ctx.settings(), external={"enabled": True})
    s = settings.search
    engine = ctx.engine(settings, workers * (settings.crawl.concurrency + 2))
    trips = _circuit_trips("search")
    with mock.patch.object(crawl_module, "make_search", two_providers):
        search.modes = {"exa": "down", "backup": "ok"}
        await ctx.submit(engine, settings, sites[:n])
        seconds_a = await ctx.drain(settings, engine, workers, n, web, search)
        calls_a = dict(search.calls)
        search.modes = {"exa": "down", "backup": "down"}
        await ctx.submit(engine, settings, sites[n:])
        seconds_b = await ctx.drain(settings, engine, workers, 2 * n, web, search)
    trips = _circuit_trips("search") - trips
    external = [st.get("external") or {} for st in ctx.crawl_stats()]
    phase_a, phase_b = external[:n], external[n:]
    jobs = await asyncio.to_thread(ctx.jobs)
    primary_a = sum(v for k, v in calls_a.items() if k.startswith("exa:"))
    per_crawl_cap = s.circuit_failure_threshold * (s.max_retries + 1)
    ctx.extra.update(companies_per_phase=n, jobs=jobs, circuit_trips=trips,
                     phase_a_seconds=round(seconds_a, 2), phase_b_seconds=round(seconds_b, 2),
                     calls_phase_a=calls_a, calls_total=dict(search.calls),
                     phase_a_providers=Counter(p for e in phase_a for p in e.get("providers", {})),
                     phase_b_search_errors=sum(1 for e in phase_b
                                               if e.get("search_error")))  # fmt: skip
    ctx.check("all jobs completed despite search failures", jobs == {"completed": 2 * n}, jobs)
    ctx.check("search circuit breaker opened", trips >= 1, f"{trips} trips")
    ctx.check("failover to the backup provider",
              all(e.get("providers", {}).get("backup") for e in phase_a),
              [e.get("providers") for e in phase_a])  # fmt: skip
    ctx.check("breaker bounds calls to the failing provider", primary_a <= n * per_crawl_cap,
              f"{primary_a} calls for {n} crawls, cap {per_crawl_cap} per crawl")  # fmt: skip
    ctx.check("total outage recorded, crawl not collapsed",
              all(e.get("search_error") for e in phase_b), len(phase_b))  # fmt: skip


class Faults:
    """Database pressure on one engine: ``latency`` seconds added to every statement
    (a slow network), and ``limit`` real server-side statement timeouts, one every
    ``every`` statements (``pg_sleep`` past ``statement_timeout``)."""

    def __init__(self, every: int, limit: int, latency: float) -> None:
        self.every, self.limit, self.latency = every, limit, latency
        self.injected = 0
        self._count = itertools.count(1)
        self._lock = threading.Lock()

    def install(self, engine: Engine) -> None:
        @event.listens_for(engine, "before_cursor_execute")
        def _pressure(conn: Any, cursor: Any, *_: Any) -> None:
            if self.latency:
                time.sleep(self.latency)
            with self._lock:
                fire = next(self._count) % self.every == 0 and self.injected < self.limit
                self.injected += fire
            if fire:
                try:
                    cursor.execute("SELECT pg_sleep(5)")  # cancelled by statement_timeout
                except Exception as exc:  # noqa: BLE001 - re-raised as SQLAlchemy would
                    # Raised from a hook, the driver error is not wrapped: wrap it as
                    # SQLAlchemy wraps the error of a statement that timed out.
                    raise OperationalError("SELECT pg_sleep(5)", None, exc) from exc


async def db_pressure(ctx: Ctx, workers: int = 4) -> None:
    """``scale`` companies, 4 workers sharing a 2-connection pool, +1 ms per
    statement, and 3 statement timeouts injected mid-run."""
    sites = ctx.sites(ctx.scale, 20)
    web = SimWeb(sites, latency=0.01, jitter=0.01)
    settings = tuned(ctx.settings(), database={"pool_size": 2, "max_overflow": 0,
                                               "pool_timeout": 120.0,
                                               "statement_timeout_ms": 500})  # fmt: skip
    engine = ctx.engine(settings)  # the configured tiny pool, not the worker's larger one
    faults = Faults(every=max(200, ctx.scale * 60), limit=3, latency=0.001)
    faults.install(engine)
    await ctx.submit(engine, settings, sites)
    seconds = await ctx.drain(settings, engine, workers, len(sites), web)
    jobs = await asyncio.to_thread(ctx.jobs)
    with ctx.observer.connect() as conn:
        reclaimed = conn.execute(text(
            "SELECT count(*) FROM crawl_event e JOIN crawl_job j ON j.id = e.job_id"
            " WHERE j.root_url LIKE :p AND e.event = 'reclaimed'"
        ), {"p": ctx.pattern}).scalar_one()  # fmt: skip
    mem = memory()
    ctx.extra.update(companies=len(sites), workers=workers, pool_size=2, max_overflow=0,
                     statement_timeout_ms=500, injected_timeouts=faults.injected,
                     reclaimed_jobs=reclaimed, jobs=jobs, seconds=round(seconds, 2))  # fmt: skip
    ctx.check("injected statement timeouts happened", faults.injected == faults.limit,
              faults.injected)  # fmt: skip
    ctx.check("all jobs completed (no deadlock, faults recovered)",
              jobs == {"completed": len(sites)}, jobs)  # fmt: skip
    ctx.check("memory bounded", mem["peak_mb"] < 2048, f"{mem['peak_mb']} MB peak")


async def queue_pressure(ctx: Ctx, workers: int = 2) -> None:
    """``scale`` jobs through the API with api.max_queued_jobs = scale / 4."""
    from fastapi.testclient import TestClient  # fastapi is a dependency; its client needs httpx

    from .api import create_app

    n = ctx.scale
    limit = max(2, n // 4)
    sites = ctx.sites(n, 5)
    web = SimWeb(sites)
    # Backpressure is what is measured here, not the per-client rate limit.
    settings = tuned(ctx.settings(),
                     api={"max_queued_jobs": limit, "rate_limit_per_minute": 10**9})  # fmt: skip
    engine = ctx.engine(settings, workers * (settings.crawl.concurrency + 2))
    seen: dict[str, Any] = {"rejected": 0, "retry_after": set(), "max_queued": 0,
                            "ids": set(), "replays_ok": 0}  # fmt: skip

    def submit() -> None:
        client = TestClient(create_app(settings, engine))
        for i, site in enumerate(sites):
            key = f"{ctx.prefix}-{i}"
            while True:
                r = client.post("/v1/crawls", json={"root_url": site.root},
                                headers={"Idempotency-Key": key})  # fmt: skip
                if r.status_code != 429:
                    break
                seen["rejected"] += 1
                seen["retry_after"].add(r.headers.get("retry-after"))
                time.sleep(0.1)  # a real client waits Retry-After (60 s)
            job_id = r.json()["crawl_id"]
            seen["ids"].add(job_id)
            again = client.post("/v1/crawls", json={"root_url": site.root},
                                headers={"Idempotency-Key": key})  # fmt: skip
            seen["replays_ok"] += again.status_code == 200 and again.json()["crawl_id"] == job_id
            seen["max_queued"] = max(seen["max_queued"], ctx.jobs().get("queued", 0))

    drain = asyncio.create_task(ctx.drain(settings, engine, workers, n, web))
    await asyncio.to_thread(submit)
    await drain
    jobs = await asyncio.to_thread(ctx.jobs)
    ctx.extra.update(submitted=n, max_queued_jobs=limit, workers=workers,
                     rejected_429=seen["rejected"], retry_after=sorted(seen["retry_after"]),
                     max_queued_seen=seen["max_queued"], jobs=jobs)  # fmt: skip
    ctx.check(
        "backpressure observed (429 with Retry-After)",
        seen["rejected"] > 0 and None not in seen["retry_after"],
        seen["rejected"],
    )
    ctx.check("queue bound held", seen["max_queued"] <= limit,
              f"max {seen['max_queued']} queued, limit {limit}")  # fmt: skip
    ctx.check("idempotent resubmission", seen["replays_ok"] == n, seen["replays_ok"])
    ctx.check("no lost or duplicate jobs", len(seen["ids"]) == n and jobs == {"completed": n}, jobs)


def crash_web(prefix: str, pages: int) -> tuple[Site, SimWeb]:
    site = Site(f"{prefix}-c0.example", "Crashco Systems", pages)
    return site, SimWeb([site], latency=0.01, jitter=0.01)


async def crash_recovery(ctx: Ctx) -> None:
    """A worker process is killed (TerminateProcess / SIGKILL) a third of the way
    into a ``scale``-page crawl; a second worker takes over after the lease expires."""
    site, web = crash_web(ctx.prefix, ctx.scale)
    settings = ctx.settings()
    engine = ctx.engine(settings, settings.crawl.concurrency + 2)
    job, _ = await asyncio.to_thread(JobStore(engine).create, site.root, settings.urls)
    child = doomed_worker(ctx, ctx.scale)

    def parsed() -> tuple[int, int | None]:
        with ctx.observer.connect() as conn:
            row = conn.execute(text(
                "SELECT count(*) FILTER (WHERE u.state = 'processed'), max(c.id) FROM crawl c"
                " LEFT JOIN crawl_url u ON u.crawl_id = c.id WHERE c.job_id = :j"
            ), {"j": job.id}).one()  # fmt: skip
        return int(row[0]), row[1]

    target, deadline = ctx.scale // 3, time.monotonic() + 300
    while child.poll() is None and time.monotonic() < deadline:
        if (await asyncio.to_thread(parsed))[0] >= target:
            break
        await asyncio.sleep(0.1)
    child.kill()  # no cleanup: leases stay held, as after a crash
    child.wait()
    killed = time.perf_counter()
    done_at_kill, crawl_id = await asyncio.to_thread(parsed)
    with ctx.observer.connect() as conn:
        held = list(conn.execute(text(
            "SELECT url_id FROM crawl_url WHERE crawl_id = :c AND state = ANY(:s)"
        ), {"c": crawl_id, "s": IN_FLIGHT}).scalars())  # fmt: skip
    await ctx.drain(settings, engine, 1, 1, web)
    recovery = time.perf_counter() - killed
    with ctx.observer.connect() as conn:
        lost = conn.execute(text(
            "SELECT count(*) FROM crawl_url WHERE crawl_id = :c AND url_id = ANY(:ids)"
            " AND NOT state = ANY(:final)"
        ), {"c": crawl_id, "ids": held, "final": FINAL_URLS}).scalar_one()  # fmt: skip
        events = list(conn.execute(text(
            "SELECT event FROM crawl_event WHERE job_id = :j ORDER BY id"
        ), {"j": job.id}).scalars())  # fmt: skip
    jobs = await asyncio.to_thread(ctx.jobs)
    ctx.extra.update(site_pages=ctx.scale, processed_at_kill=done_at_kill,
                     in_flight_at_kill=len(held), recovery_seconds=round(recovery, 2),
                     job_events=events, jobs=jobs)  # fmt: skip
    ctx.check("worker killed mid-crawl", 0 < done_at_kill < ctx.scale and bool(held),
              f"{done_at_kill} processed, {len(held)} URLs leased at the kill")  # fmt: skip
    ctx.check("job reclaimed after lease expiry", "reclaimed" in events, events)
    ctx.check("crawl resumed and completed", jobs == {"completed": 1}, jobs)
    ctx.check("no leased URL lost", lost == 0, f"{lost} of {len(held)} still open")


def doomed_worker(ctx: Ctx, pages: int) -> subprocess.Popen[bytes]:
    """A worker process for ``crash_web(ctx.prefix, pages)``, for the caller to kill."""
    spec = Path(ctx.raw) / "crash-spec.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps({"database_url": ctx.url, "raw": ctx.raw, "prefix": ctx.prefix,
                                "pages": pages}), encoding="utf-8")  # fmt: skip
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    return subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments
        [sys.executable, "-m", "gtm_crawler.loadtest", "_worker", str(spec)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


async def _crash_worker(spec_path: str) -> None:
    """The worker process ``crash_recovery`` kills."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    settings = base_settings(spec["database_url"], spec["raw"])
    _, web = crash_web(spec["prefix"], spec["pages"])
    engine = make_engine(settings.database, pool_size=settings.crawl.concurrency + 2)
    instrument_engine(engine)
    logging.getLogger("gtm_crawler").setLevel(logging.ERROR)
    await run_worker(settings, engine, owner=f"{spec['prefix']}-doomed",
                     transport=httpx.MockTransport(web))  # fmt: skip


@dataclass(frozen=True)
class Scenario:
    run: Callable[[Ctx], Awaitable[None]]
    scale: int  # default (full) scale
    small: int  # scale for the default test suite


SCENARIOS: dict[str, Scenario] = {
    "deep_single_domain": Scenario(deep_single_domain, 3000, 60),
    "many_domain": Scenario(many_domain, 50, 4),
    "browser_heavy": Scenario(browser_heavy, 300, 20),
    "provider_degradation": Scenario(provider_degradation, 10, 2),
    "db_pressure": Scenario(db_pressure, 20, 3),
    "queue_pressure": Scenario(queue_pressure, 40, 8),
    "crash_recovery": Scenario(crash_recovery, 1500, 60),
}


async def run_scenario(
    name: str, scale: int | None = None, database_url: str = DEFAULT_DATABASE_URL,
    *, fresh: bool = True,
) -> dict[str, Any]:  # fmt: skip
    """Run one scenario; its measurements, checks and overall ``passed``.

    ``fresh`` recreates the database first (see ``scratch_database``); otherwise
    the run only reads back rows of its own (a random host prefix)."""
    scenario = SCENARIOS[name]
    if fresh:
        await asyncio.to_thread(scratch_database, database_url)
    observer = create_engine(database_url, pool_size=2, max_overflow=0)
    counter, timer = _LogCounter(), StageTimer()
    with tempfile.TemporaryDirectory(prefix="gtm-load-", ignore_cleanup_errors=True) as raw:
        ctx = Ctx(name, scale or scenario.scale, database_url, raw, observer)
        db_count, db_seconds = _db_totals()
        cpu, started = time.process_time(), time.perf_counter()
        try:
            with counter.attached(), timer.installed():
                async with Probes(observer, ctx.pattern) as probes:
                    try:
                        await asyncio.wait_for(scenario.run(ctx), ctx.timeout)
                    except TimeoutError:
                        ctx.check("finished within the time limit", False, f"{ctx.timeout} s")
            wall = time.perf_counter() - started
            cpu = time.process_time() - cpu
            db_count, db_seconds = (a - b for a, b in zip(_db_totals(), (db_count, db_seconds),
                                                            strict=True))  # fmt: skip
            stored = await asyncio.to_thread(summary, observer, ctx.pattern)
            host = await asyncio.to_thread(machine, observer)
        finally:
            for engine in ctx.engines:
                engine.dispose()
            observer.dispose()
    in_flight = sum(stored["url_states"].get(s, 0) for s in IN_FLIGHT)
    ctx.check("no URL left leased or in flight", in_flight == 0, in_flight)
    ctx.check("no duplicate pages", stored["duplicate_pages"] == 0, stored["duplicate_pages"])
    ctx.check("no duplicate evidence", stored["duplicate_evidence"] == 0,
              stored["duplicate_evidence"])  # fmt: skip
    return {
        "scenario": name, "scale": ctx.scale, "passed": all(c["ok"] for c in ctx.checks),
        "wall_seconds": round(wall, 2),
        "pages_per_second": round(stored["unique_pages"] / wall, 1),
        "cpu_seconds": round(cpu, 2), "cpu_cores_busy": round(cpu / wall, 2),
        "memory": memory(), "stages": timer.report(),
        "db": {"statements": int(db_count), "seconds": round(db_seconds, 2),
               "mean_ms": round(db_seconds / db_count * 1000, 3) if db_count else None,
               "scope": "statements of the scenario's engines in this process"},
        "queues": {"max_queued_jobs": probes.max_queued, "max_running_jobs": probes.max_running,
                   "max_open_urls": probes.max_open_urls},
        "event_loop_lag_ms": percentiles(probes.lag_ms),
        "stored": stored, "log_warnings_errors": dict(counter.counts.most_common(15)),
        "details": ctx.extra, "checks": ctx.checks, "machine": host,
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_worker"]:
        asyncio.run(_crash_worker(argv[1]))
        return 0
    parser = argparse.ArgumentParser(
        prog="python -m gtm_crawler.loadtest",
        description="Run a load scenario against a simulated web.",
    )
    parser.add_argument("scenario", choices=[*sorted(SCENARIOS), "all"])
    parser.add_argument("--scale", type=int, help="size of the scenario (see SCENARIOS)")
    parser.add_argument("--out", type=Path, help="write the result(s) as JSON here")
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help="scratch database, dropped and recreated (name must contain 'load')",
    )
    args = parser.parse_args(argv)
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    for name in names:
        result = asyncio.run(run_scenario(name, args.scale, args.database_url))
        results.append(result)
        for check in result["checks"]:
            print(f"{'PASS' if check['ok'] else 'FAIL'}  {check['name']}: {check['detail']}")
        print(
            f"{name} scale={result['scale']}: {result['wall_seconds']} s, "
            f"{result['pages_per_second']} pages/s -> {'PASS' if result['passed'] else 'FAIL'}"
        )
    if args.out:
        out = results[0] if len(results) == 1 else results
        args.out.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8", newline="\n")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
