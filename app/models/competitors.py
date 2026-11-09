from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Computed, DateTime, Float,
    ForeignKey, Identity, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from app.core.db import Base


def now():
    return datetime.now(timezone.utc)


def pk():
    return mapped_column(
        BigInteger, Identity(), primary_key=True
    )


class CompetitorJob(Base):
    __tablename__ = "competitor_jobs"

    id: Mapped[int] = pk()
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True
    )

    status: Mapped[str] = mapped_column(
        String(30), default="queued"
    )
    stage: Mapped[str] = mapped_column(
        String(40), default="queued"
    )

    queries: Mapped[list] = mapped_column(
        JSONB, default=list
    )
    search_hits: Mapped[int] = mapped_column(
        Integer, default=0
    )
    candidates_total: Mapped[int] = mapped_column(
        Integer, default=0
    )
    candidates_done: Mapped[int] = mapped_column(
        Integer, default=0
    )
    confirmed_count: Mapped[int] = mapped_column(
        Integer, default=0
    )

    warnings: Mapped[list] = mapped_column(
        JSONB, default=list
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer, default=0
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now,
        onupdate=now,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    finished_at: Mapped[datetime | None] = (
        mapped_column(DateTime(timezone=True))
    )


class CompetitorCandidate(Base):
    __tablename__ = "competitor_candidates"

    __table_args__ = (
        UniqueConstraint(
            "project_id", "domain",
            name="uq_project_competitor_domain",
        ),
    )

    id: Mapped[int] = pk()

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True
    )

    name: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str] = mapped_column(String(255))
    website_url: Mapped[str] = mapped_column(
        String(2048)
    )

    status: Mapped[str] = mapped_column(
        String(30), default="pending"
    )

    discovered_via: Mapped[list] = mapped_column(
        JSONB, default=list
    )

    # Includes description, features, ICP, pricing,
    # geography, business model, evidence references.
    profile: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )

    match_percent: Mapped[int | None] = (
        mapped_column(Integer)
    )

    evidence_coverage: Mapped[float] = mapped_column(
        Float, default=0.0
    )

    score_details: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )

    explanation: Mapped[str | None] = (
        mapped_column(Text)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now,
        onupdate=now,
    )


class CompetitorSource(Base):
    __tablename__ = "competitor_sources"

    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "url", "content_hash",
            name="uq_competitor_source_version",
        ),
    )

    id: Mapped[int] = pk()

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_candidates.id"),
        index=True,
    )

    url: Mapped[str] = mapped_column(String(2048))
    page_type: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(
        String(64)
    )
    text: Mapped[str] = mapped_column(Text)

    structured: Mapped[dict] = mapped_column(
        JSONB, default=dict
    )

    status: Mapped[str] = mapped_column(
        String(30), default="ok"
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class CompetitorChunk(Base):
    __tablename__ = "competitor_chunks"

    __table_args__ = (
        UniqueConstraint(
            "source_id", "idx",
            name="uq_competitor_chunk_index",
        ),
        Index(
            "ix_competitor_chunks_tsv",
            "tsv",
            postgresql_using="gin",
        ),
        Index(
            "ix_competitor_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={
                "embedding": "vector_cosine_ops"
            },
        ),
    )

    id: Mapped[int] = pk()

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_candidates.id"),
        index=True,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_sources.id"),
        index=True,
    )

    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_type: Mapped[str] = mapped_column(String(40))

    embedding: Mapped[list[float] | None] = (
        mapped_column(Vector(384))
    )

    tsv = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(text, ''))",
            persisted=True,
        ),
    )