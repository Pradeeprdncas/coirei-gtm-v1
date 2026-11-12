"""Market research as a service: the run store, the worker and the API (PostgreSQL)."""

import asyncio
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, text, update
from sqlalchemy.exc import OperationalError

from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, MarketSettings, SearchSettings, Settings
from gtm_crawler.db.models import CrawlJob, MarketResearch
from gtm_crawler.market.models import Report, ResearchMode, ResearchRequest, RunStatus
from gtm_crawler.market.runner import process_run, verify_stored
from gtm_crawler.market.store import MarketStore
from gtm_crawler.worker import run_worker

from .test_market import SEARCH_URLS, handler, settings

pytestmark = pytest.mark.db


def exa(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"results": [{"url": u} for u in SEARCH_URLS]})


def service_settings(tmp_path: Path, **kw: object) -> Settings:
    return replace(settings(tmp_path), search=SearchSettings(api_key="k", max_retries=0),
                   **kw)  # type: ignore[arg-type]  # fmt: skip


@pytest.fixture
def clean(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM market_research"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM market_research"))


def request(**kw: object) -> ResearchRequest:
    return ResearchRequest(mode=ResearchMode.INDUSTRY, subject="CRM software", geography="India",
                           **kw)  # type: ignore[arg-type]  # fmt: skip


def test_store_lease_cancel_and_cache(engine: Engine, clean: None, tmp_path: Path) -> None:
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    assert store.get(req.research_id).status == "queued"
    run = store.claim("w1", 60)
    assert run.id == req.research_id and run.attempts == 1
    assert store.claim("w2", 60) is None  # leased
    assert store.heartbeat(req.research_id, "w2", 60) is None  # not the owner
    assert store.heartbeat(req.research_id, "w1", 60, {"stage": "collection"}) == "continue"
    assert store.cancel(req.research_id) == "running"
    assert store.heartbeat(req.research_id, "w1", 60) == "cancel"
    assert store.get(req.research_id).progress == {"stage": "collection"}

    report = asyncio.run(process_and_get(engine, tmp_path, req, run))
    assert report.status is RunStatus.CANCELLED
    assert store.get(req.research_id).status == "cancelled"
    assert store.finish(req.research_id, "w1", None, RunStatus.FAILED) is False  # already final

    queued = request()
    store.create(queued, max_queued=5)
    assert store.cancel(queued.research_id) == "cancelled"
    with pytest.raises(Exception, match="already queued"):
        store.create(request(), max_queued=0)


def test_a_cancelled_run_whose_worker_died_is_not_run_again(engine: Engine, clean: None) -> None:
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    store.claim("w1", 60)
    assert store.cancel(req.research_id) == "running"  # asks w1, which then dies
    with engine.begin() as conn:  # its lease expires
        conn.execute(update(MarketResearch).where(MarketResearch.id == req.research_id)
                     .values(lease_expires_at=func.now() - text("interval '1 s'")))  # fmt: skip
    assert store.claim("w2", 60) is None
    row = store.get(req.research_id)
    assert row.status == "cancelled" and row.finished_at is not None and row.lease_owner is None


