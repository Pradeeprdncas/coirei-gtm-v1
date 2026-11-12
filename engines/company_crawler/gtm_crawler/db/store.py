"""Persistence for crawl records (crawl, url, crawl_url, fetch, page, link).

Methods are synchronous and short; the crawl loop calls them through
``asyncio.to_thread`` so database I/O never blocks fetching.

``crawl_url`` is the durable frontier: ``claim`` leases the best queued URLs
with ``FOR UPDATE SKIP LOCKED`` (so concurrent claimers never get the same
URL), and a lease that expires (its worker died) makes the URL claimable again.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, case, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from .. import deadletter
from ..domain import IN_FLIGHT_URL_STATES, UrlState
from ..fetch.http import FetchResult
from ..frontier import FrontierEntry
from ..parse import PARSER_VERSION, ParsedPage
from ..rawstore import ARTIFACT_VERSION
from ..urls import registrable_domain, url_host
from .models import (
    Artifact,
    Crawl,
    CrawlEvent,
    CrawlUrl,
    Fetch,
    Link,
    Organization,
    Page,
    RobotsSnapshot,
    SearchQuery,
    SearchResultRow,
    SitemapFetch,
    Url,
)


def ensure_organization(conn: Connection, domain: str) -> int:
    """The organization for ``domain``, created if unknown."""
    org_id: int = conn.execute(
        pg_insert(Organization)
        .values(registrable_domain=domain)
        .on_conflict_do_update(
            index_elements=["registrable_domain"],
            index_where=Organization.registrable_domain.is_not(None),
            set_={"registrable_domain": domain},
        )
        .returning(Organization.id)
    ).scalar_one()
    return org_id


@dataclass(frozen=True)
class Validators:
    """What an earlier successful fetch of a URL left for a conditional request."""

    etag: str | None
    last_modified: str | None
    raw_sha256: str
    content_type: str | None

    @property
    def validators(self) -> dict[str, str]:
        return {k: v for k, v in (("etag", self.etag), ("last_modified", self.last_modified))
                if v}  # fmt: skip


@dataclass
class ResumeState:
    """What a restarted crawl needs to rebuild its in-memory state."""

    urls: list[tuple[str, str]]  # (url, state) of site URLs
    pages: list[tuple[int, str | None, int | None]]  # (page id, text sha256, simhash)
    inlinks: dict[str, int]
    reference_urls: list[str]
    checkpoint: dict[str, Any]


class CrawlStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._url_ids: dict[str, int] = {}
        self._lock = threading.Lock()

    def start_crawl(
        self,
        seed_url: str,
        config: dict[str, Any],
        job_id: int | None = None,
        versions: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """Create (or reuse) the organization for the seed's site and a new crawl row."""
        with self.engine.begin() as conn:
            org_id = ensure_organization(conn, registrable_domain(url_host(seed_url)))
            crawl_id = conn.execute(
                insert(Crawl)
                .values(
                    organization_id=org_id,
                    seed_url=seed_url,
                    config=config,
                    job_id=job_id,
                    versions=versions,
                )
                .returning(Crawl.id)
            ).scalar_one()
        return org_id, crawl_id

    def finish_crawl(self, crawl_id: int, status: str, stats: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(Crawl)
                .where(Crawl.id == crawl_id)
                .values(status=status, stats=stats, finished_at=datetime.now(UTC))
            )

    def url_id(self, url: str) -> int:
        return self.url_ids([url])[url]

    def url_ids(self, urls: list[str]) -> dict[str, int]:
        """Ids for ``urls``, inserting unknown ones in one statement."""
        with self._lock:
            result = {u: self._url_ids[u] for u in urls if u in self._url_ids}
        missing = sorted(set(urls) - set(result))
        if missing:
            rows = [
                {
                    "url": u,
                    "host": url_host(u),
                    "registrable_domain": registrable_domain(url_host(u)),
                }
                for u in missing
            ]
            insert_rows = pg_insert(Url).values(rows)
            # The no-op update (so RETURNING gives existing rows too) sets a
            # non-key column: that locks FOR NO KEY UPDATE, which does not wait
            # on link/crawl_url foreign-key checks (KEY SHARE). Setting ``url``,
            # a unique key, locked FOR UPDATE and deadlocked against them.
            stmt = insert_rows.on_conflict_do_update(
                index_elements=["url"], set_={"host": insert_rows.excluded.host}
            ).returning(Url.url, Url.id)
            with self.engine.begin() as conn:
                fetched = {row.url: row.id for row in conn.execute(stmt)}
            with self._lock:
                self._url_ids.update(fetched)
            result.update(fetched)
        return result

    def _frontier_rows(
        self, crawl_id: int, entries: list[FrontierEntry], **extra: Any
    ) -> list[dict[str, Any]]:
        ids = self.url_ids(
            [e.url for e in entries] + [e.parent_url for e in entries if e.parent_url]
        )
        return [
            dict(
                crawl_id=crawl_id,
                url_id=ids[e.url],
                parent_url_id=ids[e.parent_url] if e.parent_url else None,
                depth=e.depth,
                score=e.score,
                priority_signals=e.signals,
                discovered_via=e.discovered_via,
                anchor_text=e.anchor_text,
                **extra,
            )  # fmt: skip
            for e in entries
        ]

    def record_queued(
        self, crawl_id: int, entries: list[FrontierEntry], external: bool = False
    ) -> None:
        """Insert admitted URLs as eligible. Idempotent: a URL already in the crawl is
        left alone, except one that was deferred, which the new admission promotes."""
        if not entries:
            return
        # Discovery order is kept: crawl_url ids break score ties first-come-first-served.
        rows = self._frontier_rows(crawl_id, entries, is_external=external)
        stmt = pg_insert(CrawlUrl)
        promote = stmt.on_conflict_do_update(
            index_elements=["crawl_id", "url_id"],
            set_={
                "state": UrlState.ELIGIBLE, "skip_reason": None,
                **{c: stmt.excluded[c] for c in ("parent_url_id", "depth", "score",
                                                 "priority_signals", "discovered_via",
                                                 "anchor_text")},
            },
            where=CrawlUrl.__table__.c.state == UrlState.DEFERRED,
        )  # fmt: skip
        with self.engine.begin() as conn:
            conn.execute(promote, rows)

    def record_refused(
        self, crawl_id: int, refused: list[tuple[FrontierEntry, UrlState, str]]
    ) -> None:
        """Store URLs the frontier refused (trap / deferred) with the reason. Idempotent."""
        if not refused:
            return
        rows = self._frontier_rows(crawl_id, [e for e, _, _ in refused])
        for row, (_, state, reason) in zip(rows, refused, strict=True):
            row.update(state=state, skip_reason=reason)
        with self.engine.begin() as conn:
            conn.execute(
                pg_insert(CrawlUrl).on_conflict_do_nothing(index_elements=["crawl_id", "url_id"]),
                rows,
            )

    def set_state(
        self,
        crawl_id: int,
        url: str,
        state: str,
        reason: str | None = None,
        retry_in: float | None = None,
    ) -> None:
        """Move ``url`` to ``state`` and release its lease (``retry_in``: retry_wait delay)."""
        url_id = self.url_id(url)
        retry_at = func.now() + timedelta(seconds=retry_in) if retry_in is not None else None
        with self.engine.begin() as conn:
            conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.url_id == url_id)
                .values(state=state, skip_reason=reason, lease_owner=None,
                        lease_expires_at=None, next_attempt_at=retry_at)
            )  # fmt: skip

    def postpone(self, crawl_id: int, url: str, reason: str, retry_in: float) -> None:
        """Hand a leased URL back for ``retry_in`` seconds without using up an
        attempt (it was not fetched: e.g. its host's circuit is open)."""
        url_id = self.url_id(url)
        with self.engine.begin() as conn:
            conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.url_id == url_id)
                .values(state=UrlState.RETRY, skip_reason=reason, lease_owner=None,
                        lease_expires_at=None, attempts=func.greatest(CrawlUrl.attempts - 1, 0),
                        next_attempt_at=func.now() + timedelta(seconds=retry_in))
            )  # fmt: skip

    def advance(self, crawl_id: int, url: str, state: UrlState) -> None:
        """Record progress of a leased URL (fetching, fetched, parsed); keeps the lease.

        Idempotent, and only moves a URL that is still in flight, so a late
        write from a worker that lost its lease cannot reopen a finished URL.
        """
        url_id = self.url_id(url)
        with self.engine.begin() as conn:
            conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.url_id == url_id,
                       CrawlUrl.state.in_(IN_FLIGHT_URL_STATES))
                .values(state=state)
            )  # fmt: skip

    def claim(
        self,
        crawl_id: int,
        owner: str,
        lease_seconds: float,
        limit: int,
        max_lease_expiries: int | None = None,
    ) -> list[tuple[FrontierEntry, int]]:
        """Lease up to ``limit`` of the best claimable site URLs: (entry, attempt number).

        Claimable: queued, retry_wait whose time has come, or in_progress whose
        lease expired. Best: highest score, then shallowest, then oldest.
        """
        if limit <= 0:
            return []
        if max_lease_expiries is not None:
            with self.engine.begin() as conn:
                _expire_leases(conn, crawl_id, max_lease_expiries, expired_only=True)
        now = func.now()
        ready = or_(
            CrawlUrl.state == UrlState.ELIGIBLE,
            and_(CrawlUrl.state == UrlState.RETRY, CrawlUrl.next_attempt_at <= now),
            and_(CrawlUrl.state.in_(IN_FLIGHT_URL_STATES), CrawlUrl.lease_expires_at < now),
        )
        order = (CrawlUrl.score.desc(), CrawlUrl.depth, CrawlUrl.id)
        ids = (
            select(CrawlUrl.id)
            .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.is_external.is_(False), ready)
            .order_by(*order)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        parent = aliased(Url)
        with self.engine.begin() as conn:
            claimed = conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.id.in_(ids.scalar_subquery()))
                .values(state=UrlState.LEASED, lease_owner=owner, attempts=CrawlUrl.attempts + 1,
                        lease_expires_at=now + timedelta(seconds=lease_seconds))
                .returning(CrawlUrl.id)
            ).scalars().all()  # fmt: skip
            if not claimed:
                return []
            rows = conn.execute(
                select(Url.url, parent.url.label("parent"), CrawlUrl.depth, CrawlUrl.score,
                       CrawlUrl.discovered_via, CrawlUrl.anchor_text, CrawlUrl.attempts)
                .join(Url, Url.id == CrawlUrl.url_id)
                .outerjoin(parent, parent.id == CrawlUrl.parent_url_id)
                .where(CrawlUrl.id.in_(claimed))
                .order_by(*order)
            ).all()  # fmt: skip
        return [
            (FrontierEntry(r.url, r.depth, r.score, r.discovered_via, r.parent, r.anchor_text),
             r.attempts)
            for r in rows
        ]  # fmt: skip

    def boost(self, crawl_id: int, deltas: dict[str, float]) -> None:
        """Raise the scores of still-queued URLs (more inlinks found)."""
        if not deltas:
            return
        ids = self.url_ids(list(deltas))
        with self.engine.begin() as conn:
            # Rows in one order (url id) in every transaction: two pages boosting the
            # same URLs concurrently otherwise deadlock (seen on a live crawl).
            for url, delta in sorted(deltas.items(), key=lambda d: ids[d[0]]):
                conn.execute(
                    update(CrawlUrl)
                    .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.url_id == ids[url],
                           CrawlUrl.state == UrlState.ELIGIBLE)
                    .values(
                        score=CrawlUrl.score + delta,
                        # Keep the breakdown summing to the score.
                        priority_signals=func.jsonb_set(
                            func.coalesce(CrawlUrl.priority_signals, text("'{}'::jsonb")),
                            text("'{inlinks}'::text[]"),
                            func.to_jsonb(
                                func.coalesce(
                                    CrawlUrl.priority_signals["inlinks"].as_float(), 0.0
                                ) + delta
                            ),
                        ),
                    )
                )  # fmt: skip

    def recover(self, crawl_id: int, max_lease_expiries: int) -> int:
        """After a crash: URLs a dead worker held go back to the queue, each
        counting a lost lease; one lost too often is dead-lettered (it may be
        what crashed the worker). Returns how many were dead-lettered."""
        with self.engine.begin() as conn:
            return _expire_leases(conn, crawl_id, max_lease_expiries, expired_only=False)

    def dead_letter_url(
        self,
        crawl_id: int,
        url: str,
        error_class: str,
        error: str | None,
        attempts: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Move ``url`` to ``dlq`` and add its dead-letter item, in one transaction."""
        url_id = self.url_id(url)
        with self.engine.begin() as conn:
            conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.url_id == url_id)
                .values(state=UrlState.DLQ, skip_reason=error_class, lease_owner=None,
                        lease_expires_at=None, next_attempt_at=None)
            )  # fmt: skip
            deadletter.record(conn, "url", error_class, crawl_id=crawl_id, url_id=url_id,
                              error=error, attempts=attempts, payload=payload)  # fmt: skip

    def release(self, crawl_id: int) -> None:
        """Return the crawl's leased URLs to the queue (their owner stopped or died)."""
        with self.engine.begin() as conn:
            conn.execute(
                update(CrawlUrl)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.state.in_(IN_FLIGHT_URL_STATES))
                .values(state=UrlState.ELIGIBLE, lease_owner=None, lease_expires_at=None)
            )

    def seconds_to_next_retry(self, crawl_id: int) -> float | None:
        """Seconds until the earliest retry_wait URL is due (0 if overdue); None if none."""
        with self.engine.connect() as conn:
            wait = conn.execute(
                select(func.extract("epoch", func.min(CrawlUrl.next_attempt_at) - func.now()))
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.state == UrlState.RETRY)
            ).scalar()  # fmt: skip
        return None if wait is None else max(0.0, float(wait))

    def validators(self, url: str) -> Validators | None:
        """ETag / Last-Modified of the latest stored 200 response for ``url``, any crawl."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(Fetch.headers, Fetch.raw_sha256, Fetch.content_type)
                .join(Url, Url.id == Fetch.url_id)
                .where(Url.url == url, Fetch.status_code.in_((200, 304)),
                       Fetch.raw_sha256.is_not(None), Fetch.final_url_id.is_(None),
                       Fetch.method == "http")
                .order_by(Fetch.id.desc())
                .limit(1)
            ).one_or_none()  # fmt: skip
        if row is None or not row.headers:
            return None
        etag, modified = row.headers.get("etag"), row.headers.get("last-modified")
        if not (etag or modified):
            return None
        return Validators(etag, modified, row.raw_sha256, row.content_type)

    def url_state(self, crawl_id: int, url: str) -> str | None:
        with self.engine.connect() as conn:
            state: str | None = conn.execute(
                select(CrawlUrl.state)
                .join(Url, Url.id == CrawlUrl.url_id)
                .where(CrawlUrl.crawl_id == crawl_id, Url.url == url)
            ).scalar()
        return state

    def save_checkpoint(self, crawl_id: int, checkpoint: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(Crawl).where(Crawl.id == crawl_id).values(checkpoint=checkpoint))

    def resume_state(self, crawl_id: int) -> ResumeState:
        with self.engine.connect() as conn:
            urls = conn.execute(
                select(Url.url, CrawlUrl.state)
                .join(Url, Url.id == CrawlUrl.url_id)
                .where(CrawlUrl.crawl_id == crawl_id, CrawlUrl.is_external.is_(False))
                .order_by(CrawlUrl.id)
            ).all()
            pages = conn.execute(
                select(Page.id, Page.text_sha256, Page.simhash)
                .join(Fetch, Fetch.id == Page.fetch_id)
                .where(Fetch.crawl_id == crawl_id, Page.duplicate_of_page_id.is_(None))
                .order_by(Page.id)
            ).all()
            links = conn.execute(
                select(Url.url, Link.is_external, Link.is_reference, func.count())
                .join(Url, Url.id == Link.target_url_id)
                .join(Page, Page.id == Link.page_id)
                .join(Fetch, Fetch.id == Page.fetch_id)
                .where(Fetch.crawl_id == crawl_id)
                .group_by(Url.url, Link.is_external, Link.is_reference)
            ).all()
            checkpoint = conn.execute(
                select(Crawl.checkpoint).where(Crawl.id == crawl_id)
            ).scalar()  # fmt: skip
        return ResumeState(
            urls=[(u.url, u.state) for u in urls],
            pages=[(p.id, p.text_sha256, p.simhash) for p in pages],
            inlinks={u: n for u, external, _, n in links if not external},
            reference_urls=[u for u, _, reference, _ in links if reference],
            checkpoint=checkpoint or {},
        )

    def record_fetch(
        self,
        crawl_id: int,
        result: FetchResult,
        method: str,
        raw_sha256: str | None,
        trigger: str | None = None,
    ) -> int:
        final_id = self.url_id(result.final_url) if result.final_url != result.url else None
        values = dict(
            crawl_id=crawl_id, url_id=self.url_id(result.url), final_url_id=final_id,
            method=method, attempt=result.attempts, redirect_chain=result.redirect_chain,
            status_code=result.status, headers=result.headers or None,
            content_type=result.content_type,
            content_length=len(result.body) if result.body is not None else None,
            raw_sha256=raw_sha256,
            error=f"{result.error}: {result.error_detail}" if result.error else None,
            fetched_at=result.fetched_at, duration_ms=result.duration_ms, trigger=trigger,
        )  # fmt: skip
        with self.engine.begin() as conn:
            _record_artifact(conn, raw_sha256, result.body, result.content_type)
            return int(
                conn.execute(insert(Fetch).values(**values).returning(Fetch.id)).scalar_one()
            )

    def record_page(self, fetch_id: int, page: ParsedPage, duplicate_of: int | None) -> int:
        canonical_id = self.url_id(page.canonical_url) if page.canonical_url else None
        meta = {
            "tags": page.meta,
            "encoding": page.encoding,
            "published_at_source": page.published_at_source,
            "json_ld_errors": page.json_ld_errors,
            "invalid_links": page.invalid_links,
            "blocks_truncated": page.blocks_truncated,
            "links_truncated": page.links_truncated,
            "parse_error": page.parse_error,
            "parse_warnings": page.parse_warnings,
            "description": page.description,
            "headings": page.headings[:200],
            "microdata": [
                {"xpath": m.xpath, "types": list(m.types), "properties": m.properties}
                for m in page.microdata
            ],
        }
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    insert(Page)
                    .values(
                        fetch_id=fetch_id,
                        canonical_url_id=canonical_id,
                        duplicate_of_page_id=duplicate_of,
                        title=page.title,
                        language=page.language,
                        cleaned_text=page.cleaned_text,
                        text_sha256=page.text_sha256,
                        simhash=page.simhash,
                        published_at=page.published_at,
                        structured_data=page.json_ld,
                        meta=meta,
                        parser_version=PARSER_VERSION,
                    )  # fmt: skip
                    .returning(Page.id)
                ).scalar_one()
            )

    def record_links(
        self, page_id: int, links: list[tuple[str, str, str | None, bool, bool]]
    ) -> None:
        """``links`` are (url, anchor_text, rel, is_external, is_reference)."""
        if not links:
            return
        ids = self.url_ids([link[0] for link in links])
        rows = [
            dict(page_id=page_id, target_url_id=ids[url], anchor_text=text or None,
                 rel=rel, is_external=external, is_reference=reference)
            for url, text, rel, external, reference in links
        ]  # fmt: skip
        with self.engine.begin() as conn:
            conn.execute(insert(Link), rows)

    def record_robots(
        self, crawl_id: int, origin: str, status: str, result: FetchResult, raw_sha256: str | None
    ) -> int:
        values = dict(
            crawl_id=crawl_id, origin=origin, status=status, status_code=result.status,
            raw_sha256=raw_sha256, fetched_at=result.fetched_at,
            error=f"{result.error}: {result.error_detail}" if result.error else None,
        )  # fmt: skip
        with self.engine.begin() as conn:
            _record_artifact(conn, raw_sha256, result.body, "text/plain")
            snapshot_id: int = conn.execute(
                insert(RobotsSnapshot).values(**values).returning(RobotsSnapshot.id)
            ).scalar_one()
        return snapshot_id

    def record_search(
        self,
        crawl_id: int,
        template: str,
        query: str,
        include_domains: tuple[str, ...],
        status: str,
        *,
        provider: str | None = None,
        results: list[tuple[str, str | None, str | None, int]] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        """Store one search query and its results
        ``(normalized url, title, published, provider position)``."""
        results = results or []
        ids = self.url_ids(list(dict.fromkeys(r[0] for r in results)))
        with self.engine.begin() as conn:
            query_id = int(conn.execute(
                insert(SearchQuery).values(
                    crawl_id=crawl_id, template=template, query=query,
                    include_domains=list(include_domains), provider=provider, status=status,
                    error=error, result_count=len(results), duration_ms=duration_ms,
                ).returning(SearchQuery.id)
            ).scalar_one())  # fmt: skip
            if results:
                conn.execute(
                    insert(SearchResultRow),
                    [{"query_id": query_id, "position": position, "url_id": ids[url],
                      "title": title, "published": published}
                     for url, title, published, position in results],
                )  # fmt: skip
        return query_id

    def mark_pruned(self, digests: list[str]) -> None:
        """Record that retention deleted these bodies; their rows stay for lineage."""
        with self.engine.begin() as conn:
            for start in range(0, len(digests), 1000):
                conn.execute(
                    update(Artifact)
                    .where(Artifact.sha256.in_(digests[start : start + 1000]))
                    .values(pruned_at=func.now())
                )

    def record_sitemap(self, crawl_id: int, record: Any, raw_sha256: str | None) -> None:
        """Store one sitemap request (``sitemaps.SitemapFetchRecord``)."""
        result = record.result
        ids = self.url_ids([record.url] + ([record.parent] if record.parent else []))
        values = dict(
            crawl_id=crawl_id, url_id=ids[record.url],
            parent_url_id=ids[record.parent] if record.parent else None, depth=record.depth,
            status_code=result.status if result else None,
            content_type=result.content_type if result else None,
            content_length=len(result.body) if result and result.body is not None else None,
            raw_sha256=raw_sha256, error=record.error, url_count=record.url_count,
            child_count=record.child_count, dropped=record.dropped,
            duration_ms=result.duration_ms if result else None,
            fetched_at=result.fetched_at if result else func.now(),
        )  # fmt: skip
        with self.engine.begin() as conn:
            if result is not None:
                _record_artifact(conn, raw_sha256, result.body, result.content_type)
            conn.execute(insert(SitemapFetch).values(**values))

    def record_decision(
        self,
        crawl_id: int,
        job_id: int | None,
        url: str,
        decision: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append a policy decision about ``url`` to the crawl's audit trail."""
        with self.engine.begin() as conn:
            conn.execute(
                insert(CrawlEvent).values(
                    entity="url", job_id=job_id, crawl_id=crawl_id, url_id=self.url_id(url),
                    event="policy", to_state=str(decision), reason=reason, detail=detail or None,
                )
            )  # fmt: skip

    def crawl_row(self, crawl_id: int) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(select(Crawl).where(Crawl.id == crawl_id)).one()


