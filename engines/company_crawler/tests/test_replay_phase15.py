"""Replay of stored crawls and regression comparison (spec sections 24, 25, 29)."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, inspect, select, text

from gtm_crawler import extract
from gtm_crawler.api import create_app
from gtm_crawler.cli import main
from gtm_crawler.config import Settings
from gtm_crawler.db.models import Extraction, Fetch, Observation, Page, Url
from gtm_crawler.dossier import build_dossier
from gtm_crawler.pipeline import run_pipeline
from gtm_crawler.rawstore import RawStore
from gtm_crawler.replay import replay_crawl, replay_many

from .conftest import TEST_DATABASE_URL, alembic_config
from .test_crawl import FakeSite, html, settings_for

pytestmark = pytest.mark.db

TABLES = ("organization", "product", "extraction", "evidence", "observation", "relationship",
          "field_assessment", "corroboration", "fetch", "page", "crawl")  # fmt: skip


def crawled(engine: Engine, tmp_path: Path) -> tuple[Settings, dict[str, Any]]:
    """A finished pipeline run over a fresh company (its own domain, so its
    field states depend on this crawl only)."""
    site = f"https://r{uuid.uuid4().hex[:10]}.example"
    routes = {
        f"{site}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{site}/": ("text/html", html("Acme", '<a href="/about">About</a><p>Acme builds.</p>',
                                       '<meta property="og:site_name" content="Acme">')),
        f"{site}/about": ("text/html", html("About", "<p>Acme Inc was founded in 2019.</p>")),
    }  # fmt: skip
    s = settings_for(tmp_path)
    t = httpx.MockTransport(FakeSite(routes))
    result = asyncio.run(run_pipeline(f"{site}/", s, engine, transport=t))
    assert result["status"] == "completed"
    return s, result


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one() for t in TABLES}  # noqa: S608


def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("replay must not do network I/O")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def drop_founded_year(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed company extractor: a new version that no longer finds founded_year."""
    original = extract.extract_company

    def changed(page: Any, ctx: Any) -> Any:
        return [d for d in original(page, ctx) if d.field != "founded_year"]

    monkeypatch.setattr(
        extract, "EXTRACTORS", tuple(changed if e is original else e for e in extract.EXTRACTORS)
    )
    monkeypatch.setitem(extract.EXTRACTOR_VERSIONS, "company", "99")


