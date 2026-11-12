"""Phase 13: retry policy by error class, dead-letter queue, poison work, backpressure."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update

from gtm_crawler.api import create_app
from gtm_crawler.config import UrlSettings
from gtm_crawler.db.models import Crawl, CrawlJob, CrawlUrl, DeadLetterItem
from gtm_crawler.db.store import CrawlStore
from gtm_crawler.deadletter import DeadLetterError, DeadLetters
from gtm_crawler.domain import JobState
from gtm_crawler.fetch.http import FetchError, FetchResult, retry_class
from gtm_crawler.frontier import FrontierEntry
from gtm_crawler.jobs import JobStore, QueueFull
from gtm_crawler.metrics import _QUEUE, instrument_engine

from .test_crawl import SITE, FakeSite, crawl_urls, html, run, settings_for

URLS = UrlSettings()


def result(status: int | None = None, error: FetchError | None = None) -> FetchResult:
    return FetchResult(url="https://a.com/", final_url="https://a.com/", status=status,
                       error=error)  # fmt: skip


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (429, None, "rate_limited"), (503, None, "rate_limited"), (500, None, "transient"),
        (504, None, "transient"), (404, None, None), (403, None, None),
        (None, FetchError.DNS_FAILURE, "dns"), (None, FetchError.READ_TIMEOUT, "transient"),
        (None, FetchError.CONNECTION_ERROR, "transient"), (None, FetchError.BLOCKED, None),
        (None, FetchError.TOO_LARGE, None), (None, FetchError.TLS_ERROR, None),
    ],
)  # fmt: skip
def test_retry_policy_by_error_class(
    status: int | None, error: FetchError | None, expected: str | None
) -> None:
    assert retry_class(result(status, error)) == expected


def items(engine: Engine, **where: Any) -> list[Any]:
    query = select(DeadLetterItem).order_by(DeadLetterItem.id)
    for key, value in where.items():
        query = query.where(getattr(DeadLetterItem, key) == value)
    with engine.connect() as conn:
        return list(conn.execute(query).all())


ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html("Acme", '<a href="/flaky">F</a><a href="/gone">G</a>'
                                          '<a href="/poison">P</a><a href="/ok">OK</a>')),
    f"{SITE}/flaky": 500,
    f"{SITE}/poison": ("text/html", html("Poison", "<p>Crashes the parser.</p>")),
    f"{SITE}/ok": ("text/html", html("OK", "<p>Fine.</p>")),
}  # fmt: skip


@pytest.mark.db
def test_failing_and_poison_urls_are_dead_lettered_and_the_crawl_goes_on(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gtm_crawler.crawl as crawl_module
    from gtm_crawler.parse import parse_page as real

    def parse(body: bytes, url: str, *args: Any) -> Any:
        if url.endswith("/poison"):
            raise RecursionError("maximum recursion depth exceeded")
        return real(body, url, *args)

    monkeypatch.setattr(crawl_module, "parse_page", parse)
    summary = run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    assert summary.status == "completed" and summary.stats["dead_lettered"] == 2
    rows = crawl_urls(engine, summary.crawl_id)
    assert rows[f"{SITE}/ok"].state == "processed"
    # Not worth retrying: failed for good, no dead letter.
    assert (rows[f"{SITE}/gone"].state, rows[f"{SITE}/gone"].skip_reason) == ("failed", "http_404")
    assert rows[f"{SITE}/flaky"].state == rows[f"{SITE}/poison"].state == "dlq"
    dead = {r.error_class: r for r in items(engine, crawl_id=summary.crawl_id)}
    assert set(dead) == {"http_500", "RecursionError"}
    assert dead["http_500"].payload == {"retry_class": "transient", "status": 500}
    assert dead["RecursionError"].attempts == 1 and "recursion" in dead["RecursionError"].error


def claim_setup(engine: Engine, tmp_path: Path) -> tuple[CrawlStore, int]:
    store = CrawlStore(engine)
    _, crawl_id = store.start_crawl(f"{SITE}/", {})
    store.record_queued(crawl_id, [FrontierEntry(f"{SITE}/crash", 1, 1.0, "link")])
    return store, crawl_id


@pytest.mark.db
def test_a_url_that_keeps_killing_its_worker_is_dead_lettered(
    engine: Engine, tmp_path: Path
) -> None:
    store, crawl_id = claim_setup(engine, tmp_path)
    # A lease of -1 s has already expired: as if each worker died holding it.
    assert len(store.claim(crawl_id, "w1", -1, 5, max_lease_expiries=2)) == 1
    assert len(store.claim(crawl_id, "w2", -1, 5, max_lease_expiries=2)) == 1  # 1 lost lease
    assert store.claim(crawl_id, "w3", -1, 5, max_lease_expiries=2) == []  # 2: dead-lettered
    row = crawl_urls(engine, crawl_id)[f"{SITE}/crash"]
    assert (row.state, row.skip_reason) == ("dlq", "lease_expired")
    [item] = items(engine, crawl_id=crawl_id)
    assert item.error_class == "lease_expired" and item.attempts == 2
    assert item.payload == {"lease_expiries": 2}
    # Writing it again (at-least-once) keeps one open item.
    with engine.begin() as conn:
        from gtm_crawler import deadletter

        deadletter.record(conn, "url", "lease_expired", crawl_id=crawl_id, url_id=item.url_id)
    assert len(items(engine, crawl_id=crawl_id)) == 1


@pytest.mark.db
def test_recovery_after_a_crash_counts_lost_leases(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = claim_setup(engine, tmp_path)
    store.claim(crawl_id, "dead-worker", 600, 5)
    assert store.recover(crawl_id, max_lease_expiries=3) == 0
    with engine.connect() as conn:
        state, lost = conn.execute(
            select(CrawlUrl.state, CrawlUrl.lease_expiries).where(CrawlUrl.crawl_id == crawl_id)
        ).one()  # fmt: skip
    assert (state, lost) == ("eligible", 1)
    store.claim(crawl_id, "dead-worker", 600, 5)  # and its successor dies too
    assert store.recover(crawl_id, 2) == 1  # second lost lease: dead-lettered


@pytest.mark.db
def test_requeue_and_discard(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = claim_setup(engine, tmp_path)
    store.claim(crawl_id, "w", 600, 5)
    store.dead_letter_url(crawl_id, f"{SITE}/crash", "http_503", "HTTP 503", 2)
    [item] = items(engine, crawl_id=crawl_id)
    dead = DeadLetters(engine)
    assert dead.requeue(item.id, URLS) == {"entity": "url", "crawl_id": crawl_id}
    row = crawl_urls(engine, crawl_id)[f"{SITE}/crash"]
    assert row.state == "eligible"
    with engine.connect() as conn:
        assert conn.execute(select(CrawlUrl.attempts).where(
            CrawlUrl.crawl_id == crawl_id)).scalar_one() == 0  # fmt: skip
    with pytest.raises(DeadLetterError, match="already requeued"):
        dead.discard(item.id)

    # A finished crawl cannot take it back.
    store.claim(crawl_id, "w", 600, 5)
    store.dead_letter_url(crawl_id, f"{SITE}/crash", "http_503", "HTTP 503", 2)
    second = items(engine, crawl_id=crawl_id)[-1]
    with engine.begin() as conn:
        conn.execute(update(Crawl).where(Crawl.id == crawl_id).values(status="completed"))
    with pytest.raises(DeadLetterError, match="is completed"):
        dead.requeue(second.id, URLS)
    dead.discard(second.id, actor="ops")
    discarded = dead.get(second.id)
    assert (discarded.resolution, discarded.resolved_by) == ("discarded", "ops")
    with pytest.raises(DeadLetterError, match="no dead-letter item"):
        dead.discard(10**12)


@pytest.mark.db
def test_abandoned_and_crashed_jobs_are_dead_lettered(engine: Engine) -> None:
    jobs = JobStore(engine)
    job, _ = jobs.create("https://dlq-a.com/", URLS)
    for owner in ("w1", "w2"):
        assert jobs.claim(owner, -1, max_attempts=2) is not None  # each worker "dies"
    assert jobs.claim("w3", -1, max_attempts=2) is None
    assert jobs.get(job.id).status == JobState.FAILED
    [item] = items(engine, job_id=job.id)
    assert (item.entity, item.error_class, item.attempts) == ("job", "abandoned", 2)

    requeued = DeadLetters(engine).requeue(item.id, URLS, actor="ops")
    assert requeued["entity"] == "job" and requeued["created"]
    assert requeued["job_id"] != job.id and jobs.get(requeued["job_id"]).status == "queued"
    assert DeadLetters(engine).get(item.id).payload["requeued_as_job"] == (
        requeued["job_id"])  # fmt: skip

    claimed = jobs.claim("w4", 60, 5, job_id=requeued["job_id"])
    assert jobs.finish(claimed.id, "w4", JobState.FAILED, None, "KeyError: 'x'", "KeyError")
    [crashed] = items(engine, job_id=claimed.id)
    assert crashed.error_class == "KeyError" and crashed.payload["root_url"] == "https://dlq-a.com/"


@pytest.mark.db
def test_new_jobs_are_refused_while_the_queue_is_full(engine: Engine, tmp_path: Path) -> None:
    jobs = JobStore(engine)
    with engine.connect() as conn:
        waiting = conn.execute(
            select(func.count()).where(CrawlJob.status == "queued")
        ).scalar_one()  # fmt: skip
    first, created = jobs.create("https://bp-a.com/", URLS, max_queued=waiting + 1)
    assert created
    with pytest.raises(QueueFull):
        jobs.create("https://bp-b.com/", URLS, max_queued=waiting + 1)
    # Asking again for a job that exists is not refused.
    assert jobs.create("https://bp-a.com/", URLS, max_queued=waiting + 1) == (first, False)

    settings = settings_for(tmp_path)
    settings = replace(settings, api=replace(settings.api, max_queued_jobs=waiting + 1))
    api = TestClient(create_app(settings, engine))
    refused = api.post("/v1/crawls", json={"root_url": "https://bp-c.com"})
    assert refused.status_code == 429 and refused.headers["retry-after"] == "60"
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.id == first.id).values(status="failed"))


@pytest.mark.db
def test_dead_letter_api_and_queue_metrics(engine: Engine, tmp_path: Path) -> None:
    store, crawl_id = claim_setup(engine, tmp_path)
    store.claim(crawl_id, "w", 600, 5)
    store.dead_letter_url(crawl_id, f"{SITE}/crash", "read_timeout", "timed out", 2)
    api = TestClient(create_app(settings_for(tmp_path), engine))
    listed = api.get("/v1/dead-letters").json()["items"]
    mine = next(i for i in listed if i["crawl_id"] == crawl_id)
    assert mine["url"] == f"{SITE}/crash" and mine["error_class"] == "read_timeout"
    assert api.post(f"/v1/dead-letters/{mine['id']}/discard").json()["resolution"] == "discarded"
    assert api.post(f"/v1/dead-letters/{mine['id']}/requeue").status_code == 409
    assert all(i["id"] != mine["id"] for i in api.get("/v1/dead-letters").json()["items"])

    instrument_engine(engine)
    names = {m.name for m in _QUEUE.collect()}
    assert names == {"queue_jobs", "queue_urls", "dead_letter_open", "database_up"}
    assert "dead_letter_items_total" in api.get("/metrics").text
