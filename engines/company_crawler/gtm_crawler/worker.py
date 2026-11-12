"""``gtm-worker``: claims crawl jobs and runs them (production LLD sections 3, 21, 26, 29).

When no crawl job is waiting it runs a queued market-research run
(``market/runner.py``) under the same lease rules.

Each worker runs one job at a time and may be replicated: jobs are claimed
with ``FOR UPDATE SKIP LOCKED``, so two workers never run the same job. While
it runs a job, the worker renews the job's lease every ``lease_seconds / 3``;
the heartbeat also delivers pause and cancel requests. If the worker dies, the lease
expires and another worker resumes the job from its durable state.

* Finished, paused, cancelled and failed jobs are recorded on the job row,
  and every transition in ``crawl_event``.
* A database outage leaves the job unacknowledged (its lease expires and it
  is resumed later); the worker keeps polling until the database is back.
* Stopping the worker (Ctrl+C, SIGTERM) hands the job back at once.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from .config import ConfigError, Settings, load_settings, secret_values
from .db import make_engine
from .domain import JobState
from .fetch.http import build_user_agent
from .jobs import JobStore
from .log import configure_logging
from .market.runner import process_run
from .market.store import MarketStore
from .metrics import CRAWL_JOBS, WORKER_LAST_SEEN, serve_metrics
from .pipeline import job_result, run_job
from .tracing import continue_trace, span
from .versions import current_versions

log = logging.getLogger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


async def run_worker(
    settings: Settings,
    engine: Engine,
    *,
    once: bool = False,
    owner: str | None = None,
    shutdown: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Claim and run jobs until ``shutdown`` is set (or one job / none, with ``once``).

    Returns the number of jobs run.
    """
    owner = owner or worker_id()
    shutdown = shutdown or asyncio.Event()
    jobs = JobStore(engine)
    market = MarketStore(engine)
    w = settings.worker
    versions = current_versions(settings)
    ran = 0
    log.info("worker.started", extra={"owner": owner})
    while not shutdown.is_set():
        WORKER_LAST_SEEN.set_to_current_time()
        try:
            job = await asyncio.to_thread(
                jobs.claim, owner, w.lease_seconds, w.max_job_attempts, None, versions
            )
        except DBAPIError as exc:
            log.warning("worker.database_unavailable", extra={"error": str(exc.orig)})
            job = None
        if job is None:
            # No crawl waiting: a market-research run, if one is (gtm_crawler.market).
            try:
                run = await asyncio.to_thread(market.claim, owner, w.lease_seconds)
            except DBAPIError as exc:
                log.warning("worker.database_unavailable", extra={"error": str(exc.orig)})
                run = None
            if run is not None:
                outcome = await process_run(settings, engine, market, run, owner, shutdown,
                                            transport=transport,
                                            search_transport=search_transport)  # fmt: skip
                log.info("market.run_finished", extra={"research_id": run.id,
                                                       "outcome": str(outcome)})  # fmt: skip
                ran += 1
                if once:
                    break
                continue
            if once:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), w.poll_seconds)
            continue
        await process_job(settings, engine, jobs, job, owner, shutdown,
                          transport=transport, search_transport=search_transport)  # fmt: skip
        ran += 1
        if once:
            break
    log.info("worker.stopped", extra={"owner": owner, "jobs": ran})
    return ran


