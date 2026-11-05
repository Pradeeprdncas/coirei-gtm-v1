from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, Computed, DateTime, ForeignKey, Identity, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.core.db import Base


def utcnow():
    return datetime.now(timezone.utc)


def pk():
    return mapped_column(BigInteger, Identity(), primary_key=True)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Company(Base):
    __tablename__ = 'companies'
    __table_args__ = (UniqueConstraint('owner_id', 'domain', name='uq_company_owner_domain'),)
    id: Mapped[int] = pk()
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    name: Mapped[str] = mapped_column(String(255))
    website_url: Mapped[str | None] = mapped_column(String(2048))
    domain: Mapped[str | None] = mapped_column(String(255))
    industry: Mapped[str | None] = mapped_column(String(255))
    size_band: Mapped[str | None] = mapped_column(String(100))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    headquarters: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(100))
    founded_year: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    business_model: Mapped[dict] = mapped_column(JSONB, default=dict)
    market_segment: Mapped[dict] = mapped_column(JSONB, default=dict)
    value_proposition: Mapped[dict] = mapped_column(JSONB, default=dict)
    icp: Mapped[dict] = mapped_column(JSONB, default=dict)
    socials: Mapped[dict] = mapped_column(JSONB, default=dict)
    field_meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(50), default='created')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (UniqueConstraint('company_id', 'name', name='uq_company_product_name'),)
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'))
    name: Mapped[str] = mapped_column(String(255))
    website_url: Mapped[str | None] = mapped_column(String(2048))
    candidate_kind: Mapped[str] = mapped_column(String(40), default='standalone_product')
    category: Mapped[str | None] = mapped_column(String(160))
    product_type: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    key_features: Mapped[list] = mapped_column(JSONB, default=list)
    main_use_case: Mapped[str | None] = mapped_column(Text)
    business_model: Mapped[dict] = mapped_column(JSONB, default=dict)
    pricing_status: Mapped[str] = mapped_column(String(40), default='not_found')
    positioning: Mapped[str | None] = mapped_column(Text)
    value_proposition: Mapped[dict] = mapped_column(JSONB, default=dict)
    target_market: Mapped[dict] = mapped_column(JSONB, default=dict)
    icp: Mapped[dict] = mapped_column(JSONB, default=dict)
    field_meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(40), default='detected')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Source(Base):
    __tablename__ = 'sources'
    __table_args__ = (UniqueConstraint('company_id', 'url', 'content_hash', name='uq_company_source_version'),)
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'))
    kind: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(String(2048))
    page_type: Mapped[str] = mapped_column(String(40), default='other')
    http_status: Mapped[int | None] = mapped_column(Integer)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    content_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default='')
    structured: Mapped[dict] = mapped_column(JSONB, default=dict)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(40), default='ok')
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chunk(Base):
    __tablename__ = 'chunks'
    __table_args__ = (UniqueConstraint('source_id', 'idx', name='uq_source_chunk_index'),
                      Index('ix_chunks_company_product', 'company_id', 'product_id'),
                      Index('ix_chunks_tsv', 'tsv', postgresql_using='gin'),
                      Index('ix_chunks_embedding_hnsw', 'embedding', postgresql_using='hnsw',
                            postgresql_ops={'embedding': 'vector_cosine_ops'}))
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'))
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id'), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_type: Mapped[str] = mapped_column(String(40))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', coalesce(text, ''))", persisted=True))


class PricePlan(Base):
    __tablename__ = 'pricing_items'
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey('sources.id'))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), default='base')
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    unit: Mapped[str | None] = mapped_column(String(50))
    period: Mapped[str | None] = mapped_column(String(50))
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    quote: Mapped[str | None] = mapped_column(Text)


class FieldClaim(Base):
    __tablename__ = 'field_claims'
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'))
    source_id: Mapped[int | None] = mapped_column(ForeignKey('sources.id'))
    field_key: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict] = mapped_column(JSONB)
    quote: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(30), default='website')
    status: Mapped[str] = mapped_column(String(30), default='evidence')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldAudit(Base):
    __tablename__ = 'field_audits'
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    field_key: Mapped[str] = mapped_column(String(100))
    before: Mapped[dict] = mapped_column(JSONB, default=dict)
    after: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = 'conversations'
    __table_args__ = (UniqueConstraint('company_id', 'kind', name='uq_company_conversation_kind'),)
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    kind: Mapped[str] = mapped_column(String(40), default='onboarding')
    title: Mapped[str] = mapped_column(String(255), default='Onboarding')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (UniqueConstraint('conversation_id', 'client_message_id', name='uq_chat_client_message'),)
    id: Mapped[int] = pk()
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), index=True)
    role: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(40), default='text')
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    client_message_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GapQuestion(Base):
    __tablename__ = 'gap_questions'
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    field_key: Mapped[str] = mapped_column(String(120))
    question: Mapped[str] = mapped_column(Text)
    suggestions: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default='open')
    round: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GapAnswer(Base):
    __tablename__ = 'gap_answers'
    id: Mapped[int] = pk()
    question_id: Mapped[int] = mapped_column(ForeignKey('gap_questions.id'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionJob(Base):
    __tablename__ = 'extraction_jobs'
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    status: Mapped[str] = mapped_column(String(30), default='queued')
    stage: Mapped[str] = mapped_column(String(40), default='queued')
    current_url: Mapped[str | None] = mapped_column(String(2048))
    pages_total: Mapped[int] = mapped_column(Integer, default=0)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Phase 2 data model: no campaign API endpoints in this MVP.
class Project(Base):
    __tablename__ = 'projects'
    __table_args__ = (UniqueConstraint('company_id', 'product_id', name='uq_project_product'),)
    id: Mapped[int] = pk()
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default='draft')
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class Campaign(Base):
    __tablename__ = 'campaigns'
    id: Mapped[int] = pk()
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id'), index=True)
    name: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str | None] = mapped_column(String(100))
    objective: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='draft')
    config: Mapped[dict] = mapped_column(JSONB, default=dict)


class CampaignBudget(Base):
    __tablename__ = 'campaign_budgets'
    id: Mapped[int] = pk()
    campaign_id: Mapped[int] = mapped_column(ForeignKey('campaigns.id'), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default='USD')
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allocation: Mapped[dict] = mapped_column(JSONB, default=dict)


class CampaignResult(Base):
    __tablename__ = 'campaign_results'
    id: Mapped[int] = pk()
    campaign_id: Mapped[int] = mapped_column(ForeignKey('campaigns.id'), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    impressions: Mapped[int | None] = mapped_column(BigInteger)
    clicks: Mapped[int | None] = mapped_column(BigInteger)
    leads: Mapped[int | None] = mapped_column(BigInteger)
    conversions: Mapped[int | None] = mapped_column(BigInteger)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
