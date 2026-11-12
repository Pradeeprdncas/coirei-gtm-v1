"""The full run for one company: its site, extraction, then its competitors.

1. Crawl the company's site, extract and assess it; then the bounded external
   phase searches only for the fields the site left open, and extraction and
   assessment run again over everything if it fetched anything.
2. Resolve competitors' domains, only from a link on the very page that named
   the competitor: its anchor text is the competitor's name, or its domain's
   first label is the name's slug ("Globex" -> globex.com). Exactly one
   candidate domain across the competitor's evidence pages, or it stays
   unresolved (UNKNOWN beats a guessed domain). The named competitor is then
   linked to the organization of that domain (``db/identity.py``); the
   decision and the links it rests on are stored either way.
3. Crawl each resolved competitor's own site within the approved bounds
   (``competitor_max_pages``, no external phase) and extract, so competitor
   fields come from the competitor's first-party pages.
4. Assess every field of the company and of each crawled competitor
   (``assess.py``) and render the dossier (``dossier.py``).

``run_job`` runs these steps for a claimed ``crawl_job`` and can be called
again for the same job after a pause or a crash: each crawl of the job is
found and resumed (a finished one is kept as it is), and extraction and
assessment simply run again. ``run_pipeline`` creates a job and runs it in
this process (the CLI); ``worker.py`` runs jobs submitted through the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import DBAPIError

from . import cost
from .assess import assess_organization, field_gaps, field_values
from .config import Settings
from .crawl import crawl
from .db.identity import resolve_member, resolve_profiles
from .db.models import (
    Crawl,
    Evidence,
    Link,
    Observation,
    Organization,
    Relationship,
    Url,
    is_current,
)
from .domain import JobState
from .dossier import build_dossier
from .extract import extract_crawl
from .extract.llm import read_crawl
from .extract.product import slugify
from .fetch.http import build_user_agent
from .jobs import JOB_STATUS, JobStore
from .log import log_context
from .metrics import CRAWL_JOBS, instrument_engine
from .profile import build_profile
from .quality import observe, quality_report
from .questions import word_questions
from .tracing import span
from .urls import registrable_domain, url_host
from .versions import current_versions

log = logging.getLogger(__name__)

# Fields whose values seed competitor queries (crawl._competitor_search).
SEED_FIELDS = ("product_name", "product_category", "market_domain", "competitor")


async def run_pipeline(
    url: str,
    settings: Settings,
    engine: Engine,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Create (or reuse the unfinished) job for ``url`` and run it here, with the dossier."""
    build_user_agent(settings.identity)  # refuse before writing anything
    jobs = JobStore(engine)
    job, _ = await asyncio.to_thread(jobs.create, url, settings.urls)
    owner = f"cli-{uuid.uuid4().hex[:12]}"
    w = settings.worker
    claimed = await asyncio.to_thread(
        jobs.claim, owner, w.lease_seconds, w.max_job_attempts, job.id,
        current_versions(settings),
    )  # fmt: skip
    if claimed is None:
        raise JobUnavailable(f"job {job.id} is {job.status} and held by another worker")
    try:
        with span("job.run", job_id=job.id, worker=owner, stage="job"):
            out = await run_job(engine, settings, claimed, owner=owner, transport=transport,
                                search_transport=search_transport)  # fmt: skip
    except (asyncio.CancelledError, KeyboardInterrupt, DBAPIError):
        # Interrupted, not wrong: leave the job resumable.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(jobs.release, job.id, owner)
        raise
    except Exception as exc:
        await asyncio.to_thread(
            jobs.finish, job.id, owner, JobState.FAILED, None, f"{type(exc).__name__}: {exc}"
        )
        raise
    await asyncio.to_thread(jobs.finish, job.id, owner, out["job_status"], job_result(out))
    CRAWL_JOBS.labels(out["job_status"]).inc()
    if out["status"] != "failed":
        out["dossier"] = await asyncio.to_thread(build_dossier, engine, out["organization_id"])
    return out