async def process_job(
    settings: Settings,
    engine: Engine,
    jobs: JobStore,
    job: Any,
    owner: str,
    shutdown: asyncio.Event | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Run one claimed job to its next resting state. Returns what happened."""
    trace = None
    with contextlib.suppress(DBAPIError):  # untraced beats not running the job
        trace = await asyncio.to_thread(jobs.traceparent, job.id)
    with (
        continue_trace(trace),
        span("job.run", job_id=job.id, worker=owner, stage="job", attempt=job.attempts) as sp,
    ):
        sp["outcome"] = await _process_job(settings, engine, jobs, job, owner, shutdown,
                                           transport, search_transport)  # fmt: skip
        return str(sp["outcome"])


async def _process_job(
    settings: Settings,
    engine: Engine,
    jobs: JobStore,
    job: Any,
    owner: str,
    shutdown: asyncio.Event | None,
    transport: httpx.AsyncBaseTransport | None,
    search_transport: httpx.AsyncBaseTransport | None,
) -> str:
    w = settings.worker
    stop = asyncio.Event()
    lost = cancelled = False

    async def beat() -> bool:
        """Renew the lease and take in a pause or cancel; False once the lease is lost."""
        nonlocal lost, cancelled
        try:
            directive = await asyncio.to_thread(jobs.heartbeat, job.id, owner, w.lease_seconds)
        except DBAPIError as exc:
            log.warning("job.heartbeat_failed", extra={"job_id": job.id, "error": str(exc)})
            return True
        WORKER_LAST_SEEN.set_to_current_time()
        if directive is None:
            lost = True  # another worker took over: stop without writing
            stop.set()
            return False
        if directive in ("pause", "cancel"):
            cancelled = cancelled or directive == "cancel"
            stop.set()
        return True

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(w.lease_seconds / 3)
            if not await beat():
                return

    async def watch_shutdown() -> None:
        if shutdown is not None:
            await shutdown.wait()
            stop.set()

    async def finish(status: JobState, result: dict[str, Any] | None = None,
                     error: str | None = None, error_class: str | None = None) -> str:  # fmt: skip
        """Record the outcome; a database error leaves the job to its lease (another worker
        resumes it), not the worker stopped."""
        try:
            done = await asyncio.to_thread(jobs.finish, job.id, owner, status, result, error,
                                           error_class)  # fmt: skip
        except DBAPIError as exc:
            log.error("job.finish_failed", extra={"job_id": job.id, "error": str(exc.orig)})
            return "interrupted"
        if not done:
            log.warning("job.lease_lost", extra={"job_id": job.id, "owner": owner})
            return "lost"
        CRAWL_JOBS.labels(status).inc()
        return status

    # Once before the work: a job claimed with a cancel or pause pending (a reclaimed job
    # whose worker died) stops at its first step, not after a long one.
    await beat()
    beating = asyncio.create_task(heartbeat())
    watcher = asyncio.create_task(watch_shutdown())
    try:
        out = await run_job(engine, settings, job, owner=owner, stop=stop, transport=transport,
                            search_transport=search_transport)  # fmt: skip
    except (asyncio.CancelledError, KeyboardInterrupt):
        with contextlib.suppress(Exception):
            await asyncio.to_thread(jobs.release, job.id, owner)
        raise
    except DBAPIError as exc:
        log.error("job.interrupted", extra={"job_id": job.id, "error": str(exc.orig)})
        return "interrupted"  # unacknowledged: the lease expires and the job resumes
    except Exception as exc:  # noqa: BLE001 - a job's failure must not stop the worker
        log.exception("job.failed", extra={"job_id": job.id})
        return await finish(JobState.FAILED, None, f"{type(exc).__name__}: {exc}",
                            type(exc).__name__)  # fmt: skip
    finally:
        beating.cancel()
        watcher.cancel()
    if lost:
        log.warning("job.lease_lost", extra={"job_id": job.id, "owner": owner})
        return "lost"
    status = JobState(out["job_status"])
    if status == JobState.PAUSED and cancelled:
        status = JobState.CANCELLED  # stopped at a safe point because it was cancelled
    elif status == JobState.PAUSED and shutdown is not None and shutdown.is_set():
        # Stopped for shutdown, not paused by request: hand it back for resumption.
        try:
            await asyncio.to_thread(jobs.release, job.id, owner)
        except DBAPIError as exc:
            log.error("job.release_failed", extra={"job_id": job.id, "error": str(exc.orig)})
            return "interrupted"  # the lease expires and the job resumes
        return "released"
    error = None
    if status == JobState.FAILED:  # the company's own site could not be crawled: say why
        error = f"site crawl failed: {(out.get('stats') or {}).get('stop_reason') or 'unknown'}"
    return await finish(status, job_result(out), error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gtm-worker", description="Run crawl jobs.")
    parser.add_argument("--config", type=Path, help="config file (default: $GTM_CONFIG)")
    parser.add_argument("--once", action="store_true", help="run at most one job, then exit")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        build_user_agent(settings.identity)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.logging, secrets=secret_values(settings))
    engine = make_engine(settings.database, pool_size=settings.crawl.concurrency + 2)
    serve_metrics(settings.worker.metrics_host, settings.worker.metrics_port, engine)

    async def run() -> None:
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C still works
                loop.add_signal_handler(sig, shutdown.set)
        await run_worker(settings, engine, once=args.once, shutdown=shutdown)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
