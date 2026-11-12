"""dead-letter queue (production spec Phase 13)

* dead_letter_item: work that failed repeatedly or crashed its worker (a URL
  of a crawl, or a job), with the error class, attempts and a payload for
  inspection. Resolved items keep their row (requeued or discarded).
* crawl_url.lease_expiries: how often a URL's lease ran out (its worker
  died while holding it); past crawl.max_url_lease_expiries it is dead-lettered.

At most one unresolved item per URL of a crawl and per job (partial unique
indexes), so writing one is idempotent under at-least-once delivery.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crawl_url",
        sa.Column("lease_expiries", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.create_table(
        "dead_letter_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("crawl_id", sa.BigInteger(), nullable=True),
        sa.Column("url_id", sa.BigInteger(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),  # fmt: skip
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.CheckConstraint("entity IN ('url', 'job')", name=op.f("ck_dead_letter_item_entity")),
        sa.CheckConstraint(
            "(entity = 'url' AND crawl_id IS NOT NULL AND url_id IS NOT NULL)"
            " OR (entity = 'job' AND job_id IS NOT NULL)",
            name=op.f("ck_dead_letter_item_refs"),
        ),
        sa.CheckConstraint(
            "(resolved_at IS NULL AND resolution IS NULL)"
            " OR (resolved_at IS NOT NULL AND resolution IN ('requeued', 'discarded'))",
            name=op.f("ck_dead_letter_item_resolution"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["crawl_job.id"], name=op.f("fk_dead_letter_item_job_id_crawl_job")
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_dead_letter_item_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(
            ["url_id"], ["url.id"], name=op.f("fk_dead_letter_item_url_id_url")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dead_letter_item")),
    )
    op.create_index(
        "uq_dead_letter_item_open_url", "dead_letter_item", ["crawl_id", "url_id"],
        unique=True, postgresql_where=sa.text("entity = 'url' AND resolved_at IS NULL"),
    )  # fmt: skip
    op.create_index(
        "uq_dead_letter_item_open_job", "dead_letter_item", ["job_id"],
        unique=True, postgresql_where=sa.text("entity = 'job' AND resolved_at IS NULL"),
    )  # fmt: skip


def downgrade() -> None:
    op.drop_index("uq_dead_letter_item_open_job", table_name="dead_letter_item")
    op.drop_index("uq_dead_letter_item_open_url", table_name="dead_letter_item")
    op.drop_table("dead_letter_item")
    op.drop_column("crawl_url", "lease_expiries")
