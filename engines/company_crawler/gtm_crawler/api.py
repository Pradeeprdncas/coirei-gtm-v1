"""``gtm-api``: the job API (production LLD section 31; FastAPI, approved 2026-09-29).

A "crawl" in the API is a crawl job: the whole run for one company (its
site, the external phase, competitors). The API only records and reads
jobs; ``gtm-worker`` processes run them.

* ``POST /v1/crawls`` ``{"root_url": ..., "domain"?: ..., "idempotency_key"?: ...}``
  (or an ``Idempotency-Key`` header). 201 with a new job, 200 with the
  existing one: the same key, or the company's unfinished job.
* ``GET /v1/crawls/{id}``: status, timestamps, the job's site crawls, result.
* ``POST /v1/crawls/{id}/pause`` and ``/resume``.
* ``GET /v1/companies/{id}/dossier``: the current dossier, built on request.
* ``GET /v1/companies/{id}/cost``: what crawling the company cost, in total
  and per job, by kind (spec section 27; ``cost.py``).
* ``GET /v1/companies/{id}/lineage/{field}[?product_id=]``: the field's latest
  assessment and, for every observation it cites, the chain back to the
  retrieval event and raw artifact (spec section 25).
* ``GET /healthz`` (database reachable) and ``GET /metrics`` (Prometheus).
* ``GET /v1/dead-letters``, ``POST /v1/dead-letters/{id}/requeue`` and
  ``/discard``: the dead-letter queue (spec section 23).
* ``GET /v1/crawls/{id}/quality``: the job's crawl-quality report, computed
  on request (spec section 27; ``quality.py``).
* ``/v1/market/...``: market research runs and sizing (``market/api.py``).

``POST /v1/crawls`` answers 429 with ``Retry-After`` while
``api.max_queued_jobs`` jobs are waiting for a worker (backpressure).

With ``api.token`` set, every ``/v1`` request needs ``Authorization: Bearer
<token>`` (or, during a rotation, ``api.previous_token``). The API listens on
loopback unless ``api.host`` says otherwise.
Requests to ``/v1`` are traced (``tracing.py``): an incoming W3C
``traceparent`` is continued and every response carries one; a new job keeps
its request's trace, which the worker continues.
Responses are JSON only, sent with ``X-Content-Type-Options: nosniff`` and a
``default-src 'none'`` CSP: crawled text is returned as data and never
rendered (stored XSS, section 28). Reusing an idempotency key for another
``root_url`` is a 409.
"""

from __future__ import annotations

import hmac
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select, text
from starlette.concurrency import run_in_threadpool

from . import __version__
from .business_profile import build_business_profile
from .config import ConfigError, Settings, load_settings, secret_values
from .cost import company_costs
from .db import make_engine
from .db.evidence import EvidenceStore
from .db.migrate import known_revisions, migration_head
from .db.models import Crawl, CrawlJob, Organization, Product
from .deadletter import DeadLetterError, DeadLetters
from .documents import DocumentError, ingest_document
from .domain import FINAL_JOB_STATES
from .dossier import DOSSIER_FIELDS, build_dossier, clean_dossier
from .jobs import JobStore, QueueFull
from .live import Screenshots, recent_pages, stored_page
from .log import configure_logging
from .market.api import add_market_routes
from .metrics import instrument_engine
from .onboarding import VerificationRequest, onboarding_view, save_verification, verification
from .profile import stored_profile
from .quality import quality_report
from .questions import AnswerRequest, questions_view, save_answers
from .rawstore import make_rawstore
from .tracing import continue_trace, span, traceparent
from .urls import InvalidUrl, normalize_url, registrable_domain, url_host

# On every response. JSON never renders, but should a browser be pointed at
# the API, crawled text must not run or be framed (stored XSS, section 28).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# The Swagger UI page alone (api.docs): its scripts and styles from the CDN
# FastAPI's page uses, and calls back to this API only.
DOCS_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src data: https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'"
)


