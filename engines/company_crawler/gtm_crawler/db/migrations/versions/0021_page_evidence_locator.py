"""page evidence locator

Allows evidence.locator_type = 'page': a property of the page itself, located
as "title" (its <title>) or "url" (the address it was served at).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-08 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'xpath', 'json_path', 'meta'"
_NEW = _OLD + ", 'page'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_evidence_locator_type"), "evidence", type_="check")
    op.create_check_constraint(
        op.f("ck_evidence_locator_type"), "evidence", f"locator_type IN ({_NEW})"
    )


def downgrade() -> None:
    # Evidence is append-only, so existing 'page' rows are kept: the old rule is
    # restored NOT VALID, which applies to new rows only.
    op.drop_constraint(op.f("ck_evidence_locator_type"), "evidence", type_="check")
    op.execute(
        "ALTER TABLE evidence ADD CONSTRAINT ck_evidence_locator_type"
        f" CHECK (locator_type IN ({_OLD})) NOT VALID"
    )
