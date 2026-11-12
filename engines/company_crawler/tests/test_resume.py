"""The durable frontier: leases, retries, and crawls that stop and resume."""

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, update

from gtm_crawler.config import ConfigError
from gtm_crawler.crawl import CrawlSummary, config_snapshot, crawl
from gtm_crawler.db.models import Crawl, CrawlUrl
from gtm_crawler.db.store import CrawlStore
from gtm_crawler.domain import IN_FLIGHT_URL_STATES, OPEN_URL_STATES, UrlState
from gtm_crawler.frontier import FrontierEntry
from gtm_crawler.throttle import HostThrottle

from .helpers import response
from .test_crawl import SITE, FakeSite, crawl_urls, settings_for

pytestmark = pytest.mark.db


def queued_crawl(
    engine: Engine, tmp_path: Path, scores: dict[str, float]
) -> tuple[CrawlStore, int]:
    store = CrawlStore(engine)
    _, crawl_id = store.start_crawl(f"{SITE}/", config_snapshot(settings_for(tmp_path)))
    store.record_queued(crawl_id, [FrontierEntry(f"{SITE}{p}", 1, s, "link")
                                   for p, s in scores.items()])  # fmt: skip
    return store, crawl_id


def test_claim_order_and_boost(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = queued_crawl(engine, tmp_path, {"/blog": 1, "/pricing": 5, "/about": 3})
    store.boost(crawl_id, {f"{SITE}/blog": 10})
    claimed = store.claim(crawl_id, "w1", 60, 10)
    assert [e.url for e, _ in claimed] == [f"{SITE}/blog", f"{SITE}/pricing", f"{SITE}/about"]
    assert all(attempt == 1 for _, attempt in claimed)
    assert store.claim(crawl_id, "w1", 60, 10) == []  # all leased


def test_concurrent_claims_never_share_a_url(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = queued_crawl(engine, tmp_path, {f"/p{i}": i for i in range(40)})
    got: list[list[str]] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        mine = []
        while batch := store.claim(crawl_id, name, 60, 3):
            mine += [e.url for e, _ in batch]
        with lock:
            got.append(mine)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    urls = [u for mine in got for u in mine]
    assert len(urls) == len(set(urls)) == 40


def test_expired_lease_and_due_retry_are_claimable(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = queued_crawl(engine, tmp_path, {"/a": 2, "/b": 1})
    store.claim(crawl_id, "dead-worker", 60, 2)
    store.set_state(crawl_id, f"{SITE}/b", "retry", "timeout", retry_in=3600)
    assert store.claim(crawl_id, "w2", 60, 5) == []
    assert 3500 < (store.seconds_to_next_retry(crawl_id) or 0) <= 3600
    with engine.begin() as conn:  # time passes: the lease expires, the retry is due
        conn.execute(update(CrawlUrl).where(CrawlUrl.crawl_id == crawl_id).values(
            lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1),
            next_attempt_at=func.now()))  # fmt: skip
    claimed = store.claim(crawl_id, "w2", 60, 5)
    assert [(e.url, n) for e, n in claimed] == [(f"{SITE}/a", 2), (f"{SITE}/b", 2)]


def test_retryable_failure_is_retried_later(engine: Engine, tmp_path: Path) -> None:
    calls: dict[str, int] = {}

    def site(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls[url] = calls.get(url, 0) + 1
        if url == f"{SITE}/robots.txt":
            return response(404)
        if calls[url] == 1:
            return response(503, **{"Retry-After": "0"})
        return response(200, b"<html><body><p>Back again.</p></body></html>",
                        **{"Content-Type": "text/html"})  # fmt: skip

    settings = settings_for(tmp_path, max_url_attempts=2, retry_delay_seconds=0.0)
    summary = asyncio.run(crawl(f"{SITE}/", settings, engine, transport=httpx.MockTransport(site)))
    assert summary.status == "completed" and summary.stats["retries_scheduled"] == 1
    row = crawl_urls(engine, summary.crawl_id)[f"{SITE}/"]
    assert row.state == "processed"
    with engine.connect() as conn:
        attempts = conn.execute(
            select(CrawlUrl.attempts).where(CrawlUrl.crawl_id == summary.crawl_id)
        ).scalar_one()  # fmt: skip
    assert attempts == 2


def test_host_backoff_delays_every_request() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    throttle = HostThrottle(2, 0.0, clock=lambda: now[0], sleep=sleep)
    throttle.backoff("acme.example", 30)

    async def run() -> None:
        async with throttle.slot("acme.example"):
            pass
        async with throttle.slot("other.example"):
            pass

    asyncio.run(run())
    assert slept == [30]


class StoppingSite(FakeSite):
    """Calls ``on_request`` with the number of page requests so far."""

    def __init__(self, on_request: Any) -> None:
        super().__init__()
        self.on_request = on_request

    def __call__(self, request: httpx.Request) -> httpx.Response:
        out = super().__call__(request)
        if not str(request.url).endswith((".txt", ".xml")):
            self.on_request(len([u for u in self.requested if not u.endswith((".txt", ".xml"))]))
        return out


def complete_urls(engine: Engine, crawl_id: int) -> set[str]:
    return {u for u, r in crawl_urls(engine, crawl_id).items() if r.state == "processed"}


def test_paused_crawl_resumes_where_it_stopped(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    reference = asyncio.run(crawl(f"{SITE}/", settings, engine,
                                  transport=httpx.MockTransport(FakeSite())))  # fmt: skip

    async def paused() -> CrawlSummary:
        stop = asyncio.Event()
        site = StoppingSite(lambda n: stop.set() if n == 3 else None)
        return await crawl(f"{SITE}/", settings, engine, transport=httpx.MockTransport(site),
                           stop=stop)  # fmt: skip

    first = asyncio.run(paused())
    assert first.status == "paused" and first.stats["stop_reason"] == "paused"
    before = complete_urls(engine, first.crawl_id)
    assert 0 < len(before) < len(complete_urls(engine, reference.crawl_id))

    site = FakeSite()
    resumed = asyncio.run(crawl(f"{SITE}/", settings, engine, transport=httpx.MockTransport(site),
                                resume=first.crawl_id))  # fmt: skip
    assert resumed.crawl_id == first.crawl_id and resumed.status == "completed"
    # Nothing done before the pause is fetched again, and the result is the same.
    assert not before & set(site.requested)
    assert complete_urls(engine, first.crawl_id) == complete_urls(engine, reference.crawl_id)
    assert resumed.stats["fetches"] == reference.stats["fetches"]
    assert resumed.stats["pages_parsed"] == reference.stats["pages_parsed"]
    # Finished crawls are returned as they are.
    again = asyncio.run(crawl(f"{SITE}/", settings, engine, resume=first.crawl_id,
                              transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    assert again.status == "completed"


def test_crashed_crawl_is_resumable(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def crash() -> None:
        task: asyncio.Task[CrawlSummary] | None = None

        def on_request(n: int) -> None:
            if n == 4 and task is not None:
                task.cancel()  # the worker dies mid-crawl

        site = StoppingSite(on_request)
        task = asyncio.create_task(crawl(f"{SITE}/", settings, engine,
                                         transport=httpx.MockTransport(site)))  # fmt: skip
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(crash())
    with engine.connect() as conn:
        crawl_id, status = conn.execute(
            select(Crawl.id, Crawl.status).order_by(Crawl.id.desc()).limit(1)
        ).one()  # fmt: skip
    assert status == "running"  # not failed: it can be resumed
    rows = crawl_urls(engine, crawl_id)
    assert not any(r.state in IN_FLIGHT_URL_STATES for r in rows.values())  # handed back
    resumed = asyncio.run(crawl(f"{SITE}/", settings, engine, resume=crawl_id,
                                transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    assert resumed.status == "completed"
    rows = crawl_urls(engine, crawl_id)
    assert all(r.state not in OPEN_URL_STATES for r in rows.values())
    reference = asyncio.run(crawl(f"{SITE}/", settings, engine,
                                  transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    assert complete_urls(engine, crawl_id) == complete_urls(engine, reference.crawl_id)


def test_resume_refuses_changed_parse_settings(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    async def paused() -> CrawlSummary:
        stop = asyncio.Event()
        stop.set()  # pause before the first page
        return await crawl(f"{SITE}/", settings, engine, stop=stop,
                           transport=httpx.MockTransport(FakeSite()))  # fmt: skip

    first = asyncio.run(paused())
    assert first.status == "paused"
    changed = replace(settings, parse=replace(settings.parse, max_blocks=10))
    with pytest.raises(ConfigError, match="parse"):
        asyncio.run(crawl(f"{SITE}/", changed, engine, resume=first.crawl_id,
                          transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    # Unchanged settings still resume it.
    done = asyncio.run(crawl(f"{SITE}/", settings, engine, resume=first.crawl_id,
                             transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    assert done.status == "completed"


def test_url_progress_is_idempotent_and_cannot_reopen_finished_urls(
    engine: Engine, tmp_path: Path
) -> None:
    store, crawl_id = queued_crawl(engine, tmp_path, {"/a": 1})
    url = f"{SITE}/a"
    store.claim(crawl_id, "w1", 60, 1)
    for state in ("fetching", "fetching", "fetched", "parsed"):
        store.advance(crawl_id, url, UrlState(state))
    assert crawl_urls(engine, crawl_id)[url].state == "parsed"
    store.set_state(crawl_id, url, UrlState.PROCESSED)
    store.advance(crawl_id, url, UrlState.FETCHING)  # a late write from a lost lease
    assert crawl_urls(engine, crawl_id)[url].state == "processed"


def test_a_deferred_url_is_promoted_when_admitted_later(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = queued_crawl(engine, tmp_path, {})
    deep = FrontierEntry(f"{SITE}/deep", 9, 1.0, "link", signals={"depth": 1.0})
    store.record_refused(crawl_id, [(deep, UrlState.DEFERRED, "max_depth")])
    store.record_refused(crawl_id, [(deep, UrlState.DEFERRED, "max_depth")])  # idempotent
    row = crawl_urls(engine, crawl_id)[f"{SITE}/deep"]
    assert (row.state, row.skip_reason) == ("deferred", "max_depth")
    shallow = FrontierEntry(f"{SITE}/deep", 1, 3.0, "sitemap", signals={"sitemap": 3.0})
    store.record_queued(crawl_id, [shallow])
    row = crawl_urls(engine, crawl_id)[f"{SITE}/deep"]
    assert (row.state, row.skip_reason, row.discovered_via) == ("eligible", None, "sitemap")
    # An admitted URL is never overwritten by a later insert.
    store.record_queued(crawl_id, [FrontierEntry(f"{SITE}/deep", 5, 0.0, "link")])
    assert crawl_urls(engine, crawl_id)[f"{SITE}/deep"].discovered_via == "sitemap"
