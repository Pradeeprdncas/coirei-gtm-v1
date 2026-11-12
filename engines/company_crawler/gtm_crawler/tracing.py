"""Lightweight distributed tracing on the stdlib (spec section 26).

``span(name, **attrs)`` times a unit of work (a job, a site crawl, one URL's
fetch/parse/store, a search query, extraction, assessment, the dossier) and
logs one ``span`` record when it ends, through the structured JSON log
(``log.py``), so span attributes are redacted like every other log field::

    {"event": "span", "span": "fetch.http", "trace_id": "...", "span_id": "...",
     "parent_span_id": "...", "start": "...", "duration_ms": 12.3,
     "status": "ok", "job_id": 7, "crawl_id": 9, "url": "...", "stage": "fetch"}

Ids follow W3C Trace Context (32-hex trace id, 16-hex span id), so a trace
crosses processes as a ``traceparent`` header: the API continues an incoming
one on ``POST /v1/crawls``, stores it with the job (the ``created``
crawl_event's ``detail``) and the worker continues it, so one job's API
request, crawls and URLs share one ``trace_id``. Every log line inside a span
carries ``trace_id`` and ``span_id`` (and the span's correlation attributes:
job, crawl, url, domain, worker, stage).

Context lives in a ContextVar, so spans nest across ``await``, tasks and
``asyncio.to_thread``. ``logging.trace_sample_rate`` decides, per new trace,
whether its span records are logged (ids are always assigned and logged).

``traceparent`` is never sent to crawled sites: it would let third parties
link our requests together (fingerprinting) and leak internal ids.
"""

from __future__ import annotations

import logging
import random
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, NamedTuple

from .log import log_context

log = logging.getLogger("gtm_crawler.trace")

# Attributes that also go into the log context of everything logged inside the span.
CORRELATION = frozenset({"job_id", "crawl_id", "url", "domain", "worker", "stage"})
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
SAMPLE_RATE = 1.0  # set by log.configure_logging from logging.trace_sample_rate


class SpanContext(NamedTuple):
    trace_id: str
    span_id: str
    sampled: bool


_CURRENT: ContextVar[SpanContext | None] = ContextVar("gtm_span", default=None)


def parse_traceparent(header: str | None) -> SpanContext | None:
    """A W3C ``traceparent`` (version 00); None if it is missing or invalid."""
    m = _TRACEPARENT.fullmatch((header or "").strip().lower())
    if m is None or set(m[1]) == {"0"} or set(m[2]) == {"0"}:
        return None
    return SpanContext(m[1], m[2], bool(int(m[3], 16) & 1))


def traceparent() -> str | None:
    """The current span as a ``traceparent`` header value."""
    cur = _CURRENT.get()
    return cur and f"00-{cur.trace_id}-{cur.span_id}-{'01' if cur.sampled else '00'}"


@contextmanager
def continue_trace(header: str | None) -> Iterator[None]:
    """Make spans started inside children of the remote span ``header`` names."""
    parent = parse_traceparent(header)
    if parent is None:
        yield  # absent or invalid: the next span starts a new trace
        return
    token = _CURRENT.set(parent)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Time the block as span ``name``; a child of the current span, if any.

    Yields the span's non-correlation attributes, to add results to."""
    parent = _CURRENT.get()
    if parent is None:
        parent_id = None
        sampled = random.random() < SAMPLE_RATE  # noqa: S311 - sampling, not security
        trace_id = secrets.token_hex(16)
    else:
        parent_id, trace_id, sampled = parent.span_id, parent.trace_id, parent.sampled
    ctx = SpanContext(trace_id, secrets.token_hex(8), sampled)
    fields = {k: v for k, v in attrs.items() if k in CORRELATION}
    other = {k: v for k, v in attrs.items() if k not in CORRELATION}
    token = _CURRENT.set(ctx)
    start, t0 = datetime.now(UTC), time.perf_counter()
    status, error = "ok", None
    try:
        with log_context(trace_id=trace_id, span_id=ctx.span_id, **fields):
            try:
                yield other
            except BaseException as exc:
                status, error = "error", type(exc).__name__
                raise
            finally:
                if sampled:
                    log.info("span", extra={
                        "span": name, "parent_span_id": parent_id,
                        "start": start.isoformat(), "status": status, "error": error,
                        "duration_ms": round((time.perf_counter() - t0) * 1000, 3),
                        **({"attrs": other} if other else {}),
                    })  # fmt: skip
    finally:
        _CURRENT.reset(token)
