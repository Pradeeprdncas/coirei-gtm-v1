"""Competitor discovery schema."""

from alembic import op

from app.core.db import Base
from app.models import entities
from app.models.competitors import (
    CompetitorJob,
    CompetitorCandidate,
    CompetitorSource,
    CompetitorChunk,
)

revision = "0002_competitors"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TABLES = [
    CompetitorJob.__table__,
    CompetitorCandidate.__table__,
    CompetitorSource.__table__,
    CompetitorChunk.__table__,
]


def upgrade():
    op.execute(
        "CREATE EXTENSION IF NOT EXISTS vector"
    )
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=TABLES,
    )


def downgrade():
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=list(reversed(TABLES)),
    )