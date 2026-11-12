"""Market-research runs in PostgreSQL (``market_research``), leased like crawl jobs.

``create`` queues a validated request (refused with ``QueueFull`` beyond
``market.max_queued_runs``); a worker ``claim``s the oldest queued run, or
one whose lease expired (its worker died), and renews the lease with
``heartbeat``, which also delivers cancellation. ``finish`` stores the
report. A run claimed ``MAX_ATTEMPTS`` times without finishing fails.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import Engine, Table, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..db.models import MarketResearch
from ..jobs import QueueFull
from .models import Report, ResearchRequest, RunStatus

MAX_ATTEMPTS = 3
TABLE = cast(Table, MarketResearch.__table__)
FINAL = (RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELLED)


class MarketStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(self, req: ResearchRequest, max_queued: int,
               idempotency_key: str | None = None) -> Any:  # fmt: skip
        """The queued run; with ``idempotency_key``, the run already created with that key
        if there is one (whatever its status or request: the caller compares). The unique
        key makes concurrent creates with one key return one run."""
        by_key = select(MarketResearch).where(MarketResearch.idempotency_key == idempotency_key)
        with self.engine.begin() as conn:
            if idempotency_key is not None:
                existing = conn.execute(by_key).one_or_none()
                if existing is not None:
                    return existing
            queued = conn.execute(
                select(func.count()).where(MarketResearch.status == RunStatus.QUEUED)
            ).scalar_one()
            if queued >= max_queued:
                raise QueueFull(f"{queued} market research runs are already queued")
            try:
                with conn.begin_nested():
                    return conn.execute(
                        TABLE.insert()
                        .values(id=req.research_id, mode=req.mode, subject_key=req.subject_key(),
                                status=RunStatus.QUEUED, request=req.model_dump(mode="json"),
                                idempotency_key=idempotency_key)
                        .returning(*TABLE.c)
                    ).one()  # fmt: skip
            except IntegrityError:
                if idempotency_key is None:
                    raise
                return conn.execute(by_key).one()  # a concurrent create with the key won

    def save(self, req: ResearchRequest, report: Report) -> None:
        """Record a run finished elsewhere (``gtm-market --save``)."""
        with self.engine.begin() as conn:
            conn.execute(TABLE.insert().values(
                id=req.research_id, mode=req.mode, subject_key=req.subject_key(),
                status=report.status, request=req.model_dump(mode="json"),
                report=report.model_dump(mode="json"), started_at=func.now(),
                finished_at=func.now()))  # fmt: skip

    def get(self, research_id: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(
                select(MarketResearch).where(MarketResearch.id == research_id)
            ).one_or_none()

    def claim(self, owner: str, lease_seconds: float) -> Any:
        """Lease the oldest claimable run to ``owner``; None if there is none."""
        now = func.now()
        expired = or_(MarketResearch.lease_expires_at.is_(None),
                      MarketResearch.lease_expires_at < now)  # fmt: skip
        claimable = or_(MarketResearch.status == RunStatus.QUEUED,
                        and_(MarketResearch.status == RunStatus.RUNNING, expired))  # fmt: skip
        pick = (select(MarketResearch.id, MarketResearch.attempts,
                       MarketResearch.cancel_requested).where(claimable)
                .order_by(MarketResearch.created_at).limit(1)
                .with_for_update(skip_locked=True))  # fmt: skip
        with self.engine.begin() as conn:
            row = conn.execute(pick).one_or_none()
            if row is None:
                return None
            if row.cancel_requested:  # cancelled while its worker died: not run again
                conn.execute(update(TABLE).where(TABLE.c.id == row.id).values(
                    status=RunStatus.CANCELLED, finished_at=now, lease_owner=None,
                    lease_expires_at=None))  # fmt: skip
                return None
            if row.attempts >= MAX_ATTEMPTS:
                conn.execute(update(TABLE).where(TABLE.c.id == row.id).values(
                    status=RunStatus.FAILED, finished_at=now, lease_owner=None,
                    error_summary=f"abandoned after {MAX_ATTEMPTS} attempts"))  # fmt: skip
                return None
            return conn.execute(
                update(TABLE).where(TABLE.c.id == row.id)
                .values(status=RunStatus.RUNNING, lease_owner=owner, attempts=TABLE.c.attempts + 1,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        started_at=func.coalesce(TABLE.c.started_at, now))
                .returning(*TABLE.c)
            ).one()  # fmt: skip

    def heartbeat(self, research_id: str, owner: str, lease_seconds: float,
                  progress: dict[str, Any] | None = None) -> str | None:  # fmt: skip
        """Renew the lease (and record progress): "continue" or "cancel"; None if lost."""
        values: dict[str, Any] = {"lease_expires_at": func.now() + timedelta(seconds=lease_seconds)}
        if progress is not None:
            values["progress"] = progress
        with self.engine.begin() as conn:
            flag = conn.execute(
                update(TABLE)
                .where(TABLE.c.id == research_id, TABLE.c.lease_owner == owner,
                       TABLE.c.status == RunStatus.RUNNING)
                .values(**values).returning(TABLE.c.cancel_requested)
            ).scalar_one_or_none()  # fmt: skip
        if flag is None:
            return None
        return "cancel" if flag else "continue"

    def finish(self, research_id: str, owner: str, report: Report | None, status: RunStatus,
               error: str | None = None) -> bool:  # fmt: skip
        with self.engine.begin() as conn:
            done = conn.execute(
                update(TABLE)
                .where(TABLE.c.id == research_id, TABLE.c.lease_owner == owner,
                       TABLE.c.status == RunStatus.RUNNING)
                .values(status=status, finished_at=func.now(), lease_owner=None,
                        lease_expires_at=None, error_summary=error,
                        report=report.model_dump(mode="json") if report else None)
            ).rowcount  # fmt: skip
        return bool(done)

    def cited_digests(self) -> set[str]:
        """Raw artifacts that stored reports' evidence cites (the page or PDF and its
        extraction): ``gtm-crawl --prune`` keeps them, so a report stays verifiable."""
        # ponytail: reads every stored report; a digest table if reports grow to many thousands.
        out: set[str] = set()
        stored = select(MarketResearch.report).where(MarketResearch.report.is_not(None))
        with self.engine.connect() as conn:
            for r in conn.execute(stored).scalars():
                cited = {e["source_id"] for e in r.get("evidence", [])}
                for s in r.get("sources", []):
                    if s["source_id"] in cited:
                        out.update(d for d in (s.get("raw_sha256"), s.get("extracted_sha256")) if d)
        return out

    def record_verification(self, research_id: str, verification: dict[str, Any]) -> None:
        """Keep the latest verification beside the report (which is never rewritten)."""
        with self.engine.begin() as conn:
            conn.execute(update(TABLE).where(TABLE.c.id == research_id)
                         .values(verification=verification))  # fmt: skip

    def release(self, research_id: str, owner: str) -> None:
        """Hand a run back for another worker (shutdown), without counting it as done or
        as an attempt (only runs whose worker died count towards ``MAX_ATTEMPTS``)."""
        with self.engine.begin() as conn:
            conn.execute(update(TABLE).where(TABLE.c.id == research_id,
                                             TABLE.c.lease_owner == owner)
                         .values(status=RunStatus.QUEUED, lease_owner=None,
                                 lease_expires_at=None,
                                 attempts=TABLE.c.attempts - 1))  # fmt: skip

    def cancel(self, research_id: str) -> str | None:
        """Cancel a queued run at once, ask a running one to stop; its status, or None."""
        with self.engine.begin() as conn:
            row = conn.execute(select(TABLE.c.status).where(TABLE.c.id == research_id)
                               .with_for_update()).one_or_none()  # fmt: skip
            if row is None:
                return None
            if row.status == RunStatus.QUEUED:
                conn.execute(update(TABLE).where(TABLE.c.id == research_id).values(
                    status=RunStatus.CANCELLED, finished_at=func.now()))  # fmt: skip
                return str(RunStatus.CANCELLED)
            if row.status == RunStatus.RUNNING:
                conn.execute(update(TABLE).where(TABLE.c.id == research_id)
                             .values(cancel_requested=True))  # fmt: skip
            return str(row.status)

    def latest_report(self, subject_key: str) -> Report | None:
        """The newest finished report of the subject with evidence in it (cache fallback)."""
        with self.engine.connect() as conn:
            data = conn.execute(
                select(MarketResearch.report)
                .where(MarketResearch.subject_key == subject_key,
                       MarketResearch.status.in_((RunStatus.COMPLETED, RunStatus.PARTIAL)),
                       MarketResearch.report.is_not(None))
                .order_by(MarketResearch.finished_at.desc()).limit(1)
            ).scalar_one_or_none()  # fmt: skip
        return Report.model_validate(data) if data else None
