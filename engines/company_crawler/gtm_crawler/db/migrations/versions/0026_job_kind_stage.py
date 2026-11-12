"""crawl_job: a ``kind`` (a crawl, or a refresh that only re-reads stored evidence)
and the ``stage`` it is at, for progress

Additive: existing jobs are crawls with no stage recorded.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-10 20:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crawl_job", sa.Column("kind", sa.Text(), server_default="crawl", nullable=False)
    )
    op.add_column("crawl_job", sa.Column("stage", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_crawl_job_kind"), "crawl_job", "kind IN ('crawl', 'refresh')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_crawl_job_kind"), "crawl_job", type_="check")
    op.drop_column("crawl_job", "stage")
    op.drop_column("crawl_job", "kind")