def test_a_database_error_at_finish_does_not_stop_the_worker(
    engine: Engine, clean: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    row = store.claim("w1", 60)

    def down(*_a: object, **_kw: object) -> bool:
        raise OperationalError("UPDATE market_research", {}, Exception("connection closed"))

    monkeypatch.setattr(store, "finish", down)
    outcome = asyncio.run(process_run(service_settings(tmp_path), engine, store, row, "w1",
                                      transport=httpx.MockTransport(handler),
                                      search_transport=httpx.MockTransport(exa)))  # fmt: skip
    assert outcome == "interrupted"  # not raised: the lease expires and another worker retries
    assert store.get(req.research_id).status == "running"


def test_a_run_handed_back_at_shutdown_keeps_its_attempts(
    engine: Engine, clean: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    s = service_settings(tmp_path)
    for n in range(4):  # more deploys than MAX_ATTEMPTS
        row = store.claim(f"w{n}", 60)
        assert row is not None and row.attempts == 1
        shutdown = asyncio.Event()
        shutdown.set()
        outcome = asyncio.run(process_run(s, engine, store, row, f"w{n}", shutdown,
                                          transport=httpx.MockTransport(handler),
                                          search_transport=httpx.MockTransport(exa)))  # fmt: skip
        assert outcome == "released"
        assert (store.get(req.research_id).status, store.get(req.research_id).attempts) == (
            "queued",
            0,
        )
    # A worker that dies still uses an attempt; MAX_ATTEMPTS of them fail the run.
    for n in range(3):
        assert store.claim(f"dead{n}", -1) is not None
    assert store.claim("w9", 60) is None
    assert store.get(req.research_id).status == "failed"

    # A database error while handing back does not stop the worker either.
    again = request()
    store.create(again, max_queued=5)
    row = store.claim("w1", 60)

    def down(*_a: object, **_kw: object) -> None:
        raise OperationalError("UPDATE market_research", {}, Exception("connection closed"))

    monkeypatch.setattr(store, "release", down)
    shutdown = asyncio.Event()
    shutdown.set()
    outcome = asyncio.run(process_run(s, engine, store, row, "w1", shutdown,
                                      transport=httpx.MockTransport(handler),
                                      search_transport=httpx.MockTransport(exa)))  # fmt: skip
    assert outcome == "interrupted"


async def process_and_get(engine: Engine, tmp_path: Path, req: ResearchRequest,
                          row: object) -> Report:  # fmt: skip
    store = MarketStore(engine)
    stop = asyncio.Event()
    stop.set()  # the cancel request reaches the run at its first check
    from gtm_crawler.market import runner

    original = runner.research

    async def cancelled(*a: object, **kw: object) -> Report:
        kw["stop"] = stop
        return await original(*a, **kw)  # type: ignore[arg-type]

    runner.research = cancelled  # type: ignore[assignment]
    try:
        await process_run(service_settings(tmp_path), engine, store, row, "w1",
                          transport=httpx.MockTransport(handler),
                          search_transport=httpx.MockTransport(exa))  # fmt: skip
    finally:
        runner.research = original  # type: ignore[assignment]
    stored = store.get(req.research_id)
    return Report.model_validate(stored.report)


def test_worker_runs_a_queued_run_and_reuses_it_as_cache(engine: Engine, clean: None,
                                                         tmp_path: Path) -> None:  # fmt: skip
    with engine.begin() as conn:  # a crawl job would be claimed first
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    s = service_settings(tmp_path)
    ran = asyncio.run(run_worker(s, engine, once=True, owner="w-market",
                                 transport=httpx.MockTransport(handler),
                                 search_transport=httpx.MockTransport(exa)))  # fmt: skip
    assert ran == 1
    row = store.get(req.research_id)
    assert row.status == "partial" and row.finished_at is not None and row.lease_owner is None
    report = Report.model_validate(row.report)
    assert report.evidence and report.contradictions
    assert store.latest_report(req.subject_key()) == report

    # Search down: the next run of the same subject reuses this evidence.
    down = request()
    store.create(down, max_queued=5)
    asyncio.run(run_worker(s, engine, once=True, owner="w-market",
                           transport=httpx.MockTransport(handler),
                           search_transport=httpx.MockTransport(
                               lambda r: httpx.Response(503))))  # fmt: skip
    again = Report.model_validate(store.get(down.research_id).report)
    assert again.evidence and all(e.cached_from == req.research_id for e in again.evidence)


def test_api_flow(engine: Engine, clean: None, tmp_path: Path) -> None:
    s = service_settings(tmp_path)
    api = TestClient(create_app(s, engine))
    body = {"mode": "industry", "subject": "CRM software", "geography": "India",
            "research_id": "mr_chosenbyclient"}  # fmt: skip
    created = api.post("/v1/market/research", json=body)
    assert created.status_code == 201
    research_id = created.json()["research_id"]
    assert research_id != "mr_chosenbyclient" and created.json()["status"] == "QUEUED"
    assert api.get(f"/v1/market/research/{research_id}").json()["subject"] == "CRM software"
    assert api.get(f"/v1/market/research/{research_id}/report").status_code == 409
    assert api.get("/v1/market/research/mr_nope").status_code == 404
    assert (
        api.post("/v1/market/research", json={"mode": "product", "subject": "X"}).status_code == 422
    )

    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    asyncio.run(run_worker(s, engine, once=True, transport=httpx.MockTransport(handler),
                           search_transport=httpx.MockTransport(exa)))  # fmt: skip
    view = api.get(f"/v1/market/research/{research_id}").json()
    assert view["status"] == "PARTIAL" and view["summary"]["executive_summary"]
    full = api.get(f"/v1/market/research/{research_id}/report")
    assert full.status_code == 200 and full.json()["evidence"]
    md = api.get(f"/v1/market/research/{research_id}/report?format=markdown").json()["markdown"]
    assert md.startswith("# CRM software: industry market research") and "## Sources" in md
    assert api.post(f"/v1/market/research/{research_id}/cancel").status_code == 409

    asked = api.post(f"/v1/market/research/{research_id}/ask",
                     json={"question": "How large is the CRM market in 2023?"}).json()  # fmt: skip
    assert asked["evidence"] and asked["answer"] is None  # no model configured
    assert "2023" in asked["evidence"][0]["excerpt"]

    sized = api.post("/v1/market/sizing", json={
        "market_definition": "CRM in India", "currency": "USD", "top_down": {
            "market_total": {"name": "total", "base": 1000, "unit": "USD", "basis": "assumption",
                             "rationale": "test"},
            "sam_filters": [{"name": "smb", "base": 0.5, "unit": "fraction", "basis": "assumption",
                             "rationale": "test"}]}})  # fmt: skip
    assert sized.status_code == 200 and sized.json()["results"][0]["base"]["sam"] == 500.0

    capped = replace(s, market=MarketSettings(max_queued_runs=1))
    full_api = TestClient(create_app(capped, engine))
    assert full_api.post("/v1/market/research", json=body).status_code == 201
    assert full_api.post("/v1/market/research", json=body).status_code == 429


def test_a_retried_create_with_one_idempotency_key_makes_one_run(
    engine: Engine, clean: None, tmp_path: Path
) -> None:
    s = service_settings(tmp_path)
    body = {"mode": "industry", "subject": "CRM software", "geography": "India"}
    key = {"Idempotency-Key": "order-42"}
    first = TestClient(create_app(s, engine)).post("/v1/market/research", json=body, headers=key)
    assert first.status_code == 201 and first.json()["created"] is True
    # A retry after a timeout, on another API process (or after a restart).
    again = TestClient(create_app(s, engine)).post("/v1/market/research", json=body, headers=key)
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["research_id"] == first.json()["research_id"]
    other = TestClient(create_app(s, engine)).post(
        "/v1/market/research", json={**body, "geography": "Brazil"}, headers=key
    )
    assert other.status_code == 409 and first.json()["research_id"] in other.json()["detail"]
    api = TestClient(create_app(s, engine))
    assert api.post("/v1/market/research", json=body).status_code == 201  # no key: a new run
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM market_research")).scalar() == 2
    # The key returns its run even once the queue is full.
    full = TestClient(create_app(replace(s, market=MarketSettings(max_queued_runs=1)), engine))
    assert full.post("/v1/market/research", json=body, headers=key).status_code == 200


def test_concurrent_creates_with_one_idempotency_key_make_one_run(engine: Engine,
                                                                  clean: None) -> None:  # fmt: skip
    store = MarketStore(engine)
    barrier = threading.Barrier(6)

    def create(_: int) -> str:
        barrier.wait()
        return str(store.create(request(), max_queued=50, idempotency_key="same").id)

    with ThreadPoolExecutor(6) as pool:
        ids = set(pool.map(create, range(6)))
    assert len(ids) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM market_research")).scalar() == 1


def test_operator_verification_of_a_stored_report(engine: Engine, clean: None,
                                                  tmp_path: Path) -> None:  # fmt: skip
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    s = service_settings(tmp_path)
    asyncio.run(run_worker(s, engine, once=True, transport=httpx.MockTransport(handler),
                           search_transport=httpx.MockTransport(exa)))  # fmt: skip
    stored = store.get(req.research_id).report

    ok = verify_stored(store, s, req.research_id)
    assert ok["verdict"] == "verified", ok["problems"]
    assert ok["counts"]["verified_reextracted"] == ok["evidence"] > 0
    view = TestClient(create_app(s, engine)).get(f"/v1/market/research/{req.research_id}").json()
    assert view["verification"]["verdict"] == "verified"
    assert store.get(req.research_id).report == stored  # never rewritten

    # A quote that is not where its record says fails verification.
    report = Report.model_validate(stored)
    report.evidence[0].excerpt += " (edited)"
    with engine.begin() as conn:
        conn.execute(update(MarketResearch).where(MarketResearch.id == req.research_id)
                     .values(report=report.model_dump(mode="json")))  # fmt: skip
    bad = verify_stored(store, s, req.research_id)
    assert bad["verdict"] == "failed"
    assert [p["evidence_id"] for p in bad["problems"]] == [report.evidence[0].evidence_id]

    # An expired artifact cannot be checked: incomplete, never verified, never failed.
    for f in (tmp_path / "raw").rglob("*"):
        if f.is_file():
            f.unlink()
    with engine.begin() as conn:
        conn.execute(update(MarketResearch).where(MarketResearch.id == req.research_id)
                     .values(report=stored))  # fmt: skip
    gone = verify_stored(store, s, req.research_id)
    assert gone["verdict"] == "incomplete" and gone["missing_artifacts"]
    assert gone["counts"].get("verified_reextracted", 0) == 0

    # Evidence without a re-checkable locator is unchecked, so never "verified".
    report = Report.model_validate(stored)
    report.evidence[0].locator = None
    with engine.begin() as conn:
        conn.execute(update(MarketResearch).where(MarketResearch.id == req.research_id)
                     .values(report=report.model_dump(mode="json")))  # fmt: skip
    assert verify_stored(store, s, req.research_id)["counts"]["unchecked"] == 1

    queued = request()
    store.create(queued, max_queued=5)
    assert verify_stored(store, s, queued.research_id)["verdict"] == "incomplete"


def test_prune_keeps_what_stored_reports_cite(engine: Engine, clean: None,
                                             tmp_path: Path) -> None:  # fmt: skip
    from gtm_crawler.cli import prune
    from gtm_crawler.rawstore import make_rawstore

    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    s = service_settings(tmp_path)
    asyncio.run(run_worker(s, engine, once=True, transport=httpx.MockTransport(handler),
                           search_transport=httpx.MockTransport(exa)))  # fmt: skip
    report = Report.model_validate(store.get(req.research_id).report)
    cited = {
        src.raw_sha256
        for src in report.sources
        if src.raw_sha256 and any(e.source_id == src.source_id for e in report.evidence)
    }
    raw = make_rawstore(s.rawstore)
    unused = raw.put(b"nobody cites this")
    long_ago = time.time() - 400 * 86_400  # past rawstore.retention_days
    for path in (tmp_path / "raw").rglob("*.gz"):
        os.utime(path, (long_ago, long_ago))
    prune(engine, s)
    assert cited and all(raw.exists(d) for d in cited)  # a delivered report stays verifiable
    assert not raw.exists(unused)
    assert verify_stored(store, s, req.research_id)["verdict"] == "verified"


def test_ask_with_a_model_keeps_only_cited_sentences(engine: Engine, clean: None,
                                                     tmp_path: Path) -> None:  # fmt: skip
    store = MarketStore(engine)
    req = request()
    store.create(req, max_queued=5)
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    s = service_settings(tmp_path)
    asyncio.run(run_worker(s, engine, once=True, transport=httpx.MockTransport(handler),
                           search_transport=httpx.MockTransport(exa)))  # fmt: skip
    report = Report.model_validate(store.get(req.research_id).report)
    real = next(e.evidence_id for e in report.evidence if "USD 2.0 billion in 2023" in e.excerpt)
    seen: list[dict[str, object]] = []

    def model(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        answer = (
            f"The market was valued at USD 2.0 billion in 2023 [{real}]. "
            f"It was worth USD 9 billion in 2023 [{real}]. "  # a number the excerpt does not state
            "It will triple next year [ev_000000000000]. Trust me."
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    from gtm_crawler.market import llm

    original = llm.ChatModel.__init__

    def patched(self: llm.ChatModel, market: MarketSettings, transport: object = None) -> None:
        original(self, market, httpx.MockTransport(model))

    llm.ChatModel.__init__ = patched  # type: ignore[method-assign]
    try:
        with_model = replace(
            s, market=MarketSettings(llm_model="m", llm_base_url="https://llm.example/v1")
        )
        api = TestClient(create_app(with_model, engine))
        out = api.post(f"/v1/market/research/{req.research_id}/ask",
                       json={"question": "market size 2023"}).json()  # fmt: skip
    finally:
        llm.ChatModel.__init__ = original  # type: ignore[method-assign]
    assert out["answer"]["text"] == f"The market was valued at USD 2.0 billion in 2023 [{real}]."
    assert out["answer"]["evidence_ids"] == [real] and out["answer"]["basis"] == "model_generated"
    prompt = seen[0]["messages"][1]["content"]  # type: ignore[index]
    assert "<evidence id=" in prompt and "untrusted" in seen[0]["messages"][0]["content"]  # type: ignore[index]


def test_token_guards_market_routes(engine: Engine, clean: None, tmp_path: Path) -> None:
    s = replace(service_settings(tmp_path), api=ApiSettings(token="s3cret"))  # noqa: S106
    api = TestClient(create_app(s, engine))
    assert api.get("/v1/market/research/x").status_code == 401
    assert api.post("/v1/market/sizing", json={}).status_code == 401


def test_worker_runs_a_waiting_crawl_job_before_a_market_run(engine: Engine, clean: None,
                                                             tmp_path: Path) -> None:  # fmt: skip
    """Audit L3 / workflow A: competitor crawls keep priority over market runs."""
    from gtm_crawler.jobs import JobStore

    from .test_crawl import SITE, FakeSite
    from .test_jobs import worker_settings

    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.status == "queued").values(status="failed"))
    s = worker_settings(tmp_path)
    store = MarketStore(engine)
    research = request()
    store.create(research, max_queued=5)
    jobs = JobStore(engine)
    job, _ = jobs.create(f"{SITE}/", s.urls)
    ran = asyncio.run(run_worker(s, engine, once=True, transport=httpx.MockTransport(FakeSite())))
    assert ran == 1
    assert jobs.get(job.id).status == "completed"
    assert store.get(research.research_id).status == "queued"


def test_schema_has_market_table(engine: Engine) -> None:
    assert MarketResearch.__tablename__ == "market_research"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM market_research")).scalar() is not None