class JobUnavailable(Exception):
    pass


def job_result(out: dict[str, Any]) -> dict[str, Any]:
    """What is kept on the job row: everything but bulky per-crawl statistics."""
    keep = ("crawl_id", "organization_id", "status", "job_status", "extraction", "assessment",
            "cost", "quality")  # fmt: skip
    competitors = [{k: v for k, v in c.items() if k not in ("extraction",)}
                   for c in out.get("competitors", [])]  # fmt: skip
    return {**{k: out[k] for k in keep if k in out}, "competitors": competitors}


async def run_job(
    engine: Engine,
    settings: Settings,
    job: Any,
    *,
    owner: str,
    stop: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Run (or resume) ``job``. ``out["job_status"]`` is the job's resulting status.

    The job's cost so far is recorded when the run ends (``cost.py``), in ``out["cost"]``.
    """
    instrument_engine(engine)  # statement time is what the database cost is measured from
    run_id = uuid.uuid4().hex[:12]
    with cost.metered() as db_seconds:
        try:
            out = await _run_job(engine, settings, job, owner=owner, stop=stop,
                                 transport=transport,
                                 search_transport=search_transport)  # fmt: skip
        except DBAPIError:
            raise  # the database is gone: nothing can be recorded now
        except Exception:
            await asyncio.to_thread(cost.record_quietly, engine, settings.cost, job.id, run_id,
                                    db_seconds[0])  # fmt: skip
            raise
        out["cost"] = await asyncio.to_thread(cost.record_quietly, engine, settings.cost, job.id,
                                              run_id, db_seconds[0])  # fmt: skip
    return out


# Where a job is, in order (crawl_job.stage); a refresh only extracts, profiles and asks.
STAGES = ("site_crawl", "external_research", "extract", "competitors", "profile",
          "questions", "done")  # fmt: skip


async def _run_job(
    engine: Engine,
    settings: Settings,
    job: Any,
    *,
    owner: str,
    stop: asyncio.Event | None,
    transport: httpx.AsyncBaseTransport | None,
    search_transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    store = JobStore(engine)

    async def stage(name: str) -> None:
        await asyncio.to_thread(store.note_stage, job.id, name)

    if getattr(job, "kind", "crawl") == "refresh":
        return await _refresh(engine, settings, job, owner, stage, stop)
    first_party: dict[str, Any] = {}

    def gaps(crawl_id: int, organization_id: int) -> set[str]:
        store.note_stage(job.id, "external_research")  # what is left open is searched next
        first_party.update(reextract(engine, settings, crawl_id, organization_id))
        return field_gaps(engine, organization_id)

    def progress(crawl_id: int, organization_id: int) -> None:
        """What the site has shown so far, assessed while it is still crawled."""
        read_crawl(engine, settings, crawl_id)
        extract_crawl(engine, settings, crawl_id, "crawl")
        assess_organization(engine, organization_id, settings.assessment)

    def known(crawl_id: int, organization_id: int) -> dict[str, list[str]]:
        return field_values(engine, organization_id, SEED_FIELDS)

    previous = await asyncio.to_thread(_job_crawl, engine, job.id, job.organization_id)
    await stage("site_crawl")
    with log_context(stage="crawl"):
        target = await crawl(
            job.root_url, settings, engine, transport=transport,
            search_transport=search_transport, gaps=gaps, known=known, job_id=job.id,
            resume=previous, stop=stop, owner=owner, progress=progress,
        )  # fmt: skip
    out: dict[str, Any] = {
        "job_id": job.id, "crawl_id": target.crawl_id,
        "organization_id": target.organization_id, "status": target.status,
        "job_status": str(JOB_STATUS[target.status]), "stats": target.stats,
        "competitors": [],
    }  # fmt: skip
    if target.status in ("failed", "paused"):
        return await _with_quality(engine, job.id, out)
    external = target.stats.get("external") or {}
    fetched = (external.get("reference_pages") or external.get("search_pages")
               or (external.get("competitors") or {}).get("search_pages"))  # fmt: skip
    if first_party and not fetched:
        out.update(first_party)  # nothing new was fetched after the first-party pass
    else:
        await stage("extract")
        with log_context(stage="extract"):
            out.update(await asyncio.to_thread(reextract, engine, settings, target.crawl_id))

    ext = settings.external
    if ext.enabled and ext.max_competitor_crawls > 0:
        await stage("competitors")
        with log_context(stage="competitors"):
            out["competitors"] = await _competitors(
                engine, settings, target.crawl_id, transport, job.id, stop, owner
            )
        if any(c.get("status") == "paused" for c in out["competitors"]):
            out["job_status"] = str(JobState.PAUSED)
    if _stopped(stop):  # paused or cancelled while the competitors were crawled
        out["job_status"] = str(JobState.PAUSED)
        return await _with_quality(engine, job.id, out)
    await _profile_and_questions(engine, settings, target.organization_id, out, stage)
    if out["job_status"] == JobState.COMPLETED:
        # Acquisition and interpretation are done: only the result is left.
        await stage("done")
        await asyncio.to_thread(store.set_stage, job.id, owner, JobState.FINALIZING)
    return await _with_quality(engine, job.id, out)


async def _profile_and_questions(
    engine: Engine, settings: Settings, organization_id: int, out: dict[str, Any], stage: Any
) -> None:
    """The model's profile of the company's current evidence, then its questions' wording."""
    if not settings.llm.model:
        return
    await stage("profile")
    with log_context(stage="profile"):
        built = await asyncio.to_thread(build_profile, engine, settings, organization_id)
    out["profile"] = {k: built[k] for k in ("status", "cached", "error") if k in built}
    await stage("questions")
    with log_context(stage="questions"):
        out["questions"] = await asyncio.to_thread(word_questions, engine, settings,
                                                   organization_id)  # fmt: skip


def _stopped(stop: asyncio.Event | None) -> bool:
    return stop is not None and stop.is_set()


async def _refresh(
    engine: Engine,
    settings: Settings,
    job: Any,
    owner: str,
    stage: Any,
    stop: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Re-read the company's stored evidence (its latest crawl, documents included in the
    assessment) and rebuild its profile and questions; nothing is fetched. A pause or
    cancel (``stop``) is honoured between steps: the job ends PAUSED (CANCELLED once the
    worker sees it was cancelled), before the model is called."""
    with engine.connect() as conn:
        crawl_id = conn.execute(
            select(Crawl.id).where(Crawl.organization_id == job.organization_id)
            # The site crawl when there is one, else the documents crawl.
            .order_by(Crawl.job_id.is_(None), Crawl.id.desc()).limit(1)
        ).scalar()  # fmt: skip
    out: dict[str, Any] = {"job_id": job.id, "crawl_id": crawl_id,
                           "organization_id": job.organization_id, "status": "completed",
                           "job_status": str(JobState.COMPLETED), "stats": {},
                           "competitors": []}  # fmt: skip
    if crawl_id is not None and not _stopped(stop):
        await stage("extract")
        with log_context(stage="extract"):
            out.update(await asyncio.to_thread(reextract, engine, settings, crawl_id))
    if _stopped(stop):
        out["job_status"] = str(JobState.PAUSED)
        return await _with_quality(engine, job.id, out)
    await _profile_and_questions(engine, settings, job.organization_id, out, stage)
    await stage("done")
    await asyncio.to_thread(JobStore(engine).set_stage, job.id, owner, JobState.FINALIZING)
    return await _with_quality(engine, job.id, out)


async def _with_quality(engine: Engine, job_id: int, out: dict[str, Any]) -> dict[str, Any]:
    """Add the crawl-quality report (spec section 27); export it once the job is final."""
    out["quality"] = await asyncio.to_thread(quality_report, engine, job_id)
    if out["job_status"] in (JobState.COMPLETED, JobState.FAILED) and out["quality"]:
        observe(out["quality"])
    return out


def _job_crawl(engine: Engine, job_id: int, organization_id: int | None) -> int | None:
    """The job's existing crawl of ``organization_id``'s site, if any."""
    if organization_id is None:
        return None
    with engine.connect() as conn:
        crawl_id: int | None = conn.execute(
            select(func.max(Crawl.id)).where(
                Crawl.job_id == job_id, Crawl.organization_id == organization_id
            )
        ).scalar()
    return crawl_id


def reextract(
    engine: Engine,
    settings: Settings,
    crawl_id: int,
    org_id: int | None = None,
    trigger: str = "crawl",
) -> dict[str, Any]:
    """A new extraction run and assessment over a stored crawl; nothing is fetched.

    ``trigger`` is recorded on the run: 'crawl' from the pipeline, 'replay' from
    ``replay.py``."""
    if org_id is None:
        with engine.connect() as conn:
            org_id = conn.execute(
                select(Crawl.organization_id).where(Crawl.id == crawl_id)
            ).scalar_one()  # fmt: skip
    with span("identity", crawl_id=crawl_id, stage="identity"), engine.begin() as conn:
        # Before extraction: whose profile a fetched page is decides what it may say.
        profiles = resolve_profiles(conn, crawl_id)
    with span("llm", crawl_id=crawl_id, stage="extract"):
        # Pages not read yet (replay.py reads stored answers only, no call).
        llm = read_crawl(engine, settings, crawl_id)
    with span("extract", crawl_id=crawl_id, stage="extract"):
        extraction = extract_crawl(engine, settings, crawl_id, trigger)
    with span("assess", crawl_id=crawl_id, stage="assess", organization_id=org_id):
        assessment = assess_organization(engine, org_id, settings.assessment)
    return {"extraction": extraction, "assessment": assessment, "profiles": profiles, "llm": llm}


# Competitor sites crawled at once (each host stays throttled; one database each).
COMPETITOR_CRAWLS_AT_ONCE = 2


async def _competitors(
    engine: Engine,
    settings: Settings,
    crawl_id: int,
    transport: httpx.AsyncBaseTransport | None,
    job_id: int | None = None,
    stop: asyncio.Event | None = None,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    ext = settings.external
    resolved = await asyncio.to_thread(
        resolve_competitor_domains, engine, crawl_id, settings.crawl.reference_domains
    )
    bounded = replace(
        settings,
        crawl=replace(settings.crawl, max_pages=ext.competitor_max_pages),
        external=replace(ext, enabled=False),
    )
    seeded = [c for c in resolved if c["seed"] is not None]
    chosen = {id(c) for c in seeded[: ext.max_competitor_crawls]}
    gate = asyncio.Semaphore(COMPETITOR_CRAWLS_AT_ONCE)

    async def one(competitor: dict[str, Any]) -> dict[str, Any]:
        if competitor["seed"] is None:
            return competitor
        if id(competitor) not in chosen:
            return {**competitor, "skipped": "max_competitor_crawls"}
        async with gate:
            if stop is not None and stop.is_set():
                return {**competitor, "skipped": "paused"}
            previous = await asyncio.to_thread(
                _job_crawl, engine, job_id, competitor["organization_id"]
            ) if job_id is not None else None  # fmt: skip
            summary = await crawl(competitor["seed"], bounded, engine, transport=transport,
                                  job_id=job_id, resume=previous, stop=stop,
                                  owner=owner)  # fmt: skip
            entry = {**competitor, "crawl_id": summary.crawl_id, "status": summary.status}
            if summary.status not in ("failed", "paused"):
                entry.update(await asyncio.to_thread(reextract, engine, bounded, summary.crawl_id))
            return entry

    return list(await asyncio.gather(*(one(c) for c in resolved)))


def resolve_competitor_domains(
    engine: Engine, crawl_id: int, reference_domains: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Competitors named in ``crawl_id`` with the domain their evidence pages link to.

    ``organization_id`` is the company once resolved (the row that owns the
    domain), else the named row; ``named_organization_id`` is always the latter.
    """
    with engine.begin() as conn:
        own_domain = conn.execute(
            select(Organization.registrable_domain)
            .join(Crawl, Crawl.organization_id == Organization.id)
            .where(Crawl.id == crawl_id)
        ).scalar_one()
        named = conn.execute(
            select(Organization.id, Organization.name, Organization.registrable_domain,
                   Evidence.page_id)
            .join(Relationship, Relationship.object_org_id == Organization.id)
            .join(Observation, Observation.id == Relationship.observation_id)
            .join(Evidence, Evidence.id == Observation.evidence_id)
            .where(Observation.crawl_id == crawl_id, is_current(),
                   Relationship.type == "competitor")
        ).all()  # fmt: skip
        pages: dict[int, set[int]] = defaultdict(set)
        orgs: dict[int, tuple[str, str | None]] = {}
        for org_id, name, domain, page_id in named:
            pages[org_id].add(page_id)
            orgs[org_id] = (name, domain)

        out = []
        for org_id, (name, known) in sorted(orgs.items()):
            entry: dict[str, Any] = {"name": name, "organization_id": org_id,
                                     "named_organization_id": org_id, "domain": known,
                                     "seed": None}  # fmt: skip
            if known:
                # Named before migration 0015, when the domain was written onto the row.
                entry["seed"] = f"https://{known}/"
                out.append(entry)
                continue
            links = conn.execute(
                select(Link.id, Link.page_id, Url.url, Link.anchor_text)
                .join(Url, Url.id == Link.target_url_id)
                .where(Link.page_id.in_(pages[org_id]), Link.is_external.is_(True))
                .order_by(Link.id)
            ).all()
            seeds: dict[str, str] = {}
            signals: dict[str, list[dict[str, Any]]] = {}
            for link_id, page_id, link_url, anchor in links:
                domain = registrable_domain(url_host(link_url))
                if domain == own_domain or _listed(domain, reference_domains):
                    continue
                by_anchor = (anchor or "").strip().lower() == name.lower()
                if by_anchor or domain.split(".")[0] == slugify(name).replace("-", ""):
                    parts = urlsplit(link_url)
                    seeds.setdefault(domain, f"{parts.scheme}://{parts.netloc}/")
                    signals.setdefault(domain, []).append(
                        {"signal": "evidence_page_link", "link_id": link_id, "page_id": page_id,
                         "matched": "anchor_text" if by_anchor else "domain_label"}
                    )  # fmt: skip
            resolution = resolve_member(conn, org_id, signals, crawl_id=crawl_id)
            entry["identity"] = {"outcome": resolution.outcome,
                                 "decision_id": resolution.decision_id}  # fmt: skip
            if resolution.organization_id is not None:
                resolved = str(
                    conn.execute(
                        select(Organization.registrable_domain).where(
                            Organization.id == resolution.organization_id
                        )
                    ).scalar_one()
                )
                entry.update(organization_id=resolution.organization_id, domain=resolved,
                             seed=seeds.get(resolved, f"https://{resolved}/"),
                             basis="link_on_evidence_page")  # fmt: skip
            elif resolution.outcome == "conflicting":
                entry["unresolved"] = "ambiguous"
            else:
                entry["unresolved"] = "no_link" if not seeds else "rejected"
            # "name" is a LogRecord attribute: passing it in extra raises KeyError.
            fields = {k: v for k, v in entry.items() if k not in ("seed", "name")}
            log.info("competitor.domain", extra={"competitor": name, **fields})
            out.append(entry)
    return out


def _listed(domain: str, domains: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)
