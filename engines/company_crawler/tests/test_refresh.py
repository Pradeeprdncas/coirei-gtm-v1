"""Phase 6: the profile is refreshed when new information arrives, research runs its
sources in parallel, and fields are assessed while the site is still being crawled."""

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, func, select

from gtm_crawler.crawl import fetch_until
from gtm_crawler.db.models import CrawlJob, FieldAssessment
from gtm_crawler.worker import run_worker

from .test_crawl import FakeSite, run, settings_for
from .test_onboarding_api import crawled, upload


def test_fetch_until_runs_in_parallel_and_stops_at_the_limit() -> None:
    running, peak, fetched = 0, 0, []

    async def fetch(url: str) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        fetched.append(url)
        return 0 if url.endswith("-skip") else 1  # a page that was not fetched counts 0

    urls = ["a", "b-skip", "c", "d", "e", "f", "g"]
    got = asyncio.run(fetch_until(urls, 4, 3, fetch))
    assert got == 4 and peak == 3
    # In order, and never more at once than could still count: a, b-skip, c first; then
    # two more for the two still wanted.
    assert fetched == ["a", "b-skip", "c", "d", "e"]


@pytest.mark.db
def test_a_refresh_job_rebuilds_the_profile_without_fetching(
    engine: Engine, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    api, company = crawled(engine, settings, "bip-refresh")
    newest = select(func.max(FieldAssessment.computed_at)).where(
        FieldAssessment.organization_id == company
    )
    with engine.connect() as conn:
        before = conn.execute(newest).scalar()
    queued = api.post(f"/v1/companies/{company}/refresh")
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["kind"] == "refresh" and job["status"] == "QUEUED"
    site = FakeSite({})  # a refresh fetches nothing
    transport = httpx.MockTransport(site)
    assert asyncio.run(run_worker(settings, engine, once=True, transport=transport)) == 1
    assert site.requested == []
    done = api.get(f"/v1/crawls/{job['crawl_id']}").json()
    assert done["status"] == "COMPLETED" and done["kind"] == "refresh"
    with engine.connect() as conn:
        after = conn.execute(newest).scalar()
    assert after > before
    assert api.post("/v1/companies/999999/refresh").status_code == 404


@pytest.mark.db
def test_a_document_upload_queues_a_refresh(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "bip-upload-refresh")
    body = upload(api, company, "about.txt", b"We were founded in 2019.\n", refresh=True).json()
    with engine.connect() as conn:
        job = conn.execute(select(CrawlJob).where(CrawlJob.id == body["refresh_job_id"])).one()
    assert job.kind == "refresh" and job.status == "queued"
    api.post(f"/v1/crawls/{job.id}/cancel")  # not this test's to run


@pytest.mark.db
def test_the_job_shows_its_stage(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "bip-stage")
    with engine.connect() as conn:
        job_id = conn.execute(select(func.max(CrawlJob.id))
                              .where(CrawlJob.organization_id == company)).scalar()  # fmt: skip
    job = api.get(f"/v1/crawls/{job_id}").json()
    assert job["kind"] == "crawl" and job["stage"] == "done"


@pytest.mark.db
def test_fields_are_assessed_while_the_site_is_crawled(engine: Engine, tmp_path: Path) -> None:
    from dataclasses import replace

    settings = settings_for(tmp_path)
    settings = replace(settings, crawl=replace(settings.crawl, progress_every_pages=1))
    calls: list[tuple[int, int]] = []

    def progress(crawl_id: int, organization_id: int) -> None:
        calls.append((crawl_id, organization_id))

    summary = run(engine, settings, FakeSite(), progress=progress)
    assert calls and all(c == (summary.crawl_id, summary.organization_id) for c in calls)


@pytest.mark.db
def test_a_cancelled_refresh_stops_before_the_model(engine: Engine, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from gtm_crawler import pipeline

    settings = settings_for(tmp_path)
    api, company = crawled(engine, settings, "bip-refresh-stop")
    job = api.post(f"/v1/companies/{company}/refresh").json()
    stop = asyncio.Event()
    stop.set()  # the heartbeat saw the cancel request
    stages: list[str] = []

    async def stage(name: str) -> None:
        stages.append(name)

    out = asyncio.run(pipeline._refresh(
        engine, settings, SimpleNamespace(id=job["crawl_id"], organization_id=company),
        "test-owner", stage, stop))  # fmt: skip
    assert out["job_status"] == "paused" and stages == []
    # Run by hand, not by a worker: cancel it, or the next test's worker would claim it.
    assert api.post(f"/v1/crawls/{job['crawl_id']}/cancel").status_code == 202
