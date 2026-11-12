"""cost accounting (production spec Phase 14)

* cost_event: resource usage of a crawl job (HTTP, browser, search, storage,
  database, worker) and its cost at the configured unit prices. Unique
  ``key`` per crawl measurement or per run, so recording is idempotent.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_event",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 6), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.CheckConstraint(
            "kind IN ('http', 'browser', 'search', 'storage', 'database', 'worker')",
            name=op.f("ck_cost_event_kind"),
        ),
        sa.CheckConstraint(
            "quantity >= 0 AND cost_usd >= 0", name=op.f("ck_cost_event_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_cost_event_organization_id_organization"),
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["job_id"], ["crawl_job.id"], name=op.f("fk_cost_event_job_id_crawl_job")
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_cost_event_crawl_id_crawl")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_event")),
        sa.UniqueConstraint("key", name=op.f("uq_cost_event_key")),
    )
    for column in ("organization_id", "job_id", "crawl_id"):
        op.create_index(op.f(f"ix_cost_event_{column}"), "cost_event", [column], unique=False)


def downgrade() -> None:
    for column in ("organization_id", "job_id", "crawl_id"):
        op.drop_index(op.f(f"ix_cost_event_{column}"), table_name="cost_event")
    op.drop_table("cost_event")
