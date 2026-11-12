"""Running market research: on a worker (``process_run``) or in this process (``gtm-market``).

``gtm-worker`` claims a queued run when no crawl job is waiting
(``worker.run_worker``) and runs it here: the lease is renewed every
``worker.lease_seconds / 3`` with the latest progress, a cancel request
stops collection at the next page, and a worker shutdown hands the run back.

``gtm-market request.json`` runs one request in this process and prints the
report (``--markdown`` for the readable export). It needs no database unless
the request names a crawled ``company_id`` or ``--save`` is given.

``gtm-market --verify RESEARCH_ID`` re-checks a stored report's quotes against
the stored artifacts before it is delivered (``verify_stored``).

``gtm-market --import-ads ads.json --operator NAME [--screenshots DIR]`` stores
competitor ads an operator captured from the public ad libraries
(``market.ads``); a product request with ``include_ads`` then reports them,
for its known competitors and its ``company_id``'s dossier competitors.
``gtm-market --ad-checklist COMPANY_ID [--country IN] [--markdown]`` lists a
crawled company's competitors with search links to open by hand, and what is
already captured.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from ..config import ConfigError, Settings, load_settings, secret_values
from ..db import make_engine
from ..dossier import build_dossier
from ..fetch.http import build_user_agent
from ..log import configure_logging
from ..rawstore import make_rawstore
from ..search import make_search
from .ads import (
    AdImportError,
    capture_checklist,
    checklist_markdown,
    dossier_competitors,
    import_ads,
    load_ads,
)
from .engine import run_research
from .llm import ChatModel
from .models import Report, ResearchRequest, RunStatus, utcnow
from .ocr import Ocr
from .report import to_markdown
from .store import MarketStore
from .verify import VERIFIED, QuoteCheck, verify_report

log = logging.getLogger(__name__)


async def research(
    req: ResearchRequest,
    settings: Settings,
    engine: Engine | None,
    *,
    progress: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None,
    stop: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> Report:
    """``run_research`` with the configured providers (search, dossier, cache, model)."""
    search = make_search(settings.search, search_transport)
    model = ChatModel(settings.market, llm_transport) if settings.market.llm_model else None
    store = MarketStore(engine) if engine is not None else None
    try:
        return await run_research(
            req, settings, search=search, transport=transport,
            dossier=(lambda cid: build_dossier(engine, cid)) if engine is not None else None,
            ads=(lambda domains: load_ads(engine, domains)) if engine is not None else None,
            prior=store.latest_report if store is not None else None,
            narrator=model.narrate if model is not None else None,
            progress=progress, stop=stop,
        )  # fmt: skip
    finally:
        if search is not None:
            await search.aclose()
        if model is not None:
            await model.aclose()


async def process_run(
    settings: Settings,
    engine: Engine,
    store: MarketStore,
    row: Any,
    owner: str,
    shutdown: asyncio.Event | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    search_transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Run one claimed research run to a final state (or hand it back). Returns what happened."""
    lease = settings.worker.lease_seconds
    stop = asyncio.Event()
    state: dict[str, Any] = {"stage": "starting"}
    lost = False

    async def finish(report: Report | None, status: RunStatus, error: str | None = None) -> str:
        """Store the outcome; a database error leaves the run to be retried, not the worker
        stopped (the lease expires and another worker claims it)."""
        try:
            done = await asyncio.to_thread(store.finish, row.id, owner, report, status, error)
        except DBAPIError as exc:
            log.error("market.finish_failed", extra={"research_id": row.id, "error": str(exc)})
            return "interrupted"
        if not done:
            log.warning("market.lease_lost", extra={"research_id": row.id, "owner": owner})
            return "lost"
        return status

    try:
        req = ResearchRequest.model_validate(row.request)
    except ValidationError as exc:
        return await finish(None, RunStatus.FAILED, f"invalid stored request: {exc}"[:1000])

    def progress(stage: str, detail: dict[str, Any]) -> None:
        state.clear()
        state.update(stage=stage, **{k: v for k, v in detail.items() if k != "url"})

    async def heartbeat() -> None:
        nonlocal lost
        while True:
            await asyncio.sleep(lease / 3)
            try:
                directive = await asyncio.to_thread(store.heartbeat, row.id, owner, lease,
                                                    dict(state))  # fmt: skip
            except DBAPIError as exc:
                log.warning("market.heartbeat_failed", extra={"research_id": row.id,
                                                              "error": str(exc)})  # fmt: skip
                continue
            if directive is None:
                lost = True
            if directive != "continue":
                stop.set()
                return

    async def watch_shutdown() -> None:
        if shutdown is not None:
            await shutdown.wait()
            stop.set()

    beat = asyncio.create_task(heartbeat())
    watcher = asyncio.create_task(watch_shutdown())
    try:
        report = await research(req, settings, engine, progress=progress, stop=stop,
                                transport=transport, search_transport=search_transport)  # fmt: skip
    except (asyncio.CancelledError, KeyboardInterrupt):
        with contextlib.suppress(Exception):
            await asyncio.to_thread(store.release, row.id, owner)
        raise
    except DBAPIError:
        return "interrupted"  # the lease expires and another worker retries it
    except Exception as exc:  # noqa: BLE001 - a run's failure must not stop the worker
        log.exception("market.run_failed", extra={"research_id": row.id})
        return await finish(None, RunStatus.FAILED, f"{type(exc).__name__}: {exc}"[:1000])
    finally:
        beat.cancel()
        watcher.cancel()
    if lost:
        return "lost"
    if shutdown is not None and shutdown.is_set() and report.status is RunStatus.CANCELLED:
        # Stopped for shutdown, not by request: hand it back.
        try:
            await asyncio.to_thread(store.release, row.id, owner)
        except DBAPIError as exc:
            log.error("market.release_failed", extra={"research_id": row.id, "error": str(exc)})
            return "interrupted"  # the lease expires and another worker claims it
        return "released"
    return await finish(report, report.status)


