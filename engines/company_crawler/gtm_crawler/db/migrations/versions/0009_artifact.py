"""raw artifacts (production spec Phase 7)

* artifact: one row per stored raw document (content-addressed by SHA-256):
  size, content type, artifact format version, when it was first stored and,
  once retention deleted the body, when.
* fetch / robots_snapshot / sitemap_fetch.raw_sha256 reference it, so the
  database cannot cite a document it has no record of.

Existing references are backfilled; their size comes from the fetch record
(robots.txt snapshots never recorded one, so theirs is NULL).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCES = ("fetch", "robots_snapshot", "sitemap_fetch")


def upgrade() -> None:
    op.create_table(
        "artifact",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(sha256) = 64", name=op.f("ck_artifact_sha256")),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name=op.f("ck_artifact_size_bytes")
        ),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_artifact")),
    )
    op.execute(
        """
        INSERT INTO artifact (sha256, size_bytes, content_type, artifact_version, created_at)
        SELECT DISTINCT ON (sha256) sha256, size_bytes, content_type, '1', fetched_at
        FROM (
            SELECT raw_sha256 AS sha256, content_length AS size_bytes, content_type, fetched_at
            FROM "fetch" WHERE raw_sha256 IS NOT NULL
            UNION ALL
            SELECT raw_sha256, content_length, content_type, fetched_at
            FROM sitemap_fetch WHERE raw_sha256 IS NOT NULL
            UNION ALL
            SELECT raw_sha256, NULL, 'text/plain', fetched_at
            FROM robots_snapshot WHERE raw_sha256 IS NOT NULL
        ) refs
        ORDER BY sha256, fetched_at
        """
    )
    for table in _REFERENCES:
        op.create_foreign_key(
            op.f(f"fk_{table}_raw_sha256_artifact"), table, "artifact", ["raw_sha256"], ["sha256"]
        )
        op.create_index(op.f(f"ix_{table}_raw_sha256"), table, ["raw_sha256"], unique=False)


def downgrade() -> None:
    for table in _REFERENCES:
        op.drop_index(op.f(f"ix_{table}_raw_sha256"), table_name=table)
        op.drop_constraint(op.f(f"fk_{table}_raw_sha256_artifact"), table, type_="foreignkey")
    op.drop_table("artifact")
