"""Database schema.

The schema is evidence-first: nothing about a company is stored as a bare
answer. What a source said lives in ``observation`` (each tied to an
``evidence`` quote on a fetched ``page``); what the system concludes lives in
``field_assessment``, which references the observations it rests on.

``observation``, ``evidence``, ``relationship`` and ``field_assessment`` are
append-only (enforced by triggers in the initial migration), so history is
never overwritten when newer information arrives.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain import FINAL_JOB_STATES, JobState, UrlState

# Spec section 19 (method fa-v2), then fa-v1's states, kept for earlier rows.
FIELD_STATES = (
    "UNKNOWN", "SUPPORTED", "CORROBORATED", "CONFLICTING", "STALE", "UNSUPPORTED",
    "NOT_APPLICABLE", "FOUND", "VERIFIED", "UNVERIFIED",
)  # fmt: skip
EVIDENCE_ROLES = ("supports", "qualifies", "contradicts", "superseded")
CRAWL_STATUSES = ("running", "paused", "completed", "partial", "failed")
# Spec sections 5 and 8; see domain.py for their meaning and transitions.
JOB_STATUSES = tuple(s.value for s in JobState)
# A crawl fetches; a refresh re-reads stored evidence (new documents, new prompts).
JOB_KINDS = ("crawl", "refresh")
ACTIVE_JOB_STATUSES = tuple(s.value for s in JobState if s not in FINAL_JOB_STATES)
CRAWL_URL_STATES = tuple(s.value for s in UrlState)
CRAWL_EVENT_ENTITIES = ("job", "crawl", "url")
DISCOVERY_METHODS = ("seed", "link", "sitemap", "robots", "redirect", "search", "canonical",
                     "structured_data")  # fmt: skip
# "upload": a document the company gave (documents.py), not a retrieval.
# "search_contents": the search provider's copy of a page we may not fetch
# ourselves (a LinkedIn company page; crawl._contents).
FETCH_METHODS = ("http", "browser", "upload", "search_contents")
LOCATOR_TYPES = ("xpath", "json_path", "meta", "page")
OBSERVATION_KINDS = ("stated", "derived")
RELATIONSHIP_TYPES = (
    "investor", "tech_partner", "strategic_partner", "competitor", "candidate_competitor",
)  # fmt: skip
ROBOTS_STATUSES = ("parsed", "unavailable", "unreachable")
SEARCH_QUERY_STATUSES = ("ok", "failed", "over_budget")
EXTRACTION_TRIGGERS = ("crawl", "replay", "document")
# Identity resolution (docs/architecture/ENTITY_RESOLUTION.md). A decision may
# also be 'unknown'; a link is only kept for a candidate that was named.
IDENTITY_LINK_STATUSES = ("match", "probable_match", "conflicting", "rejected")
IDENTITY_OUTCOMES = (*IDENTITY_LINK_STATUSES, "unknown")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    """Any company the system knows about: crawl targets, competitors, investors, partners."""

    __tablename__ = "organization"
    __table_args__ = (
        # A competitor named on a comparison page may have no known domain yet.
        Index(
            "uq_organization_registrable_domain",
            "registrable_domain",
            unique=True,
            postgresql_where=text("registrable_domain IS NOT NULL"),
        ),
        # One row per name per naming organization, whatever the capitalization.
        Index(
            "uq_organization_named_by_name",
            "named_by_org_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("named_by_org_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    registrable_domain: Mapped[str | None] = mapped_column(Text)
    # The organization whose pages named this one. Such a row is that company's
    # mention of a name, not yet a known company: the same name used by another
    # company is another row, and ``identity_link`` says which company it is.
    # NULL for crawl targets and for rows named before migration 0015.
    named_by_org_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"))
    created_at: Mapped[datetime] = _created_at()


class CrawlJob(Base):
    """One requested run of the pipeline for a company: its crawls, extraction, dossier.

    Workers claim jobs through a lease (``lease_owner``, ``lease_expires_at``)
    renewed by heartbeats; a job whose lease expired is resumed by another
    worker from the durable crawl state.
    """

    __tablename__ = "crawl_job"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint(_in("kind", JOB_KINDS), name="kind"),
        # At most one unfinished job per company: starting a crawl is idempotent.
        Index(
            "uq_crawl_job_active_organization",
            "organization_id",
            unique=True,
            postgresql_where=text(_in("status", ACTIVE_JOB_STATUSES)),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    root_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="created", index=True)
    kind: Mapped[str] = mapped_column(Text, server_default="crawl")
    # Where a running job is (pipeline.STAGES), for progress; 'done' once it is.
    stage: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    # Versions of the code and configuration that last ran it (spec section 24).
    versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error_summary: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Crawl(Base):
    """One crawl of one site, with the exact configuration it ran under."""

    __tablename__ = "crawl"
    __table_args__ = (CheckConstraint(_in("status", CRAWL_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_job.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    seed_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="running")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # In-memory crawl state saved periodically so a restarted worker can resume.
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Versions of the code and configuration it started under (spec section 24).
    versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlEvent(Base):
    """An append-only record of a state change or decision (spec section 21).

    Job transitions are recorded with who made them (``actor``: a worker id,
    ``api``, ``cli``) and why, so a job's history can be reconstructed.
    """

    __tablename__ = "crawl_event"
    __table_args__ = (
        CheckConstraint(_in("entity", CRAWL_EVENT_ENTITIES), name="entity"),
        Index("ix_crawl_event_job_id_id", "job_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_job.id"))
    crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawl.id"), index=True)
    url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    event: Mapped[str] = mapped_column(Text)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = _created_at()


class Artifact(Base):
    """A stored raw document (spec section 22). The body is in the raw store
    under its SHA-256; this row is the database's record of it.

    ``artifact_version`` is the storage format (rawstore.ARTIFACT_VERSION).
    ``pruned_at`` is set when retention deleted the body: the row stays, so
    lineage still shows what was retrieved even after the bytes are gone.
    """

    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="sha256"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_bytes"),
    )

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    # NULL only for robots.txt snapshots stored before artifacts were recorded.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(Text)
    artifact_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RobotsSnapshot(Base):
    """The robots.txt a crawl obeyed for one origin; the body is in the raw store."""

    __tablename__ = "robots_snapshot"
    __table_args__ = (
        CheckConstraint(_in("status", ROBOTS_STATUSES), name="status"),
        CheckConstraint("raw_sha256 IS NULL OR length(raw_sha256) = 64", name="raw_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # A resumed crawl loads robots.txt again; each load is its own snapshot.
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    origin: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    raw_sha256: Mapped[str | None] = mapped_column(ForeignKey("artifact.sha256"), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _created_at()


class SitemapFetch(Base):
    """One sitemap request of a crawl and what it yielded (spec section 21)."""

    __tablename__ = "sitemap_fetch"
    __table_args__ = (
        CheckConstraint("raw_sha256 IS NULL OR length(raw_sha256) = 64", name="raw_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    url_id: Mapped[int] = mapped_column(ForeignKey("url.id"))
    # The sitemap index that listed it; None for robots.txt / /sitemap.xml.
    parent_url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    depth: Mapped[int] = mapped_column(SmallInteger)
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    content_type: Mapped[str | None] = mapped_column(Text)
    content_length: Mapped[int | None] = mapped_column(BigInteger)
    raw_sha256: Mapped[str | None] = mapped_column(ForeignKey("artifact.sha256"), index=True)
    # Why it yielded nothing: robots_disallowed, http_404, parse_error: ...
    error: Mapped[str | None] = mapped_column(Text)
    url_count: Mapped[int] = mapped_column(Integer, server_default="0")
    child_count: Mapped[int] = mapped_column(Integer, server_default="0")
    dropped: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = _created_at()


DEAD_LETTER_ENTITIES = ("url", "job")
DEAD_LETTER_RESOLUTIONS = ("requeued", "discarded")


class DeadLetterItem(Base):
    """Work that failed repeatedly or crashed its worker (spec sections 21, 23).

    A URL of a crawl (retryable failures past ``max_url_attempts``, leases
    lost past ``max_url_lease_expiries``, an unexpected error while processing
    it) or a job (abandoned after ``max_job_attempts``, or failed by an
    unexpected error). Resolving keeps the row: ``requeued`` or ``discarded``.
    """

    __tablename__ = "dead_letter_item"
    __table_args__ = (
        CheckConstraint(_in("entity", DEAD_LETTER_ENTITIES), name="entity"),
        CheckConstraint(
            "(entity = 'url' AND crawl_id IS NOT NULL AND url_id IS NOT NULL)"
            " OR (entity = 'job' AND job_id IS NOT NULL)",
            name="refs",
        ),
        CheckConstraint(
            "(resolved_at IS NULL AND resolution IS NULL)"
            " OR (resolved_at IS NOT NULL AND resolution IN ('requeued', 'discarded'))",
            name="resolution",
        ),
        # One open item per URL of a crawl and per job: writing one is idempotent.
        Index("uq_dead_letter_item_open_url", "crawl_id", "url_id", unique=True,
              postgresql_where=text("entity = 'url' AND resolved_at IS NULL")),
        Index("uq_dead_letter_item_open_job", "job_id", unique=True,
              postgresql_where=text("entity = 'job' AND resolved_at IS NULL")),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_job.id"))
    crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawl.id"))
    url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    # What kind of failure: a fetch error class, http_<status>, lease_expired,
    # abandoned, or an exception type.
    error_class: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(Text)


class SearchQuery(Base):
    """One search query of a crawl (spec section 21): what was asked, of whom,
    and what came back. Credentials are never stored."""

    __tablename__ = "search_query"
    __table_args__ = (CheckConstraint(_in("status", SEARCH_QUERY_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    # The SEARCH_PLAN template, and the query sent after filling it in.
    template: Mapped[str] = mapped_column(Text)
    query: Mapped[str] = mapped_column(Text)
    include_domains: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The provider that answered (None if none did).
    provider: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    result_count: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class SearchResultRow(Base):
    """One normalized result of a search query. Discovery only: a result is
    never evidence; the page it names must be fetched first."""

    __tablename__ = "search_result"
    __table_args__ = (UniqueConstraint("query_id", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("search_query.id"))
    position: Mapped[int] = mapped_column(SmallInteger)
    url_id: Mapped[int] = mapped_column(ForeignKey("url.id"), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    published: Mapped[str | None] = mapped_column(Text)


class Extraction(Base):
    """One extraction run over a stored crawl. The latest run of a crawl is current.

    ``versions`` is ``versions.current_versions`` at run time (NULL: made before
    migration 0014, unknown); ``trigger`` says whether the pipeline or a replay
    (``replay.py``) made it.
    """

    __tablename__ = "extraction"
    __table_args__ = (CheckConstraint(_in("trigger", EXTRACTION_TRIGGERS), name="trigger"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    trigger: Mapped[str] = mapped_column(Text, server_default="crawl")
    created_at: Mapped[datetime] = _created_at()


class Url(Base):
    """A normalized URL, shared across crawls."""

    __tablename__ = "url"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    host: Mapped[str] = mapped_column(Text)
    registrable_domain: Mapped[str] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = _created_at()


class CrawlUrl(Base):
    """A URL's place in one crawl's frontier: how it was found and what happened to it."""

    __tablename__ = "crawl_url"
    __table_args__ = (
        UniqueConstraint("crawl_id", "url_id"),
        CheckConstraint(_in("state", CRAWL_URL_STATES), name="state"),
        CheckConstraint(_in("discovered_via", DISCOVERY_METHODS), name="discovered_via"),
        CheckConstraint("depth >= 0", name="depth"),
        Index("ix_crawl_url_claim", "crawl_id", "state", "score"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"))
    url_id: Mapped[int] = mapped_column(ForeignKey("url.id"))
    parent_url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    depth: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    # The score's breakdown by signal (priority.Prioritizer.signals).
    priority_signals: Mapped[dict[str, float] | None] = mapped_column(JSONB(none_as_null=True))
    state: Mapped[str] = mapped_column(Text, server_default="eligible")
    skip_reason: Mapped[str | None] = mapped_column(Text)
    discovered_via: Mapped[str] = mapped_column(Text)
    anchor_text: Mapped[str | None] = mapped_column(Text)
    # Single pages fetched off-site (external phase); never claimed by the site loop.
    is_external: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    # Times its lease ran out (its worker died holding it); see crawl.max_url_lease_expiries.
    lease_expiries: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = _created_at()


class Fetch(Base):
    """One HTTP or browser retrieval attempt, successful or not."""

    __tablename__ = "fetch"
    __table_args__ = (
        CheckConstraint(_in("method", FETCH_METHODS), name="method"),
        CheckConstraint("raw_sha256 IS NULL OR length(raw_sha256) = 64", name="raw_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    url_id: Mapped[int] = mapped_column(ForeignKey("url.id"), index=True)
    final_url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    method: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    content_type: Mapped[str | None] = mapped_column(Text)
    content_length: Mapped[int | None] = mapped_column(BigInteger)
    # Raw bodies live in the content-addressed raw store; the hash is the key.
    raw_sha256: Mapped[str | None] = mapped_column(ForeignKey("artifact.sha256"), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Why a browser render was attempted (browser fetches only).
    trigger: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _created_at()


class Page(Base):
    """Parsed result of a successful fetch. Raw HTML stays in the raw store."""

    __tablename__ = "page"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fetch_id: Mapped[int] = mapped_column(ForeignKey("fetch.id"), unique=True)
    canonical_url_id: Mapped[int | None] = mapped_column(ForeignKey("url.id"))
    # Exact or near-duplicate of an earlier page in the same crawl. Duplicates
    # are stored (they are fetch evidence) but their links are not followed.
    duplicate_of_page_id: Mapped[int | None] = mapped_column(ForeignKey("page.id"))
    title: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    cleaned_text: Mapped[str | None] = mapped_column(Text)
    text_sha256: Mapped[str | None] = mapped_column(Text, index=True)
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    structured_data: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    parser_version: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime] = _created_at()


class Link(Base):
    """An outbound link found on a page."""

    __tablename__ = "link"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("page.id"), index=True)
    target_url_id: Mapped[int] = mapped_column(ForeignKey("url.id"), index=True)
    anchor_text: Mapped[str | None] = mapped_column(Text)
    rel: Mapped[str | None] = mapped_column(Text)
    is_external: Mapped[bool] = mapped_column(Boolean)
    # External profiles (LinkedIn, Crunchbase, ...) are recorded, never fetched.
    is_reference: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"))
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    identification_basis: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Evidence(Base):
    """The exact quote supporting an observation and where on the page it sits."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(_in("locator_type", LOCATOR_TYPES), name="locator_type"),
        CheckConstraint("length(quote) > 0", name="quote_not_empty"),
        CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL)"
            " OR (0 <= char_start AND char_start <= char_end)",
            name="char_range",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("page.id"), index=True)
    quote: Mapped[str] = mapped_column(Text)
    locator_type: Mapped[str] = mapped_column(Text)
    locator: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class Observation(Base):
    """One thing one source stated at one point in time. Append-only.

    ``value_verbatim`` is always the source's own wording. ``value_normalized``
    is filled only when the wording supports an exact typed value, and keeps
    qualifiers ("more than 500" is {"qualifier": "gt", "value": 500}, never 500).

    Source tier and type are recorded here rather than in a lookup table
    because they depend on the observation's subject: the same domain is
    first-party for its own company and third-party for everyone else.
    """

    __tablename__ = "observation"
    __table_args__ = (
        CheckConstraint(_in("kind", OBSERVATION_KINDS), name="kind"),
        CheckConstraint("source_tier BETWEEN 1 AND 4", name="source_tier"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("extraction_certainty BETWEEN 0 AND 1", name="extraction_certainty"),
        CheckConstraint("length(value_verbatim) > 0", name="value_verbatim_not_empty"),
        Index("ix_observation_subject_field", "organization_id", "product_id", "field"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id"), index=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extraction.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"))
    field: Mapped[str] = mapped_column(Text)
    value_verbatim: Mapped[str] = mapped_column(Text)
    value_normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    qualifier: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text, server_default="stated")
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id"))
    source_tier: Mapped[int] = mapped_column(SmallInteger)
    source_type: Mapped[str] = mapped_column(Text)
    is_first_party: Mapped[bool] = mapped_column(Boolean)
    extractor: Mapped[str] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(Text)
    extraction_certainty: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


