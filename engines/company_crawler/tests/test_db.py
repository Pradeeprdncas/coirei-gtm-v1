"""Schema tests against a real PostgreSQL (see README for setup)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from gtm_crawler.db.models import (
    Artifact,
    Base,
    Crawl,
    Evidence,
    Extraction,
    Fetch,
    FieldAssessment,
    Observation,
    Organization,
    Page,
    Url,
)

from .conftest import alembic_config

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _insert(conn: Connection, model: Any, **values: Any) -> int:
    return conn.execute(insert(model).values(**values).returning(model.id)).scalar_one()


def _evidence_chain(conn: Connection) -> dict[str, int]:
    org = _insert(conn, Organization, name="Example Inc", registrable_domain="example.com")
    crawl = _insert(conn, Crawl, organization_id=org, seed_url="https://example.com/", config={})
    url = _insert(
        conn,
        Url,
        url="https://example.com/about",
        host="example.com",
        registrable_domain="example.com",
    )
    conn.execute(
        insert(Artifact).values(sha256="a" * 64, size_bytes=10, artifact_version="1")
    )  # fmt: skip
    fetch = _insert(
        conn, Fetch, crawl_id=crawl, url_id=url, method="http", status_code=200, raw_sha256="a" * 64
    )
    page = _insert(
        conn, Page, fetch_id=fetch, title="About", cleaned_text="We serve more than 500 customers."
    )
    evidence = _insert(
        conn,
        Evidence,
        page_id=page,
        quote="more than 500 customers",
        locator_type="xpath",
        locator="/html/body/p[1]",
        char_start=8,
        char_end=31,
    )
    extraction = _insert(conn, Extraction, crawl_id=crawl)
    return {"org": org, "crawl": crawl, "page": page, "evidence": evidence,
            "extraction": extraction}  # fmt: skip


def _observation(conn: Connection, ids: dict[str, int], **overrides: Any) -> int:
    values: dict[str, Any] = dict(
        crawl_id=ids["crawl"],
        extraction_id=ids["extraction"],
        organization_id=ids["org"],
        field="customer_count",
        value_verbatim="more than 500 customers",
        value_normalized={"qualifier": "gt", "value": 500},
        qualifier="gt",
        evidence_id=ids["evidence"],
        source_tier=1,
        source_type="official_company_site",
        is_first_party=True,
        extractor="test",
        extractor_version="1",
        extraction_certainty=Decimal("0.90"),
        confidence=Decimal("0.81"),
        retrieved_at=NOW,
    )
    values.update(overrides)
    return _insert(conn, Observation, **values)


def _rejects(conn: Connection, stmt: Any, exc: type[Exception] = DBAPIError) -> None:
    with pytest.raises(exc), conn.begin_nested():
        conn.execute(stmt)


def test_migration_matches_models(engine: Engine) -> None:
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_roundtrip(engine: Engine) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())


def test_evidence_chain_persists_with_qualifier(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    obs_id = _observation(conn, ids)
    row = conn.execute(select(Observation).where(Observation.id == obs_id)).one()
    assert row.value_verbatim == "more than 500 customers"
    assert row.value_normalized == {"qualifier": "gt", "value": 500}
    assert row.kind == "stated"


@pytest.mark.parametrize("table", ["observation", "evidence"])
def test_append_only_tables_refuse_update_and_delete(conn: Connection, table: str) -> None:
    ids = _evidence_chain(conn)
    obs_id = _observation(conn, ids)
    row_id = obs_id if table == "observation" else ids["evidence"]
    _rejects(
        conn, text(f"UPDATE {table} SET created_at = now() WHERE id = :id").bindparams(id=row_id)
    )
    _rejects(conn, text(f"DELETE FROM {table} WHERE id = :id").bindparams(id=row_id))


def test_historical_observations_are_kept(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    old = _observation(
        conn,
        ids,
        field="total_funding",
        value_verbatim="$10M",
        value_normalized={"amount": 10_000_000, "currency": "USD"},
        qualifier=None,
        as_of=date(2022, 3, 1),
    )
    new = _observation(
        conn,
        ids,
        field="total_funding",
        value_verbatim="$30M",
        value_normalized={"amount": 30_000_000, "currency": "USD"},
        qualifier=None,
        as_of=date(2024, 6, 1),
    )
    rows = conn.execute(
        select(Observation.id, Observation.as_of)
        .where(Observation.field == "total_funding")
        .order_by(Observation.as_of)
    ).all()
    assert [r.id for r in rows] == [old, new]


def test_unknown_field_cannot_carry_a_value(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    base = dict(organization_id=ids["org"], field="annual_revenue", method_version="1")
    _insert(conn, FieldAssessment, state="UNKNOWN", **base)  # no value: allowed
    for state in ("UNKNOWN", "CONFLICTING", "NOT_APPLICABLE"):
        _rejects(
            conn,
            insert(FieldAssessment).values(
                state=state,
                value={"amount": 1},
                confidence=Decimal("0.5"),
                observation_ids=[1, 2],
                **base,
            ),
            IntegrityError,
        )


def test_found_value_requires_observations_and_confidence(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    obs = _observation(conn, ids)
    base = dict(
        organization_id=ids["org"],
        field="customer_count",
        method_version="1",
        value={"qualifier": "gt", "value": 500},
    )
    _rejects(
        conn,
        insert(FieldAssessment).values(state="FOUND", confidence=Decimal("0.8"), **base),
        IntegrityError,
    )
    _rejects(
        conn,
        insert(FieldAssessment).values(state="FOUND", observation_ids=[obs], **base),
        IntegrityError,
    )
    _insert(
        conn,
        FieldAssessment,
        state="FOUND",
        confidence=Decimal("0.81"),
        observation_ids=[obs],
        **base,
    )


def test_conflicting_requires_multiple_observations(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    obs = _observation(conn, ids)
    base = dict(
        organization_id=ids["org"], field="employee_count", method_version="1", state="CONFLICTING"
    )
    _rejects(conn, insert(FieldAssessment).values(observation_ids=[obs], **base), IntegrityError)
    _insert(conn, FieldAssessment, observation_ids=[obs, obs + 1], **base)


def test_field_assessment_is_append_only(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    fa = _insert(
        conn,
        FieldAssessment,
        organization_id=ids["org"],
        field="valuation",
        state="UNKNOWN",
        method_version="1",
    )
    _rejects(conn, update(FieldAssessment).where(FieldAssessment.id == fa).values(state="UNKNOWN"))


def test_observation_rejects_out_of_range_values(conn: Connection) -> None:
    ids = _evidence_chain(conn)
    for bad in (
        {"source_tier": 5},
        {"confidence": Decimal("1.5")},
        {"value_verbatim": ""},
        {"kind": "guessed"},
    ):
        with pytest.raises(DBAPIError), conn.begin_nested():
            _observation(conn, ids, **bad)


def test_engine_applies_statement_and_lock_timeouts(engine: Engine) -> None:
    from gtm_crawler.config import DatabaseSettings
    from gtm_crawler.db import make_engine

    from .conftest import TEST_DATABASE_URL

    configured = make_engine(
        DatabaseSettings(url=TEST_DATABASE_URL, statement_timeout_ms=1500, lock_timeout_ms=700)
    )
    with configured.connect() as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar() == "1500ms"
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "700ms"
        with pytest.raises(DBAPIError, match="statement timeout"):
            conn.execute(text("SELECT pg_sleep(3)"))
    configured.dispose()


def test_url_upsert_does_not_wait_on_foreign_key_checks(engine: Engine) -> None:
    # A link or crawl_url insert holds KEY SHARE on the url rows it references
    # until it commits. Upserting those urls again must not wait for it: two
    # writers doing both in opposite order deadlocked (crawl 24, 2026-11-09).
    from sqlalchemy import create_engine

    from gtm_crawler.db.store import CrawlStore

    url = "https://lock.example/a"
    url_id = CrawlStore(engine).url_ids([url])[url]
    impatient = create_engine(engine.url, connect_args={"options": "-c lock_timeout=2000"})
    try:
        with engine.connect() as holder:
            holder.execute(text("SELECT 1 FROM url WHERE id = :id FOR KEY SHARE"), {"id": url_id})
            assert CrawlStore(impatient).url_ids([url]) == {url: url_id}
            holder.rollback()
    finally:
        impatient.dispose()