def verify_stored(store: MarketStore, settings: Settings, research_id: str) -> dict[str, Any]:
    """Re-check every evidence record of a stored report against its stored artifacts
    (``verify.verify_report``) and record the result beside the report.

    ``verdict``: ``verified`` only when every evidence record was checked and verified;
    ``failed`` when any quote is not where its record says (or its source changed);
    otherwise ``incomplete`` (checks that could not run: missing or expired artifacts,
    locators of a kind not re-checked here, such as OCR images and the crawled dossier).
    """
    row = store.get(research_id)
    if row is None:
        raise ConfigError(f"no market research {research_id}")
    out: dict[str, Any] = {"research_id": research_id, "checked_at": utcnow().isoformat()}
    if row.report is None:
        return {**out, "verdict": "incomplete", "reason": f"run is {row.status}: no report"}
    report = Report.model_validate(row.report)
    raw = make_rawstore(settings.rawstore)
    checks = asyncio.run(verify_report(report, raw, settings.market))
    urls = {s.source_id: s.url for s in report.sources}
    counts = Counter(c.status.value for c in checks.values())
    unchecked = [e for e in report.evidence if e.evidence_id not in checks]
    counts["unchecked"] = len(unchecked)
    cited = {s.source_id: s for s in report.sources
             if any(e.source_id == s.source_id for e in report.evidence)}  # fmt: skip
    missing = sorted({s.url for s in cited.values() for d in (s.raw_sha256, s.extracted_sha256)
                      if d and not raw.exists(d)})  # fmt: skip
    problems = [{"evidence_id": e.evidence_id, "status": c.status.value, "detail": c.detail,
                 "url": urls.get(e.source_id)}
                for e in report.evidence if (c := checks.get(e.evidence_id)) is not None
                and c.status not in VERIFIED]  # fmt: skip
    problems += [{"evidence_id": e.evidence_id, "status": "unchecked",
                  "detail": f"{e.locator.type if e.locator else 'no'} locator: not re-checked "
                            "here", "url": urls.get(e.source_id)} for e in unchecked]  # fmt: skip
    if counts[QuoteCheck.MISMATCH] or counts[QuoteCheck.SOURCE_CHANGED]:
        verdict = "failed"
    elif problems or not report.evidence:
        verdict = "incomplete"
    else:
        verdict = "verified"
    out.update(verdict=verdict, report_status=report.status.value,
               evidence=len(report.evidence), counts=dict(counts),
               missing_artifacts=missing, problems=problems)  # fmt: skip
    store.record_verification(research_id, out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gtm-market", description="Run industry or product market research in this process."
    )
    parser.add_argument("request", type=Path, nargs="?",
                        help="research request as JSON (see docs)")  # fmt: skip
    parser.add_argument("--config", type=Path, help="config file (default: $GTM_CONFIG)")
    parser.add_argument("--markdown", action="store_true", help="print the Markdown report")
    parser.add_argument("--save", action="store_true",
                        help="store the report in the database (market_research)")  # fmt: skip
    parser.add_argument("--verify", metavar="RESEARCH_ID",
                        help="re-check a stored report's evidence before delivery; exit 0 "
                             "verified, 1 failed, 3 incomplete")  # fmt: skip
    parser.add_argument(
        "--import-ads",
        type=Path,
        metavar="FILE",
        help="store manually captured competitor ads (JSON, market.ads)",
    )
    parser.add_argument("--operator", help="who captured the ads (required with --import-ads)")
    parser.add_argument("--screenshots", type=Path, metavar="DIR",
                        help="folder of the screenshots the import file names")  # fmt: skip
    parser.add_argument("--ad-checklist", type=int, metavar="COMPANY_ID",
                        help="a crawled company's competitors with ad library search links "
                             "to open by hand, and what is captured")  # fmt: skip
    parser.add_argument("--country", default="IN",
                        help="ad library country for --ad-checklist (default IN)")  # fmt: skip
    args = parser.parse_args(argv)
    modes = (args.request, args.verify, args.import_ads, args.ad_checklist)
    if sum(x is not None for x in modes) != 1:
        parser.error("give a request file, --verify RESEARCH_ID, --import-ads FILE or "
                     "--ad-checklist COMPANY_ID")  # fmt: skip
    if args.import_ads is not None:
        if not args.operator:
            parser.error("--import-ads needs --operator")
        return _import(args)
    if args.ad_checklist is not None:
        if not re.fullmatch(r"[A-Z]{2}", args.country):
            parser.error("--country is a two-letter country code, e.g. IN")
        return _checklist(args)
    if args.verify is not None:
        try:
            settings = load_settings(args.config)
            configure_logging(settings.logging, secrets=secret_values(settings))
            result = verify_stored(MarketStore(make_engine(settings.database)), settings,
                                   args.verify)  # fmt: skip
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return {"verified": 0, "failed": 1}.get(result["verdict"], 3)
    try:
        settings = load_settings(args.config)
        build_user_agent(settings.identity)
        req = ResearchRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"error: invalid request: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.logging, secrets=secret_values(settings))
    needs_db = args.save or (
        req.product is not None and (req.product.company_id is not None or req.product.include_ads)
    )
    engine = make_engine(settings.database) if needs_db else None
    report = asyncio.run(research(req, settings, engine))
    if args.save and engine is not None:
        MarketStore(engine).save(req, report)
    out = (to_markdown(report) if args.markdown else
           json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))  # fmt: skip
    # UTF-8 whatever the console's code page (a Windows console cannot print "₹").
    sys.stdout.buffer.write(out.encode("utf-8") + b"\n")
    sys.stdout.flush()
    return 1 if report.status is RunStatus.FAILED else 0


