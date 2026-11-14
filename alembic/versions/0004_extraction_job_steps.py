
"""Growing step log on ExtractionJob, for a live progress feed."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_extraction_job_steps"
down_revision = "0003_intelligence_links"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('extraction_jobs', sa.Column(
        'steps', JSONB, nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('extraction_jobs', 'steps')