# What /openapi.json (and Swagger UI, with api.docs) says about the API as a whole.
API_DESCRIPTION = """\
Crawls a company's website and builds a cited GTM dossier.

A **crawl** here is a job: the company's own site, the bounded external (search)
phase and its competitors' sites. The API only records and reads jobs;
`gtm-worker` processes run them.

**Typical use:** `POST /v1/crawls` → poll `GET /v1/crawls/{crawl_id}` until
`COMPLETED` → `GET /v1/companies/{company_id}/dossier`.

**Auth:** with `api.token` set, every `/v1` request needs
`Authorization: Bearer <token>`; without it the header is ignored. Probes and
`/metrics` are never authenticated.

**Not listed below:** `GET /metrics` (Prometheus text format).
"""
API_TAGS = [
    {"name": "Crawls", "description": "Start, follow and control crawl jobs."},
    {"name": "Companies", "description": "Results for a crawled company."},
    {"name": "Onboarding", "description": "Company documents, the onboarding view to verify "
                                         "and the user's verification."},
    {"name": "Dead letters", "description": "URLs and jobs that failed for good, to inspect, "
                                            "requeue or discard."},
    {"name": "Market research", "description": "Industry and product market research runs, "
                                               "their reports, and market sizing."},
    {"name": "Probes", "description": "Liveness and readiness for orchestrators."},
]  # fmt: skip
Responses = dict[int | str, dict[str, Any]]
NO_CRAWL: Responses = {404: {"description": "No crawl job with this id."}}
NO_COMPANY: Responses = {404: {"description": "No company with this id."}}
RESOLVED: Responses = {409: {"description": "The item does not exist or is already resolved."}}


class CrawlRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"root_url": "https://example.com"}]})

    root_url: str = Field(max_length=2048, description="Company website to crawl (http or https).")
    domain: str | None = Field(
        default=None, max_length=253,
        description="Optional safety check: must equal root_url's registrable domain "
                    "(`example.com`), else 422. Leave it out.",
    )  # fmt: skip
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="Optional. Repeating a request with the same key returns the same job; "
                    "reusing it for another root_url is a 409.",
    )  # fmt: skip


