"""Initial numeric-ID company/onboarding schema."""
from alembic import op
from app.core.db import Base
from app.models import entities  # noqa: F401

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
