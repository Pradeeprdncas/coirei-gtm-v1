"""Tracing and log correlation (spec section 26): spans, W3C traceparent, API -> worker."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from gtm_crawler.api import create_app
from gtm_crawler.config import ConfigError, LoggingSettings
from gtm_crawler.log import configure_logging
from gtm_crawler.tracing import continue_trace, parse_traceparent, span, traceparent
from gtm_crawler.worker import run_worker

from .test_crawl import SITE, FakeSite, settings_for

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING = f"00-{TRACE}-00f067aa0ba902b7-01"


@pytest.fixture
def logs(request: pytest.FixtureRequest) -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="INFO", **getattr(request, "param", {})), stream)
    yield stream
    configure_logging(LoggingSettings(level="WARNING"))


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def spans(stream: io.StringIO) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in records(stream):
        if r["event"] == "span":
            out.setdefault(r["span"], []).append(r)
    return out


def test_spans_nest_and_log_lines_carry_the_trace(logs: io.StringIO) -> None:
    with span("outer", job_id=7, note="kept on the span only") as sp:
        with span("inner", url="https://a.example/"):
            logging.getLogger("gtm_crawler.test").info("inside")
        sp["result"] = 3
    inside, inner, outer = records(logs)
    assert inside["trace_id"] == inner["trace_id"] == outer["trace_id"]
    assert len(outer["trace_id"]) == 32 and len(outer["span_id"]) == 16
    assert inside["span_id"] == inner["span_id"] != outer["span_id"]
    assert inner["parent_span_id"] == outer["span_id"] and outer["parent_span_id"] is None
    # Correlation attributes reach every line inside; others stay on the span record.
    assert inside["job_id"] == 7 and inside["url"] == "https://a.example/"
    assert "note" not in inside
    assert outer["attrs"] == {"note": "kept on the span only", "result": 3}
    assert outer["status"] == "ok" and outer["duration_ms"] >= 0 and outer["start"]
    assert traceparent() is None  # the context is restored


def test_error_status_and_redaction(logs: io.StringIO) -> None:
    with pytest.raises(KeyError), span("boom", target="https://u:hunter2@x.example/"):
        raise KeyError("page content must not be logged")
    (rec,) = records(logs)
    assert rec["status"] == "error" and rec["error"] == "KeyError"
    assert "page content" not in json.dumps(rec) and "hunter2" not in json.dumps(rec)


def test_traceparent_parse_format_and_invalid_headers() -> None:
    ctx = parse_traceparent(INCOMING)
    assert ctx is not None and ctx.trace_id == TRACE and ctx.sampled
    for bad in (None, "", "garbage", f"01-{TRACE}-00f067aa0ba902b7-01",
                f"00-{'0' * 32}-00f067aa0ba902b7-01", f"00-{TRACE}-{'0' * 16}-01",
                f"00-{TRACE}-00f067aa0ba902b7"):  # fmt: skip
        assert parse_traceparent(bad) is None
    with continue_trace(INCOMING), span("child"):
        header = traceparent()
        assert header is not None and header.startswith(f"00-{TRACE}-")
        assert header.endswith("-01") and "00f067aa0ba902b7" not in header
    with continue_trace("not-a-traceparent"), span("fresh"):
        fresh = traceparent()
        assert fresh is not None and TRACE not in fresh


@pytest.mark.parametrize("logs", [{"trace_sample_rate": 0.0}], indirect=True)
def test_unsampled_traces_keep_ids_but_drop_span_records(logs: io.StringIO) -> None:
    with span("quiet"):
        logging.getLogger("gtm_crawler.test").info("inside")
        assert (traceparent() or "").endswith("-00")
    (line,) = records(logs)
    assert line["event"] == "inside" and len(line["trace_id"]) == 32
    with pytest.raises(ConfigError):
        LoggingSettings(trace_sample_rate=1.5)


class HeaderSpy(FakeSite):
    def __init__(self) -> None:
        super().__init__()
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        return super().__call__(request)


@pytest.mark.db
def test_api_trace_continues_in_the_worker(
    engine: Engine, tmp_path: Path, logs: io.StringIO
) -> None:
    settings = settings_for(tmp_path)
    api = TestClient(create_app(settings, engine))
    created = api.post("/v1/crawls", json={"root_url": f"{SITE}/"},
                       headers={"traceparent": INCOMING})  # fmt: skip
    assert created.status_code == 201
    assert created.headers["traceparent"].startswith(f"00-{TRACE}-")
    job_id = created.json()["crawl_id"]
    # Asking again returns the job's own trace, whatever the new request carries.
    again = api.post("/v1/crawls", json={"root_url": f"{SITE}/"})
    assert (
        again.status_code == 200 and again.headers["traceparent"] == created.headers["traceparent"]
    )
    other = api.get(f"/v1/crawls/{job_id}", headers={"traceparent": "bogus"})
    assert TRACE not in other.headers["traceparent"]  # invalid: a new trace
    assert "traceparent" not in api.get("/livez").headers

    site = HeaderSpy()
    ran = asyncio.run(run_worker(settings, engine, once=True, owner="worker-t",
                                 transport=httpx.MockTransport(site)))  # fmt: skip
    assert ran == 1
    # Never sent to crawled sites.
    assert site.headers and not any("traceparent" in h for h in site.headers)

    found = spans(logs)
    (request,) = [s for s in found["http.request"] if s["trace_id"] == TRACE]
    assert request["parent_span_id"] == "00f067aa0ba902b7"
    (job,) = found["job.run"]
    assert job["trace_id"] == TRACE and job["parent_span_id"] == request["span_id"]
    assert job["job_id"] == job_id and job["worker"] == "worker-t"
    assert job["attrs"]["outcome"] == "completed"
    (site_crawl,) = found["crawl.site"]
    assert site_crawl["parent_span_id"] == job["span_id"] and site_crawl["crawl_id"]
    for name in ("url.process", "fetch.http", "parse", "store", "extract", "assess"):
        assert found[name], name
        for s in found[name]:
            assert s["trace_id"] == TRACE and s["job_id"] == job_id
            assert s["worker"] == "worker-t" and s["crawl_id"] == site_crawl["crawl_id"]
            assert s["stage"]
    fetch = next(s for s in found["fetch.http"] if s["url"] == f"{SITE}/about")
    assert fetch["domain"] == "acme.example" and fetch["attrs"]["http_status"] == 200
    process = {s["span_id"] for s in found["url.process"]}
    assert all(s["parent_span_id"] in process for s in found["fetch.http"])
    # Ordinary log lines of the job carry the trace too.
    fetched = [r for r in records(logs) if r["event"] == "fetch.completed" and "url" in r]
    assert fetched and all(r["trace_id"] == TRACE for r in fetched)

    dossier = api.get(f"/v1/companies/{created.json()['company_id']}/dossier")
    assert dossier.status_code == 200 and spans(logs)["dossier.build"]
