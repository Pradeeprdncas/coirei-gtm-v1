"""Cost accounting: resource usage per job and crawl, priced (spec sections 21, 27).

When a job run ends (``pipeline.run_job``), each of the job's site crawls is
measured from what it already recorded, and priced with ``[cost]``:

* ``http``: ``requests`` (fetches, robots.txt and sitemap requests) and
  ``bytes`` (response bodies of fetches and sitemaps).
* ``browser``: ``renders``, ``seconds`` of render time, ``bytes`` the pages requested.
* ``search``: ``queries`` answered, per provider.
* ``storage``: ``bytes`` of raw artifacts the crawl references (uncompressed).
* ``worker``: ``seconds`` of crawl wall time (resumed crawls included).
* ``database``: ``seconds`` of statement time of this run of the job
  (``metered``; summed by the statement timer in ``metrics.py``).

Crawl rows are keyed per crawl and measure it cumulatively, database rows
per run, so recording again (a retry, a resumed job, a second worker after a
lost lease) updates rows instead of adding them. A run that dies before it
records loses its database time; everything else is re-measured.

An unknown price (0) records the quantity with cost 0. Prometheus:
``cost_usd_total{kind}`` and ``resource_usage_total{kind,unit}``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Connection, Engine, func, select, union
from sqlalchemy.dialects.postgresql import insert

from .config import CostSettings
from .db.models import (
    Artifact,
    CostEvent,
    Crawl,
    CrawlJob,
    Fetch,
    RobotsSnapshot,
    SearchQuery,
    SitemapFetch,
)
from .metrics import COST_USD, DB_SECONDS, RESOURCE_USAGE

log = logging.getLogger(__name__)

GB = Decimal(10**9)
# (kind, unit) -> the [cost] price; bytes are priced per GB.
PRICES = {
    ("http", "requests"): "http_request_usd", ("http", "bytes"): "http_gb_usd",
    ("browser", "renders"): "browser_render_usd", ("browser", "seconds"): "browser_second_usd",
    ("browser", "bytes"): "browser_gb_usd", ("storage", "bytes"): "storage_gb_usd",
    ("database", "seconds"): "database_second_usd", ("worker", "seconds"): "worker_second_usd",
}  # fmt: skip
Usage = tuple[str, str, str | None, Decimal]  # kind, unit, provider, quantity


@contextlib.contextmanager
def metered() -> Iterator[list[float]]:
    """Sum the statement seconds of this context (threads started from it included)."""
    seconds = [0.0]
    token = DB_SECONDS.set(seconds)
    try:
        yield seconds
    finally:
        DB_SECONDS.reset(token)


def price(prices: CostSettings, kind: str, unit: str, provider: str | None,
          quantity: Decimal) -> Decimal:  # fmt: skip
    if kind == "search":
        unit_price = prices.search_prices().get(provider or "", 0.0)
    else:
        unit_price = getattr(prices, PRICES[(kind, unit)])
    cost = quantity * Decimal(str(unit_price)) / (GB if unit == "bytes" else 1)
    return cost.quantize(Decimal("1e-8"), ROUND_HALF_UP)


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("1e-6"), ROUND_HALF_UP)


def crawl_usage(conn: Connection, crawl: Any) -> list[Usage]:
    """What one crawl used, from its rows and stats."""
    cid = crawl.id

    def scalar(query: Any) -> Any:
        return conn.execute(query).scalar() or 0

    fetches = {m: (n, b or 0) for m, n, b in conn.execute(
        select(Fetch.method, func.count(), func.sum(Fetch.content_length))
        .where(Fetch.crawl_id == cid).group_by(Fetch.method)
    ).tuples()}  # fmt: skip
    http_n, http_bytes = fetches.get("http", (0, 0))
    requests = (http_n + scalar(select(func.count()).where(RobotsSnapshot.crawl_id == cid))
                + scalar(select(func.count()).where(SitemapFetch.crawl_id == cid)))  # fmt: skip
    sitemap_bytes = scalar(select(func.sum(SitemapFetch.content_length))
                           .where(SitemapFetch.crawl_id == cid))  # fmt: skip
    shas = union(
        select(Fetch.raw_sha256.label("sha")).where(Fetch.crawl_id == cid),
        select(RobotsSnapshot.raw_sha256).where(RobotsSnapshot.crawl_id == cid),
        select(SitemapFetch.raw_sha256).where(SitemapFetch.crawl_id == cid),
    ).subquery()
    # ponytail: content-addressed artifacts shared by several crawls count for each.
    stored = scalar(select(func.sum(Artifact.size_bytes)).where(Artifact.sha256.in_(select(shas))))
    stats = crawl.stats or {}
    browser = stats.get("browser") or {}
    out: list[Usage] = [
        ("http", "requests", None, _qty(requests)),
        ("http", "bytes", None, _qty(http_bytes + sitemap_bytes)),
        ("browser", "renders", None, _qty(fetches.get("browser", (0, 0))[0])),
        ("browser", "seconds", None, _qty((browser.get("render_ms") or 0) / 1000)),
        ("browser", "bytes", None, _qty(browser.get("bytes"))),
        ("storage", "bytes", None, _qty(stored)),
        ("worker", "seconds", None, _qty(stats.get("duration_seconds"))),
    ]
    for provider, n in conn.execute(
        select(SearchQuery.provider, func.count())
        .where(SearchQuery.crawl_id == cid, SearchQuery.status == "ok")
        .group_by(SearchQuery.provider)
    ).tuples():  # fmt: skip
        out.append(("search", "queries", provider, _qty(n)))
    return out


def record_job_costs(
    engine: Engine, prices: CostSettings, job_id: int, run_id: str, db_seconds: float
) -> dict[str, Any]:
    """Measure and price the job's crawls and this run's database time; upsert
    the rows (idempotent) and return the job's cost summary."""
    with engine.begin() as conn:
        org_id = conn.execute(
            select(CrawlJob.organization_id).where(CrawlJob.id == job_id)
        ).scalar_one()  # fmt: skip
        rows: list[dict[str, Any]] = []
        for crawl in conn.execute(select(Crawl).where(Crawl.job_id == job_id)).all():
            for kind, unit, provider, qty in crawl_usage(conn, crawl):
                if qty:
                    rows.append({"key": f"crawl:{crawl.id}:{kind}:{unit}:{provider or ''}",
                                 "crawl_id": crawl.id, "kind": kind, "unit": unit,
                                 "provider": provider, "quantity": qty})  # fmt: skip
        rows.append({"key": f"run:{job_id}:{run_id}:database:seconds", "crawl_id": None,
                     "kind": "database", "unit": "seconds", "provider": None,
                     "quantity": _qty(db_seconds)})  # fmt: skip
        for r in rows:
            r.update(
                organization_id=org_id,
                job_id=job_id,
                cost_usd=price(prices, r["kind"], r["unit"], r["provider"], r["quantity"]),
            )
        before = {
            k: (q, c) for k, q, c in conn.execute(
                select(CostEvent.key, CostEvent.quantity, CostEvent.cost_usd)
                .where(CostEvent.key.in_([r["key"] for r in rows])).with_for_update()
            ).tuples()
        }  # fmt: skip
        stmt = insert(CostEvent).values(rows)
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"quantity": stmt.excluded.quantity, "cost_usd": stmt.excluded.cost_usd,
                  "recorded_at": func.now()},
        ))  # fmt: skip
        summary = cost_summary(conn, CostEvent.job_id == job_id)
    for r in rows:  # counters grow by what this recording added
        q, c = before.get(r["key"], (Decimal(0), Decimal(0)))
        RESOURCE_USAGE.labels(r["kind"], r["unit"]).inc(float(max(r["quantity"] - q, 0)))
        COST_USD.labels(r["kind"]).inc(float(max(r["cost_usd"] - c, 0)))
    return summary


