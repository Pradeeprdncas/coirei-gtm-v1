"""Crawl jobs and the worker: idempotent start, leases, pause/resume, crash recovery."""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, update

from gtm_crawler.config import Settings, UrlSettings, WorkerSettings
from gtm_crawler.db.models import Crawl, CrawlJob
from gtm_crawler.domain import JobState
from gtm_crawler.jobs import JobStore
from gtm_crawler.pipeline import run_job
from gtm_crawler.worker import run_worker

from .test_crawl import SITE, FakeSite, crawl_urls, settings_for

pytestmark = pytest.mark.db

URLS = UrlSettings()


def finish(jobs: JobStore, job_id: int) -> None:
    """Clean up a unit-test job so the company has no unfinished job left."""
    with jobs.engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.id == job_id).values(status="failed"))


def test_starting_a_crawl_is_idempotent(engine: Engine) -> None:
    jobs = JobStore(engine)
    first, created = jobs.create("https://jobs-a.com/", URLS, "key-a")
    assert created and first.status == "queued" and first.root_url == "https://jobs-a.com/"
    assert jobs.create("https://jobs-a.com/", URLS, "key-a") == (first, False)
    # Same company, no key: its unfinished job, not a second one.
    again, created = jobs.create("https://www.jobs-a.com/pricing", URLS)
    assert not created and again.id == first.id
    finish(jobs, first.id)
    fresh, created = jobs.create("https://jobs-a.com/", URLS)
    assert created and fresh.id != first.id
    finish(jobs, fresh.id)


def test_claims_are_exclusive_and_expired_leases_are_reclaimed(engine: Engine) -> None:
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-b.com/", URLS)
    claimed = jobs.claim("w1", 60, 5, job.id)
    assert claimed is not None and claimed.status == "running" and claimed.lease_owner == "w1"
    assert jobs.claim("w2", 60, 5, job.id) is None
    assert jobs.heartbeat(job.id, "w1", 60) == "continue"
    assert jobs.heartbeat(job.id, "w2", 60) is None  # not w2's
    with engine.begin() as conn:  # w1 dies; its lease runs out
        conn.execute(update(CrawlJob).where(CrawlJob.id == job.id)
                     .values(lease_expires_at=func.now()))  # fmt: skip
    taken = jobs.claim("w2", 60, 5, job.id)
    assert taken is not None and taken.lease_owner == "w2" and taken.attempts == 2
    assert jobs.heartbeat(job.id, "w1", 60) is None  # w1 has lost it
    assert not jobs.finish(job.id, "w1", "completed")
    assert not jobs.set_stage(job.id, "w1", JobState.FINALIZING)
    assert jobs.set_stage(job.id, "w2", JobState.FINALIZING)
    assert jobs.finish(job.id, "w2", "completed", {"ok": True})
    assert jobs.get(job.id).status == "completed"


def test_a_job_that_keeps_crashing_is_failed(engine: Engine) -> None:
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-c.com/", URLS)
    for attempt in range(2):  # each worker dies: its lease (-1 s) is expired at once
        assert jobs.claim(f"w{attempt}", -1, 2, job.id) is not None
    assert jobs.claim("w9", 60, 2, job.id) is None
    row = jobs.get(job.id)
    assert row.status == "failed" and "abandoned" in row.error_summary


def test_a_job_handed_back_at_shutdown_keeps_its_attempts(engine: Engine) -> None:
    """Deploys stop workers cleanly: a release is not a failed attempt."""
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-c2.com/", URLS)
    for attempt in range(5):
        assert jobs.claim(f"w{attempt}", 60, 2, job.id) is not None
        jobs.release(job.id, f"w{attempt}")
    assert jobs.get(job.id).attempts == 0
    assert jobs.claim("w8", -1, 2, job.id) is not None  # a crash still counts
    assert jobs.claim("w9", -1, 2, job.id) is not None
    assert jobs.claim("w10", 60, 2, job.id) is None
    assert jobs.get(job.id).status == "failed"


def test_pause_before_start_and_resume(engine: Engine) -> None:
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-d.com/", URLS)
    assert jobs.request_pause(job.id).status == "paused"
    assert jobs.claim("w1", 60, 5, job.id) is None
    assert jobs.resume(job.id).status == "queued"
    assert jobs.claim("w1", 60, 5, job.id) is not None
    finish(jobs, job.id)


def worker_settings(tmp_path: Path) -> Settings:
    return replace(settings_for(tmp_path),
                   worker=WorkerSettings(lease_seconds=0.3, poll_seconds=0.05))  # fmt: skip


