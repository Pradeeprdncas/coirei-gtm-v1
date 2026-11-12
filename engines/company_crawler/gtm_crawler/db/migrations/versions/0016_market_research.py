"""market research runs (gtm_crawler.market)

* market_research: one industry or product research run: its request,
  progress, lease, and the finished report (sources, evidence, findings).

Additive only: no existing table is changed.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_research",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "progress", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "report", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial', 'failed', 'cancelled')",
            name=op.f("ck_market_research_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_research")),
    )
    op.create_index(op.f("ix_market_research_subject_key"), "market_research", ["subject_key"])
    op.create_index(op.f("ix_market_research_status"), "market_research", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_market_research_status"), table_name="market_research")
    op.drop_index(op.f("ix_market_research_subject_key"), table_name="market_research")
    op.drop_table("market_research")
