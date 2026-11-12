"""market research: idempotent creation, verification record

* market_research.idempotency_key: the client's ``Idempotency-Key``; unique,
  so a retried or concurrent create returns the run already queued.
* market_research.verification: the latest ``gtm-market --verify`` result
  (the report itself is never rewritten).

Additive only: no existing column is changed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-23 23:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("market_research", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_market_research_idempotency_key"), "market_research", ["idempotency_key"]
    )
    op.add_column(
        "market_research",
        sa.Column(
            "verification",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("market_research", "verification")
    op.drop_constraint(
        op.f("uq_market_research_idempotency_key"), "market_research", type_="unique"
    )
    op.drop_column("market_research", "idempotency_key")
