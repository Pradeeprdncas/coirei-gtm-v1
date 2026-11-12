"""Phase 17 disaster recovery (spec sections 30 and 35): the restore drill at small
scale against the docker PostgreSQL, then ``verify`` and ``restore`` refusing
damage. Skipped unless ``pg_dump`` runs through ``GTM_PG_COMMAND_PREFIX``
(default: the compose container ``scraper10-postgres-1``)."""

import asyncio
import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from gtm_crawler.backup import BackupError, pg_command, restore, verify
from gtm_crawler.cli import main
from gtm_crawler.drill import run_drill
from gtm_crawler.loadtest import base_settings, scratch_database
from gtm_crawler.rawstore import RawStore

from .conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.db

PREFIX = os.environ.get(
    "GTM_PG_COMMAND_PREFIX", "docker exec -i -e PGPASSWORD scraper10-postgres-1"
)
# GTM_DRILL_DATABASE_URL: a drill database of its own (parallel runs, the acceptance runner).
DRILL_URL = make_url(
    os.environ.get("GTM_DRILL_DATABASE_URL")
    or make_url(TEST_DATABASE_URL).set(database="gtm_crawler_drill_pytest")
)
DRILL_URL_STR = DRILL_URL.render_as_string(hide_password=False)


def _pg_dump_available() -> bool:
    argv = [*pg_command(PREFIX), "pg_dump", "--version"]
    if not shutil.which(argv[0]):
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=30, check=False).returncode == 0  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def drilled(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _pg_dump_available():
        pytest.skip(f"pg_dump not runnable via {PREFIX!r}; set GTM_PG_COMMAND_PREFIX")
    work = tmp_path_factory.mktemp("drill")
    try:
        result = asyncio.run(run_drill(30, DRILL_URL_STR, PREFIX, work))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable ({exc.orig}); run `docker compose up -d`")
    return result


def test_restore_drill_passes(drilled: dict) -> None:
    failed = [c for c in drilled["checks"] if not c["ok"]]
    assert drilled["passed"] and not failed, failed
    m = drilled["measurements"]
    assert m["backup"]["dump_bytes"] > 0 and m["backup"]["raw_objects"] > 0
    assert m["restore_seconds"] > 0


def test_verify_reports_damage_and_cli_exits_nonzero(
    drilled: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    raw = Path(drilled["workdir"]) / "raw"
    store = RawStore(raw)
    engine = create_engine(DRILL_URL)
    with engine.begin() as conn:
        cited = conn.execute(text(
            "SELECT f.raw_sha256 FROM \"fetch\" f JOIN page p ON p.fetch_id = f.id"
            " JOIN evidence e ON e.page_id = p.id LIMIT 1"
        )).scalar_one()  # fmt: skip
        other = conn.execute(text(
            "SELECT sha256 FROM artifact WHERE sha256 <> :s AND pruned_at IS NULL LIMIT 1"
        ), {"s": cited}).scalar_one()  # fmt: skip
        org = conn.execute(text("SELECT min(id) FROM organization")).scalar_one()
        # A valued assessment citing an observation that does not exist.
        conn.execute(text(
            "INSERT INTO field_assessment (organization_id, field, state, value, confidence,"
            " observation_ids, method_version) VALUES (:o, 'founded_year', 'SUPPORTED',"
            " '{\"value\": 1999}', 0.9, ARRAY[999999999]::bigint[], 'drill-test')"
        ), {"o": org})  # fmt: skip
    store._path(cited).unlink()
    store._path(other).write_bytes(gzip.compress(b"tampered", mtime=0))
    try:
        report = verify(engine, store)
    finally:
        engine.dispose()
    assert report["status"] == "failed"
    assert report["problems"] == {"missing_cited": 1, "corrupt": 1, "missing_observation": 1}
    assert report["samples"]["missing_cited"] == [cited]

    config = tmp_path / "config.toml"
    config.write_text(
        '[identity]\nuser_agent_contact = "ops@example.com"\n'
        f'[rawstore]\nroot = "{raw.as_posix()}"\n'
    )
    monkeypatch.setenv("GTM__DATABASE__URL", DRILL_URL_STR)
    assert main(["--verify", "--config", str(config)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]["corrupt"] == 1


def test_restore_refuses_a_damaged_backup_or_a_nonempty_database(
    drilled: dict, tmp_path: Path
) -> None:
    backup_dir = Path(drilled["workdir"]) / "backup"
    settings = base_settings(DRILL_URL_STR, str(tmp_path / "raw"))
    with pytest.raises(BackupError, match="not empty"):
        restore(settings, backup_dir, PREFIX)  # the drill database holds the restored data
    damaged = tmp_path / "damaged"
    shutil.copytree(backup_dir, damaged)
    with (damaged / "db.dump").open("ab") as fh:
        fh.write(b"x")
    with pytest.raises(BackupError, match="SHA-256"):
        restore(settings, damaged, PREFIX)
    with pytest.raises(BackupError, match="incomplete"):
        restore(settings, tmp_path / "raw", PREFIX)
    assert not (tmp_path / "raw").exists()  # nothing written


def test_scratch_database_refuses_non_drill_names() -> None:
    with pytest.raises(ValueError, match="must contain 'drill'"):
        scratch_database("postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler", "drill")
