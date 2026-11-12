"""company_verification: answers to questions may be "I don't know" or skipped

Adds the statuses ``dont_know`` and ``skipped`` (questions.py) to the check.
Downgrade deletes those answers, then restores the three-status check.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-10 20:15:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_company_verification_status"
_OLD = "'confirmed', 'edited', 'rejected'"
_NEW = f"{_OLD}, 'dont_know', 'skipped'"


def upgrade() -> None:
    op.drop_constraint(op.f(_CHECK), "company_verification", type_="check")
    op.create_check_constraint(op.f(_CHECK), "company_verification", f"status IN ({_NEW})")


def downgrade() -> None:
    op.execute("DELETE FROM company_verification WHERE status IN ('dont_know', 'skipped')")
    op.drop_constraint(op.f(_CHECK), "company_verification", type_="check")
    op.create_check_constraint(op.f(_CHECK), "company_verification", f"status IN ({_OLD})")
