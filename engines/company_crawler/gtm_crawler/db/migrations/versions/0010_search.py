"""search queries and results (production spec Phase 9)

* search_query: every query a crawl sent (or could not send), its template,
  the provider that answered, outcome and duration. Never the API key.
* search_result: each normalized result of a query, by position. Results are
  discovery only; a result becomes evidence only through a page we fetched.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_query",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "include_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.CheckConstraint(
            "status IN ('ok', 'failed', 'over_budget')", name=op.f("ck_search_query_status")
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_search_query_crawl_id_crawl")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_query")),
    )
    op.create_index(op.f("ix_search_query_crawl_id"), "search_query", ["crawl_id"], unique=False)
    op.create_table(
        "search_result",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("url_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("published", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["query_id"], ["search_query.id"], name=op.f("fk_search_result_query_id_search_query")
        ),
        sa.ForeignKeyConstraint(["url_id"], ["url.id"], name=op.f("fk_search_result_url_id_url")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_result")),
        sa.UniqueConstraint(
            "query_id", "position", name=op.f("uq_search_result_query_id_position")
        ),
    )
    op.create_index(op.f("ix_search_result_url_id"), "search_result", ["url_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_search_result_url_id"), table_name="search_result")
    op.drop_table("search_result")
    op.drop_index(op.f("ix_search_query_crawl_id"), table_name="search_query")
    op.drop_table("search_query")
