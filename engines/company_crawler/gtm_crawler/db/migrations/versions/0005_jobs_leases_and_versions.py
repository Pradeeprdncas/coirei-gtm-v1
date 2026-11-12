"""crawl jobs, URL leases and processing versions (production LLD)

* ``crawl_job``: one pipeline run per request, leased by workers.
* ``crawl.job_id`` and ``crawl.checkpoint`` (resumable in-memory state);
  crawl status 'paused'.
* ``crawl_url``: the LLD URL states, leases, attempts and retry time, and an
  ``is_external`` flag. Old states map as fetched -> complete, failed ->
  failed_permanent (blocked when the reason was a security rejection).
* ``robots_snapshot``: one row per load (a resumed crawl loads again).
* ``page.parser_version``, ``fetch.duration_ms``, ``fetch.trigger``.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_OLD_URL_STATES = "'queued', 'fetched', 'skipped', 'failed'"
_NEW_URL_STATES = (
    "'queued', 'in_progress', 'retry_wait', 'blocked', 'failed_permanent', 'skipped',"
    " 'duplicate', 'complete'"
)
_OLD_CRAWL = "'running', 'completed', 'partial', 'failed'"
_NEW_CRAWL = "'running', 'paused', 'completed', 'partial', 'failed'"
_JOB = "'created', 'running', 'paused', 'completed', 'completed_budget', 'failed'"


def _check(name: str, table: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.create_table(
        "crawl_job",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("root_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="created", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", _TZ, nullable=True),
        sa.Column("pause_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", _TZ, nullable=True),
        sa.Column("finished_at", _TZ, nullable=True),
        sa.CheckConstraint(f"status IN ({_JOB})", name=op.f("ck_crawl_job_status")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_crawl_job_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_job")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_crawl_job_idempotency_key")),
    )
    op.create_index(
        op.f("ix_crawl_job_organization_id"), "crawl_job", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_crawl_job_status"), "crawl_job", ["status"], unique=False)
    op.create_index(
        "uq_crawl_job_active_organization",
        "crawl_job",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('created', 'running', 'paused')"),
    )

    op.add_column("crawl", sa.Column("job_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "crawl", sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_crawl_job_id_crawl_job"), "crawl", "crawl_job", ["job_id"], ["id"]
    )
    op.create_index(op.f("ix_crawl_job_id"), "crawl", ["job_id"], unique=False)
    _check("ck_crawl_status", "crawl", f"status IN ({_NEW_CRAWL})")

    for column in (
        sa.Column("is_external", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", _TZ, nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", _TZ, nullable=True),
    ):
        op.add_column("crawl_url", column)
    op.drop_constraint(op.f("ck_crawl_url_state"), "crawl_url", type_="check")
    op.execute("UPDATE crawl_url SET state = 'complete' WHERE state = 'fetched'")
    op.execute(
        "UPDATE crawl_url SET state = CASE WHEN skip_reason LIKE 'blocked%'"
        " THEN 'blocked' ELSE 'failed_permanent' END WHERE state = 'failed'"
    )
    # Pages fetched outside the crawled site's domain were external-phase pages.
    op.execute("""
        UPDATE crawl_url cu SET is_external = true
        FROM url u, crawl c, organization o
        WHERE u.id = cu.url_id AND c.id = cu.crawl_id AND o.id = c.organization_id
          AND u.registrable_domain <> o.registrable_domain
    """)
    op.create_check_constraint(
        op.f("ck_crawl_url_state"), "crawl_url", f"state IN ({_NEW_URL_STATES})"
    )
    op.create_index("ix_crawl_url_claim", "crawl_url", ["crawl_id", "state", "score"], unique=False)

    op.drop_constraint(
        op.f("uq_robots_snapshot_crawl_id_origin"), "robots_snapshot", type_="unique"
    )
    op.create_index(
        op.f("ix_robots_snapshot_crawl_id"), "robots_snapshot", ["crawl_id"], unique=False
    )

    op.add_column("page", sa.Column("parser_version", sa.Text(), nullable=True))
    op.add_column("fetch", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("fetch", sa.Column("trigger", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("fetch", "trigger")
    op.drop_column("fetch", "duration_ms")
    op.drop_column("page", "parser_version")

    op.drop_index(op.f("ix_robots_snapshot_crawl_id"), table_name="robots_snapshot")
    op.execute("""
        DELETE FROM robots_snapshot a USING robots_snapshot b
        WHERE a.crawl_id = b.crawl_id AND a.origin = b.origin AND a.id > b.id
    """)
    op.create_unique_constraint(
        op.f("uq_robots_snapshot_crawl_id_origin"), "robots_snapshot", ["crawl_id", "origin"]
    )

    op.drop_index("ix_crawl_url_claim", table_name="crawl_url")
    op.drop_constraint(op.f("ck_crawl_url_state"), "crawl_url", type_="check")
    op.execute("""
        UPDATE crawl_url SET state = CASE
            WHEN state IN ('complete', 'duplicate') THEN 'fetched'
            WHEN state IN ('blocked', 'failed_permanent') THEN 'failed'
            WHEN state IN ('in_progress', 'retry_wait') THEN 'queued'
            ELSE state END
    """)
    op.create_check_constraint(
        op.f("ck_crawl_url_state"), "crawl_url", f"state IN ({_OLD_URL_STATES})"
    )
    for name in ("next_attempt_at", "attempts", "lease_expires_at", "lease_owner", "is_external"):
        op.drop_column("crawl_url", name)

    op.execute("UPDATE crawl SET status = 'partial' WHERE status = 'paused'")
    _check("ck_crawl_status", "crawl", f"status IN ({_OLD_CRAWL})")
    op.drop_index(op.f("ix_crawl_job_id"), table_name="crawl")
    op.drop_constraint(op.f("fk_crawl_job_id_crawl_job"), "crawl", type_="foreignkey")
    op.drop_column("crawl", "checkpoint")
    op.drop_column("crawl", "job_id")

    op.drop_index("uq_crawl_job_active_organization", table_name="crawl_job")
    op.drop_index(op.f("ix_crawl_job_status"), table_name="crawl_job")
    op.drop_index(op.f("ix_crawl_job_organization_id"), table_name="crawl_job")
    op.drop_table("crawl_job")
