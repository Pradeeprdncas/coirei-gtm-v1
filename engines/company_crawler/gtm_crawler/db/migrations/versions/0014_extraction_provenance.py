"""extraction run provenance (production spec Phase 15, sections 24 and 29)

* extraction.versions: ``versions.current_versions`` when the run was made
  (parser, extractors, assessment, ...). NULL for runs made before 0014:
  their versions are unknown.
* extraction.trigger: what made the run, 'crawl' (the pipeline) or 'replay'
  (``replay.py`` over stored artifacts). Existing rows become 'crawl'.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction", sa.Column("versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "extraction", sa.Column("trigger", sa.Text(), server_default="crawl", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_extraction_trigger"), "extraction", "trigger IN ('crawl', 'replay')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_extraction_trigger"), "extraction", type_="check")
    op.drop_column("extraction", "trigger")
    op.drop_column("extraction", "versions")
