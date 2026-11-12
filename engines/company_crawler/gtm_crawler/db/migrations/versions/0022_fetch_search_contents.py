"""fetch.method may be 'search_contents': the search provider's copy of a page
we do not fetch ourselves (a LinkedIn company page; crawl._contents).

Downgrade restores the old check as NOT VALID (fetch is append-only, so rows
already written stay).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-09 16:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'http', 'browser', 'upload'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_fetch_method"), "fetch", type_="check")
    op.create_check_constraint(
        op.f("ck_fetch_method"), "fetch", f"method IN ({_OLD}, 'search_contents')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_fetch_method"), "fetch", type_="check")
    op.execute(
        f'ALTER TABLE "fetch" ADD CONSTRAINT ck_fetch_method CHECK (method IN ({_OLD})) NOT VALID'
    )
