"""Phase 14: crawl-quality report, its API and metrics, alert rules and dashboard."""

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import Engine, text

from gtm_crawler import metrics
from gtm_crawler.api import create_app
from gtm_crawler.breaker import CircuitBreaker
from gtm_crawler.config import ApiSettings
from gtm_crawler.quality import percentile
from gtm_crawler.worker import run_worker

from .test_crawl import SITE, FakeSite, settings_for

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([7], 99) == 7
    values = list(range(100, 0, -1))  # 1..100, unsorted
    assert (percentile(values, 50), percentile(values, 90), percentile(values, 99)) == (50, 90, 99)
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([1, 2, 3, 4], 51) == 3
    assert percentile([5, 1, 3], 100) == 5 and percentile([5, 1, 3], 1) == 1


def test_descendant_rss_reads_the_process_tree(tmp_path: Path) -> None:
    def proc(pid: int, ppid: int, pages: int) -> None:
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (a (b) c) S {ppid} 1 1")
        (tmp_path / str(pid) / "statm").write_text(f"999 {pages} 0")

    proc(10, 1, 100)  # the worker
    proc(11, 10, 2)  # playwright driver
    proc(12, 11, 3)  # chromium, a grandchild
    proc(13, 1, 50)  # unrelated
    page = metrics.descendant_rss_bytes(10, tmp_path)
    assert page is not None and page % 5 == 0 and page > 0
    assert metrics.descendant_rss_bytes(12, tmp_path) == 0
    assert metrics.descendant_rss_bytes(10, tmp_path / "missing") is None


def test_circuit_trips_are_counted() -> None:
    before = REGISTRY.get_sample_value("circuit_opened_total", {"breaker": "t14"}) or 0
    breaker = CircuitBreaker("t14", threshold=2, cooldown=10, max_cooldown=60)
    breaker.record("k", ok=False)
    breaker.record("k", ok=False)
    assert REGISTRY.get_sample_value("circuit_opened_total", {"breaker": "t14"}) == before + 1


# --- alert rules and dashboard reference only metrics that exist ----------------------

_KEYWORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "and", "or",
             "unless", "bool", "offset", "up"}  # fmt: skip  # "up": Prometheus's own


