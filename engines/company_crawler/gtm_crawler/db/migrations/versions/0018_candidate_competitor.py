"""relationship type candidate_competitor

* relationship.type may be 'candidate_competitor': a company co-listed with
  the subject on another site ("Best CRM software"). Never a competitor.

Downgrade restores the old check as NOT VALID: relationship is append-only,
so candidate rows already written stay; new ones are refused.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-06 19:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'investor', 'tech_partner', 'strategic_partner', 'competitor'"
_NEW = f"{_OLD}, 'candidate_competitor'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_relationship_type"), "relationship", type_="check")
    op.create_check_constraint(op.f("ck_relationship_type"), "relationship", f"type IN ({_NEW})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_relationship_type"), "relationship", type_="check")
    op.execute(
        f"ALTER TABLE relationship ADD CONSTRAINT ck_relationship_type CHECK (type IN ({_OLD}))"
        " NOT VALID"
    )
