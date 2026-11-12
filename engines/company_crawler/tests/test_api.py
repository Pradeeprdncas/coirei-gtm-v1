"""The job API: create, read, pause/resume, dossier, auth, health and metrics."""

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, DatabaseSettings, Settings
from gtm_crawler.db import make_engine
from gtm_crawler.db.models import CrawlJob
from gtm_crawler.worker import run_worker

from .test_crawl import SITE, FakeSite, settings_for

pytestmark = pytest.mark.db


def client(engine: Engine, settings: Settings) -> TestClient:
    return TestClient(create_app(settings, engine))


def close(engine: Engine, job_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(update(CrawlJob).where(CrawlJob.id == job_id).values(status="failed"))


def test_create_is_idempotent_and_validated(engine: Engine, tmp_path: Path) -> None:
    api = client(engine, settings_for(tmp_path))
    first = api.post("/v1/crawls", json={"root_url": "https://api-a.com", "domain": "api-a.com"})
    assert first.status_code == 201
    body = first.json()
    assert body["status"] == "QUEUED" and body["created"] and body["site_crawls"] == []
    again = api.post("/v1/crawls", json={"root_url": "https://www.api-a.com/about"})
    assert again.status_code == 200 and again.json()["crawl_id"] == body["crawl_id"]
    assert api.get(f"/v1/crawls/{body['crawl_id']}").json()["root_url"] == "https://api-a.com/"
    close(engine, body["crawl_id"])

    keyed = api.post("/v1/crawls", json={"root_url": "https://api-b.com"},
                     headers={"Idempotency-Key": "k-1"})  # fmt: skip
    close(engine, keyed.json()["crawl_id"])
    # The same key returns the same job even after it finished.
    replay = api.post("/v1/crawls", json={"root_url": "https://api-b.com"},
                      headers={"Idempotency-Key": "k-1"})  # fmt: skip
    assert replay.status_code == 200 and replay.json()["crawl_id"] == keyed.json()["crawl_id"]

    assert api.post("/v1/crawls", json={"root_url": "ftp://api-c.com/"}).status_code == 422
    mismatch = api.post("/v1/crawls", json={"root_url": "https://api-c.com", "domain": "x.com"})
    assert mismatch.status_code == 422
    assert api.get("/v1/crawls/999999").status_code == 404


def test_pause_and_resume(engine: Engine, tmp_path: Path) -> None:
    api = client(engine, settings_for(tmp_path))
    job_id = api.post("/v1/crawls", json={"root_url": "https://api-d.com"}).json()["crawl_id"]
    paused = api.post(f"/v1/crawls/{job_id}/pause")
    assert paused.status_code == 202 and paused.json()["status"] == "PAUSED"
    resumed = api.post(f"/v1/crawls/{job_id}/resume")
    assert resumed.status_code == 202 and resumed.json()["status"] == "QUEUED"
    assert api.post(f"/v1/crawls/{job_id}/resume").status_code == 409
    close(engine, job_id)
    assert api.post(f"/v1/crawls/{job_id}/pause").status_code == 409


def test_token_guards_the_api_but_not_health(engine: Engine, tmp_path: Path) -> None:
    settings = replace(settings_for(tmp_path), api=ApiSettings(token="s3cret"))  # noqa: S106
    api = client(engine, settings)
    assert api.get("/v1/crawls/1").status_code == 401
    assert api.get("/v1/companies/1/lineage/founded_year").status_code == 401
    assert api.get("/v1/crawls/1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = api.get("/v1/crawls/999999", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 404  # authorized; the crawl just does not exist
    assert api.get("/healthz").json()["status"] == "ok"
    assert api.get("/livez").status_code == 200 and api.get("/readyz").status_code == 200


def test_submitted_crawl_runs_on_a_worker_and_serves_a_dossier(
    engine: Engine, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    api = client(engine, settings)
    created = api.post("/v1/crawls", json={"root_url": f"{SITE}/"}).json()
    ran = asyncio.run(run_worker(settings, engine, once=True,
                                 transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    assert ran == 1
    job = api.get(f"/v1/crawls/{created['crawl_id']}").json()
    assert job["status"] == "COMPLETED" and job["finished_at"] is not None
    assert [c["status"] for c in job["site_crawls"]] == ["completed"]
    assert job["site_crawls"][0]["pages_parsed"] > 0

    dossier = api.get(f"/v1/companies/{job['company_id']}/dossier")
    assert dossier.status_code == 200
    company = dossier.json()["company"]
    assert company["domain"] == "acme.example"
    assert "founded_year" in company["fields"]  # every field is reported, UNKNOWN or not
    assert job["result"]["extraction"]["pages"] > 0
    assert api.get("/v1/companies/999999/dossier").status_code == 404
    assert dossier.headers["x-content-type-options"] == "nosniff"

    # Spec section 20: sections, state summary, freshness, versions.
    doc = dossier.json()
    assert doc["schema_version"] == "7" and doc["versions"]["dossier_schema"] == "7"
    assert "founded_year" in doc["sections"]["identity"]
    assert sum(doc["summary"]["states"].values()) >= len(company["fields"])
    fresh = doc["freshness"]
    assert fresh["last_crawl_finished_at"] is not None
    assert fresh["oldest_evidence_retrieved_at"] <= fresh["newest_evidence_retrieved_at"]

    # Spec section 25: lineage from a stated field back to the raw artifact.
    stated = next(f for f, v in company["fields"].items()
                  if any(e["citations"] for e in (v if isinstance(v, list) else [v])))  # fmt: skip
    lineage = api.get(f"/v1/companies/{job['company_id']}/lineage/{stated}").json()
    shown = company["fields"][stated]
    assert len(lineage["items"]) == (len(shown) if isinstance(shown, list) else 1)
    obs = next(o for item in lineage["items"] for o in item["observations"])
    assert obs["field"] == stated and obs["artifact"]["sha256"] == obs["fetch"]["raw_sha256"]
    base = f"/v1/companies/{job['company_id']}/lineage"
    assert api.get(f"{base}/not_a_field").status_code == 404
    assert api.get(f"{base}/price?product_id=999999").status_code == 404
    assert api.get("/v1/companies/999999/lineage/founded_year").status_code == 404

    metrics = api.get("/metrics/").text
    assert "crawl_jobs_total" in metrics and "http_requests_total" in metrics


def test_readiness_reports_schema_and_database(engine: Engine, tmp_path: Path) -> None:
    from gtm_crawler.db.migrate import migration_head

    api = client(engine, settings_for(tmp_path))
    ready = api.get("/readyz")
    assert ready.status_code == 200 and ready.json()["schema"] == migration_head()
    down = make_engine(DatabaseSettings(url="postgresql+psycopg://x:y@10.255.255.1:5432/none",
                                        connect_timeout_seconds=1))  # fmt: skip
    unavailable = client(down, settings_for(tmp_path)).get("/readyz")
    assert unavailable.status_code == 503 and unavailable.json()["status"] == "unavailable"
    assert client(down, settings_for(tmp_path)).get("/livez").status_code == 200


def test_cancel_queued_and_final_jobs(engine: Engine, tmp_path: Path) -> None:
    api = client(engine, settings_for(tmp_path))
    job_id = api.post("/v1/crawls", json={"root_url": "https://api-e.com"}).json()["crawl_id"]
    cancelled = api.post(f"/v1/crawls/{job_id}/cancel")
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["finished_at"] is not None
    assert api.post(f"/v1/crawls/{job_id}/cancel").status_code == 409
    assert api.post(f"/v1/crawls/{job_id}/pause").status_code == 409
    assert api.post("/v1/crawls/999999/cancel").status_code == 404


def test_a_failed_site_crawl_says_why(engine: Engine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    api = client(engine, settings)
    site = "https://robots-down.example"
    created = api.post("/v1/crawls", json={"root_url": f"{site}/"}).json()
    down = FakeSite({f"{site}/robots.txt": 503})
    ran = asyncio.run(run_worker(settings, engine, once=True, transport=httpx.MockTransport(down)))
    assert ran == 1
    job = api.get(f"/v1/crawls/{created['crawl_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error_summary"] == "site crawl failed: robots_unreachable"
