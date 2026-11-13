
"""Company-to-crawler intelligence mapping."""

from alembic import op

from app.core.db import Base
from app.models.intelligence import IntelligenceLink

revision = "0003_intelligence_links"
down_revision = "0002_competitors"
branch_labels = None
depends_on = None

TABLES = [IntelligenceLink.__table__]


def upgrade():
    # 0001_initial's blanket Base.metadata.create_all() already creates every
    # table registered on Base.metadata at that point, including this one (its
    # model is imported eagerly in env.py) — create_all() checks existence
    # first and no-ops here, unlike a raw op.create_table() which would always
    # fail with "already exists" on a fresh database. Same pattern as 0002.
    Base.metadata.create_all(bind=op.get_bind(), tables=TABLES)


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind(), tables=list(reversed(TABLES)))
