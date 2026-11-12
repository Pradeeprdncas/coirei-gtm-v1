"""llm_page_result: a model's page type and quoted facts per page text (extract/llm.py)

Additive only: no existing table is changed.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-09 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_page_result",
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "text_sha256", "model", "prompt_version", name=op.f("pk_llm_page_result")
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_page_result")
