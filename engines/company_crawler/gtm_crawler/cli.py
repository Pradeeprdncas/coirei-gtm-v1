"""Command-line entry point.

* ``gtm-crawl <url>``: run the whole pipeline for a company in this process.
* ``gtm-crawl --reextract <crawl id>``: re-run extraction and assessment over a
  stored crawl and print the dossier (a write replay, recorded as trigger 'replay').
* ``gtm-crawl --replay <crawl id> [--write]``: replay a stored crawl with the
  current parser and extractors and print the comparison with its current run
  (``replay.py``); a dry run unless ``--write``.
* ``gtm-crawl --replay-all [--organization ID] [--write]``: the same for every
  finished crawl. Exit status 1 if a crawl could not be replayed, or, with
  ``--fail-on-change``, if any observation or field state changed (a CI gate).
* ``gtm-crawl --prune``: apply the raw-data retention policy.
* ``gtm-crawl --verify``: read-only integrity check of the database and raw store
  (``backup.verify``); exit status 1 if any problem is found.

For a service deployment, submit jobs through ``gtm-api`` and run them on
``gtm-worker`` processes instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from .backup import verify
from .config import ConfigError, Settings, load_settings, secret_values
from .db import make_engine
from .db.evidence import EvidenceStore
from .db.models import Crawl
from .db.store import CrawlStore
from .dossier import build_dossier
from .log import configure_logging
from .market.ads import cited_digests as ad_digests
from .market.store import MarketStore
from .pipeline import JobUnavailable, reextract, run_pipeline
from .profile import build_profile
from .rawstore import make_rawstore
from .replay import ReplayError, replay_crawl, replay_many
from .urls import InvalidUrl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gtm-crawl", description="Crawl a company website and record what it publishes."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("url", nargs="?", help="company website, e.g. https://example.com")
    target.add_argument(
        "--reextract", type=int, metavar="CRAWL_ID",
        help="re-run extraction and assessment over a stored crawl without fetching",
    )  # fmt: skip
    target.add_argument(
        "--replay", type=int, metavar="CRAWL_ID",
        help="replay a stored crawl with the current extractors and compare (no fetching)",
    )  # fmt: skip
    target.add_argument(
        "--replay-all", action="store_true", help="replay every finished crawl and compare"
    )
    target.add_argument(
        "--prune", action="store_true",
        help="delete raw documents past rawstore.retention_days that no evidence cites",
    )  # fmt: skip
    target.add_argument(
        "--verify", action="store_true",
        help="check raw documents against their SHA-256 and the lineage of every field",
    )  # fmt: skip
    parser.add_argument(
        "--config", type=Path, help="config file (default: $GTM_CONFIG or config.toml)"
    )
    parser.add_argument(
        "--write", action="store_true",
        help="with --replay/--replay-all: record the replay as a new extraction run",
    )  # fmt: skip
    parser.add_argument(
        "--fail-on-change", action="store_true",
        help="with --replay/--replay-all: exit 1 if any observation or field state changed",
    )  # fmt: skip
    parser.add_argument(
        "--organization", type=int, metavar="ID", help="with --replay-all: only this company"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
        configure_logging(settings.logging, secrets=secret_values(settings))
        engine = make_engine(settings.database)
        if args.prune:
            result = prune(engine, settings)
        elif args.verify:
            result = verify(engine, make_rawstore(settings.rawstore))
        elif args.reextract is not None:
            result = _reextract(engine, settings, args.reextract)
        elif args.replay is not None:
            result = replay_crawl(engine, settings, args.replay, write=args.write)
        elif args.replay_all:
            result = replay_many(engine, settings, organization_id=args.organization,
                                 write=args.write)  # fmt: skip
        else:
            result = asyncio.run(run_pipeline(args.url, settings, engine))
    except (ConfigError, InvalidUrl, JobUnavailable, ReplayError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, default=str))
    if args.replay is not None or args.replay_all:
        failed = result.get("totals", {}).get("failed", 0)
        return 1 if failed or (args.fail_on_change and result["changed"]) else 0
    return 1 if result["status"] == "failed" else 0


def prune(engine: Engine, settings: Settings) -> dict[str, Any]:
    """Retention policy (approved 2026-09-29): keep what evidence cites and robots
    snapshots forever; delete other raw documents older than retention_days. What
    stored market reports cite counts as evidence too, and so do stored ad captures
    and their screenshots."""
    store = make_rawstore(settings.rawstore)
    keep = EvidenceStore(engine, store, settings.sources).evidence_raw_digests()
    keep |= MarketStore(engine).cited_digests()
    keep |= ad_digests(engine)
    days = settings.rawstore.retention_days
    removed = store.prune(keep, days)
    CrawlStore(engine).mark_pruned(removed)
    return {"status": "completed", "removed": len(removed), "kept": len(keep),
            "older_than_days": days}  # fmt: skip


def _reextract(engine: Engine, settings: Settings, crawl_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        crawl = conn.execute(select(Crawl).where(Crawl.id == crawl_id)).one_or_none()
    if crawl is None:
        raise ConfigError(f"no crawl {crawl_id}")
    result: dict[str, Any] = {"crawl_id": crawl_id, "status": crawl.status}
    if crawl.status in ("failed", "running", "paused"):
        return result  # nothing complete to extract from
    result.update(reextract(engine, settings, crawl_id, trigger="replay"))
    result["profile"] = build_profile(engine, settings, crawl.organization_id)
    result["dossier"] = build_dossier(engine, crawl.organization_id)
    return result


if __name__ == "__main__":
    sys.exit(main())