def record_quietly(
    engine: Engine, prices: CostSettings, job_id: int, run_id: str, db_seconds: float
) -> dict[str, Any] | None:
    """``record_job_costs``, but a failure is logged, never raised: cost
    accounting must not fail or block a job."""
    try:
        return record_job_costs(engine, prices, job_id, run_id, db_seconds)
    except Exception:  # noqa: BLE001
        log.warning("cost.record_failed", extra={"job_id": job_id}, exc_info=True)
        return None


def cost_summary(conn: Connection, where: Any) -> dict[str, Any]:
    """Totals by kind (cost and usage by unit) of the cost events matching ``where``."""
    usage: dict[str, dict[str, float]] = {}
    costs: dict[str, Decimal] = {}
    for kind, unit, qty, cost in conn.execute(
        select(CostEvent.kind, CostEvent.unit, func.sum(CostEvent.quantity),
               func.sum(CostEvent.cost_usd))
        .where(where).group_by(CostEvent.kind, CostEvent.unit)
        .order_by(CostEvent.kind, CostEvent.unit)
    ).tuples():  # fmt: skip
        usage.setdefault(kind, {})[unit] = float(qty)
        costs[kind] = costs.get(kind, Decimal(0)) + cost
    return {
        "currency": "USD",
        "total_usd": float(sum(costs.values(), Decimal(0))),
        "by_kind": {k: {"cost_usd": float(costs[k]), "usage": usage[k]} for k in usage},
    }


def company_costs(engine: Engine, organization_id: int) -> dict[str, Any]:
    """The company's cost: totals, and per job (newest first)."""
    with engine.connect() as conn:
        jobs = conn.execute(
            select(CostEvent.job_id).where(CostEvent.organization_id == organization_id)
            .group_by(CostEvent.job_id).order_by(CostEvent.job_id.desc())
        ).scalars().all()  # fmt: skip
        return {
            "company_id": organization_id,
            **cost_summary(conn, CostEvent.organization_id == organization_id),
            "jobs": [{"job_id": j, **cost_summary(conn, CostEvent.job_id == j)} for j in jobs],
        }
