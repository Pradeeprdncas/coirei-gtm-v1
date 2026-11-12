"""Crawl jobs: the unit a client requests and a worker runs (spec sections 5, 7, 23).

States and allowed moves are in ``domain.py``. Every state change goes
through ``_move``, which refuses an illegal transition and appends a
``crawl_event`` row in the same transaction, so a job's history is complete
and ordered.

A worker claims a queued job with ``FOR UPDATE SKIP LOCKED`` and holds it
under a lease it renews by heartbeat. A running or finalizing job whose lease
expired (its worker died) is claimable again and resumes from the durable
crawl state; a worker that stops cleanly hands the job back to ``queued``.
Pause and cancel are requests: queued work is paused or cancelled at once,
running work at the worker's next heartbeat. A job claimed
``max_job_attempts`` times without finishing is failed rather than retried
forever.

Starting a job is idempotent: the same ``idempotency_key`` returns the same
job, and while a company has an unfinished job, asking again returns it
(enforced by a partial unique index). A new job is refused (``QueueFull``)
while ``max_queued`` jobs are already waiting (backpressure, spec section 23).

Abandoned jobs, and jobs failed by an unexpected error, also get a
dead-letter item (``deadletter``), written in the same transaction.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import deadletter
from .config import UrlSettings
from .db.models import ACTIVE_JOB_STATUSES, CrawlEvent, CrawlJob
from .db.store import ensure_organization
from .domain import FINAL_JOB_STATES, IllegalTransition, JobState, can_move
from .metrics import JOB_LEASES_EXPIRED
from .urls import normalize_url, registrable_domain, url_host

log = logging.getLogger(__name__)

# Job state for the target crawl's final status. A budget stop is a completed
# job; the stop reason stays in the job result.
JOB_STATUS = {"completed": JobState.COMPLETED, "partial": JobState.COMPLETED,
              "failed": JobState.FAILED, "paused": JobState.PAUSED}  # fmt: skip
_HELD = (JobState.RUNNING, JobState.FINALIZING)


class QueueFull(Exception):
    """Too many jobs are waiting for a worker; try again later."""


class JobStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # --- transitions -----------------------------------------------------------------

    def _move(
        self,
        conn: Connection,
        row: Any,
        to: JobState,
        *,
        event: str,
        actor: str | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        **values: Any,
    ) -> Any:
        if not can_move(row.status, to):
            raise IllegalTransition(f"job {row.id}: {row.status} -> {to}")
        if to in FINAL_JOB_STATES:
            values.setdefault("finished_at", func.now())
        moved = conn.execute(
            update(CrawlJob).where(CrawlJob.id == row.id).values(status=to, **values)
            .returning(CrawlJob)
        ).one()  # fmt: skip
        self._event(conn, row.id, event, row.status, to, actor, reason, detail)
        log.info("job.transition", extra={"job_id": row.id, "from_state": row.status,
                                          "to_state": str(to), "reason": reason})  # fmt: skip
        return moved

    @staticmethod
    def _event(
        conn: Connection,
        job_id: int,
        event: str,
        src: str | None,
        dst: str | None,
        actor: str | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            insert(CrawlEvent).values(
                entity="job", job_id=job_id, event=event, from_state=src, to_state=dst,
                actor=actor, reason=reason, detail=detail,
            )
        )  # fmt: skip

    @staticmethod
    def _locked(conn: Connection, job_id: int, owner: str | None = None) -> Any:
        query = select(CrawlJob).where(CrawlJob.id == job_id)
        if owner is not None:
            query = query.where(CrawlJob.lease_owner == owner)
        return conn.execute(query.with_for_update()).one_or_none()

    # --- API side ----------------------------------------------------------------------

    def create(
        self,
        root_url: str,
        urls: UrlSettings,
        idempotency_key: str | None = None,
        actor: str = "api",
        max_queued: int | None = None,
        trace: str | None = None,
        kind: str = "crawl",
    ) -> tuple[Any, bool]:
        """The job for ``root_url``: (row, created). A new job is queued.

        ``kind`` 'refresh' re-reads the company's stored evidence without fetching. An
        unfinished job of either kind is returned instead of a new one.

        ``trace`` (a W3C ``traceparent``) is kept on the ``created`` event, so
        the worker continues the requester's trace (``traceparent()``).

        Raises ``InvalidUrl``, or ``QueueFull`` when a new job would be the
        ``max_queued``-th waiting one (an existing job is still returned).
        """
        root = normalize_url(root_url, urls)
        domain = registrable_domain(url_host(root))
        with self.engine.begin() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    select(CrawlJob).where(CrawlJob.idempotency_key == idempotency_key)
                ).one_or_none()
                if existing is not None:
                    return existing, False
            org_id = ensure_organization(conn, domain)
            active = and_(CrawlJob.organization_id == org_id,
                          CrawlJob.status.in_(ACTIVE_JOB_STATUSES))  # fmt: skip
            found = conn.execute(select(CrawlJob).where(active).limit(1)).one_or_none()
            if found is not None:
                return found, False
            if max_queued is not None:
                # ponytail: an unlocked count, so concurrent requests may overshoot
                # the bound by a few; a hard cap would need a lock row.
                waiting = conn.execute(
                    select(func.count()).where(CrawlJob.status == JobState.QUEUED)
                ).scalar_one()  # fmt: skip
                if waiting >= max_queued:
                    raise QueueFull(f"{waiting} jobs are waiting for a worker")
            try:
                with conn.begin_nested():
                    row = conn.execute(
                        insert(CrawlJob)
                        .values(organization_id=org_id, root_url=root, kind=kind,
                                idempotency_key=idempotency_key, status=JobState.CREATED)
                        .returning(CrawlJob)
                    ).one()  # fmt: skip
            except IntegrityError:
                # A concurrent request won: same key, or a job for this company.
                taken = (
                    CrawlJob.idempotency_key == idempotency_key
                    if idempotency_key is not None
                    else active
                )
                return conn.execute(select(CrawlJob).where(taken)).one(), False
            detail = {"root_url": root, **({"traceparent": trace} if trace else {})}
            self._event(conn, row.id, "created", None, JobState.CREATED, actor, detail=detail)
            row = self._move(conn, row, JobState.QUEUED, event="queued", actor=actor)
        return row, True

    def note_stage(self, job_id: int, stage: str) -> None:
        """Record where the job is, for progress only (no state change, no lease check)."""
        with self.engine.begin() as conn:
            conn.execute(update(CrawlJob).where(CrawlJob.id == job_id).values(stage=stage))

    def get(self, job_id: int) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(select(CrawlJob).where(CrawlJob.id == job_id)).one_or_none()

    def events(self, job_id: int) -> list[Any]:
        with self.engine.connect() as conn:
            return list(conn.execute(
                select(CrawlEvent).where(CrawlEvent.job_id == job_id).order_by(CrawlEvent.id)
            ).all())  # fmt: skip

    def traceparent(self, job_id: int) -> str | None:
        """The trace the job was created in, if one was recorded."""
        with self.engine.connect() as conn:
            detail = conn.execute(
                select(CrawlEvent.detail)
                .where(CrawlEvent.job_id == job_id, CrawlEvent.event == "created")
            ).scalar()  # fmt: skip
        return (detail or {}).get("traceparent")

    def request_pause(self, job_id: int, actor: str = "api") -> Any:
        """Pause a job: at once if no worker has it, else at the worker's next heartbeat."""
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id)
            if row is None:
                return None
            if row.status in (JobState.CREATED, JobState.QUEUED):
                return self._move(conn, row, JobState.PAUSED, event="paused", actor=actor)
            if row.status in _HELD and not row.pause_requested:
                row = conn.execute(
                    update(CrawlJob).where(CrawlJob.id == job_id)
                    .values(pause_requested=True).returning(CrawlJob)
                ).one()  # fmt: skip
                self._event(conn, job_id, "pause_requested", row.status, row.status, actor)
            return row

    def resume(self, job_id: int, actor: str = "api") -> Any:
        """A paused job goes back to the queue."""
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id)
            if row is None or row.status != JobState.PAUSED:
                return row
            return self._move(conn, row, JobState.QUEUED, event="resumed", actor=actor,
                              pause_requested=False, lease_owner=None,
                              lease_expires_at=None, attempts=0)  # fmt: skip

    def request_cancel(self, job_id: int, actor: str = "api") -> Any:
        """Cancel a job: at once if no worker has it, else at the worker's next heartbeat."""
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id)
            if row is None or row.status in FINAL_JOB_STATES:
                return row
            if row.status in _HELD:
                if not row.cancel_requested:
                    row = conn.execute(
                        update(CrawlJob).where(CrawlJob.id == job_id)
                        .values(cancel_requested=True).returning(CrawlJob)
                    ).one()  # fmt: skip
                    self._event(conn, job_id, "cancel_requested", row.status, row.status, actor)
                return row
            return self._move(conn, row, JobState.CANCELLED, event="cancelled", actor=actor)

    # --- worker side -------------------------------------------------------------------

    def claim(
        self,
        owner: str,
        lease_seconds: float,
        max_attempts: int,
        job_id: int | None = None,
        versions: dict[str, Any] | None = None,
    ) -> Any:
        """Lease the oldest claimable job (or ``job_id``) to ``owner``; None if there is none."""
        now = func.now()
        expired = or_(CrawlJob.lease_expires_at.is_(None), CrawlJob.lease_expires_at < now)
        claimable = or_(CrawlJob.status == JobState.QUEUED,
                        and_(CrawlJob.status.in_(_HELD), expired))  # fmt: skip
        pick = select(CrawlJob).where(claimable)
        if job_id is not None:
            pick = pick.where(CrawlJob.id == job_id)
        pick = pick.order_by(CrawlJob.id).limit(1).with_for_update(skip_locked=True)
        with self.engine.begin() as conn:
            row = conn.execute(pick).one_or_none()
            if row is None:
                return None
            reclaim = row.status != JobState.QUEUED
            if row.attempts + 1 > max_attempts:
                self._move(conn, row, JobState.FAILED, event="abandoned", actor=owner,
                           reason=f"claimed {max_attempts} times without finishing",
                           error_summary=f"abandoned after {max_attempts} attempts",
                           lease_owner=None, lease_expires_at=None)  # fmt: skip
                deadletter.record(
                    conn, "job", "abandoned", job_id=row.id, attempts=row.attempts,
                    error=f"claimed {max_attempts} times without finishing (workers died)",
                    payload={"root_url": row.root_url},
                )  # fmt: skip
                log.error("job.abandoned", extra={"job_id": row.id, "attempts": row.attempts})
                return None
            row = self._move(
                conn, row, JobState.RUNNING, event="reclaimed" if reclaim else "claimed",
                actor=owner, reason="lease expired" if reclaim else None,
                lease_owner=owner, attempts=CrawlJob.attempts + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=func.coalesce(CrawlJob.started_at, now),
                versions=versions if versions is not None else CrawlJob.versions,
            )  # fmt: skip
        if reclaim:
            JOB_LEASES_EXPIRED.inc()  # its previous worker died or hung (spec section 26)
        return row

    def heartbeat(self, job_id: int, owner: str, lease_seconds: float) -> str | None:
        """Renew the lease: "continue", "pause" or "cancel"; None if the lease is lost."""
        with self.engine.begin() as conn:
            flags = conn.execute(
                update(CrawlJob)
                .where(CrawlJob.id == job_id, CrawlJob.lease_owner == owner,
                       CrawlJob.status.in_(_HELD))
                .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
                .returning(CrawlJob.pause_requested, CrawlJob.cancel_requested)
            ).one_or_none()  # fmt: skip
        if flags is None:
            return None
        return (
            "cancel"
            if flags.cancel_requested
            else "pause"
            if flags.pause_requested
            else ("continue")
        )

    def set_stage(self, job_id: int, owner: str, to: JobState) -> bool:
        """Move a held job between RUNNING and FINALIZING. False if ``owner`` lost it."""
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id, owner)
            if row is None:
                return False
            if row.status != to:
                self._move(conn, row, to, event=str(to), actor=owner)
        return True

    def finish(
        self,
        job_id: int,
        owner: str,
        status: JobState | str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        error_class: str | None = None,
    ) -> bool:
        """Record the outcome if ``owner`` still holds the job. ``error_class``
        (an unexpected failure) also dead-letters it."""
        to = JobState(status)
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id, owner)
            if row is None:
                return False
            self._move(conn, row, to, event=str(to), actor=owner, reason=error,
                       result=result, error_summary=error, lease_owner=None,
                       lease_expires_at=None, pause_requested=False,
                       cancel_requested=False)  # fmt: skip
            if error_class is not None:
                deadletter.record(conn, "job", error_class, job_id=job_id, error=error,
                                  attempts=row.attempts,
                                  payload={"root_url": row.root_url})  # fmt: skip
        return True

    def release(self, job_id: int, owner: str, reason: str = "worker stopping") -> None:
        """Hand the job back unfinished; another worker may resume it at once. The claim's
        attempt is given back: a clean stop (a deploy) is not a failed attempt."""
        with self.engine.begin() as conn:
            row = self._locked(conn, job_id, owner)
            if row is not None and row.status in _HELD:
                self._move(conn, row, JobState.QUEUED, event="released", actor=owner,
                           reason=reason, lease_owner=None, lease_expires_at=None,
                           attempts=CrawlJob.attempts - 1)  # fmt: skip