class SlowSite(FakeSite):
    """Answers asynchronously, a little slowly, so heartbeats happen mid-crawl."""

    def __init__(self, on_page: Any = None) -> None:
        super().__init__()
        self.on_page = on_page

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        out = self(request)
        if self.on_page and not str(request.url).endswith((".txt", ".xml")):
            await asyncio.to_thread(self.on_page, len(self.requested))
        return out


def complete(engine: Engine, crawl_id: int) -> set[str]:
    return {u for u, r in crawl_urls(engine, crawl_id).items() if r.state == "processed"}


def job_crawl(engine: Engine, job_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(select(Crawl.id).where(Crawl.job_id == job_id)).scalar_one()


def test_worker_pauses_on_request_and_resumes(engine: Engine, tmp_path: Path) -> None:
    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create(f"{SITE}/", settings.urls)
    site = SlowSite(lambda n: jobs.request_pause(job.id) if n == 3 else None)

    ran = asyncio.run(run_worker(settings, engine, once=True,
                                 transport=httpx.MockTransport(site.handle)))  # fmt: skip
    assert ran == 1
    row = jobs.get(job.id)
    assert row.status == "paused" and not row.pause_requested and row.lease_owner is None
    crawl_id = job_crawl(engine, job.id)
    done_before = complete(engine, crawl_id)

    jobs.resume(job.id)
    site = SlowSite()
    asyncio.run(run_worker(settings, engine, once=True,
                           transport=httpx.MockTransport(site.handle)))  # fmt: skip
    row = jobs.get(job.id)
    assert row.status == "completed" and row.finished_at is not None
    assert row.result["crawl_id"] == crawl_id and job_crawl(engine, job.id) == crawl_id
    assert not done_before & set(site.requested)  # nothing fetched twice
    assert done_before < complete(engine, crawl_id)


def test_worker_resumes_a_job_whose_worker_died(engine: Engine, tmp_path: Path) -> None:
    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create(f"{SITE}/", settings.urls)
    claimed = jobs.claim("doomed", 30, 5)
    assert claimed is not None and claimed.id == job.id

    async def die() -> None:
        task: asyncio.Task[Any] | None = None
        site = SlowSite(lambda n: task.cancel() if n == 4 and task else None)
        task = asyncio.create_task(run_job(engine, settings, claimed, owner="doomed",
                                           transport=httpx.MockTransport(site.handle)))  # fmt: skip
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(die())
    assert jobs.get(job.id).status == "running"  # nobody finished it
    # Another worker finds nothing until the dead worker's lease expires...
    assert asyncio.run(run_worker(settings, engine, once=True)) == 0
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.id == job.id)
                     .values(lease_expires_at=func.now()))  # fmt: skip
    # ...then resumes the job where it stopped.
    site = SlowSite()
    assert asyncio.run(run_worker(settings, engine, once=True,
                                  transport=httpx.MockTransport(site.handle))) == 1  # fmt: skip
    row = jobs.get(job.id)
    assert row.status == "completed" and row.attempts == 2
    assert job_crawl(engine, job.id) == row.result["crawl_id"]


def test_a_failing_job_does_not_stop_the_worker(engine: Engine, tmp_path: Path) -> None:
    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-e.com/", settings.urls)

    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a handler")

    assert asyncio.run(run_worker(settings, engine, once=True,
                                  transport=httpx.MockTransport(broken))) == 1  # fmt: skip
    row = jobs.get(job.id)
    assert row.status == "failed" and row.lease_owner is None
    assert "RuntimeError: bug in a handler" in row.error_summary


