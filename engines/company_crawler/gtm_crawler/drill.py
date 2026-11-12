"""Automated restore drill (spec sections 30 and 35; Appendix B phase 17).

Against a simulated web (``loadtest`` helpers) and a scratch database (its name
must contain "drill"; dropped and recreated)::

    python -m gtm_crawler.drill [--pages 200] [--pg-prefix "docker exec -i -e PGPASSWORD C"]

1. crawls company A to completion and records its dossier and an integrity check;
2. starts crawl B in a worker process and kills it a third of the way in, so
   URLs stay leased, as after a crash;
3. backs up (``backup.backup``: pg_dump, then the raw store);
4. drops the database and deletes the raw store;
5. restores (``backup.restore``: pg_restore, raw store, ``alembic upgrade head``,
   ``verify``);
6. checks A's dossier is identical (only ``generated_at``, the build time, is
   ignored), a replay of A's crawl changes nothing, and crawl B resumes and
   completes with none of the URLs leased at the backup lost;
7. verifies again after the resumed crawl.

Prints PASS/FAIL per check and the measurements (backup and restore time,
sizes) that docs/disaster-recovery.md records as the measured RTO baseline.
Exit status 1 if any check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text

from .backup import backup, restore, verify
from .dossier import build_dossier
from .jobs import JobStore
from .loadtest import (
    FINAL_URLS,
    IN_FLIGHT,
    Ctx,
    SimWeb,
    Site,
    crash_web,
    doomed_worker,
    scratch_database,
)
from .rawstore import make_rawstore
from .replay import replay_crawl

DEFAULT_DATABASE_URL = os.environ.get(
    "GTM_DRILL_DATABASE_URL",
    "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_drill_scratch",
)
VOLATILE = ("generated_at",)  # dossier keys that are the time of building it


def _stable(dossier: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in dossier.items() if k not in VOLATILE}


def _crawl_of(observer: Engine, job_id: int) -> tuple[int, int]:
    with observer.connect() as conn:
        row = conn.execute(text(
            "SELECT id, organization_id FROM crawl WHERE job_id = :j ORDER BY id LIMIT 1"
        ), {"j": job_id}).one()  # fmt: skip
    return row.id, row.organization_id


def _url_states(observer: Engine, crawl_id: int) -> tuple[int, list[int]]:
    """Processed URLs and the URLs in flight (leased or further) of a crawl."""
    with observer.connect() as conn:
        processed = conn.execute(text(
            "SELECT count(*) FROM crawl_url WHERE crawl_id = :c AND state = 'processed'"
        ), {"c": crawl_id}).scalar_one()  # fmt: skip
        held = list(conn.execute(text(
            "SELECT url_id FROM crawl_url WHERE crawl_id = :c AND state = ANY(:s)"
        ), {"c": crawl_id, "s": IN_FLIGHT}).scalars())  # fmt: skip
    return processed, held


async def run_drill(
    pages: int = 200, database_url: str = DEFAULT_DATABASE_URL, pg_prefix: str | None = None,
    workdir: Path | None = None,
) -> dict[str, Any]:  # fmt: skip
    await asyncio.to_thread(scratch_database, database_url, "drill")
    work = workdir or Path(tempfile.mkdtemp(prefix="gtm-drill-"))
    raw, saved = work / "raw", work / "backup"
    observer = create_engine(database_url, pool_size=2, max_overflow=0)
    ctx = Ctx("restore_drill", pages, database_url, str(raw), observer)
    settings = ctx.settings()
    engine = ctx.engine(settings, settings.crawl.concurrency + 2)
    jobs = JobStore(engine)
    m: dict[str, Any] = {"pages_per_site": pages}

    # 1. Company A, crawled to completion.
    site_a = Site(f"{ctx.prefix}-a.example", "Drillco Systems", pages)
    job_a, _ = await asyncio.to_thread(jobs.create, site_a.root, settings.urls)
    m["crawl_a_seconds"] = round(await ctx.drain(settings, engine, 1, 1, SimWeb([site_a])), 2)
    crawl_a, org_a = _crawl_of(observer, job_a.id)
    before = await asyncio.to_thread(build_dossier, engine, org_a)
    store = make_rawstore(settings.rawstore)
    ctx.check("integrity before backup",
              (v := await asyncio.to_thread(verify, engine, store))["status"] == "completed",
              v["problems"] or v["checked"])  # fmt: skip

    # 2. Crawl B, its worker killed mid-run.
    site_b, web_b = crash_web(ctx.prefix, pages)
    job_b, _ = await asyncio.to_thread(jobs.create, site_b.root, settings.urls)
    child = doomed_worker(ctx, pages)
    deadline = time.monotonic() + 300
    crawl_b = None
    while child.poll() is None and time.monotonic() < deadline:
        with observer.connect() as conn:
            crawl_b = conn.execute(text("SELECT max(id) FROM crawl WHERE job_id = :j"),
                                   {"j": job_b.id}).scalar()  # fmt: skip
        if crawl_b and (await asyncio.to_thread(_url_states, observer, crawl_b))[0] >= pages // 3:
            break
        await asyncio.sleep(0.05)
    child.kill()  # leases stay held, as after a crash
    child.wait()

    # 3. Backup.
    for e in ctx.engines:
        e.dispose()
    t0 = time.perf_counter()
    manifest = await asyncio.to_thread(backup, settings, saved, pg_prefix)
    m["backup_seconds"] = round(time.perf_counter() - t0, 3)
    m["backup"] = {"dump_seconds": manifest["dump"]["seconds"],
                   "raw_seconds": manifest["raw"]["seconds"],
                   "database_bytes": manifest["database"]["size_bytes"],
                   "dump_bytes": manifest["dump"]["bytes"],
                   "raw_objects": manifest["raw"]["objects"],
                   "raw_bytes": manifest["raw"]["bytes"]}  # fmt: skip

    # 4. Disaster: the database and the raw store are gone.
    observer.dispose()
    await asyncio.to_thread(scratch_database, database_url, "drill", migrate=False)
    shutil.rmtree(raw)

    # 5. Restore.
    t1 = time.perf_counter()
    restored = await asyncio.to_thread(restore, settings, saved, pg_prefix)
    m["restore_seconds"] = round(time.perf_counter() - t1, 3)
    m["restore"] = restored["seconds"]
    ctx.check("integrity after restore", restored["status"] == "completed",
              restored["verify"]["problems"] or restored["verify"]["checked"])  # fmt: skip

    # 6. Same dossier, replay unchanged, interrupted crawl resumes.
    ctx.observer = observer = create_engine(database_url, pool_size=2, max_overflow=0)
    engine = ctx.engine(settings, settings.crawl.concurrency + 2)
    after = await asyncio.to_thread(build_dossier, engine, org_a)
    ctx.check("dossier identical after restore", _stable(before) == _stable(after),
              f"{len(json.dumps(_stable(after), default=str))} bytes compared, "
              f"ignoring {list(VOLATILE)}")  # fmt: skip
    replay = await asyncio.to_thread(replay_crawl, engine, settings, crawl_a, write=False)
    ctx.check("replay of the restored crawl unchanged",
              not replay["changed"] and not replay["unreadable_pages"],
              {k: len(replay["observations"][k]) for k in ("added", "removed", "changed")}
              | {"observations": replay["observations"]["after"],
                 "unreadable_pages": len(replay["unreadable_pages"])})  # fmt: skip
    processed, held = (await asyncio.to_thread(_url_states, observer, crawl_b)
                       if crawl_b else (0, []))  # fmt: skip
    ctx.check("backup taken mid-crawl", 0 < processed < pages and bool(held),
              f"{processed} processed, {len(held)} URLs leased in the backup")  # fmt: skip
    t2 = time.perf_counter()
    await ctx.drain(settings, engine, 1, 2, web_b)
    m["resume_seconds"] = round(time.perf_counter() - t2, 2)
    with observer.connect() as conn:
        lost = conn.execute(text(
            "SELECT count(*) FROM crawl_url WHERE crawl_id = :c AND url_id = ANY(:ids)"
            " AND NOT state = ANY(:final)"
        ), {"c": crawl_b, "ids": held, "final": FINAL_URLS}).scalar_one()  # fmt: skip
    states = await asyncio.to_thread(ctx.jobs)
    ctx.check("interrupted crawl resumed and completed", states == {"completed": 2}, states)
    ctx.check("no leased URL lost", lost == 0, f"{lost} of {len(held)} still open")

    # 7. Integrity after the resumed crawl.
    v = await asyncio.to_thread(verify, engine, make_rawstore(settings.rawstore))
    ctx.check("integrity after resume", v["status"] == "completed", v["problems"] or v["checked"])
    m["verify_after_resume"] = v["checked"] | {"seconds": v["seconds"]}
    for e in [*ctx.engines, observer]:
        e.dispose()
    return {"passed": all(c["ok"] for c in ctx.checks), "measurements": m,
            "checks": ctx.checks, "workdir": str(work)}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gtm_crawler.drill", description="Back up, destroy, restore and check."
    )
    parser.add_argument("--pages", type=int, default=200, help="pages per simulated site")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL,
                        help="scratch database, dropped and recreated (name must contain "
                             "'drill')")  # fmt: skip
    parser.add_argument("--pg-prefix", help="command prefix for pg_dump/pg_restore "
                        "(default: $GTM_PG_COMMAND_PREFIX)")  # fmt: skip
    parser.add_argument("--out", type=Path, help="write the result as JSON here")
    args = parser.parse_args(argv)
    logging.getLogger("gtm_crawler").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="gtm-drill-", ignore_cleanup_errors=True) as work:
        result = asyncio.run(run_drill(args.pages, args.database_url, args.pg_prefix, Path(work)))
    for check in result["checks"]:
        print(f"{'PASS' if check['ok'] else 'FAIL'}  {check['name']}: {check['detail']}")
    print(json.dumps(result["measurements"], indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8",
                            newline="\n")  # fmt: skip
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