def test_dry_run_writes_nothing_and_unchanged_code_has_empty_diff(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, result = crawled(engine, tmp_path)
    no_network(monkeypatch)
    before = counts(engine)
    report = replay_crawl(engine, s, result["crawl_id"], write=False)
    assert counts(engine) == before
    assert report["mode"] == "dry_run" and report["changed"] is False
    assert report["baseline_extraction_id"] == result["extraction"]["extraction_id"]
    obs = report["observations"]
    assert obs["added"] == obs["removed"] == obs["changed"] == []
    assert obs["before"] == obs["after"] > 0
    assert obs["per_field"]["founded_year"] == {"before": 1, "after": 1, "added": 0,
                                                "removed": 0, "changed": 0}  # fmt: skip
    assert report["versions"]["changed"] == {}
    assert report["unreadable_pages"] == report["rejected_drafts"] == []
    json.dumps(report)  # JSON-able as it is


def test_extractor_change_shows_removed_observations_and_state_transitions(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    s, result = crawled(engine, tmp_path)
    crawl_id, org = result["crawl_id"], result["organization_id"]
    drop_founded_year(monkeypatch)
    no_network(monkeypatch)

    dry = replay_crawl(engine, s, crawl_id, write=False)
    assert dry["changed"] is True
    (removed,) = dry["observations"]["removed"]
    assert (removed["field"], removed["value_verbatim"]) == ("founded_year", "2019")
    assert removed["url"].endswith("/about")
    assert dry["versions"]["changed"]["extractors"]["after"]["company"] == "99"
    assert dry["field_states"] is None  # assessing needs recorded observations

    config = tmp_path / "config.toml"
    config.write_text(f'[rawstore]\nroot = "{(tmp_path / "raw").as_posix()}"\n')
    monkeypatch.setenv("GTM__DATABASE__URL", TEST_DATABASE_URL)
    argv = ["--replay", str(crawl_id), "--config", str(config)]
    assert main(argv) == 0  # a report, not a gate
    capsys.readouterr()
    assert main([*argv, "--fail-on-change"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["observations"]["removed"] == [removed]

    fetches = counts(engine)["fetch"]
    wrote = replay_crawl(engine, s, crawl_id, write=True)
    assert counts(engine)["fetch"] == fetches
    assert [o["field"] for o in wrote["observations"]["removed"]] == ["founded_year"]
    (transition,) = [t for t in wrote["field_states"] if t["field"] == "founded_year"]
    assert transition["to"] == "UNKNOWN" and transition["from"] != "UNKNOWN"
    assert transition["before"][0]["value"] is not None
    with engine.connect() as conn:
        trigger, versions = conn.execute(
            select(Extraction.trigger, Extraction.versions)
            .where(Extraction.id == wrote["extraction_id"])
        ).one()  # fmt: skip
    assert trigger == "replay" and versions["extractors"]["company"] == "99"
    assert build_dossier(engine, org)  # still renders

    # Replaying again with the same code: nothing changes.
    again = replay_crawl(engine, s, crawl_id, write=True)
    assert again["changed"] is False and again["versions"]["changed"] == {}


def test_lineage_survives_reprocessing(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, result = crawled(engine, tmp_path)
    org, first = result["organization_id"], result["extraction"]["extraction_id"]
    no_network(monkeypatch)
    wrote = replay_crawl(engine, s, result["crawl_id"], write=True)
    assert wrote["changed"] is False

    monkeypatch.undo()  # the test client sends through httpx
    api = TestClient(create_app(s, engine))
    lineage = api.get(f"/v1/companies/{org}/lineage/founded_year").json()
    (obs,) = lineage["items"][0]["observations"]
    assert obs["extraction_id"] == wrote["extraction_id"]
    assert obs["value_verbatim"] == "2019" and obs["artifact"]["sha256"]
    dossier = json.dumps(build_dossier(engine, org), default=str)
    assert str(obs["observation_id"]) in dossier
    with engine.connect() as conn:
        old = conn.execute(
            select(func.count()).select_from(Observation).where(Observation.extraction_id == first)
        ).scalar_one()  # fmt: skip
    assert old > 0  # superseded, not deleted
    assert api.get(f"/v1/companies/{org}/lineage/founded_year").status_code == 200


def test_missing_artifact_is_reported(engine: Engine, tmp_path: Path) -> None:
    s, result = crawled(engine, tmp_path)
    with engine.connect() as conn:
        digest = conn.execute(
            select(Fetch.raw_sha256)
            .join(Page, Page.fetch_id == Fetch.id)
            .join(Url, Url.id == Fetch.url_id)
            .where(Fetch.crawl_id == result["crawl_id"], Url.url.like("%/about"))
        ).scalar_one()  # fmt: skip
    RawStore(tmp_path / "raw")._path(digest).unlink()
    report = replay_crawl(engine, s, result["crawl_id"], write=False)
    (page,) = report["unreadable_pages"]
    assert page["url"].endswith("/about") and "raw document unavailable" in page["error"]
    assert "founded_year" in {o["field"] for o in report["observations"]["removed"]}
    assert report["changed"] is True


def test_batch_continues_after_a_failure(engine: Engine, tmp_path: Path) -> None:
    s, result = crawled(engine, tmp_path)
    crawl_id, org = result["crawl_id"], result["organization_id"]
    batch = replay_many(engine, s, [999999999, crawl_id], write=False)
    assert batch["totals"]["crawls"] == 2 and batch["totals"]["failed"] == 1
    assert "no crawl 999999999" in batch["crawls"][0]["error"]
    assert batch["crawls"][1]["changed"] is False and batch["changed"] is False
    by_org = replay_many(engine, s, organization_id=org, write=False)
    assert [c["crawl_id"] for c in by_org["crawls"]] == [crawl_id]


def test_replay_all_command(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    s, result = crawled(engine, tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(f'[rawstore]\nroot = "{(tmp_path / "raw").as_posix()}"\n')
    monkeypatch.setenv("GTM__DATABASE__URL", TEST_DATABASE_URL)
    argv = ["--replay-all", "--organization", str(result["organization_id"]),
            "--config", str(config), "--fail-on-change"]  # fmt: skip
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["crawls"] == 1
    assert main(["--replay", "999999999", "--config", str(config)]) == 2


def test_migration_0014_downgrade_and_upgrade(engine: Engine) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "0013")
    assert "trigger" not in {c["name"] for c in inspect(engine).get_columns("extraction")}
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        rows = conn.execute(select(Extraction.trigger, Extraction.versions)).all()
    assert rows and set(rows) == {("crawl", None)}  # existing runs: versions unknown
