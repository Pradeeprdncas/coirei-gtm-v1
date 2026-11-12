"""identity resolution (docs/architecture/ENTITY_RESOLUTION.md)

* organization.named_by_org_id: the organization whose pages named a
  name-only row. Rows that exist keep NULL: nobody can say now who named
  them, so they are never matched by name again and stay as history.
* external_identity: a company profile on another platform.
* identity_decision: every resolution attempt, append-only.
* identity_link: which company a named organization or a profile is, as
  currently decided; nothing is merged or deleted.

Additive only: no existing row is changed.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'match', 'probable_match', 'conflicting', 'rejected'"


def _created_at(name: str = "created_at") -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _json_list(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column("organization", sa.Column("named_by_org_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_organization_named_by_org_id_organization"),
        "organization", "organization", ["named_by_org_id"], ["id"],
    )  # fmt: skip
    op.create_index(
        "uq_organization_named_by_name", "organization",
        ["named_by_org_id", sa.text("lower(name)")], unique=True,
        postgresql_where=sa.text("named_by_org_id IS NOT NULL"),
    )  # fmt: skip

    op.create_table(
        "external_identity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("profile_url", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identity")),
        sa.UniqueConstraint(
            "source_type", "external_id", name=op.f("uq_external_identity_source_type_external_id")
        ),
    )

    op.create_table(
        "identity_decision",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("member_org_id", sa.BigInteger(), nullable=True),
        sa.Column("external_identity_id", sa.BigInteger(), nullable=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        _json_list("candidates"),
        _json_list("signals"),
        _json_list("contradictions"),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("crawl_id", sa.BigInteger(), nullable=True),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            f"outcome IN ({_STATUSES}, 'unknown')", name=op.f("ck_identity_decision_outcome")
        ),
        sa.CheckConstraint(
            "num_nonnulls(member_org_id, external_identity_id) = 1",
            name=op.f("ck_identity_decision_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["member_org_id"], ["organization.id"],
            name=op.f("fk_identity_decision_member_org_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["external_identity_id"], ["external_identity.id"],
            name=op.f("fk_identity_decision_external_identity_id_external_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"],
            name=op.f("fk_identity_decision_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"], ["crawl.id"], name=op.f("fk_identity_decision_crawl_id_crawl")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["identity_decision.id"],
            name=op.f("fk_identity_decision_supersedes_id_identity_decision"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_decision")),
    )  # fmt: skip
    op.create_index(
        op.f("ix_identity_decision_member_org_id"), "identity_decision", ["member_org_id"]
    )
    op.create_index(
        op.f("ix_identity_decision_external_identity_id"), "identity_decision",
        ["external_identity_id"],
    )  # fmt: skip
    # History, like evidence: never rewritten (reject_modification() is from 0001).
    op.execute(
        "CREATE TRIGGER identity_decision_append_only BEFORE UPDATE OR DELETE ON identity_decision "
        "FOR EACH ROW EXECUTE FUNCTION reject_modification()"
    )

    op.create_table(
        "identity_link",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("member_org_id", sa.BigInteger(), nullable=True),
        sa.Column("external_identity_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=False),
        _created_at(),
        _created_at("updated_at"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=op.f("ck_identity_link_status")),
        sa.CheckConstraint(
            "num_nonnulls(member_org_id, external_identity_id) = 1",
            name=op.f("ck_identity_link_subject"),
        ),
        sa.CheckConstraint(
            "member_org_id <> organization_id", name=op.f("ck_identity_link_distinct_orgs")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"],
            name=op.f("fk_identity_link_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["member_org_id"], ["organization.id"],
            name=op.f("fk_identity_link_member_org_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["external_identity_id"], ["external_identity.id"],
            name=op.f("fk_identity_link_external_identity_id_external_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["identity_decision.id"],
            name=op.f("fk_identity_link_decision_id_identity_decision"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_link")),
        sa.UniqueConstraint(
            "member_org_id", "organization_id",
            name=op.f("uq_identity_link_member_org_id_organization_id"),
        ),
        sa.UniqueConstraint(
            "external_identity_id", "organization_id",
            name=op.f("uq_identity_link_external_identity_id_organization_id"),
        ),
    )  # fmt: skip
    op.create_index(op.f("ix_identity_link_organization_id"), "identity_link", ["organization_id"])
    op.create_index(
        "uq_identity_link_member_open", "identity_link", ["member_org_id"], unique=True,
        postgresql_where=sa.text("status <> 'rejected'"),
    )  # fmt: skip
    op.create_index(
        "uq_identity_link_external_match", "identity_link", ["external_identity_id"], unique=True,
        postgresql_where=sa.text("status = 'match'"),
    )  # fmt: skip


def downgrade() -> None:
    # Drops the identity tables and with them the decisions made so far; the
    # organizations, evidence and observations they refer to are untouched.
    op.drop_table("identity_link")
    op.drop_table("identity_decision")
    op.drop_table("external_identity")
    op.drop_index("uq_organization_named_by_name", table_name="organization")
    op.drop_constraint(
        op.f("fk_organization_named_by_org_id_organization"), "organization", type_="foreignkey"
    )
    op.drop_column("organization", "named_by_org_id")
