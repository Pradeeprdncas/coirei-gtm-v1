"""competitor ads: ad and ad_observation

* ad: one advertisement on one platform (``platform`` + the platform's own ad
  id), run by an advertiser profile (``external_identity``).
* ad_observation: one capture of an ad, append-only (a trigger refuses
  UPDATE and DELETE, as on observation). Fields carry their own
  state (present, absent_on_source, not_collected, inaccessible, redacted);
  the platform's dates and the capture time are kept apart.

Additive only: no existing table is changed.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-06 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "ad",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_ad_id", sa.Text(), nullable=False),
        sa.Column("external_identity_id", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.CheckConstraint("platform IN ('meta', 'google')", name=op.f("ck_ad_platform")),
        sa.CheckConstraint("length(platform_ad_id) > 0", name=op.f("ck_ad_platform_ad_id")),
        sa.ForeignKeyConstraint(
            ["external_identity_id"],
            ["external_identity.id"],
            name=op.f("fk_ad_external_identity_id_external_identity"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad")),
        sa.UniqueConstraint(
            "platform", "platform_ad_id", name=op.f("uq_ad_platform_platform_ad_id")
        ),
    )
    op.create_index(op.f("ix_ad_external_identity_id"), "ad", ["external_identity_id"])
    op.create_table(
        "ad_observation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ad_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("import_id", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("raw_sha256", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "media",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("verification", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint("method IN ('manual_import')", name=op.f("ck_ad_observation_method")),
        sa.CheckConstraint(
            "verification IN ('operator_transcribed', 'ocr_corroborated')",
            name=op.f("ck_ad_observation_verification"),
        ),
        sa.CheckConstraint(
            "method <> 'manual_import' OR length(operator) > 0",
            name=op.f("ck_ad_observation_operator"),
        ),
        sa.CheckConstraint(
            "length(raw_sha256) = 64 AND length(content_sha256) = 64",
            name=op.f("ck_ad_observation_digests"),
        ),
        sa.ForeignKeyConstraint(["ad_id"], ["ad.id"], name=op.f("fk_ad_observation_ad_id_ad")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad_observation")),
        sa.UniqueConstraint("ad_id", "import_id", name=op.f("uq_ad_observation_ad_id_import_id")),
    )
    op.create_index(
        "ix_ad_observation_ad_id_observed_at", "ad_observation", ["ad_id", "observed_at"]
    )
    # History, like evidence: never rewritten (reject_modification() is from 0001).
    op.execute(
        "CREATE TRIGGER ad_observation_append_only BEFORE UPDATE OR DELETE ON ad_observation "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
    )


def downgrade() -> None:
    op.drop_index("ix_ad_observation_ad_id_observed_at", table_name="ad_observation")
    op.drop_table("ad_observation")
    op.drop_index(op.f("ix_ad_external_identity_id"), table_name="ad")
    op.drop_table("ad")
