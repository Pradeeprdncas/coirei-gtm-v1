"""page duplicates and canonical discovery

Adds page.duplicate_of_page_id and allows crawl_url.discovered_via = 'canonical'.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28 18:57:34.794871
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_METHODS = "'seed', 'link', 'sitemap', 'robots', 'redirect', 'search'"
_NEW_METHODS = _OLD_METHODS + ", 'canonical'"


def upgrade() -> None:
    op.add_column("page", sa.Column("duplicate_of_page_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_page_duplicate_of_page_id_page"), "page", "page", ["duplicate_of_page_id"], ["id"]
    )
    op.drop_constraint(op.f("ck_crawl_url_discovered_via"), "crawl_url", type_="check")
    op.create_check_constraint(
        op.f("ck_crawl_url_discovered_via"), "crawl_url", f"discovered_via IN ({_NEW_METHODS})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM crawl_url WHERE discovered_via = 'canonical'")
    op.drop_constraint(op.f("ck_crawl_url_discovered_via"), "crawl_url", type_="check")
    op.create_check_constraint(
        op.f("ck_crawl_url_discovered_via"), "crawl_url", f"discovered_via IN ({_OLD_METHODS})"
    )
    op.drop_constraint(op.f("fk_page_duplicate_of_page_id_page"), "page", type_="foreignkey")
    op.drop_column("page", "duplicate_of_page_id")
