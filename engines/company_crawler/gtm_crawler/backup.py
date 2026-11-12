"""Backup, restore and integrity verification (spec sections 30 and 35; Appendix B phase 17).

PostgreSQL is the source of truth; raw documents live in the content-addressed
raw store (``rawstore.py``). A backup is a directory::

    <dir>/db.dump         pg_dump -Fc of the database (one consistent snapshot)
    <dir>/raw/aa/bb/*.gz  the filesystem raw store's objects (filesystem backend only)
    <dir>/manifest.json   written last: a backup without it is incomplete

Order: the database is dumped *before* the raw store is copied. Raw documents
are written (and fsynced) before any row cites them, so every digest in the
dump's snapshot is already in the store when the copy starts; copying the store
first would let rows committed in between cite documents the copy missed.
Retention (``gtm-crawl --prune``) must not run during a backup: it could delete
an uncited document the dump still lists as present.

For the ``s3`` backend nothing is copied: protect the bucket with versioning
(and replication) instead; see docs/disaster-recovery.md.

``pg_dump``/``pg_restore`` are external binaries. ``GTM_PG_COMMAND_PREFIX`` (or
``--pg-prefix``) is put in front of them, e.g. ``docker exec -i -e PGPASSWORD
<container>`` to run the container's binaries; the database URL is then
resolved inside the container. The password goes through ``PGPASSWORD``, never
the command line.

Restore: ``pg_restore`` into an empty database, copy the raw objects back,
``alembic upgrade head``, then ``verify``.

``verify`` (also ``gtm-crawl --verify``) is read-only: every raw document the
database records as present must be in the store with a matching SHA-256 (a
cited one, by evidence or a robots snapshot, must never have been pruned), and
every non-UNKNOWN field assessment must trace to observations whose evidence
sits on a page whose retrieval has a raw artifact.

    python -m gtm_crawler.backup backup --out DIR [--config FILE]
    python -m gtm_crawler.backup restore DIR [--config FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from . import __version__
from .config import ConfigError, Settings, load_settings, secret_values
from .db import make_engine
from .db.migrate import migration_head, upgrade_head
from .log import configure_logging
from .rawstore import RawStoreBackend, RawStoreError, make_rawstore
from .versions import current_versions

MANIFEST_FORMAT = "1"
SAMPLES = 20  # problems listed per kind; all are counted

# Raw documents with whether the database still holds them and whether anything cites them.
_ARTIFACTS = text("""
SELECT a.sha256, a.pruned_at IS NOT NULL AS pruned,
       EXISTS (SELECT 1 FROM "fetch" f JOIN page p ON p.fetch_id = f.id
               JOIN evidence e ON e.page_id = p.id WHERE f.raw_sha256 = a.sha256)
       OR EXISTS (SELECT 1 FROM robots_snapshot r WHERE r.raw_sha256 = a.sha256) AS cited
FROM artifact a
""")
# Links of a non-UNKNOWN assessment that do not reach a raw artifact. Evidence,
# page and fetch are foreign keys; observation_ids and fetch.raw_sha256 are not enforced.
_BROKEN_LINEAGE = text("""
SELECT fa.id AS assessment_id, fa.field, fa.state, x.observation_id,
       CASE WHEN o.id IS NULL THEN 'missing_observation' ELSE 'no_raw_artifact' END AS kind
FROM field_assessment fa
CROSS JOIN LATERAL unnest(fa.observation_ids) AS x(observation_id)
LEFT JOIN observation o ON o.id = x.observation_id
LEFT JOIN evidence e ON e.id = o.evidence_id
LEFT JOIN page p ON p.id = e.page_id
LEFT JOIN "fetch" f ON f.id = p.fetch_id
WHERE fa.state <> 'UNKNOWN' AND (o.id IS NULL OR f.raw_sha256 IS NULL)
""")
_LINEAGE_COUNTS = text("""
SELECT count(*) FILTER (WHERE state <> 'UNKNOWN'),
       count(*) FILTER (WHERE state NOT IN ('UNKNOWN', 'NOT_APPLICABLE', 'CONFLICTING')
                        AND (value IS NULL OR cardinality(observation_ids) = 0)),
       coalesce(sum(cardinality(observation_ids)) FILTER (WHERE state <> 'UNKNOWN'), 0)