def _expire_leases(
    conn: Connection, crawl_id: int, max_lease_expiries: int, *, expired_only: bool
) -> int:
    """Hand back in-flight URLs (all, or those whose lease expired), counting a
    lost lease each; dead-letter those at ``max_lease_expiries``. Returns that count."""
    condition = [CrawlUrl.crawl_id == crawl_id, CrawlUrl.state.in_(IN_FLIGHT_URL_STATES)]
    if expired_only:
        condition.append(CrawlUrl.lease_expires_at < func.now())
    poisoned = CrawlUrl.lease_expiries + 1 >= max_lease_expiries
    rows = conn.execute(
        update(CrawlUrl)
        .where(*condition)
        .values(lease_expiries=CrawlUrl.lease_expiries + 1, lease_owner=None,
                lease_expires_at=None,
                state=case((poisoned, UrlState.DLQ), else_=UrlState.ELIGIBLE),
                skip_reason=case((poisoned, "lease_expired"), else_=CrawlUrl.skip_reason))
        .returning(CrawlUrl.url_id, CrawlUrl.state, CrawlUrl.attempts, CrawlUrl.lease_expiries)
    ).all()  # fmt: skip
    dead = [r for r in rows if r.state == UrlState.DLQ]
    for r in dead:
        deadletter.record(
            conn, "url", "lease_expired", crawl_id=crawl_id, url_id=r.url_id,
            error=f"lease lost {r.lease_expiries} times (worker died holding it)",
            attempts=r.attempts, payload={"lease_expiries": r.lease_expiries},
        )  # fmt: skip
    return len(dead)


def _record_artifact(
    conn: Connection, digest: str | None, body: bytes | None, content_type: str | None
) -> None:
    """Record a raw document ``put`` just stored, in the transaction that cites it.

    Idempotent: the first retrieval's metadata is kept. Storing a pruned
    document again makes it available again.
    """
    if digest is None or body is None:
        return
    conn.execute(
        pg_insert(Artifact)
        .values(sha256=digest, size_bytes=len(body), content_type=content_type,
                artifact_version=ARTIFACT_VERSION)
        .on_conflict_do_update(index_elements=["sha256"], set_={"pruned_at": None})
    )  # fmt: skip