def job_view(job: Any, crawls: list[Any]) -> dict[str, Any]:
    return {
        "crawl_id": job.id,
        "company_id": job.organization_id,
        "root_url": job.root_url,
        "kind": job.kind,
        "status": job.status.upper(),
        "stage": job.stage,
        "pause_requested": job.pause_requested,
        "cancel_requested": job.cancel_requested,
        "versions": job.versions,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error_summary": job.error_summary,
        "site_crawls": [
            {"id": c.id, "company_id": c.organization_id, "seed_url": c.seed_url,
             "status": c.status, "started_at": c.started_at, "finished_at": c.finished_at,
             "fetches": c.stats.get("fetches"), "pages_parsed": c.stats.get("pages_parsed"),
             "stop_reason": c.stats.get("stop_reason")}
            for c in crawls
        ],
        "result": job.result,
    }  # fmt: skip


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    # No Swagger/ReDoc HTML pages: the API serves JSON only (/openapi.json stays),
    # unless api.docs turns Swagger UI on for local development.
    docs_url = "/docs" if settings.api.docs else None
    app = FastAPI(title="GTM Intelligence Crawler", version=__version__,
                  description=API_DESCRIPTION, openapi_tags=API_TAGS,
                  docs_url=docs_url, redoc_url=None)  # fmt: skip
    jobs = JobStore(engine)
    rawstore = make_rawstore(settings.rawstore)
    evidence = EvidenceStore(engine, rawstore, settings.sources)
    screenshots = Screenshots(settings, rawstore)
    app.router.on_shutdown.append(screenshots.close)
    instrument_engine(engine)
    app.mount("/metrics", make_asgi_app())

    # ponytail: per process and per connecting address; behind a proxy every
    # client is the proxy, so limit there too (or use a shared store).
    window, seen = 0, Counter[str]()

    def over_limit(client: str) -> bool:
        nonlocal window
        minute = int(time.monotonic() // 60)
        if minute != window:
            window = minute
            seen.clear()
        seen[client] += 1
        return seen[client] > settings.api.rate_limit_per_minute

    @app.middleware("http")
    async def nosniff(request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith("/v1/"):
            response = await call_next(request)
        elif over_limit(request.client.host if request.client else ""):
            response = JSONResponse({"detail": "rate limit exceeded"}, 429,
                                    {"Retry-After": "60"})  # fmt: skip
        else:  # traced; probes and metrics scrapes are not
            with (
                continue_trace(request.headers.get("traceparent")),
                span("http.request", method=request.method, path=request.url.path),
            ):
                response = await call_next(request)
                response.headers.setdefault("traceparent", traceparent() or "")
        response.headers.update(SECURITY_HEADERS)
        if docs_url and request.url.path == docs_url:
            response.headers["Content-Security-Policy"] = DOCS_CSP
        return response

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        token, previous = settings.api.token, settings.api.previous_token
        if not token:
            return
        # Both compared every time (no short-circuit on which one matched).
        given = (authorization or "").encode()
        ok = hmac.compare_digest(given, f"Bearer {token}".encode())
        ok |= bool(previous) and hmac.compare_digest(given, f"Bearer {previous}".encode())
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid bearer token")

    guarded = [Depends(authorized)]

    def view(job_id: int) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no crawl {job_id}")
        with engine.connect() as conn:
            crawls = conn.execute(
                select(Crawl).where(Crawl.job_id == job_id).order_by(Crawl.id)
            ).all()  # fmt: skip
        return job_view(job, list(crawls))

    not_ready: Responses = {
        503: {"description": "Database unreachable (`unavailable`) or its schema is "
                             "behind this code (`migrations_pending`)."}
    }  # fmt: skip

    @app.get("/livez", tags=["Probes"], summary="Liveness")
    def livez() -> dict[str, str]:
        """Liveness: the process answers. No dependencies checked."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["Probes"], summary="Readiness", responses=not_ready)
    def readyz(response: Response) -> dict[str, Any]:
        """Readiness: the database answers and its schema is not behind this code's head.

        A schema a newer release already migrated still answers 200, with `schema_ahead`.
        """
        head = migration_head()
        try:
            with engine.connect() as conn:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "database": type(exc).__name__}
        if current != head and current in known_revisions():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "migrations_pending", "schema": current, "expected": head}
        if current != head:
            # A revision this code does not ship: a newer release already migrated.
            # Its migrations are additive (deploy/canary-gate.sh), so this code
            # keeps serving during the rolling deploy (docs/staging.md).
            return {"status": "ok", "schema": current, "expected": head, "schema_ahead": True}
        return {"status": "ok", "schema": current}

    @app.get("/healthz", tags=["Probes"], summary="Readiness (alias)", responses=not_ready)
    def healthz(response: Response) -> dict[str, Any]:
        """The same answer as `/readyz`, under the older name."""
        return readyz(response)

    @app.post(
        "/v1/crawls", dependencies=guarded, tags=["Crawls"], summary="Start a crawl",
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"description": "An existing job is returned (`created: false`): the same "
                                 "idempotency key, or the company already has an unfinished job."},
            201: {"description": "A new job was queued (`created: true`)."},
            409: {"description": "The idempotency key was already used for another root_url."},
            422: {"description": "Invalid root_url, or `domain` does not match it."},
            429: {"description": "`api.max_queued_jobs` jobs are already waiting; retry after "
                                 "the `Retry-After` header."},
        },
    )  # fmt: skip
    def create_crawl(
        body: CrawlRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        """Queue the whole pipeline for one company: its site, the external (search)
        phase and its competitors' sites.

        Send only `root_url`. A `gtm-worker` runs the job; follow it with
        `GET /v1/crawls/{crawl_id}`. The answer is the job (see that endpoint) plus
        `created`; note its `crawl_id` and `company_id`.

        The idempotency key may be given in the body or in the `Idempotency-Key` header.
        """
        try:
            root = normalize_url(body.root_url, settings.urls)
        except InvalidUrl as exc:
            raise HTTPException(422, str(exc)) from exc
        domain = registrable_domain(url_host(root))
        if body.domain is not None and body.domain.lower().strip(".") != domain:
            raise HTTPException(
                422,
                f"domain {body.domain!r} does not match root_url's domain {domain!r}",
            )
        key = body.idempotency_key or idempotency_key
        try:
            job, created = jobs.create(root, settings.urls, key,
                                       max_queued=settings.api.max_queued_jobs,
                                       trace=traceparent())  # fmt: skip
        except QueueFull as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc),
                                headers={"Retry-After": "60"}) from exc  # fmt: skip
        if not created and key is not None and job.idempotency_key == key and job.root_url != root:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "idempotency key was already used for another root_url"
            )
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        if not created and (trace := jobs.traceparent(job.id)):
            response.headers["traceparent"] = trace  # the existing job's trace
        return {**view(job.id), "created": created}

    finished: Responses = {
        409: {"description": "The job is already COMPLETED, FAILED or CANCELLED."}
    }

    @app.get("/v1/crawls/{crawl_id}", dependencies=guarded, tags=["Crawls"],
             summary="Get a crawl's status", responses=NO_CRAWL)  # fmt: skip
    def get_crawl(crawl_id: int) -> dict[str, Any]:
        """The job: `status`, timestamps, versions, attempts and `error_summary`.

        `status` is one of `CREATED`, `QUEUED`, `RUNNING`, `FINALIZING`, `COMPLETED`,
        `PAUSED`, `FAILED`, `CANCELLED`. `site_crawls` lists each site crawled for the
        job (the company first, then competitors) with its fetch and page counts and
        `stop_reason`. `result` is the summary once the job has finished.
        """
        return view(crawl_id)

    @app.get("/v1/crawls/{crawl_id}/live", dependencies=guarded, tags=["Crawls"],
             summary="The pages the crawl is on now", responses=NO_CRAWL)  # fmt: skip
    def live(crawl_id: int, limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
        """The job's `status` and `stage`, and the HTML pages it fetched most recently
        (newest first, the company's site and its competitors'), each with the
        `screenshot` path of its picture. Poll it while the crawl runs."""
        job = view(crawl_id)
        pages = [{**p, "screenshot": f"/v1/crawls/{crawl_id}/live/{p['fetch_id']}.png"}
                 for p in recent_pages(engine, crawl_id, limit)]  # fmt: skip
        return {"crawl_id": crawl_id, "status": job["status"], "stage": job.get("stage"),
                "pages": pages}  # fmt: skip

    @app.get("/v1/crawls/{crawl_id}/live/{fetch_id}.png", dependencies=guarded,
             tags=["Crawls"], summary="A screenshot of a page the crawl fetched",
             response_class=Response,
             responses={200: {"content": {"image/png": {}}},
                        404: {"description": "No such crawl, or no such page in it."}})  # fmt: skip
    async def live_screenshot(crawl_id: int, fetch_id: int) -> Response:
        """The page as the crawler fetched it, rendered from the stored copy with
        JavaScript off (the site is not fetched again; its styles and images are, through
        the network guard). PNG, 1280 x 900."""
        found = await run_in_threadpool(stored_page, engine, crawl_id, fetch_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no page {fetch_id} in crawl {crawl_id}")  # fmt: skip
        png = await screenshots.png(fetch_id, *found)
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})  # fmt: skip

    @app.post("/v1/crawls/{crawl_id}/pause", dependencies=guarded, tags=["Crawls"],
              summary="Pause a crawl", status_code=status.HTTP_202_ACCEPTED,
              responses={**NO_CRAWL, **finished})  # fmt: skip
    def pause_crawl(crawl_id: int, response: Response) -> dict[str, Any]:
        """Ask the job to pause: at once if it is queued, else at the next safe point
        (URLs in flight finish and the state is saved). Returns the job; `pause_requested`
        is true until its worker has stopped. Continue it with `/resume`."""
        job = jobs.request_pause(crawl_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no crawl {crawl_id}")
        if job.status in FINAL_JOB_STATES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"crawl is {job.status.upper()}")
        response.status_code = status.HTTP_202_ACCEPTED
        return view(crawl_id)

    @app.post("/v1/crawls/{crawl_id}/cancel", dependencies=guarded, tags=["Crawls"],
              summary="Cancel a crawl", status_code=status.HTTP_202_ACCEPTED,
              responses={**NO_CRAWL, **finished})  # fmt: skip
    def cancel_crawl(crawl_id: int, response: Response) -> dict[str, Any]:
        """Ask the job to stop for good: at once if it is queued, else at the next safe
        point. A cancelled job cannot be resumed; what it already stored is kept."""
        before = jobs.get(crawl_id)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no crawl {crawl_id}")
        if before.status in FINAL_JOB_STATES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"crawl is {before.status.upper()}")
        jobs.request_cancel(crawl_id)
        response.status_code = status.HTTP_202_ACCEPTED
        return view(crawl_id)

    @app.post("/v1/crawls/{crawl_id}/resume", dependencies=guarded, tags=["Crawls"],
              summary="Resume a paused crawl", status_code=status.HTTP_202_ACCEPTED,
              responses={**NO_CRAWL, 409: {"description": "The job is not PAUSED."}})  # fmt: skip
    def resume_crawl(crawl_id: int, response: Response) -> dict[str, Any]:
        """Put a paused job back in the queue. A worker continues it from the stored
        frontier; pages already fetched are not fetched again."""
        before = jobs.get(crawl_id)
        if before is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no crawl {crawl_id}")
        if before.status != "paused":
            raise HTTPException(status.HTTP_409_CONFLICT, f"crawl is {before.status.upper()}")
        jobs.resume(crawl_id)
        response.status_code = status.HTTP_202_ACCEPTED
        return view(crawl_id)

    def company(company_id: int) -> None:
        with engine.connect() as conn:
            known = conn.execute(
                select(Organization.id).where(Organization.id == company_id)
            ).scalar()  # fmt: skip
        if known is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no company {company_id}")

    def refresh(company_id: int) -> Any:
        """Queue a refresh of the company's profile (or return its unfinished job)."""
        with engine.connect() as conn:
            root = conn.execute(
                select(CrawlJob.root_url).where(CrawlJob.organization_id == company_id)
                .order_by(CrawlJob.id.desc()).limit(1)
            ).scalar() or "https://{}/".format(conn.execute(
                select(Organization.registrable_domain).where(Organization.id == company_id)
            ).scalar())  # fmt: skip
        job, _ = jobs.create(root, settings.urls, kind="refresh", trace=traceparent())
        return job

    @app.post(
        "/v1/companies/{company_id}/refresh",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="Refresh the company's profile",
        status_code=status.HTTP_202_ACCEPTED,
        responses=NO_COMPANY,
    )
    def post_refresh(company_id: int) -> dict[str, Any]:
        """Queue a job that re-reads the company's stored evidence (pages and
        documents) and rebuilds its profile and questions, fetching nothing. If the
        company already has an unfinished job, that job is returned: it will. Follow it
        with `GET /v1/crawls/{crawl_id}` (`kind`: `refresh`, `stage`)."""
        company(company_id)
        return view(refresh(company_id).id)

    @app.get("/v1/companies/{company_id}/dossier", dependencies=guarded, tags=["Companies"],
             summary="Get a company's dossier", responses=NO_COMPANY)  # fmt: skip
    def get_dossier(company_id: int, clean: bool = False) -> dict[str, Any]:
        """The current dossier, built on request from the latest assessments.

        `company`, `products` and `competitors` hold the fields; each has a `state`
        (`CORROBORATED`, `SUPPORTED`, `UNSUPPORTED`, `CONFLICTING`, `STALE`, `UNKNOWN`,
        `NOT_APPLICABLE`), a `value` only where the state allows one, a `confidence` and
        `citations` (URL, exact quote, source tier). `sections` groups the field names,
        `summary` counts states, `freshness`, `versions` and `cost` describe the run.
        Available while a crawl is still running, with what has been assessed so far.

        With `clean=true`: only what GTM Parameters V1.0 lists, as plain values:
        `name_of_company`, `company_information`, `product_information`, `traction`
        and `competitor_analysis`. Facts only; no citations, states or run metadata.
        """
        company(company_id)
        dossier = build_dossier(engine, company_id)
        return clean_dossier(dossier) if clean else dossier

    @app.get("/v1/companies/{company_id}/cost", dependencies=guarded, tags=["Companies"],
             summary="Get what crawling a company cost", responses=NO_COMPANY)  # fmt: skip
    def get_cost(company_id: int) -> dict[str, Any]:
        """Cost in USD: the total, by kind (http, browser, search, storage, database,
        worker; each with its usage by unit) and per job."""
        company(company_id)
        return company_costs(engine, company_id)

    @app.get(
        "/v1/companies/{company_id}/lineage/{field}", dependencies=guarded, tags=["Companies"],
        summary="Trace a field back to its sources",
        responses={404: {"description": "No such company, dossier field, or product of "
                                        "this company."}},
    )  # fmt: skip
    def get_lineage(company_id: int, field: str, product_id: int | None = None) -> Any:
        """The field's latest assessment and, for every observation it cites, the chain
        back to its source: value, quote and locator, page, URL, retrieval event and raw
        artifact.

        `field` is a dossier field name, e.g. `founded_year`, `funding_round`,
        `competitor`. Give `product_id` for a product's field. A field never assessed is
        `UNKNOWN` with no sources.
        """
        company(company_id)
        if field not in DOSSIER_FIELDS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no field {field!r}")
        if product_id is not None:
            with engine.connect() as conn:
                owner = conn.execute(
                    select(Product.organization_id).where(Product.id == product_id)
                ).scalar()  # fmt: skip
            if owner != company_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"no product {product_id}")
        return jsonable_encoder(evidence.field_lineage(company_id, field, product_id))

    dead = DeadLetters(engine)

    def dead_view(row: Any) -> dict[str, Any]:
        return {
            "id": row.id, "entity": row.entity, "job_id": row.job_id,
            "crawl_id": row.crawl_id, "url": row.url, "error_class": row.error_class,
            "error": row.error, "attempts": row.attempts, "payload": row.payload,
            "created_at": row.created_at, "resolved_at": row.resolved_at,
            "resolution": row.resolution, "resolved_by": row.resolved_by,
        }  # fmt: skip

    @app.get("/v1/dead-letters", dependencies=guarded, tags=["Dead letters"],
             summary="List dead-letter items")  # fmt: skip
    def list_dead_letters(
        include_resolved: bool = False, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> dict[str, Any]:
        """Work that failed for good: a URL out of attempts or lease expiries, or a job
        abandoned or failed by an unexpected error. Each item has its `entity` (`url` or
        `job`), the URL or job, `error_class`, `error` and `attempts`.

        Open items only, unless `include_resolved` is true; at most `limit` items.
        """
        rows = dead.list(include_resolved=include_resolved, limit=limit)
        return {"items": [dead_view(r) for r in rows]}

    @app.post("/v1/dead-letters/{item_id}/requeue", dependencies=guarded, tags=["Dead letters"],
              summary="Requeue a dead-letter item", responses=RESOLVED)  # fmt: skip
    def requeue_dead_letter(item_id: int) -> dict[str, Any]:
        """Put the work back and close the item: a URL returns to its crawl's frontier
        with fresh attempts (only while that crawl is running or paused), a job is
        submitted again as a new job. `requeued` says what was done."""
        try:
            outcome = dead.requeue(item_id, settings.urls)
        except DeadLetterError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {**dead_view(dead.get(item_id)), "requeued": outcome}

    @app.post("/v1/dead-letters/{item_id}/discard", dependencies=guarded, tags=["Dead letters"],
              summary="Discard a dead-letter item", responses=RESOLVED)  # fmt: skip
    def discard_dead_letter(item_id: int) -> dict[str, Any]:
        """Close the item without retrying the work."""
        try:
            dead.discard(item_id)
        except DeadLetterError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return dead_view(dead.get(item_id))

    @app.get("/v1/crawls/{crawl_id}/quality", dependencies=guarded, tags=["Crawls"],
             summary="Get a crawl's quality report", responses=NO_CRAWL)  # fmt: skip
    def get_quality(crawl_id: int) -> dict[str, Any]:
        """How the job went, computed on request: URLs by state, fetches and HTTP errors,
        pages (unique, duplicate, relevant), evidence and field-state counts, external
        sources, search queries, browser renders, bytes, latency percentiles and
        `completeness` (frontier left, budgets exhausted, gaps still unanswered)."""
        report = quality_report(engine, crawl_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no crawl {crawl_id}")
        return report

    @app.post(
        "/v1/companies/{company_id}/documents",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="Add a company document",
        status_code=status.HTTP_201_CREATED,
        responses={
            **NO_COMPANY,
            413: {"description": "Larger than `api.max_document_bytes`."},
            415: {"description": "Not a PDF, DOCX, PPTX or UTF-8 text/Markdown file."},
            422: {"description": "No readable text (e.g. a scanned PDF)."},
        },
    )
    async def add_document(
        company_id: int,
        request: Request,
        filename: Annotated[str, Query(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        """Read a document the company gives about itself (PDF, DOCX, PPTX, TXT, MD) and add
        it to the company's evidence; the dossier is re-assessed before this returns,
        and a refresh job (`refresh_job_id`) rebuilds the profile from it.

        Send the file's bytes as the request body (`Content-Type:
        application/octet-stream`), its name in `filename`. The kind is decided by the
        bytes. Its observations cite `upload://<domain>/documents/...`, source type
        `company_document`: the company's own word, like its website."""
        await run_in_threadpool(company, company_id)
        limit = settings.api.max_document_bytes
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"too_large: over {limit} bytes"
                )
        name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip() or "document"
        try:
            stored = await ingest_document(engine, settings, company_id, name, bytes(body))
        except DocumentError as exc:
            code = (
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
                if exc.code == "unsupported"
                else status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                if "too_large" in exc.code
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            )
            raise HTTPException(code, f"{exc.code}: {exc.detail}") from exc
        # The profile is rebuilt from the new evidence by a refresh job.
        job = await run_in_threadpool(refresh, company_id)
        return {**stored, "refresh_job_id": job.id}

    @app.get(
        "/v1/companies/{company_id}/onboarding",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="What to verify at onboarding",
        responses=NO_COMPANY,
    )
    def get_onboarding(company_id: int) -> Any:
        """The company under the six onboarding headings (company info, value
        proposition, market segment, target market, business model, products and
        services), each item with its value (`null`: not found), state, confidence,
        citations and the user's verification; then the competitors discovered and the
        candidate competitors. Available while the crawl is still running."""
        company(company_id)
        profile = stored_profile(engine, settings, company_id)
        return jsonable_encoder(onboarding_view(engine, company_id, profile))

    @app.get(
        "/v1/companies/{company_id}/business-profile",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="The company's Business Intelligence Profile",
        responses=NO_COMPANY,
    )
    def get_business_profile(company_id: int) -> Any:
        """Everything known about the company under fourteen headings, every field with
        its `value`, `status` (Extracting, Extracted, Verified, Inferred, Needs
        confirmation, Conflicting, Not found), `confidence`, `extracted_at`, `sources`,
        `origin` (crawler, document, model, user) and `access` (public, document,
        private, integration). Evidence first, the model's cited profile for what it
        left empty (Inferred), the user's answers over both. `missing_information` lists
        what is still open; `source_records` every field's sources. Available while the
        crawl is still running."""
        company(company_id)
        return jsonable_encoder(build_business_profile(engine, settings, company_id))

    @app.get(
        "/v1/companies/{company_id}/profile",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="The company's structured profile (model-generated)",
        responses=NO_COMPANY,
    )
    def get_profile(company_id: int) -> Any:
        """Company, business model (with its reasoning), offerings, target customers,
        market, value proposition (with its reasoning), differentiators and competitors,
        organised by a language model from the company's current evidence. Every value
        cites the evidence it rests on (`citations`: URL and quote); values without
        valid evidence were dropped (`dropped`). `status`: `ready`, `not_built` (the
        current evidence has no profile yet: re-extract), `no_evidence` or `disabled`
        (no `llm.model`)."""
        company(company_id)
        return jsonable_encoder(stored_profile(engine, settings, company_id))

    @app.get(
        "/v1/companies/{company_id}/verification",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="The user's verification",
        responses=NO_COMPANY,
    )
    def get_verification(company_id: int) -> dict[str, Any]:
        """What the user confirmed, edited or rejected at onboarding."""
        company(company_id)
        return {"items": verification(engine, company_id)}

    @app.put(
        "/v1/companies/{company_id}/verification",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="Record the user's verification",
        responses={**NO_COMPANY, 422: {"description": "Unknown section or field."}},
    )
    def put_verification(company_id: int, body: VerificationRequest) -> dict[str, Any]:
        """Record answers: `status` is `confirmed`, `edited` (with the corrected `value`)
        or `rejected`. `section` and `field` are an onboarding section's field or any
        Business Intelligence Profile field (its section key and field key);
        `item_key` is the product id for a product item, else empty; `actor` (optional)
        says who answered. Every answer is kept: the latest per item is the answer
        (returned here), the rest its `history` in the profile. User input only: no
        assessment changes."""
        company(company_id)
        try:
            return {"items": save_verification(engine, company_id, body)}
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    @app.get(
        "/v1/companies/{company_id}/questions",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="What to ask the user",
        responses=NO_COMPANY,
    )
    def get_questions(company_id: int) -> Any:
        """Once research has found something (`ready`), the questions to ask: the
        business fundamentals, commercial and sales-and-marketing questions not yet
        answered, with choices from the profile where it has them, then gap questions
        (`gap:<field>`) for public facts missing, unconfirmed or in conflict. Each has
        `id`, `group`, `text` (the model's wording when it has worded it), `type`
        (text, number, money, percent, choice, multi_choice, yes_no), `fills` (the
        profile field), `choices`, `suggested` and `allow_dont_know`."""
        company(company_id)
        return jsonable_encoder(questions_view(engine, settings, company_id))

    @app.put(
        "/v1/companies/{company_id}/answers",
        dependencies=guarded,
        tags=["Onboarding"],
        summary="Record the user's answers",
        responses={**NO_COMPANY, 422: {"description": "Unknown question, or a value that is "
                                                      "not of its type."}},
    )
    def put_answers(company_id: int, body: AnswerRequest) -> dict[str, Any]:
        """Answers: `status` `answered` (with a `value` of the question's type: money
        is `{"amount", "currency"}`, percent 0-100), `dont_know` or `skipped` (no
        value). Each is the user's answer to the field it fills, kept with its history;
        a field answered "I don't know" stays in the profile's missing information."""
        company(company_id)
        try:
            return {"answers": save_answers(
                engine, company_id, body,
                lambda: build_business_profile(engine, settings, company_id)["sections"][1][
                    "items"])}  # fmt: skip
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    add_market_routes(app, settings, engine, guarded)
    if settings.api.cors_origins:
        # Added last, so it wraps the rest: preflights are answered before auth.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api.cors_origins),
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )
    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="gtm-api", description="Serve the crawl job API.")
    parser.add_argument("--config", type=Path, help="config file (default: $GTM_CONFIG)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.logging, secrets=secret_values(settings))
    app = create_app(settings, make_engine(settings.database))
    uvicorn.run(app, host=settings.api.host, port=settings.api.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