FROM field_assessment
""")


class BackupError(Exception):
    pass


def verify(engine: Engine, store: RawStoreBackend) -> dict[str, Any]:
    """Integrity of the database and raw store (see the module docstring); read-only.

    Rows are streamed in one REPEATABLE READ, READ ONLY transaction (one snapshot);
    each raw document is read and hashed once."""
    started = time.perf_counter()
    problems: Counter[str] = Counter()
    samples: dict[str, list[Any]] = defaultdict(list)

    def problem(kind: str, item: Any) -> None:
        problems[kind] += 1
        if len(samples[kind]) < SAMPLES:
            samples[kind].append(item)

    checked: Counter[str] = Counter()
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout = 0"))  # a full scan, by design
        for sha, pruned, cited in conn.execution_options(yield_per=500).execute(_ARTIFACTS):
            checked["artifacts"] += 1
            checked["cited_artifacts"] += cited
            if pruned:
                checked["pruned_artifacts"] += 1
                if cited:
                    problem("cited_artifact_pruned", sha)
                continue
            try:
                store.get(sha)  # decompresses and checks the SHA-256
            except RawStoreError as exc:
                missing = str(exc).startswith("not found")
                problem(("missing_cited" if cited else "missing") if missing else "corrupt", sha)
            except (OSError, EOFError, zlib.error) as exc:  # not valid gzip
                problem("corrupt", f"{sha}: {type(exc).__name__}")
            else:
                checked["verified_artifacts"] += 1
        assessed, valueless, refs = conn.execute(_LINEAGE_COUNTS).one()
        checked.update(assessments_not_unknown=assessed, observation_refs=refs)
        if valueless:
            problem("value_without_observations", valueless)
        for row in conn.execution_options(yield_per=500).execute(_BROKEN_LINEAGE):
            problem(row.kind, {k: row._mapping[k] for k in
                               ("assessment_id", "field", "state", "observation_id")})  # fmt: skip
    return {"status": "failed" if problems else "completed", "checked": dict(checked),
            "problems": dict(problems), "samples": dict(samples),
            "seconds": round(time.perf_counter() - started, 3)}  # fmt: skip


# --- pg_dump / pg_restore ------------------------------------------------------------


def pg_command(prefix: str | None = None) -> list[str]:
    """The command prefix for the PostgreSQL client binaries (see the module docstring)."""
    if prefix is None:
        prefix = os.environ.get("GTM_PG_COMMAND_PREFIX", "")
    return shlex.split(prefix)


def _libpq(url: str) -> tuple[str, dict[str, str]]:
    """A libpq connection URI without the password, and the environment carrying it."""
    u = make_url(url)
    env = {**os.environ}
    if u.password is not None:
        env["PGPASSWORD"] = str(u.password)
    conninfo = u.set(drivername="postgresql", password=None)
    return conninfo.render_as_string(hide_password=False), env


def _run(prefix: list[str], tool: str, args: list[str], env: dict[str, str],
         **kwargs: Any) -> None:  # fmt: skip
    proc = subprocess.run([*prefix, tool, *args], env=env, stderr=subprocess.PIPE,  # noqa: S603
                          check=False, **kwargs)  # fmt: skip
    if proc.returncode:
        err = proc.stderr.decode(errors="replace").strip()
        raise BackupError(f"{tool} exited {proc.returncode}: {err[-2000:]}")


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def copy_objects(src: Path, dst: Path) -> dict[str, int]:
    """Copy raw-store objects (``aa/bb/<sha256>.gz``) not already in ``dst``.

    Content addressing makes this incremental and idempotent: a name present in
    both holds the same bytes (``verify`` checks that)."""
    objects = copied = size = 0
    for path in src.glob("*/*/*.gz"):
        objects += 1
        size += path.stat().st_size
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, target)  # never a partial file under a valid name
        copied += 1
    return {"objects": objects, "copied": copied, "bytes": size}


def backup(settings: Settings, out: Path, prefix: str | None = None) -> dict[str, Any]:
    """Dump the database, then copy the raw store, into ``out``; return the manifest."""
    if (out / "manifest.json").exists():
        raise BackupError(f"{out} already holds a backup")
    out.mkdir(parents=True, exist_ok=True)
    url = settings.database.url
    conninfo, env = _libpq(url)
    engine = make_engine(settings.database)
    try:
        with engine.connect() as conn:
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            db_bytes = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
    finally:
        engine.dispose()
    pg = pg_command(prefix)
    version = subprocess.run([*pg, "pg_dump", "--version"], capture_output=True, text=True,  # noqa: S603
                             check=False).stdout.strip()  # fmt: skip
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    dump = out / "db.dump"
    with dump.open("wb") as fh:
        _run(pg, "pg_dump", ["--format=custom", "--dbname", conninfo], env, stdout=fh)
    dump_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    if settings.rawstore.backend == "filesystem":
        copied = copy_objects(Path(settings.rawstore.root), out / "raw")
        raw: dict[str, Any] = {"backend": "filesystem", **copied}
    else:
        raw = {"backend": settings.rawstore.backend, "copied": False,
               "note": "protected by bucket versioning/replication, not copied"}  # fmt: skip
    raw_seconds = time.perf_counter() - t1
    u = make_url(url)
    manifest = {
        "format": MANIFEST_FORMAT,
        # The dump's snapshot is taken as pg_dump starts: this is the recovery point.
        "snapshot_started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "database": {"name": u.database, "host": u.host, "port": u.port,
                     "size_bytes": db_bytes, "alembic_revision": revision},
        "app": {"crawler": __version__, "migration_head": migration_head(),
                "versions": current_versions(settings)},
        "dump": {"file": dump.name, "bytes": dump.stat().st_size, "sha256": _sha256(dump),
                 "pg_dump": version, "seconds": round(dump_seconds, 3)},
        "raw": {**raw, "seconds": round(raw_seconds, 3)},
    }  # fmt: skip
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8",
                                       newline="\n")  # fmt: skip
    return manifest


def restore(settings: Settings, src: Path, prefix: str | None = None) -> dict[str, Any]:
    """Restore the backup in ``src`` into the configured (empty) database and raw store,
    migrate to this code's head and verify. Raises ``BackupError`` before writing
    anything if the backup is incomplete or damaged or the database is not empty."""
    try:
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackupError(f"{src}: no manifest.json (incomplete backup)") from exc
    dump = src / manifest["dump"]["file"]
    if _sha256(dump) != manifest["dump"]["sha256"]:
        raise BackupError(f"{dump}: SHA-256 does not match the manifest")
    engine = make_engine(settings.database)
    try:
        with engine.connect() as conn:
            tables = conn.execute(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            )).scalar_one()  # fmt: skip
        if tables:
            raise BackupError("the target database is not empty; restore needs an empty one")
        conninfo, env = _libpq(settings.database.url)
        t0 = time.perf_counter()
        with dump.open("rb") as fh:
            args = ["--no-owner", "--no-acl", "--single-transaction", "--dbname", conninfo]
            _run(pg_command(prefix), "pg_restore", args, env, stdin=fh)
        timings = {"pg_restore": time.perf_counter() - t0}
        t1 = time.perf_counter()
        raw: dict[str, Any] = {"backend": settings.rawstore.backend}
        if settings.rawstore.backend == "filesystem" and (src / "raw").is_dir():
            raw.update(copy_objects(src / "raw", Path(settings.rawstore.root)))
        timings["raw"] = time.perf_counter() - t1
        t2 = time.perf_counter()
        upgrade_head(settings.database.url)
        timings["migrate"] = time.perf_counter() - t2
        t3 = time.perf_counter()
        report = verify(engine, make_rawstore(settings.rawstore))
        timings["verify"] = time.perf_counter() - t3
    finally:
        engine.dispose()
    return {"status": report["status"], "backup": manifest["snapshot_started_at"], "raw": raw,
            "seconds": {k: round(v, 3) for k, v in timings.items()}, "verify": report}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gtm_crawler.backup", description="Back up or restore the crawler's data."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("backup", help="dump the database, then copy the raw store")
    b.add_argument("--out", type=Path, required=True, help="new backup directory")
    r = sub.add_parser("restore", help="restore into an empty database, then verify")
    r.add_argument("dir", type=Path, help="backup directory")
    for p in (b, r):
        p.add_argument("--config", type=Path, help="config file (default: $GTM_CONFIG)")
        p.add_argument("--pg-prefix", help="command prefix for pg_dump/pg_restore "
                       "(default: $GTM_PG_COMMAND_PREFIX)")  # fmt: skip
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        configure_logging(settings.logging, secrets=secret_values(settings))
        if args.action == "backup":
            result = backup(settings, args.out, args.pg_prefix)
        else:
            result = restore(settings, args.dir, args.pg_prefix)
    except (ConfigError, BackupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
