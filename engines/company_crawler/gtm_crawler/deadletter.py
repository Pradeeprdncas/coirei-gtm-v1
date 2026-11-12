"""The dead-letter queue (spec sections 21, 23): work that keeps failing.

Items are written where the failure is decided (``db.store`` for URLs,
``jobs`` for jobs) with ``record``, in the same transaction that moves the
work to its dead state, so the queue and the state never disagree. At most
one item is open per URL of a crawl and per job, so writing one twice (at
least once delivery, a crash between steps) is harmless.

``DeadLetters`` is the operator's side: list open items, requeue one (the URL
goes back to its crawl's frontier with fresh attempts; a job is started
again), or discard it. Resolved items keep their row.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .config import UrlSettings
from .db.models import Crawl, CrawlJob, CrawlUrl, DeadLetterItem, Url
from .domain import UrlState
from .metrics import DEAD_LETTERS

log = logging.getLogger(__name__)

# Crawls whose frontier can still take a requeued URL (a resume picks it up).
_OPEN_CRAWLS = ("running", "paused")


class DeadLetterError(Exception):
    """The item cannot be resolved that way (unknown, resolved, crawl finished)."""


def record(
    conn: Connection,
    entity: str,
    error_class: str,
    *,
    job_id: int | None = None,
    crawl_id: int | None = None,
    url_id: int | None = None,
    error: str | None = None,
    attempts: int = 0,
    payload: dict[str, Any] | None = None,
) -> None:
    """Add an open item unless one is already open for the same URL / job."""
    inserted = conn.execute(
        pg_insert(DeadLetterItem)
        .values(entity=entity, error_class=error_class, job_id=job_id, crawl_id=crawl_id,
                url_id=url_id, error=(error or "")[:2000] or None, attempts=attempts,
                payload=payload or {})
        .on_conflict_do_nothing()
        .returning(DeadLetterItem.id)
    ).scalar()  # fmt: skip
    if inserted is not None:
        DEAD_LETTERS.labels(entity, error_class).inc()
        log.warning("dead_letter.added", extra={"entity": entity, "error_class": error_class,
                                                "job_id": job_id, "crawl_id": crawl_id,
                                                "attempts": attempts})  # fmt: skip


class DeadLetters:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def list(self, *, include_resolved: bool = False, limit: int = 100) -> list[Any]:
        query = (
            select(DeadLetterItem, Url.url)
            .outerjoin(Url, Url.id == DeadLetterItem.url_id)
            .order_by(DeadLetterItem.id.desc())
            .limit(limit)
        )
        if not include_resolved:
            query = query.where(DeadLetterItem.resolved_at.is_(None))
        with self.engine.connect() as conn:
            return list(conn.execute(query).all())

    def get(self, item_id: int) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(
                select(DeadLetterItem, Url.url)
                .outerjoin(Url, Url.id == DeadLetterItem.url_id)
                .where(DeadLetterItem.id == item_id)
            ).one_or_none()  # fmt: skip

    def discard(self, item_id: int, actor: str = "api") -> None:
        with self.engine.begin() as conn:
            self._resolve(conn, self._open(conn, item_id), "discarded", actor)

    def requeue(self, item_id: int, urls: UrlSettings, actor: str = "api") -> dict[str, Any]:
        """Put the work back. A URL returns to its crawl's frontier (the crawl must
        still be running or paused); a job is started again as a new job."""
        from .jobs import JobStore  # jobs imports this module

        with self.engine.begin() as conn:
            item = self._open(conn, item_id)
            if item.entity == "url":
                status = conn.execute(
                    select(Crawl.status).where(Crawl.id == item.crawl_id)
                ).scalar_one()  # fmt: skip
                if status not in _OPEN_CRAWLS:
                    raise DeadLetterError(
                        f"crawl {item.crawl_id} is {status}; start a new job to fetch it again"
                    )
                conn.execute(
                    update(CrawlUrl)
                    .where(CrawlUrl.crawl_id == item.crawl_id, CrawlUrl.url_id == item.url_id,
                           CrawlUrl.state == UrlState.DLQ)
                    .values(state=UrlState.ELIGIBLE, attempts=0, lease_expiries=0,
                            skip_reason=None, next_attempt_at=None)
                )  # fmt: skip
                self._resolve(conn, item, "requeued", actor)
                return {"entity": "url", "crawl_id": item.crawl_id}
            root = conn.execute(
                select(CrawlJob.root_url).where(CrawlJob.id == item.job_id)
            ).scalar_one()  # fmt: skip
        job, created = JobStore(self.engine).create(root, urls, actor=actor)
        with self.engine.begin() as conn:
            self._resolve(conn, self._open(conn, item_id), "requeued", actor,
                          {"requeued_as_job": job.id})  # fmt: skip
        return {"entity": "job", "job_id": job.id, "created": created}

    @staticmethod
    def _open(conn: Connection, item_id: int) -> Any:
        item = conn.execute(
            select(DeadLetterItem).where(DeadLetterItem.id == item_id).with_for_update()
        ).one_or_none()  # fmt: skip
        if item is None:
            raise DeadLetterError(f"no dead-letter item {item_id}")
        if item.resolved_at is not None:
            raise DeadLetterError(f"dead-letter item {item_id} is already {item.resolution}")
        return item

    @staticmethod
    def _resolve(
        conn: Connection,
        item: Any,
        resolution: str,
        actor: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            update(DeadLetterItem).where(DeadLetterItem.id == item.id)
            .values(resolved_at=func.now(), resolution=resolution, resolved_by=actor,
                    payload={**item.payload, **(extra or {})})
        )  # fmt: skip
        log.info("dead_letter.resolved", extra={"item_id": item.id, "resolution": resolution})