def test_a_database_error_at_finish_does_not_stop_the_crawl_worker(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from gtm_crawler.worker import process_job

    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-e2.com/", settings.urls)
    claimed = jobs.claim("w1", 60, 5, job.id)

    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a handler")

    def down(*_a: object, **_kw: object) -> bool:
        raise OperationalError("UPDATE crawl_job", {}, Exception("connection closed"))

    monkeypatch.setattr(jobs, "finish", down)
    outcome = asyncio.run(process_job(settings, engine, jobs, claimed, "w1",
                                      transport=httpx.MockTransport(broken)))  # fmt: skip
    assert outcome == "interrupted"  # not raised: the lease expires and the job resumes
    assert jobs.get(job.id).status == "running"
    with engine.begin() as conn:  # not left for a later test's worker to resume
        conn.execute(update(CrawlJob).where(CrawlJob.id == job.id)
                     .values(status="failed", lease_owner=None, lease_expires_at=None))  # fmt: skip


def test_transition_table() -> None:
    from gtm_crawler.domain import can_move

    assert can_move("queued", "running") and can_move("running", "finalizing")
    assert can_move("running", "running")  # a lease reclaim
    assert not can_move("running", "completed")  # completion goes through FINALIZING
    assert not can_move("created", "running")
    for final in ("completed", "failed", "cancelled"):
        assert not any(can_move(final, s) for s in JobState)


def test_every_transition_is_recorded_and_history_is_immutable(engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    from gtm_crawler.db.models import CrawlEvent
    from gtm_crawler.domain import IllegalTransition

    jobs = JobStore(engine)
    job, _ = jobs.create("https://jobs-f.com/", URLS, actor="test")
    jobs.claim("w1", 60, 5, job.id)
    with pytest.raises(IllegalTransition):
        jobs.finish(job.id, "w1", "completed")  # not from RUNNING
    jobs.set_stage(job.id, "w1", JobState.FINALIZING)
    jobs.finish(job.id, "w1", "completed")
    history = [(e.event, e.from_state, e.to_state, e.actor) for e in jobs.events(job.id)]
    assert history == [
        ("created", None, "created", "test"),
        ("queued", "created", "queued", "test"),
        ("claimed", "queued", "running", "w1"),
        ("finalizing", "running", "finalizing", "w1"),
        ("completed", "finalizing", "completed", "w1"),
    ]
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(update(CrawlEvent).where(CrawlEvent.job_id == job.id).values(event="x"))


def test_worker_cancels_on_request(engine: Engine, tmp_path: Path) -> None:
    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create(f"{SITE}/", settings.urls)
    site = SlowSite(lambda n: jobs.request_cancel(job.id) if n == 3 else None)
    asyncio.run(run_worker(settings, engine, once=True,
                           transport=httpx.MockTransport(site.handle)))  # fmt: skip
    row = jobs.get(job.id)
    assert row.status == "cancelled" and row.finished_at is not None and not row.cancel_requested
    events = [e.event for e in jobs.events(job.id)]
    assert events[-2:] == ["cancel_requested", "cancelled"]
    assert len(complete(engine, job_crawl(engine, job.id))) < len(FakeSite().routes)


def test_versions_are_recorded_with_jobs_and_crawls(engine: Engine, tmp_path: Path) -> None:
    from gtm_crawler.versions import current_versions

    settings = worker_settings(tmp_path)
    jobs = JobStore(engine)
    job, _ = jobs.create(f"{SITE}/", settings.urls)
    asyncio.run(run_worker(settings, engine, once=True,
                           transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    expected = current_versions(settings)
    assert set(expected) == {"crawler", "parser", "artifact", "extractors", "assessment",
                             "dossier_schema", "quality_report", "search_templates",
                             "config"}  # fmt: skip
    assert jobs.get(job.id).versions == expected
    with engine.connect() as conn:
        crawl_versions = conn.execute(
            select(Crawl.versions).where(Crawl.job_id == job.id)
        ).scalar_one()  # fmt: skip
    assert crawl_versions == expected
    # The configuration hash follows output-affecting settings, not credentials.
    changed = replace(settings, parse=replace(settings.parse, max_blocks=10))
    assert current_versions(changed)["config"] != expected["config"]
    keyed = replace(settings, search=replace(settings.search, api_key="another-key"))
    assert current_versions(keyed)["config"] == expected["config"]


def test_worker_logs_carry_correlation_fields(engine: Engine, tmp_path: Path) -> None:
    import io
    import json

    from gtm_crawler.config import LoggingSettings
    from gtm_crawler.log import configure_logging

    settings = worker_settings(tmp_path)
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="INFO"), stream=stream)
    try:
        job, _ = JobStore(engine).create(f"{SITE}/", settings.urls)
        asyncio.run(run_worker(settings, engine, once=True, owner="worker-7",
                               transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    finally:
        configure_logging(LoggingSettings(level="WARNING"))
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fetches = [r for r in records if r["event"] == "fetch.completed" and "url" in r]
    assert fetches and all(r["job_id"] == job.id and r["worker"] == "worker-7" for r in fetches)
    page = next(r for r in fetches if r["url"] == f"{SITE}/about")
    assert page["stage"] == "crawl" and page["crawl_id"] and page["domain"] == "acme.example"
