"""extraction runs and robots.txt snapshots

* ``extraction``: one row per extraction run over a stored crawl. Every
  observation belongs to one run; the latest run of a crawl supersedes earlier
  ones, so extraction can be re-run without re-crawling and without counting
  the same statement twice. Existing observations get one run per crawl.
* ``robots_snapshot``: the robots.txt the crawl obeyed, per origin (the body
  itself lives in the raw store).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 05:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_extraction_crawl_id_crawl")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction")),
    )
    op.create_index(op.f("ix_extraction_crawl_id"), "extraction", ["crawl_id"], unique=False)

    op.add_column("observation", sa.Column("extraction_id", sa.BigInteger(), nullable=True))
    op.execute(
        "INSERT INTO extraction (crawl_id) SELECT DISTINCT crawl_id FROM observation ORDER BY 1"
    )
    # Observations are append-only; attaching existing rows to their run is
    # the one sanctioned rewrite, done with the guard briefly lifted.
    op.execute("ALTER TABLE observation DISABLE TRIGGER observation_append_only")
    op.execute(
        "UPDATE observation o SET extraction_id = e.id FROM extraction e"
        " WHERE e.crawl_id = o.crawl_id"
    )
    op.execute("ALTER TABLE observation ENABLE TRIGGER observation_append_only")
    op.alter_column("observation", "extraction_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_observation_extraction_id_extraction"),
        "observation", "extraction", ["extraction_id"], ["id"],
    )  # fmt: skip
    op.create_index(
        op.f("ix_observation_extraction_id"), "observation", ["extraction_id"], unique=False
    )

    op.create_table(
        "robots_snapshot",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("raw_sha256", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.CheckConstraint(
            "status IN ('parsed', 'unavailable', 'unreachable')",
            name=op.f("ck_robots_snapshot_status"),
        ),
        sa.CheckConstraint(
            "raw_sha256 IS NULL OR length(raw_sha256) = 64",
            name=op.f("ck_robots_snapshot_raw_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_robots_snapshot_crawl_id_crawl")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_robots_snapshot")),
        sa.UniqueConstraint("crawl_id", "origin", name=op.f("uq_robots_snapshot_crawl_id_origin")),
    )


def downgrade() -> None:
    op.drop_table("robots_snapshot")
    op.drop_index(op.f("ix_observation_extraction_id"), table_name="observation")
    op.drop_constraint(
        op.f("fk_observation_extraction_id_extraction"), "observation", type_="foreignkey"
    )
    op.drop_column("observation", "extraction_id")
    op.drop_index(op.f("ix_extraction_crawl_id"), table_name="extraction")
    op.drop_table("extraction")
