"""initial evidence schema

Revision ID: 0001
Revises:
Create Date: 2026-09-28 18:16:28.939741
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy; migrations must not change when models.py changes.
APPEND_ONLY_TABLES = ("evidence", "observation", "relationship", "field_assessment")


def upgrade() -> None:
    op.create_table(
        "organization",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("registrable_domain", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization")),
    )
    op.create_index(
        "uq_organization_registrable_domain",
        "organization",
        ["registrable_domain"],
        unique=True,
        postgresql_where=sa.text("registrable_domain IS NOT NULL"),
    )
    op.create_table(
        "url",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("registrable_domain", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_url")),
        sa.UniqueConstraint("url", name=op.f("uq_url_url")),
    )
    op.create_index(op.f("ix_url_registrable_domain"), "url", ["registrable_domain"], unique=False)
    op.create_table(
        "crawl",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("seed_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')", name=op.f("ck_crawl_status")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_crawl_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl")),
    )
    op.create_index(op.f("ix_crawl_organization_id"), "crawl", ["organization_id"], unique=False)
    op.create_table(
        "product",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("identification_basis", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_product_organization_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product")),
        sa.UniqueConstraint(
            "organization_id", "slug", name=op.f("uq_product_organization_id_slug")
        ),
    )
    op.create_table(
        "crawl_url",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("url_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_url_id", sa.BigInteger(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("discovered_via", sa.Text(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "discovered_via IN ('seed', 'link', 'sitemap', 'robots', 'redirect', 'search')",
            name=op.f("ck_crawl_url_discovered_via"),
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'fetched', 'skipped', 'failed')", name=op.f("ck_crawl_url_state")
        ),
        sa.CheckConstraint("depth >= 0", name=op.f("ck_crawl_url_depth")),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_crawl_url_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(
            ["parent_url_id"], ["url.id"], name=op.f("fk_crawl_url_parent_url_id_url")
        ),
        sa.ForeignKeyConstraint(["url_id"], ["url.id"], name=op.f("fk_crawl_url_url_id_url")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_url")),
        sa.UniqueConstraint("crawl_id", "url_id", name=op.f("uq_crawl_url_crawl_id_url_id")),
    )
    op.create_table(
        "fetch",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("url_id", sa.BigInteger(), nullable=False),
        sa.Column("final_url_id", sa.BigInteger(), nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column(
            "redirect_chain",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status_code", sa.SmallInteger(), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("content_length", sa.BigInteger(), nullable=True),
        sa.Column("raw_sha256", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("method IN ('http', 'browser')", name=op.f("ck_fetch_method")),
        sa.CheckConstraint(
            "raw_sha256 IS NULL OR length(raw_sha256) = 64", name=op.f("ck_fetch_raw_sha256")
        ),
        sa.ForeignKeyConstraint(["crawl_id"], ["crawl.id"], name=op.f("fk_fetch_crawl_id_crawl")),
        sa.ForeignKeyConstraint(
            ["final_url_id"], ["url.id"], name=op.f("fk_fetch_final_url_id_url")
        ),
        sa.ForeignKeyConstraint(["url_id"], ["url.id"], name=op.f("fk_fetch_url_id_url")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch")),
    )
    op.create_index(op.f("ix_fetch_crawl_id"), "fetch", ["crawl_id"], unique=False)
    op.create_index(op.f("ix_fetch_url_id"), "fetch", ["url_id"], unique=False)
    op.create_table(
        "field_assessment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column(
            "observation_ids",
            sa.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("method_version", sa.Text(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(state IN ('UNKNOWN', 'CONFLICTING', 'NOT_APPLICABLE') AND value IS NULL) OR (state IN ('FOUND', 'VERIFIED', 'UNVERIFIED') AND value IS NOT NULL AND confidence IS NOT NULL AND cardinality(observation_ids) > 0)",
            name=op.f("ck_field_assessment_value_matches_state"),
        ),
        sa.CheckConstraint(
            "state <> 'CONFLICTING' OR cardinality(observation_ids) > 1",
            name=op.f("ck_field_assessment_conflict_has_observations"),
        ),
        sa.CheckConstraint(
            "state IN ('FOUND', 'VERIFIED', 'UNVERIFIED', 'CONFLICTING', 'UNKNOWN', 'NOT_APPLICABLE')",
            name=op.f("ck_field_assessment_state"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name=op.f("ck_field_assessment_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_field_assessment_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_field_assessment_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_assessment")),
    )
    op.create_index(
        "ix_field_assessment_subject_field",
        "field_assessment",
        ["organization_id", "product_id", "field"],
        unique=False,
    )
    op.create_table(
        "page",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("fetch_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_url_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("cleaned_text", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.Text(), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "structured_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parsed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["canonical_url_id"], ["url.id"], name=op.f("fk_page_canonical_url_id_url")
        ),
        sa.ForeignKeyConstraint(["fetch_id"], ["fetch.id"], name=op.f("fk_page_fetch_id_fetch")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page")),
        sa.UniqueConstraint("fetch_id", name=op.f("uq_page_fetch_id")),
    )
    op.create_index(op.f("ix_page_text_sha256"), "page", ["text_sha256"], unique=False)
    op.create_table(
        "evidence",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("page_id", sa.BigInteger(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("locator_type", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "locator_type IN ('xpath', 'json_path')", name=op.f("ck_evidence_locator_type")
        ),
        sa.CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR (0 <= char_start AND char_start <= char_end)",
            name=op.f("ck_evidence_char_range"),
        ),
        sa.CheckConstraint("length(quote) > 0", name=op.f("ck_evidence_quote_not_empty")),
        sa.ForeignKeyConstraint(["page_id"], ["page.id"], name=op.f("fk_evidence_page_id_page")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_page_id"), "evidence", ["page_id"], unique=False)
    op.create_table(
        "link",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("page_id", sa.BigInteger(), nullable=False),
        sa.Column("target_url_id", sa.BigInteger(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column("rel", sa.Text(), nullable=True),
        sa.Column("is_external", sa.Boolean(), nullable=False),
        sa.Column("is_reference", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["page_id"], ["page.id"], name=op.f("fk_link_page_id_page")),
        sa.ForeignKeyConstraint(
            ["target_url_id"], ["url.id"], name=op.f("fk_link_target_url_id_url")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_link")),
    )
    op.create_index(op.f("ix_link_page_id"), "link", ["page_id"], unique=False)
    op.create_index(op.f("ix_link_target_url_id"), "link", ["target_url_id"], unique=False)
    op.create_table(
        "observation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value_verbatim", sa.Text(), nullable=False),
        sa.Column("value_normalized", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("qualifier", sa.Text(), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("kind", sa.Text(), server_default="stated", nullable=False),
        sa.Column("evidence_id", sa.BigInteger(), nullable=False),
        sa.Column("source_tier", sa.SmallInteger(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("is_first_party", sa.Boolean(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("extraction_certainty", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('stated', 'derived')", name=op.f("ck_observation_kind")),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_observation_confidence")),
        sa.CheckConstraint(
            "extraction_certainty BETWEEN 0 AND 1", name=op.f("ck_observation_extraction_certainty")
        ),
        sa.CheckConstraint(
            "length(value_verbatim) > 0", name=op.f("ck_observation_value_verbatim_not_empty")
        ),
        sa.CheckConstraint("source_tier BETWEEN 1 AND 4", name=op.f("ck_observation_source_tier")),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_observation_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.id"], name=op.f("fk_observation_evidence_id_evidence")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_observation_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_observation_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observation")),
    )
    op.create_index(op.f("ix_observation_crawl_id"), "observation", ["crawl_id"], unique=False)
    op.create_index(
        "ix_observation_subject_field",
        "observation",
        ["organization_id", "product_id", "field"],
        unique=False,
    )
    op.create_table(
        "relationship",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_org_id", sa.BigInteger(), nullable=False),
        sa.Column("object_org_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('investor', 'tech_partner', 'strategic_partner', 'competitor')",
            name=op.f("ck_relationship_type"),
        ),
        sa.CheckConstraint(
            "subject_org_id <> object_org_id", name=op.f("ck_relationship_distinct_orgs")
        ),
        sa.ForeignKeyConstraint(
            ["object_org_id"],
            ["organization.id"],
            name=op.f("fk_relationship_object_org_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_relationship_observation_id_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["subject_org_id"],
            ["organization.id"],
            name=op.f("fk_relationship_subject_org_id_organization"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship")),
    )
    op.create_index(
        "ix_relationship_subject_type", "relationship", ["subject_org_id", "type"], unique=False
    )

    # Evidence and conclusions are history: new information adds rows, never rewrites them.
    op.execute("""
        CREATE FUNCTION reject_modification() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only; % is not allowed', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
        )


