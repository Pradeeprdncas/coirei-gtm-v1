"""meta evidence locator

Allows evidence.locator_type = 'meta': a <meta> tag's content, located as
"<key>[<index>]" (e.g. "og:site_name[0]").

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29 03:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'xpath', 'json_path'"
_NEW = _OLD + ", 'meta'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_evidence_locator_type"), "evidence", type_="check")
    op.create_check_constraint(
        op.f("ck_evidence_locator_type"), "evidence", f"locator_type IN ({_NEW})"
    )


def downgrade() -> None:
    # Evidence is append-only, so existing 'meta' rows are kept: the old rule is
    # restored NOT VALID, which applies to new rows only.
    op.drop_constraint(op.f("ck_evidence_locator_type"), "evidence", type_="check")
    op.execute(
        "ALTER TABLE evidence ADD CONSTRAINT ck_evidence_locator_type"
        f" CHECK (locator_type IN ({_OLD})) NOT VALID"
    )
