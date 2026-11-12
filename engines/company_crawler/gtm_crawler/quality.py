"""Crawl-quality report for one crawl job (spec section 27, cost items excepted).

Computed from the database (the source of truth) over every crawl of the
job (the company's site and its competitors), plus the crawls' own
statistics for what only they count (bytes, stop reason). Definitions:

* URLs (``crawl_url`` rows): ``discovered`` every recorded URL, admitted or
  refused (refusals past ``frontier.max_recorded_rejections`` are only
  counted in the crawl stats' ``frontier_rejections``); ``eligible`` admitted
  to the frontier: every URL but those refused at admission (``trap`` or
  ``deferred`` and never claimed); ``fetched`` distinct URLs with an HTTP
  fetch; ``blocked`` / ``deferred`` / ``trapped`` by current state.
* ``http_errors``: HTTP fetches by error class (``fetch.http.FetchError``) or
  ``4xx`` / ``5xx``.
* ``pages.relevant``: distinct pages cited as evidence by an observation of
  the latest extraction run: a page that yielded at least one fact candidate.
  (URL priority scores say what a page might hold; this says what it held.)
* ``pages.duplicate_ratio``: duplicate pages / parsed pages;
  ``pages.unique_content_yield``: unique pages / HTTP fetches.
* ``field_states``: states of the company's latest assessment (company and
  product fields); ``completeness.gaps_unanswered``: its fields with no
  SUPPORTED or CORROBORATED item (``assess.field_gaps``).
* ``external_sources``: distinct registrable domains other than the crawled
  organization's whose pages are cited by a current observation.
* ``latency_ms``: nearest-rank percentiles of ``fetch.duration_ms`` by method.

``observe`` exports a finished job's report to Prometheus (``metrics.py``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, text

from .assess import field_gaps
from .domain import OPEN_URL_STATES
from .metrics import (
    CRAWL_URLS,
    DUPLICATE_RATIO,
    EXTERNAL_SOURCES,
    GAPS_UNANSWERED,
    PAGES_RELEVANT,
    UNIQUE_CONTENT_YIELD,
)

# Bump when a definition above changes (recorded in versions.current_versions).
QUALITY_REPORT_VERSION = "1"
BUDGET_STOPS = frozenset({"max_pages", "max_bytes", "max_duration", "error_rate"})
PERCENTILES = (50, 90, 99)


def percentile(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile (an observed value); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = -(-p * len(ordered) // 100)  # ceil(p/100 * n)
    return ordered[max(int(rank), 1) - 1]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def quality_report(engine: Engine, job_id: int) -> dict[str, Any] | None:
    """The job's quality report; None if there is no such job."""
    q: dict[str, Any] = {"ids": [], "open": sorted(s.value for s in OPEN_URL_STATES)}
    with engine.connect() as conn:

        def rows(sql: str) -> list[Any]:
            return list(conn.execute(text(sql), q).all())

        job = conn.execute(
            text("SELECT organization_id FROM crawl_job WHERE id = :id"), {"id": job_id}
        ).one_or_none()  # fmt: skip
        if job is None:
            return None
        crawls = conn.execute(
            text("SELECT id, organization_id, status, stats FROM crawl WHERE job_id = :id"
                 " ORDER BY id"), {"id": job_id},
        ).all()  # fmt: skip
        q["ids"] = [c.id for c in crawls]
        by_state = dict(rows(
            "SELECT state, count(*) FROM crawl_url WHERE crawl_id = ANY(:ids) GROUP BY state"
        ))  # fmt: skip
        refused = conn.execute(text(
            "SELECT count(*) FROM crawl_url WHERE crawl_id = ANY(:ids)"
            " AND state IN ('trap', 'deferred') AND attempts = 0"
        ), q).scalar_one()  # fmt: skip
        open_by_crawl = dict(rows(
            "SELECT crawl_id, count(*) FROM crawl_url WHERE crawl_id = ANY(:ids)"
            " AND state = ANY(:open)"
            " GROUP BY crawl_id"
        ))  # fmt: skip
        fetches = rows(
            'SELECT method, url_id, status_code, error, duration_ms FROM "fetch"'
            " WHERE crawl_id = ANY(:ids)"
        )
        pages = conn.execute(text(
            "SELECT count(*), count(p.duplicate_of_page_id),"
            " count(DISTINCT p.text_sha256) FILTER (WHERE p.duplicate_of_page_id IS NULL)"
            " FROM page p JOIN \"fetch\" f ON f.id = p.fetch_id WHERE f.crawl_id = ANY(:ids)"
        ), q).one()  # fmt: skip
        evidence = conn.execute(text(
            "SELECT count(*) FROM evidence e JOIN page p ON p.id = e.page_id"
            " JOIN \"fetch\" f ON f.id = p.fetch_id WHERE f.crawl_id = ANY(:ids)"
        ), q).scalar_one()  # fmt: skip
        # Observations of each crawl's latest extraction run (models.is_current).
        current = (
            " FROM observation o JOIN evidence e ON e.id = o.evidence_id"
            ' JOIN page p ON p.id = e.page_id JOIN "fetch" f ON f.id = p.fetch_id'
            " JOIN url u ON u.id = coalesce(f.final_url_id, f.url_id)"
            " JOIN crawl c ON c.id = o.crawl_id"
            " JOIN organization org ON org.id = c.organization_id"
            " WHERE o.crawl_id = ANY(:ids) AND o.extraction_id ="
            " (SELECT max(x.id) FROM extraction x WHERE x.crawl_id = o.crawl_id)"
        )
        observations, relevant = conn.execute(
            text(f"SELECT count(*), count(DISTINCT p.id) {current}"), q
        ).one()  # fmt: skip
        external = sorted(r[0] for r in rows(
            f"SELECT DISTINCT u.registrable_domain {current}"  # noqa: S608 - constant SQL
            " AND u.registrable_domain IS DISTINCT FROM org.registrable_domain"
        ))  # fmt: skip
        searches = dict(rows(
            "SELECT status, count(*) FROM search_query WHERE crawl_id = ANY(:ids) GROUP BY status"
        ))  # fmt: skip
        states: dict[str, int] = dict(conn.execute(text(
            "SELECT state, count(*) FROM field_assessment WHERE organization_id = :org"
            " AND computed_at = (SELECT max(computed_at) FROM field_assessment"
            " WHERE organization_id = :org) GROUP BY state"
        ), {"org": job.organization_id}).tuples().all())  # fmt: skip

    http = [f for f in fetches if f.method == "http"]
    errors: dict[str, int] = {}
    for f in http:
        # fetch.error is "<FetchError>[: detail]".
        cls = (f.error or "").split(":")[0] or (
            f"{f.status_code // 100}xx" if (f.status_code or 0) >= 400 else None
        )
        if cls:
            errors[cls] = errors.get(cls, 0) + 1
    latency = {}
    for method in sorted({f.method for f in fetches}):
        ms = [f.duration_ms for f in fetches if f.method == method and f.duration_ms is not None]
        latency[method] = {"count": len(ms), **{f"p{p}": percentile(ms, p) for p in PERCENTILES}}
    parsed, duplicates, unique = pages
    stats = [c.stats or {} for c in crawls]
    gaps = sorted(field_gaps(engine, job.organization_id)) if states else []
    per_crawl = [
        {"crawl_id": c.id, "company_id": c.organization_id, "status": c.status,
         "stop_reason": s.get("stop_reason"),
         "budget_exhausted": s.get("stop_reason") in BUDGET_STOPS,
         "frontier_remaining": open_by_crawl.get(c.id, 0)}
        for c, s in zip(crawls, stats, strict=True)
    ]  # fmt: skip
    discovered = sum(by_state.values())
    return {
        "report_version": QUALITY_REPORT_VERSION,
        "job_id": job_id,
        "company_id": job.organization_id,
        "urls": {
            "discovered": discovered,
            "eligible": discovered - refused,
            "fetched": len({f.url_id for f in http}),
            "blocked": by_state.get("blocked", 0),
            "deferred": by_state.get("deferred", 0),
            "trapped": by_state.get("trap", 0),
            "by_state": by_state,
        },
        "fetches": {"http": len(http), "browser": len(fetches) - len(http)},
        "http_errors": errors,
        "pages": {
            "parsed": parsed,
            "duplicates": duplicates,
            "unique": unique,
            "relevant": relevant,
            "duplicate_ratio": _ratio(duplicates, parsed),
            "unique_content_yield": _ratio(unique, len(http)),
        },
        "evidence": {"evidence": evidence, "observations": observations},
        "field_states": states,
        "external_sources": {"count": len(external), "domains": external},
        "browser_renders": len(fetches) - len(http),
        "search_queries": {"total": sum(searches.values()), "by_status": searches},
        "bytes": sum(s.get("bytes") or 0 for s in stats),
        "latency_ms": latency,
        "completeness": {
            "frontier_remaining": sum(open_by_crawl.values()),
            "budget_exhausted": any(c["budget_exhausted"] for c in per_crawl),
            "gaps_unanswered": gaps,
            "crawls": per_crawl,
        },
    }


def observe(report: dict[str, Any]) -> None:
    """Export a finished job's report to Prometheus (once per job)."""
    urls = report["urls"]
    for outcome in ("discovered", "eligible", "fetched", "blocked", "deferred", "trapped"):
        CRAWL_URLS.labels(outcome).inc(urls[outcome])
    PAGES_RELEVANT.inc(report["pages"]["relevant"])
    EXTERNAL_SOURCES.inc(report["external_sources"]["count"])
    GAPS_UNANSWERED.observe(len(report["completeness"]["gaps_unanswered"]))
    for histogram, value in (
        (DUPLICATE_RATIO, report["pages"]["duplicate_ratio"]),
        (UNIQUE_CONTENT_YIELD, report["pages"]["unique_content_yield"]),
    ):
        if value is not None:
            histogram.observe(value)