def is_current() -> ColumnElement[bool]:
    """True for observations from the latest extraction run of their crawl."""
    latest = (
        select(func.max(Extraction.id))
        .where(Extraction.crawl_id == Observation.crawl_id)
        .scalar_subquery()
    )
    return Observation.extraction_id == latest


class Relationship(Base):
    """A sourced link between two organizations. One row per supporting observation.

    Several rows for the same pair and type are corroboration, not duplicates.
    """

    __tablename__ = "relationship"
    __table_args__ = (
        CheckConstraint(_in("type", RELATIONSHIP_TYPES), name="type"),
        CheckConstraint("subject_org_id <> object_org_id", name="distinct_orgs"),
        Index("ix_relationship_subject_type", "subject_org_id", "type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_org_id: Mapped[int] = mapped_column(ForeignKey("organization.id"))
    object_org_id: Mapped[int] = mapped_column(ForeignKey("organization.id"))
    type: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(Text)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    created_at: Mapped[datetime] = _created_at()


class FieldAssessment(Base):
    """The system's conclusion for one field, versioned. Append-only.

    The constraints make the database itself refuse unsupported answers: a
    field with a value must cite observations, and UNKNOWN / CONFLICTING /
    NOT_APPLICABLE fields cannot carry a value.
    """

    __tablename__ = "field_assessment"
    __table_args__ = (
        CheckConstraint(_in("state", FIELD_STATES), name="state"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint(
            "(state IN ('UNKNOWN', 'CONFLICTING', 'NOT_APPLICABLE') AND value IS NULL)"
            " OR (state IN ('SUPPORTED', 'CORROBORATED', 'STALE', 'UNSUPPORTED', 'FOUND',"
            " 'VERIFIED', 'UNVERIFIED') AND value IS NOT NULL"
            " AND confidence IS NOT NULL AND cardinality(observation_ids) > 0)",
            name="value_matches_state",
        ),
        CheckConstraint(
            "state <> 'CONFLICTING' OR cardinality(observation_ids) > 1",
            name="conflict_has_observations",
        ),
        Index("ix_field_assessment_subject_field", "organization_id", "product_id", "field"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"))
    field: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    observation_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'::bigint[]")
    )
    method_version: Mapped[str] = mapped_column(Text)
    # What the confidence was computed from (fa-v2): per-source best
    # confidence, tiers, independence, evidence age and thresholds.
    confidence_components: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    computed_at: Mapped[datetime] = _created_at()


class Corroboration(Base):
    """How one observation bore on one assessment (spec sections 15, 19):
    ``supports``, ``qualifies`` (a bound compatible with the value),
    ``contradicts`` (part of a conflict) or ``superseded`` (an older figure).
    ``source`` is the registrable domain the observation came from: rows of
    one assessment with different sources are independent corroboration."""

    __tablename__ = "corroboration"
    __table_args__ = (
        CheckConstraint(_in("role", EVIDENCE_ROLES), name="role"),
        UniqueConstraint("assessment_id", "observation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("field_assessment.id"))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"), index=True)
    source: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)


class ExternalIdentity(Base):
    """A company profile on another platform (``identity.external_id``).

    The profile itself, not a claim about whose it is: ``identity_link`` holds
    the claims. ``profile_url`` is the URL it was first seen at.
    """

    __tablename__ = "external_identity"
    __table_args__ = (UniqueConstraint("source_type", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_type: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    profile_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class IdentityDecision(Base):
    """One resolution attempt and why it ended as it did. Append-only.

    About either a named organization (``member_org_id``) or a profile
    (``external_identity_id``); ``organization_id`` is the company it was
    decided to be (or, for 'conflicting' and 'rejected', the one in question),
    NULL when there was no single candidate. ``signals`` and ``contradictions``
    are lists of ``{"signal": ..., "link_id": ..., "page_id": ...}`` naming the
    stored links the decision rests on.
    """

    __tablename__ = "identity_decision"
    __table_args__ = (
        CheckConstraint(_in("outcome", IDENTITY_OUTCOMES), name="outcome"),
        CheckConstraint("num_nonnulls(member_org_id, external_identity_id) = 1", name="subject"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_org_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"), index=True)
    external_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_identity.id"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"))
    outcome: Mapped[str] = mapped_column(Text)
    candidates: Mapped[list[int]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    contradictions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    rationale: Mapped[str] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(Text)
    # A worker or "pipeline" for automatic decisions, the reviewer otherwise.
    actor: Mapped[str] = mapped_column(Text)
    automatic: Mapped[bool] = mapped_column(Boolean)
    crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawl.id"))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("identity_decision.id"))
    created_at: Mapped[datetime] = _created_at()


class IdentityLink(Base):
    """The current answer to "is this the same company?" for one pair.

    Links a named organization or a profile to the company
    (``organization_id``) without touching either row, so a wrong link is undone
    by changing ``status``; ``decision_id`` is the decision that set it. Only
    'match' is followed when reading facts. A named organization has at most
    one link that is not 'rejected'; a profile at most one 'match'.
    """

    __tablename__ = "identity_link"
    __table_args__ = (
        CheckConstraint(_in("status", IDENTITY_LINK_STATUSES), name="status"),
        CheckConstraint("num_nonnulls(member_org_id, external_identity_id) = 1", name="subject"),
        CheckConstraint("member_org_id <> organization_id", name="distinct_orgs"),
        UniqueConstraint("member_org_id", "organization_id"),
        UniqueConstraint("external_identity_id", "organization_id"),
        Index("uq_identity_link_member_open", "member_org_id", unique=True,
              postgresql_where=text("status <> 'rejected'")),
        Index("uq_identity_link_external_match", "external_identity_id", unique=True,
              postgresql_where=text("status = 'match'")),
    )  # fmt: skip

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    member_org_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"))
    external_identity_id: Mapped[int | None] = mapped_column(ForeignKey("external_identity.id"))
    status: Mapped[str] = mapped_column(Text)
    decision_id: Mapped[int] = mapped_column(ForeignKey("identity_decision.id"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


COST_KINDS = ("http", "browser", "search", "storage", "database", "worker")


class CostEvent(Base):
    """Resource usage of a crawl job and what it cost (spec sections 21, 27).

    Rows derived from a site crawl's measurements are keyed per crawl, and
    database time per run of the job (``key``), so recording again after a
    retry or resume updates a row instead of adding one. ``cost_usd`` is
    ``quantity`` at the configured unit price (0 when the price is unknown).
    ``organization_id`` is the job's company: competitor crawls count toward
    the dossier they were run for.
    """

    __tablename__ = "cost_event"
    __table_args__ = (
        CheckConstraint(_in("kind", COST_KINDS), name="kind"),
        CheckConstraint("quantity >= 0 AND cost_usd >= 0", name="non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("crawl_job.id"), index=True)
    crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawl.id"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    # The search provider for search usage.
    provider: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    recorded_at: Mapped[datetime] = _created_at()


# gtm_crawler.market.models.RunStatus, in order.
MARKET_STATUSES = ("queued", "running", "completed", "partial", "failed", "cancelled")
MARKET_ACTIVE_STATUSES = ("queued", "running")


class MarketResearch(Base):
    """One market-research run (``gtm_crawler.market``): request, progress, report.

    Claimed by workers through a lease like ``crawl_job``. ``report`` is the
    validated ``market.models.Report`` as JSON: sources, evidence and every
    finding's evidence ids travel together. ``subject_key`` finds the latest
    report of the same subject when a later run has to fall back on it.
    """

    __tablename__ = "market_research"
    __table_args__ = (CheckConstraint(_in("status", MARKET_STATUSES), name="status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text)
    subject_key: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text, server_default="queued", index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # The latest ``gtm-market --verify`` of the report (the report is never rewritten).
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # The client's Idempotency-Key: a retried create returns this run.
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    error_summary: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# gtm_crawler.market.ads: platforms, collection methods, verification levels.
AD_PLATFORMS = ("meta", "google")
AD_METHODS = ("manual_import",)
AD_VERIFICATIONS = ("operator_transcribed", "ocr_corroborated")


class Ad(Base):
    """One advertisement on one platform, by its platform id (Meta Library ID, Google
    creative id), run by an advertiser profile. Everything that can change is on its
    observations; whose company the advertiser is, is the profile's ``identity_link``."""

    __tablename__ = "ad"
    __table_args__ = (
        CheckConstraint(_in("platform", AD_PLATFORMS), name="platform"),
        CheckConstraint("length(platform_ad_id) > 0", name="platform_ad_id"),
        UniqueConstraint("platform", "platform_ad_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    platform_ad_id: Mapped[str] = mapped_column(Text)
    external_identity_id: Mapped[int] = mapped_column(
        ForeignKey("external_identity.id"), index=True
    )
    created_at: Mapped[datetime] = _created_at()


class AdObservation(Base):
    """One capture of an ad. Append-only (a trigger refuses UPDATE and DELETE): a later
    capture is a new row.

    ``fields`` maps a field name to ``{"state", "verbatim", "normalized"}``
    (``market.ads.FieldValue``); ``observed_at`` is when it was seen, the
    platform's own dates are fields. ``raw_sha256`` is the capture as submitted
    in the raw store, so every quote of it can be re-checked."""

    __tablename__ = "ad_observation"
    __table_args__ = (
        CheckConstraint(_in("method", AD_METHODS), name="method"),
        CheckConstraint(_in("verification", AD_VERIFICATIONS), name="verification"),
        CheckConstraint("method <> 'manual_import' OR length(operator) > 0", name="operator"),
        CheckConstraint(
            "length(raw_sha256) = 64 AND length(content_sha256) = 64", name="digests"
        ),  # fmt: skip
        UniqueConstraint("ad_id", "import_id"),
        Index("ix_ad_observation_ad_id_observed_at", "ad_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("ad.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(Text)
    adapter_version: Mapped[str] = mapped_column(Text)
    import_id: Mapped[str] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text)
    raw_sha256: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(Text)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    verification: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# dont_know and skipped: answers to a question (questions.py) that give no value.
VERIFICATION_STATUSES = ("confirmed", "edited", "rejected", "dont_know", "skipped")


class CompanyVerification(Base):
    """What the user confirmed, corrected or rejected at onboarding (``onboarding.py``).

    User input, never crawler evidence: assessments and the dossier ignore it;
    the onboarding view shows it beside them. Append-only (migration 0024): every
    answer for a company, field and item (``item_key``: the product id for product
    fields, else '') is kept; the latest one is the answer, the rest its history.
    """

    __tablename__ = "company_verification"
    __table_args__ = (
        CheckConstraint(_in("status", VERIFICATION_STATUSES), name="status"),
        Index("ix_company_verification_item", "organization_id", "field", "item_key", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    section: Mapped[str] = mapped_column(Text)
    field: Mapped[str] = mapped_column(Text)
    item_key: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime] = _created_at()


class LlmPageResult(Base):
    """A model's answer, keyed by the hash of the text it was given, the model and
    the prompt, so the same input never needs a second call: a page's type and
    quoted facts (``extract/llm.py``, prompt ``page-llm-*``) or a company profile
    built from a dossier's evidence (``profile.py``, prompt ``profile-*``).
    Interpretation, not evidence: a page fact becomes an observation only when its
    quote is found on the page; a profile value only when it cites evidence."""

    __tablename__ = "llm_page_result"

    text_sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    prompt_version: Mapped[str] = mapped_column(Text, primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
