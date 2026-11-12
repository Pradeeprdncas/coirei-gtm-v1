"""spec field states, confidence components, corroboration (production spec Phase 11)

* field_assessment.state accepts the spec section 19 states (method fa-v2):
  UNKNOWN, SUPPORTED, CORROBORATED, CONFLICTING, STALE, UNSUPPORTED,
  NOT_APPLICABLE. The fa-v1 states stay valid: earlier rows are history and
  are not rewritten (the table is append-only).
* field_assessment.confidence_components: what the confidence was computed from.
* corroboration: the role of each observation in an assessment
  (supports / qualifies / contradicts / superseded) and its source.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29 18:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = ("UNKNOWN", "SUPPORTED", "CORROBORATED", "CONFLICTING", "STALE", "UNSUPPORTED",
           "NOT_APPLICABLE", "FOUND", "VERIFIED", "UNVERIFIED")  # fmt: skip
_OLD_STATES = ("FOUND", "VERIFIED", "UNVERIFIED", "CONFLICTING", "UNKNOWN", "NOT_APPLICABLE")
_WITH_VALUE = ("SUPPORTED", "CORROBORATED", "STALE", "UNSUPPORTED", "FOUND", "VERIFIED",
               "UNVERIFIED")  # fmt: skip
_OLD_WITH_VALUE = ("FOUND", "VERIFIED", "UNVERIFIED")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _constraints(states: tuple[str, ...], with_value: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_field_assessment_state"), "field_assessment", type_="check")
    op.drop_constraint(
        op.f("ck_field_assessment_value_matches_state"), "field_assessment", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_field_assessment_state"), "field_assessment", f"state IN ({_in(states)})"
    )
    op.create_check_constraint(
        op.f("ck_field_assessment_value_matches_state"),
        "field_assessment",
        "(state IN ('UNKNOWN', 'CONFLICTING', 'NOT_APPLICABLE') AND value IS NULL)"
        f" OR (state IN ({_in(with_value)}) AND value IS NOT NULL"
        " AND confidence IS NOT NULL AND cardinality(observation_ids) > 0)",
    )


def upgrade() -> None:
    _constraints(_STATES, _WITH_VALUE)
    op.add_column(
        "field_assessment",
        sa.Column("confidence_components", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
    )  # fmt: skip
    op.create_table(
        "corroboration",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("assessment_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('supports', 'qualifies', 'contradicts', 'superseded')",
            name=op.f("ck_corroboration_role"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["field_assessment.id"],
            name=op.f("fk_corroboration_assessment_id_field_assessment"),
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_corroboration_observation_id_observation"),
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corroboration")),
        sa.UniqueConstraint(
            "assessment_id",
            "observation_id",
            name=op.f("uq_corroboration_assessment_id_observation_id"),
        ),  # fmt: skip
    )
    op.create_index(
        op.f("ix_corroboration_observation_id"), "corroboration", ["observation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_corroboration_observation_id"), table_name="corroboration")
    op.drop_table("corroboration")
    op.drop_column("field_assessment", "confidence_components")
    # Lossy: fa-v2 rows are relabelled with the nearest fa-v1 state (STALE
    # becomes FOUND). The append-only trigger is suspended for this only.
    op.execute("ALTER TABLE field_assessment DISABLE TRIGGER field_assessment_append_only")
    op.execute(
        "UPDATE field_assessment SET state = CASE state WHEN 'SUPPORTED' THEN 'FOUND'"
        " WHEN 'STALE' THEN 'FOUND' WHEN 'CORROBORATED' THEN 'VERIFIED'"
        " WHEN 'UNSUPPORTED' THEN 'UNVERIFIED' ELSE state END"
        " WHERE state IN ('SUPPORTED', 'STALE', 'CORROBORATED', 'UNSUPPORTED')"
    )
    op.execute("ALTER TABLE field_assessment ENABLE TRIGGER field_assessment_append_only")
    _constraints(_OLD_STATES, _OLD_WITH_VALUE)
