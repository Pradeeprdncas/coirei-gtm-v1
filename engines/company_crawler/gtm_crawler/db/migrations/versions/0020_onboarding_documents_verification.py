"""onboarding: company documents and the user's verification

* fetch.method may be 'upload': a document the company gave (documents.py).
* extraction.trigger may be 'document': the re-extraction after an upload.
* company_verification: what the user confirmed, corrected or rejected at
  onboarding, one row per company, field and item.

Downgrade restores the old checks as NOT VALID (fetch and extraction are
append-only, so rows already written stay) and drops company_verification.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-07 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    ("fetch", "ck_fetch_method", "method", "'http', 'browser'", "'upload'"),
    ("extraction", "ck_extraction_trigger", "trigger", "'crawl', 'replay'", "'document'"),
)


def upgrade() -> None:
    for table, name, column, old, added in _CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
        # NOT VALID: after a downgrade, rows a later revision allowed (e.g.
        # fetch.method 'search_contents', 0022) stay; new rows are checked.
        op.execute(
            f'ALTER TABLE "{table}" ADD CONSTRAINT {name} CHECK ({column} IN ({old}, {added})) '
            "NOT VALID"
        )
    op.create_table(
        "company_verification",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "organization.id", name=op.f("fk_company_verification_organization_id_organization")
            ),
            nullable=False,
        ),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("item_key", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'edited', 'rejected')",
            name=op.f("ck_company_verification_status"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "field",
            "item_key",
            name=op.f("uq_company_verification_organization_id_field_item_key"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_verification")),
    )
    op.create_index(
        op.f("ix_company_verification_organization_id"), "company_verification", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_table("company_verification")
    for table, name, column, old, _ in _CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
        op.execute(
            f'ALTER TABLE "{table}" ADD CONSTRAINT {name} CHECK ({column} IN ({old})) NOT VALID'
        )
