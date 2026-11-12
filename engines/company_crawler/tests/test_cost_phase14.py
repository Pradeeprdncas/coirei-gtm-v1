"""Phase 14: cost accounting (spec section 27): usage and cost per job, crawl and company."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import Engine, func, inspect, select

from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, ConfigError, CostSettings
from gtm_crawler.cost import price, record_job_costs
from gtm_crawler.db.models import CostEvent
from gtm_crawler.pipeline import run_pipeline

from .conftest import alembic_config
from .test_crawl import SITE, FakeSite, settings_for

PRICES = CostSettings(http_request_usd=0.001, http_gb_usd=2.0, storage_gb_usd=0.5,
                      worker_second_usd=0.0001, database_second_usd=0.01,
                      search_query_usd=("exa=0.005", "other = 0.25"))  # fmt: skip


def test_price_math() -> None:
    assert price(PRICES, "http", "requests", None, Decimal(7)) == Decimal("0.007")
    # Bytes are priced per GB.
    assert price(PRICES, "http", "bytes", None, Decimal(500_000_000)) == Decimal("1")
    assert price(PRICES, "search", "queries", "exa", Decimal(3)) == Decimal("0.015")
    assert price(PRICES, "search", "queries", "other", Decimal(1)) == Decimal("0.25")
    # An unknown price costs 0; the quantity is still recorded by the caller.
    assert price(PRICES, "search", "queries", "unknown", Decimal(9)) == 0
    assert price(PRICES, "browser", "renders", None, Decimal(9)) == 0
    assert price(CostSettings(), "worker", "seconds", None, Decimal(100)) == 0


@pytest.mark.parametrize("item", ["exa", "=1", "exa=-1", "exa=x", "exa=inf"])
def test_bad_search_price_is_refused(item: str) -> None:
    with pytest.raises(ConfigError):
        CostSettings(search_query_usd=(item,))


def test_negative_price_is_refused() -> None:
    with pytest.raises(ConfigError):
        CostSettings(http_request_usd=-1.0)


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _rows(engine: Engine, job_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(CostEvent).where(CostEvent.job_id == job_id)).all())


@pytest.mark.db
def test_job_cost_is_recorded_idempotently_and_served(engine: Engine, tmp_path: Path) -> None:
    requests_before = _metric("resource_usage_total", kind="http", unit="requests")
    settings = replace(settings_for(tmp_path), cost=PRICES)
    out = asyncio.run(run_pipeline(f"{SITE}/", settings, engine,
                                   transport=httpx.MockTransport(FakeSite())))  # fmt: skip
    job_id, org_id = out["job_id"], out["organization_id"]
    rows = _rows(engine, job_id)
    by = {(r.kind, r.unit): r for r in rows}
    assert {"http", "storage", "worker", "database"} <= {r.kind for r in rows}
    assert all(r.organization_id == org_id for r in rows)
    requests = by[("http", "requests")]
    assert requests.crawl_id == out["crawl_id"] and requests.quantity >= out["stats"]["fetches"]
    assert requests.cost_usd == price(PRICES, "http", "requests", None, requests.quantity)
    assert by[("database", "seconds")].quantity > 0 and by[("database", "seconds")].crawl_id is None
    assert by[("storage", "bytes")].quantity > 0
    # Prometheus counts what was recorded.
    grown = _metric("resource_usage_total", kind="http", unit="requests") - requests_before
    assert grown == float(requests.quantity)
    assert _metric("cost_usd_total", kind="http") > 0

    # The job result and the dossier carry the summary.
    total = float(sum(r.cost_usd for r in rows))
    assert out["cost"]["total_usd"] == pytest.approx(total)
    assert out["cost"]["by_kind"]["http"]["usage"]["requests"] == float(requests.quantity)
    assert out["dossier"]["cost"]["job_id"] == job_id
    assert out["dossier"]["cost"]["total_usd"] == pytest.approx(total)

    # At-least-once: recording the same run again adds nothing; the crawl rows
    # are updated in place and a new run adds only its own database time.
    [db_row] = [r for r in rows if r.kind == "database"]
    run_id = db_row.key.split(":")[2]
    again = record_job_costs(engine, PRICES, job_id, run_id, float(db_row.quantity))
    assert again == out["cost"]
    assert grown == _metric("resource_usage_total", kind="http", unit="requests") - requests_before
    record_job_costs(engine, PRICES, job_id, "another-run", 0.5)
    after = _rows(engine, job_id)
    assert len(after) == len(rows) + 1
    assert {r.key for r in after} - {r.key for r in rows} == {
        f"run:{job_id}:another-run:database:seconds"
    }
    with engine.connect() as conn:
        dupes = conn.execute(
            select(func.count()).select_from(CostEvent).group_by(CostEvent.key)
            .having(func.count() > 1)
        ).all()  # fmt: skip
    assert dupes == []

    # API: per company, totals and per job; unknown company 404; token guard.
    api = TestClient(create_app(settings, engine))
    body = api.get(f"/v1/companies/{org_id}/cost").json()
    assert body["company_id"] == org_id and body["currency"] == "USD"
    assert body["total_usd"] == pytest.approx(total + 0.5 * 0.01)
    job = next(j for j in body["jobs"] if j["job_id"] == job_id)
    assert job["by_kind"]["database"]["cost_usd"] == pytest.approx(float(db_row.cost_usd) + 0.005)
    assert api.get("/v1/companies/999999/cost").status_code == 404
    job_view = api.get(f"/v1/crawls/{job_id}").json()
    assert job_view["result"]["cost"]["total_usd"] == pytest.approx(total)
    guarded = TestClient(create_app(replace(settings, api=ApiSettings(token="s3cret")), engine))  # noqa: S106
    assert guarded.get(f"/v1/companies/{org_id}/cost").status_code == 401
    ok = guarded.get(f"/v1/companies/{org_id}/cost", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


@pytest.mark.db
def test_migration_0013_downgrade_and_upgrade(engine: Engine) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "0012")
    assert "cost_event" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "cost_event" in inspect(engine).get_table_names()
