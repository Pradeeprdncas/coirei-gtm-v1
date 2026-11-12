"""sitemap fetches (production spec Phase 4)

* sitemap_fetch: every sitemap request of a crawl and what it yielded.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29 11:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sitemap_fetch",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("url_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_url_id", sa.BigInteger(), nullable=True),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("content_length", sa.BigInteger(), nullable=True),
        sa.Column("raw_sha256", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("url_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("child_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dropped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.CheckConstraint(
            "raw_sha256 IS NULL OR length(raw_sha256) = 64",
            name=op.f("ck_sitemap_fetch_raw_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_sitemap_fetch_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(["url_id"], ["url.id"], name=op.f("fk_sitemap_fetch_url_id_url")),
        sa.ForeignKeyConstraint(
            ["parent_url_id"], ["url.id"], name=op.f("fk_sitemap_fetch_parent_url_id_url")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sitemap_fetch")),
    )
    op.create_index(op.f("ix_sitemap_fetch_crawl_id"), "sitemap_fetch", ["crawl_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sitemap_fetch_crawl_id"), table_name="sitemap_fetch")
    op.drop_table("sitemap_fetch")
