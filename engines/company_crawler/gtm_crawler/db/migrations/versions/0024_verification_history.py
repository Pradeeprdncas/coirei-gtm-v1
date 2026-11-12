"""company_verification becomes append-only: every answer is kept, the latest wins

* drops the one-row-per-(company, field, item) unique constraint;
* adds ``actor`` (who answered, when the caller says) and an index to read the
  latest answer per item.

Downgrade keeps only each item's latest answer, then restores the constraint.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-10 19:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE = "uq_company_verification_organization_id_field_item_key"
_INDEX = "ix_company_verification_item"


def upgrade() -> None:
    op.drop_constraint(_UNIQUE, "company_verification", type_="unique")
    op.add_column("company_verification", sa.Column("actor", sa.Text(), nullable=True))
    op.create_index(_INDEX, "company_verification", ["organization_id", "field", "item_key", "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, "company_verification")
    op.execute(
        "DELETE FROM company_verification v USING company_verification newer "
        "WHERE newer.organization_id = v.organization_id AND newer.field = v.field "
        "AND newer.item_key = v.item_key AND newer.id > v.id"
    )
    op.drop_column("company_verification", "actor")
    op.create_unique_constraint(
        _UNIQUE, "company_verification", ["organization_id", "field", "item_key"]
    )