def downgrade() -> None:
    op.drop_index("ix_relationship_subject_type", table_name="relationship")
    op.drop_table("relationship")
    op.drop_index("ix_observation_subject_field", table_name="observation")
    op.drop_index(op.f("ix_observation_crawl_id"), table_name="observation")
    op.drop_table("observation")
    op.drop_index(op.f("ix_link_target_url_id"), table_name="link")
    op.drop_index(op.f("ix_link_page_id"), table_name="link")
    op.drop_table("link")
    op.drop_index(op.f("ix_evidence_page_id"), table_name="evidence")
    op.drop_table("evidence")
    op.drop_index(op.f("ix_page_text_sha256"), table_name="page")
    op.drop_table("page")
    op.drop_index("ix_field_assessment_subject_field", table_name="field_assessment")
    op.drop_table("field_assessment")
    op.drop_index(op.f("ix_fetch_url_id"), table_name="fetch")
    op.drop_index(op.f("ix_fetch_crawl_id"), table_name="fetch")
    op.drop_table("fetch")
    op.drop_table("crawl_url")
    op.drop_table("product")
    op.drop_index(op.f("ix_crawl_organization_id"), table_name="crawl")
    op.drop_table("crawl")
    op.drop_index(op.f("ix_url_registrable_domain"), table_name="url")
    op.drop_table("url")
    op.drop_index(
        "uq_organization_registrable_domain",
        table_name="organization",
        postgresql_where=sa.text("registrable_domain IS NOT NULL"),
    )
    op.drop_table("organization")
    op.execute("DROP FUNCTION reject_modification()")