def _checklist(args: argparse.Namespace) -> int:
    """``--ad-checklist``: exit 0 printed, 2 no such company / no database."""
    try:
        settings = load_settings(args.config)
        configure_logging(settings.logging, secrets=secret_values(settings))
        engine = make_engine(settings.database)
        dossier = build_dossier(engine, args.ad_checklist)
        domains = [c["domain"] for c in dossier_competitors(dossier) if c["domain"]]
        checklist = capture_checklist(dossier, load_ads(engine, domains), args.country)
    except (ConfigError, ValueError, DBAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = (checklist_markdown(checklist) if args.markdown else
           json.dumps(checklist, indent=2, ensure_ascii=False) + "\n")  # fmt: skip
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.flush()
    return 0


def _import(args: argparse.Namespace) -> int:
    """``--import-ads``: exit 0 stored (or already stored), 2 refused (nothing stored)."""
    try:
        settings = load_settings(args.config)
        configure_logging(settings.logging, secrets=secret_values(settings))
        ocr = Ocr(settings.market) if settings.market.ocr_enabled else None
        result = import_ads(make_engine(settings.database), make_rawstore(settings.rawstore),
                            args.import_ads.read_bytes(), operator=args.operator,
                            screenshots=args.screenshots, ocr=ocr,
                            urls=settings.urls)  # fmt: skip
    except (ConfigError, OSError, AdImportError, DBAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if ocr is not None and not ocr.available:
        result["ocr"] = ocr.reason
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
