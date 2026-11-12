"""priority breakdown and structured-data discovery (production spec Phase 2)

* crawl_url.priority_signals: the score's breakdown by signal.
* crawl_url.discovered_via may be 'structured_data' (JSON-LD URLs).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'seed', 'link', 'sitemap', 'robots', 'redirect', 'search', 'canonical'"
_NEW = _OLD + ", 'structured_data'"


def _methods(values: str) -> None:
    op.drop_constraint(op.f("ck_crawl_url_discovered_via"), "crawl_url", type_="check")
    op.create_check_constraint(
        op.f("ck_crawl_url_discovered_via"), "crawl_url", f"discovered_via IN ({values})"
    )


def upgrade() -> None:
    op.add_column(
        "crawl_url",
        sa.Column("priority_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _methods(_NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE crawl_url SET discovered_via = 'link' WHERE discovered_via = 'structured_data'"
    )
    _methods(_OLD)
    op.drop_column("crawl_url", "priority_signals")
