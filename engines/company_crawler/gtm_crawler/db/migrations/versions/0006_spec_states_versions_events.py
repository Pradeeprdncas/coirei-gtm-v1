"""spec job and URL states, versions, crawl events (production spec Phase 1)

* Job states become the spec's (sections 5, 7): created, queued, running,
  finalizing, completed, paused, failed, cancelled. completed_budget ->
  completed (the budget stop reason stays in the job result); a running job
  without a lease holder -> queued. Adds cancel_requested and versions.
* URL states become the spec's: queued -> eligible, in_progress -> leased,
  retry_wait -> retry, failed_permanent -> failed, skipped -> blocked (its
  reason kept), complete and duplicate -> processed (duplicates keep the
  reason "duplicate").
* crawl.versions, and crawl_event: an append-only log of state changes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_JOB = "'created', 'running', 'paused', 'completed', 'completed_budget', 'failed'"
_NEW_JOB = (
    "'created', 'queued', 'running', 'finalizing', 'completed', 'paused', 'failed', 'cancelled'"
)
_OLD_ACTIVE = "status IN ('created', 'running', 'paused')"
_NEW_ACTIVE = "status IN ('created', 'queued', 'running', 'finalizing', 'paused')"
_OLD_URL = (
    "'queued', 'in_progress', 'retry_wait', 'blocked', 'failed_permanent', 'skipped',"
    " 'duplicate', 'complete'"
)
_NEW_URL = (
    "'discovered', 'eligible', 'leased', 'fetching', 'fetched', 'parsed', 'processed',"
    " 'deferred', 'blocked', 'trap', 'failed', 'retry', 'dlq'"
)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def _set_check(name: str, table: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _active_index(condition: str) -> None:
    op.drop_index("uq_crawl_job_active_organization", table_name="crawl_job")
    op.create_index(
        "uq_crawl_job_active_organization", "crawl_job", ["organization_id"], unique=True,
        postgresql_where=sa.text(condition),
    )  # fmt: skip


def upgrade() -> None:
    op.drop_constraint(op.f("ck_crawl_job_status"), "crawl_job", type_="check")
    op.execute("UPDATE crawl_job SET status = 'completed' WHERE status = 'completed_budget'")
    op.execute(
        "UPDATE crawl_job SET status = 'queued' WHERE status = 'running' AND lease_owner IS NULL"
    )
    op.create_check_constraint(op.f("ck_crawl_job_status"), "crawl_job", f"status IN ({_NEW_JOB})")
    _active_index(_NEW_ACTIVE)
    op.add_column(
        "crawl_job",
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
    )  # fmt: skip
    op.add_column("crawl_job", sa.Column("versions", _JSONB, nullable=True))
    op.add_column("crawl", sa.Column("versions", _JSONB, nullable=True))

    op.drop_constraint(op.f("ck_crawl_url_state"), "crawl_url", type_="check")
    op.execute("""
        UPDATE crawl_url SET
            skip_reason = CASE WHEN state = 'duplicate' THEN 'duplicate' ELSE skip_reason END,
            state = CASE state
                WHEN 'queued' THEN 'eligible'
                WHEN 'in_progress' THEN 'leased'
                WHEN 'retry_wait' THEN 'retry'
                WHEN 'failed_permanent' THEN 'failed'
                WHEN 'skipped' THEN 'blocked'
                WHEN 'complete' THEN 'processed'
                WHEN 'duplicate' THEN 'processed'
                ELSE state END
    """)
    op.create_check_constraint(op.f("ck_crawl_url_state"), "crawl_url", f"state IN ({_NEW_URL})")
    op.alter_column("crawl_url", "state", server_default="eligible")

    op.create_table(
        "crawl_event",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("crawl_id", sa.BigInteger(), nullable=True),
        sa.Column("url_id", sa.BigInteger(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail", _JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),  # fmt: skip
        sa.CheckConstraint("entity IN ('job', 'crawl', 'url')", name=op.f("ck_crawl_event_entity")),
        sa.ForeignKeyConstraint(
            ["job_id"], ["crawl_job.id"], name=op.f("fk_crawl_event_job_id_crawl_job")
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_crawl_event_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(["url_id"], ["url.id"], name=op.f("fk_crawl_event_url_id_url")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_event")),
    )
    op.create_index("ix_crawl_event_job_id_id", "crawl_event", ["job_id", "id"], unique=False)
    op.create_index(op.f("ix_crawl_event_crawl_id"), "crawl_event", ["crawl_id"], unique=False)
    # History, like evidence: never rewritten (reject_modification() is from 0001).
    op.execute(
        "CREATE TRIGGER crawl_event_append_only BEFORE UPDATE OR DELETE ON crawl_event "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
    )


def downgrade() -> None:
    op.drop_table("crawl_event")

    op.alter_column("crawl_url", "state", server_default="queued")
    op.drop_constraint(op.f("ck_crawl_url_state"), "crawl_url", type_="check")
    op.execute("""
        UPDATE crawl_url SET state = CASE
            WHEN state = 'processed' AND skip_reason = 'duplicate' THEN 'duplicate'
            WHEN state = 'processed' THEN 'complete'
            WHEN state IN ('eligible', 'discovered', 'deferred') THEN 'queued'
            WHEN state IN ('leased', 'fetching', 'fetched', 'parsed') THEN 'in_progress'
            WHEN state = 'retry' THEN 'retry_wait'
            WHEN state IN ('failed', 'dlq') THEN 'failed_permanent'
            WHEN state = 'trap' THEN 'skipped'
            ELSE state END
    """)
    op.create_check_constraint(op.f("ck_crawl_url_state"), "crawl_url", f"state IN ({_OLD_URL})")

    op.drop_column("crawl", "versions")
    op.drop_column("crawl_job", "versions")
    op.drop_column("crawl_job", "cancel_requested")
    op.drop_constraint(op.f("ck_crawl_job_status"), "crawl_job", type_="check")
    op.execute("""
        UPDATE crawl_job SET status = CASE
            WHEN status IN ('queued', 'finalizing') THEN 'running'
            WHEN status = 'cancelled' THEN 'failed'
            ELSE status END
    """)
    _active_index(_OLD_ACTIVE)
    op.create_check_constraint(op.f("ck_crawl_job_status"), "crawl_job", f"status IN ({_OLD_JOB})")
