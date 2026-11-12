
"""Company-to-crawler intelligence mapping."""

from alembic import op
import sqlalchemy as sa

revision = "0003_intelligence_links"
down_revision = "0002_competitors"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_intelligence_links",
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "companies.id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column(
            "crawler_company_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column(
            "crawler_job_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("company_intelligence_links")