def metric_names(expr: str) -> set[str]:
    expr = re.sub(r'"[^"]*"', "", expr)  # label values
    expr = re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", expr)  # matchers, ranges
    expr = re.sub(r"\b(by|without|on|ignoring)\s*\([^)]*\)", "", expr)  # grouping labels
    tokens = re.findall(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\b(?!\s*\()", expr)
    return set(tokens) - _KEYWORDS


def exposed_names() -> set[str]:
    families = list(REGISTRY.collect())
    for collector in (metrics._QUEUE, metrics._BrowserMemoryCollector()):
        families += list(collector.describe())
    names = set()
    for f in families:
        suffixes = {"counter": ("_total",), "histogram": ("_bucket", "_sum", "_count")}
        names.update(f.name + s for s in suffixes.get(f.type, ("",)))
    return names


def alert_exprs() -> list[str]:
    source = (DEPLOY / "prometheus/alerts.yml").read_text(encoding="utf-8")
    exprs = re.findall(r"^\s*expr:\s*(.+)$", source, re.MULTILINE)
    assert len(exprs) == len(re.findall(r"^\s*- alert:", source, re.MULTILINE)) >= 10
    return exprs


def dashboard_exprs() -> list[str]:
    dash = json.loads((DEPLOY / "grafana/crawl-health.json").read_text(encoding="utf-8"))
    return [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]


def test_metric_name_parser() -> None:
    assert metric_names(
        "histogram_quantile(0.99, sum by (le)"
        ' (rate(db_query_duration_seconds_bucket{a="x y"}[5m])))'
        ' > 0.5 unless time() - worker_last_seen_timestamp_seconds > 2e9'
    ) == {"db_query_duration_seconds_bucket", "worker_last_seen_timestamp_seconds"}  # fmt: skip


@pytest.mark.parametrize("source", ["alerts", "dashboard"])
def test_rules_and_dashboard_use_existing_metrics(source: str) -> None:
    exprs = alert_exprs() if source == "alerts" else dashboard_exprs()
    used = set().union(*(metric_names(e) for e in exprs))
    assert used and not used - exposed_names(), used - exposed_names()


def test_alerts_cover_the_spec_list() -> None:
    used = set().union(*(metric_names(e) for e in alert_exprs()))
    for name in ("queue_jobs", "job_leases_expired_total", "http_requests_total",
                 "db_query_duration_seconds_bucket", "database_up",
                 "browser_processes_resident_memory_bytes", "search_requests_total",
                 "circuit_opened_total", "worker_last_seen_timestamp_seconds"):  # fmt: skip
        assert name in used


# --- the report on a real (simulated) crawl ---------------------------------------------


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.db
def test_quality_report_api_and_metrics(engine: Engine, tmp_path: Path) -> None:
    settings = replace(settings_for(tmp_path), api=ApiSettings(token="t0k"))  # noqa: S106
    api = TestClient(create_app(settings, engine))
    auth = {"Authorization": "Bearer t0k"}
    job_id = api.post("/v1/crawls", json={"root_url": f"{SITE}/"}, headers=auth).json()["crawl_id"]
    fetched_before = sample("crawl_urls_total", outcome="fetched")
    asyncio.run(run_worker(settings, engine, once=True,
                           transport=httpx.MockTransport(FakeSite())))  # fmt: skip

    assert api.get(f"/v1/crawls/{job_id}/quality").status_code == 401
    assert api.get("/v1/crawls/999999/quality", headers=auth).status_code == 404
    resp = api.get(f"/v1/crawls/{job_id}/quality", headers=auth)
    assert resp.status_code == 200
    q = resp.json()
    job = api.get(f"/v1/crawls/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED"
    # The job result keeps the report as it was at finish; nothing changed since.
    assert job["result"]["quality"] == q
    assert q["report_version"] == "1" and q["company_id"] == job["company_id"]

    [site] = job["site_crawls"]
    with engine.connect() as conn:
        durations = conn.execute(text(
            "SELECT duration_ms FROM \"fetch\" WHERE crawl_id = :c AND method = 'http'"
        ), {"c": site["id"]}).scalars().all()  # fmt: skip
        fetched = conn.execute(text(
            "SELECT count(DISTINCT url_id) FROM \"fetch\" WHERE crawl_id = :c AND method = 'http'"
        ), {"c": site["id"]}).scalar_one()  # fmt: skip
        states = dict(conn.execute(text(
            "SELECT state, count(*) FROM crawl_url WHERE crawl_id = :c GROUP BY state"
        ), {"c": site["id"]}).all())  # fmt: skip

    urls = q["urls"]
    assert urls["by_state"] == states and urls["discovered"] == sum(states.values())
    assert urls["fetched"] == fetched > 0
    assert urls["fetched"] <= urls["eligible"] <= urls["discovered"]
    assert urls["deferred"] + urls["trapped"] + urls["blocked"] > 0  # FakeSite has traps / robots
    lat = q["latency_ms"]["http"]
    assert lat["count"] == len(durations) == q["fetches"]["http"]
    assert lat["p50"] == percentile(durations, 50) and lat["p99"] == percentile(durations, 99)
    assert lat["p50"] <= lat["p90"] <= lat["p99"]
    # FakeSite answers 404 for unknown pages and serves a PDF; classes carry no detail.
    assert q["http_errors"]["4xx"] > 0 and q["http_errors"]["unsupported_content_type"] > 0

    pages = q["pages"]
    assert pages["parsed"] == site["pages_parsed"] > 0
    assert pages["duplicates"] > 0 and 0 < pages["duplicate_ratio"] < 1
    assert 0 < pages["relevant"] <= pages["parsed"] - pages["duplicates"]
    assert pages["unique_content_yield"] == round(pages["unique"] / q["fetches"]["http"], 4)
    assert q["evidence"]["evidence"] > 0 and q["evidence"]["observations"] > 0
    assert sum(q["field_states"].values()) > 0 and "UNKNOWN" in q["field_states"]
    assert q["external_sources"] == {"count": 0, "domains": []}  # external phase disabled
    assert q["bytes"] > 0 and q["search_queries"]["total"] == 0
    done = q["completeness"]
    assert done["frontier_remaining"] == 0 and done["budget_exhausted"] is False
    assert done["crawls"][0]["stop_reason"] is None
    # A small site leaves fields open (UNKNOWN, never guessed).
    assert 0 < len(done["gaps_unanswered"]) <= sum(q["field_states"].values())

    # Exported once, at finish.
    assert sample("crawl_urls_total", outcome="fetched") == fetched_before + urls["fetched"]
    assert sample("field_states_total", state="UNKNOWN") > 0
    assert sample("worker_last_seen_timestamp_seconds") > 0
    exposition = api.get("/metrics/").text
    assert "database_up 1.0" in exposition and "crawl_duplicate_ratio_bucket" in exposition
