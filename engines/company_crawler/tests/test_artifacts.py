"""Phase 7: raw artifacts recorded in PostgreSQL, retention and full lineage."""

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from gtm_crawler.cli import prune
from gtm_crawler.db.evidence import EvidenceStore
from gtm_crawler.db.models import Artifact, Fetch, RobotsSnapshot, SitemapFetch
from gtm_crawler.dossier import build_dossier
from gtm_crawler.pipeline import run_pipeline
from gtm_crawler.rawstore import ARTIFACT_VERSION, make_rawstore

from .conftest import alembic_config
from .test_crawl import SITE, FakeSite, html, run, settings_for

pytestmark = pytest.mark.db

HOME = html(
    "Acme", "<p>Acme was founded in 2019.</p>", '<meta property="og:site_name" content="Acme">'
)
SITEMAP = (
    b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b"<url><loc>https://acme.example/</loc></url></urlset>"
)
ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/sitemap.xml": ("application/xml", SITEMAP),
    f"{SITE}/": ("text/html", HOME),
}


def artifacts(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {a.sha256: a for a in conn.execute(select(Artifact)).all()}


def test_every_stored_body_has_an_artifact_record(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    stored = artifacts(engine)
    with engine.connect() as conn:
        refs = {
            table.__tablename__: conn.execute(
                select(table.raw_sha256).where(table.crawl_id == summary.crawl_id)
            ).scalars().all()
            for table in (Fetch, RobotsSnapshot, SitemapFetch)
        }  # fmt: skip
    home = hashlib.sha256(HOME).hexdigest()
    assert refs["fetch"] == [home]
    assert refs["robots_snapshot"] == [hashlib.sha256(b"User-agent: *\nAllow: /\n").hexdigest()]
    assert refs["sitemap_fetch"] == [hashlib.sha256(SITEMAP).hexdigest()]
    a = stored[home]
    assert (a.size_bytes, a.content_type, a.artifact_version) == (
        len(HOME), "text/html", ARTIFACT_VERSION)  # fmt: skip
    assert a.pruned_at is None
    assert stored[refs["sitemap_fetch"][0]].content_type == "application/xml"

    run(engine, settings_for(tmp_path), FakeSite(ROUTES))  # the same bodies again
    assert artifacts(engine)[home].created_at == a.created_at  # recorded once


def test_database_refuses_to_cite_an_unrecorded_document(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            update(Fetch).where(Fetch.crawl_id == summary.crawl_id).values(raw_sha256="f" * 64)
        )


def lineage_run(engine: Engine, tmp_path: Path) -> tuple[dict[str, Any], Any]:
    s = settings_for(tmp_path)
    result = asyncio.run(
        run_pipeline(f"{SITE}/", s, engine, transport=httpx.MockTransport(FakeSite(ROUTES)))
    )
    return result, s


def test_a_field_traces_back_to_its_raw_artifact(engine: Engine, tmp_path: Path) -> None:
    result, s = lineage_run(engine, tmp_path)
    store = make_rawstore(s.rawstore)
    evidence = EvidenceStore(engine, store, s.sources)
    [lineage] = evidence.field_lineage(result["organization_id"], "founded_year")["items"]
    assert lineage["state"] in ("SUPPORTED", "CORROBORATED")
    # Other tests crawl the same site: take the observation from this crawl's page.
    home = hashlib.sha256(HOME).hexdigest()
    obs = next(o for o in lineage["observations"] if o["fetch"]["raw_sha256"] == home)
    assert obs["field"] == "founded_year" and "2019" in obs["evidence"]["quote"]
    assert obs["parser_version"] and obs["extractor"].count("@") == 1
    assert obs["fetch"]["status"] == 200 and obs["fetch"]["fetched_at"] is not None
    artifact = obs["artifact"]
    assert artifact["sha256"] == obs["fetch"]["raw_sha256"] == hashlib.sha256(HOME).hexdigest()
    assert artifact["version"] == ARTIFACT_VERSION and artifact["pruned_at"] is None
    # The chain ends in bytes that still hash to the recorded value and still
    # contain the quote where the evidence says it is.
    assert hashlib.sha256(store.get(artifact["sha256"])).hexdigest() == artifact["sha256"]
    assert evidence.verify(obs["evidence"]["id"]) is None

    # The dossier cites the same chain.
    fields = build_dossier(engine, result["organization_id"])["company"]["fields"]
    [cite] = [c for c in fields["founded_year"]["citations"]
              if c["artifact_sha256"] == home]  # fmt: skip
    assert cite["evidence_id"] == obs["evidence"]["id"]
    assert cite["artifact_sha256"] == artifact["sha256"]
    assert cite["locator"]["type"] == obs["evidence"]["locator_type"]


def test_unassessed_field_is_unknown_with_no_sources(engine: Engine, tmp_path: Path) -> None:
    result, s = lineage_run(engine, tmp_path)
    evidence = EvidenceStore(engine, make_rawstore(s.rawstore), s.sources)
    [lineage] = evidence.field_lineage(result["organization_id"], "annual_revenue")["items"]
    assert lineage["state"] == "UNKNOWN" and lineage["observations"] == []


def test_prune_keeps_cited_artifacts_and_marks_the_rest(engine: Engine, tmp_path: Path) -> None:
    result, s = lineage_run(engine, tmp_path)
    long_ago = time.time() - 400 * 86_400  # past rawstore.retention_days
    for path in (tmp_path / "raw").glob("*/*/*.gz"):
        os.utime(path, (long_ago, long_ago))
    outcome = prune(engine, s)
    stored = artifacts(engine)
    home = hashlib.sha256(HOME).hexdigest()
    sitemap = hashlib.sha256(SITEMAP).hexdigest()
    assert outcome["removed"] >= 1
    assert stored[home].pruned_at is None  # cited by evidence: kept forever
    assert stored[sitemap].pruned_at is not None  # retention removed it; the record stays
    evidence = EvidenceStore(engine, make_rawstore(s.rawstore), s.sources)
    [item] = evidence.field_lineage(result["organization_id"], "founded_year")["items"]
    obs = item["observations"][0]
    assert obs["artifact"]["pruned_at"] is None

    # Retrieving the same document again makes it available again.
    run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    assert artifacts(engine)[sitemap].pruned_at is None


def test_migration_backfills_existing_references(engine: Engine, tmp_path: Path) -> None:
    summary = run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    home = hashlib.sha256(HOME).hexdigest()
    cfg = alembic_config()
    command.downgrade(cfg, "0008")
    try:
        with engine.begin() as conn:
            assert conn.execute(select(Fetch.raw_sha256).where(
                Fetch.crawl_id == summary.crawl_id)).scalar_one() == home  # fmt: skip
    finally:
        command.upgrade(cfg, "head")
    stored = artifacts(engine)
    assert stored[home].size_bytes == len(HOME) and stored[home].artifact_version == "1"


def test_artifact_rows_cannot_be_deleted_while_cited(engine: Engine, tmp_path: Path) -> None:
    run(engine, settings_for(tmp_path), FakeSite(ROUTES))
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(delete(Artifact).where(Artifact.sha256 == hashlib.sha256(HOME).hexdigest()))
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(insert(Artifact).values(sha256="short", artifact_version="1"))
