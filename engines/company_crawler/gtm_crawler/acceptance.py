"""Production acceptance runner (spec section 35, rule 16; Appendix B phase 18).

For each acceptance criterion of spec section 35, runs the tests and tools
that demonstrate it and prints a matrix: criterion, evidence, PASS / FAIL /
NOT DEMONSTRATED, duration::

    python -m gtm_crawler.acceptance [--full] [--out report.json]

Evidence is the existing suite (one pytest run over the union of the node ids
below, results read from its JUnit XML) plus tools run as subprocesses:

* ``corpus``: ``python -m gtm_crawler.corpus`` (goldens and precision);
* ``replay``: a scratch database (name must contain "acceptance") seeded with
  two simulated crawls, then ``gtm-crawl --replay-all --fail-on-change``;
* ``drill``: ``python -m gtm_crawler.drill`` (backup, destroy, restore, resume);
* ``load:<scenario>``: ``python -m gtm_crawler.loadtest`` per scenario.

Default: small scales (the suite's), drill at 200 pages. ``--full``: the load
scenarios at their full scale (docs/load-results.md) and the drill at 1000.

A criterion is FAIL if any of its evidence failed, NOT DEMONSTRATED if any was
skipped or could not run (e.g. no Chromium, no ``pg_dump``) and none failed,
PASS only when every piece of evidence ran and passed. Exit status 1 unless
every criterion is PASS. Nothing here talks to the Internet.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET  # noqa: S405 - parses our own pytest's JUnit file
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL, NOT_DEMONSTRATED = "PASS", "FAIL", "NOT DEMONSTRATED"
DEFAULT_URLS = {
    "test": "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_test",
    "load": "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_load_acceptance",
    "drill": "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_drill_acceptance",
    "replay": "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_replay_acceptance",
}
ENV_URLS = {"test": "GTM_TEST_DATABASE_URL", "load": "GTM_LOAD_DATABASE_URL",
            "drill": "GTM_DRILL_DATABASE_URL", "replay": "GTM_ACCEPTANCE_DATABASE_URL"}  # fmt: skip
SEC = "tests/test_security_phase16.py::"

# Spec section 35, in its order: criterion -> evidence (pytest node ids or files,
# and tool names "corpus", "replay", "drill", "load:<scenario>").
CRITERIA: list[tuple[str, list[str]]] = [
    ("Worker crash does not permanently lose leased URLs", [
        "tests/test_queue_phase13.py::test_recovery_after_a_crash_counts_lost_leases",
        "tests/test_queue_phase13.py::test_a_url_that_keeps_killing_its_worker_is_dead_lettered",
        "tests/test_jobs.py::test_claims_are_exclusive_and_expired_leases_are_reclaimed",
        "tests/test_resume.py::test_expired_lease_and_due_retry_are_claimable",
        "tests/test_resume.py::test_concurrent_claims_never_share_a_url",
        "load:crash_recovery", "load:db_pressure"]),
    ("Crawl resumes after restart", [
        "tests/test_resume.py::test_crashed_crawl_is_resumable",
        "tests/test_resume.py::test_paused_crawl_resumes_where_it_stopped",
        "tests/test_resume.py::test_resume_refuses_changed_parse_settings",
        "tests/test_resume.py::test_url_progress_is_idempotent_and_cannot_reopen_finished_urls",
        "tests/test_jobs.py::test_worker_resumes_a_job_whose_worker_died",
        "tests/test_staging_phase17.py::test_readiness_during_a_rolling_deploy",
        "load:crash_recovery", "drill"]),
    ("Private/internal destinations cannot be reached", [
        "tests/test_netguard.py", "tests/test_urls.py::test_rejects_uncrawlable_urls",
        "tests/test_secure_network.py::test_allowlist_is_explicit_and_never_opens_metadata",
        "tests/test_secure_network.py::test_dns_answers_are_cached_bounded_and_rechecked",
        "tests/test_browser.py::test_browser_cannot_reach_private_addresses",
        SEC + "test_hostile_destinations_never_pass_the_guard",
        SEC + "test_numeric_ipv4_spellings_are_invalid_whatever_the_resolver",
        SEC + "test_idna_mapped_ip_literals_are_normalized_to_the_literal",
        SEC + "test_public_destinations_still_pass",
        SEC + "test_metadata_is_refused_even_inside_allowed_networks",
        SEC + "test_dns_rebinding_cannot_swap_the_checked_address",
        SEC + "test_dns_rebinding_is_refused_on_a_fresh_connection",
        SEC + "test_mixed_answers_are_refused_whole",
        SEC + "test_crawl_of_a_metadata_address_makes_no_connection",
        SEC + "test_browser_requests_meet_the_guard_and_cannot_escape",
        SEC + "test_browser_route_refuses_before_any_fetch"]),
    ("Redirects are revalidated", [
        "tests/test_fetch.py::test_follows_redirect_chain_resolving_relative_locations",
        "tests/test_fetch.py::test_redirect_limits",
        "tests/test_fetch.py::test_follow_filter_stops_before_requesting_the_target",
        "tests/test_netguard.py::test_redirect_to_disallowed_address_is_blocked_at_the_hop",
        "tests/test_secure_network.py::test_validators_go_to_the_requested_url_only",
        "tests/test_policy.py::test_refusals_are_recorded_with_their_evidence",
        SEC + "test_redirect_to_a_forbidden_target_is_not_followed",
        SEC + "test_redirect_chain_into_private_and_metadata_addresses_is_blocked_at_the_hop",
        SEC + "test_redirect_loops_and_long_chains_stop",
        SEC + "test_redirect_with_credentials_does_not_leak_into_logs"]),
    ("Robots decisions are enforced and auditable", [
        "tests/test_robots.py", "tests/test_policy.py",
        "tests/test_crawl.py::test_robots_snapshot_is_kept",
        "tests/test_crawl.py::test_unreachable_robots_snapshot_has_no_body",
        "tests/test_crawl.py::test_unreachable_robots_fetches_nothing_else",
        "tests/test_crawl.py::test_crawl_completes_and_respects_policies",
        "tests/test_crawl_intelligence.py::"
        "test_huge_crawl_delay_defers_the_site_with_an_audited_reason",
        "tests/test_sitemaps.py::test_robots_disallowed_sitemap_is_skipped",
        SEC + "test_oversized_robots_txt_fails_closed"]),
    ("Traps are bounded", [
        "tests/test_frontier.py::test_trap_rules",
        "tests/test_frontier.py::test_pattern_budget_caps_calendar_and_facet_traps",
        "tests/test_frontier.py::test_prefix_budget_limits_one_section",
        "tests/test_crawl_intelligence.py::test_session_ids_are_traps",
        "tests/test_crawl_intelligence.py::test_sort_filter_combinations_are_capped_per_path",
        "tests/test_crawl_intelligence.py::test_per_host_budget",
        "tests/test_crawl_intelligence.py::test_low_yield_closes_a_shape_and_its_section",
        "tests/test_crawl_intelligence.py::test_template_duplicates_close_their_section",
        "tests/test_crawl.py::test_page_budget_gives_partial",
        "tests/test_crawl.py::test_time_budget_gives_partial",
        SEC + "test_trap_flood_admits_a_bounded_number_of_urls",
        SEC + "test_sitemap_index_fan_out_is_bounded",
        "load:deep_single_domain"]),
    ("Duplicates are controlled", [
        "tests/test_crawl.py::test_duplicates_links_and_raw_documents",
        "tests/test_crawl.py::test_concurrent_crawl_matches_sequential",
        "tests/test_crawl.py::test_recrawl_revalidates_and_reuses_stored_pages",
        "tests/test_frontier.py::test_duplicates_rejected_even_after_claim",
        "tests/test_frontier.py::test_mark_seen_blocks_later_add",
        "tests/test_urls.py::test_normalize",
        "tests/test_urls.py::test_normalization_is_idempotent",
        "tests/test_parse.py::test_simhash_detects_near_duplicates",
        "tests/test_parse.py::test_shared_navigation_does_not_make_pages_duplicates",
        "tests/test_assess.py::test_same_site_repeats_are_one_source",
        "tests/test_rawstore.py::test_concurrent_puts_of_identical_content",
        "load:crash_recovery", "load:many_domain"]),
    ("Browser usage is bounded and measurable", [
        "tests/test_browser_phase8.py", "tests/test_browser.py",
        "tests/test_crawl.py::test_javascript_page_is_rendered",
        "load:browser_heavy"]),
    ("Search-provider failure does not collapse the crawl", [
        "tests/test_external.py::test_search_failure_does_not_stop_the_crawl",
        "tests/test_search.py", "load:provider_degradation"]),
    ("Every factual field has evidence or is UNKNOWN", [
        "tests/test_truthfulness.py", "tests/test_truthfulness_phase16.py",
        "tests/test_db.py::test_unknown_field_cannot_carry_a_value",
        "tests/test_db.py::test_found_value_requires_observations_and_confidence",
        "tests/test_artifacts.py", "tests/test_evidence.py",
        "tests/test_assess.py::test_nothing_observed_is_unknown",
        "tests/test_assess.py::test_low_confidence_is_unverified",
        "tests/test_assess.py::test_high_risk_claims_made_only_by_the_company_stay_unverified",
        "tests/test_staging_phase17.py::test_smoke_passes_end_to_end_in_process",
        "tests/test_staging_phase17.py::test_lineage_check_flags_every_broken_link",
        "tests/test_dr_phase17.py::test_verify_reports_damage_and_cli_exits_nonzero",
        "corpus"]),
    ("Conflicts and qualifiers are preserved", [
        "tests/test_assess.py::test_incompatible_values_conflict_without_a_value",
        "tests/test_assess.py::test_bound_and_value_are_compatible_and_the_exact_value_is_reported",
        "tests/test_assess.py::test_different_currencies_are_not_compared",
        "tests/test_assess.py::test_newer_dated_figure_supersedes_older_one",
        "tests/test_truthfulness.py::test_disagreeing_sources_are_conflicting_not_resolved",
        "tests/test_truthfulness.py::test_vague_counts_are_not_made_precise",
        "tests/test_truthfulness_phase16.py::test_employee_bounds_use_the_shared_qualifiers",
        "tests/test_truthfulness_phase16.py::test_customer_count_fewer_than",
        "tests/test_db.py::test_evidence_chain_persists_with_qualifier",
        "tests/test_db.py::test_conflicting_requires_multiple_observations",
        "tests/test_db.py::test_historical_observations_are_kept",
        "tests/test_evidence.py::test_drops_qualifier",
        "tests/test_extract_company.py::test_employee_qualifiers_are_kept",
        "tests/test_extract_signals.py::test_open_roles_keep_their_qualifier",
        "tests/test_assess_phase11.py::test_old_time_varying_figure_is_stale_but_keeps_its_value",
        "corpus"]),
    ("Extraction is replayable", [
        "tests/test_replay_phase15.py",
        "tests/test_pipeline.py::test_reextraction_supersedes_without_fetching",
        "tests/test_pipeline.py::test_reextract_command",
        "replay", "drill"]),
    ("Versions are reproducible", [
        "tests/test_acceptance_phase18.py",
        "tests/test_jobs.py::test_versions_are_recorded_with_jobs_and_crawls",
        "tests/test_corpus_phase15.py", "corpus", "replay"]),
    ("DLQ items are inspectable/replayable", [
        "tests/test_queue_phase13.py::test_retry_policy_by_error_class",
        "tests/test_queue_phase13.py::test_failing_and_poison_urls_are_dead_lettered_and_the_crawl_goes_on",
        "tests/test_queue_phase13.py::test_requeue_and_discard",
        "tests/test_queue_phase13.py::test_abandoned_and_crashed_jobs_are_dead_lettered",
        "tests/test_queue_phase13.py::test_dead_letter_api_and_queue_metrics",
        SEC + "test_dead_letter_payload_is_data_and_requeue_uses_stored_state"]),
    ("Metrics expose quality and cost", [
        "tests/test_quality_phase14.py", "tests/test_cost_phase14.py",
        "tests/test_browser_phase8.py::test_render_cost_is_in_crawl_stats",
        "tests/test_queue_phase13.py::test_dead_letter_api_and_queue_metrics",
        "tests/test_search.py::test_query_budget_is_enforced_and_recorded"]),
    ("Backups can be restored", ["tests/test_dr_phase17.py", "drill"]),
    ("Load and security tests pass for target scale", [
        "tests/test_security_phase16.py",
        "tests/test_load_phase16.py::test_throttle_respaces_starts_after_an_event_loop_stall",
        "load:deep_single_domain", "load:many_domain", "load:browser_heavy",
        "load:provider_degradation", "load:db_pressure", "load:queue_pressure",
        "load:crash_recovery"]),
]  # fmt: skip


def pytest_targets(items: list[str]) -> list[str]:
    """The union of the node ids, without those a whole file in the list already covers."""
    files = {i for i in items if "::" not in i}
    nodes = {i for i in items if "::" in i and i.split("::")[0] not in files}
    return sorted(files | nodes)


def covers(item: str, testcase: str) -> bool:
    """Whether evidence ``item`` (a file or node id) covers ``testcase`` ("file::name[param]")."""
    if "::" not in item:
        return testcase.split("::")[0] == item
    return testcase == item or testcase.startswith(item + "[")


def read_junit(path: Path) -> dict[str, dict[str, Any]]:
    """testcase id ("tests/x.py::test_y[p]") -> {"status", "seconds", "message"}."""
    out: dict[str, dict[str, Any]] = {}
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314 - our own file
        module = case.get("classname", "")
        node = f"{module.replace('.', '/')}.py::{case.get('name')}"
        status, message = PASS, ""
        for child in case:
            if child.tag in ("failure", "error"):
                status, message = FAIL, (child.get("message") or "")[:200]
            elif child.tag == "skipped" and status != FAIL:
                status, message = NOT_DEMONSTRATED, (child.get("message") or "")[:200]
        out[node] = {"status": status, "seconds": float(case.get("time") or 0), "message": message}
    return out


def run_pytest(items: list[str], env: dict[str, str], work: Path) -> dict[str, dict[str, Any]]:
    junit = work / "junit.xml"
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}", *pytest_targets(items)]  # fmt: skip
    subprocess.run(argv, cwd=ROOT, env=env, check=False, timeout=3600)  # noqa: S603
    return read_junit(junit) if junit.exists() else {}


def summarize(name: str, path: Path) -> str:
    """The headline numbers of a passed tool's ``--out`` JSON."""
    r = json.loads(path.read_text(encoding="utf-8"))
    checks = f"{sum(c['ok'] for c in r['checks'])}/{len(r['checks'])} checks"
    if name == "drill":
        m = r["measurements"]
        return (f"{checks}, {m['pages_per_site']} pages/site: backup {m['backup_seconds']} s, "
                f"restore {m['restore_seconds']} s, resume {m['resume_seconds']} s")  # fmt: skip
    return (f"{checks}, scale {r['scale']}: {r['stored']['unique_pages']} pages in "
            f"{r['wall_seconds']} s ({r['pages_per_second']} pages/s), peak "
            f"{r['memory'].get('peak_mb')} MB")  # fmt: skip


def run_tool(
    argv: list[str], env: dict[str, str], timeout: float, out: Path | None = None
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        done = subprocess.run([sys.executable, "-m", *argv], cwd=ROOT, env=env, timeout=timeout,  # noqa: S603
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)  # fmt: skip
        status = PASS if done.returncode == 0 else FAIL
        lines = (done.stdout.strip() or done.stderr.strip()).splitlines()
        detail = lines[-1][:300] if lines else f"exit {done.returncode}"
        if argv[0] == "gtm_crawler.corpus":
            detail = f"{sum(ln.startswith('ok') for ln in lines)} corpus cases match their goldens"
        if status is PASS and out is not None and out.exists():
            detail = summarize(argv[0].removeprefix("gtm_crawler."), out)
        if status is FAIL:
            failed = [ln for ln in done.stdout.splitlines() if ln.startswith("FAIL")]
            detail = "; ".join(failed)[:300] or (done.stderr.strip().splitlines() or [detail])[-1]
    except subprocess.TimeoutExpired:
        status, detail = FAIL, f"timed out after {timeout:.0f} s"
    return {"status": status, "seconds": round(time.perf_counter() - started, 2),
            "detail": detail}  # fmt: skip


def seed_replay_database(url: str, raw: str, pages: int) -> int:
    """Two simulated companies crawled to completion into ``url`` (dropped and
    recreated; the name must contain "acceptance"). Returns the crawls stored."""
    import httpx

    from .db import make_engine
    from .loadtest import SimWeb, Site, base_settings, scratch_database
    from .pipeline import run_pipeline

    scratch_database(url, "acceptance")
    settings = base_settings(url, raw)
    engine = make_engine(settings.database)
    sites = [Site(f"acceptance-{c}.example", f"{c.title()}co Systems", pages) for c in "ab"]
    web = SimWeb(sites)
    try:
        for site in sites:
            out = asyncio.run(run_pipeline(site.root, settings, engine,
                                           transport=httpx.MockTransport(web)))  # fmt: skip
            if out["status"] != "completed":
                raise RuntimeError(f"seed crawl of {site.root} ended {out['status']}")
    finally:
        engine.dispose()
    return len(sites)


def replay_tool(url: str, env: dict[str, str], pages: int, work: Path) -> dict[str, Any]:
    """Seed a scratch database, then ``gtm-crawl --replay-all --fail-on-change`` on it."""
    started = time.perf_counter()
    raw = work / "replay-raw"
    try:
        seeded = seed_replay_database(url, str(raw), pages)
    except Exception as exc:  # the database is unreachable or the seed crawl failed
        return {"status": FAIL, "seconds": round(time.perf_counter() - started, 2),
                "detail": f"seeding failed: {type(exc).__name__}: {exc}"[:300]}  # fmt: skip
    config = work / "replay-config.toml"
    config.write_text(f'[rawstore]\nroot = "{raw.as_posix()}"\n', encoding="utf-8",
                      newline="\n")  # fmt: skip
    argv = [sys.executable, "-m", "gtm_crawler.cli", "--replay-all", "--fail-on-change",
            "--config", str(config)]  # fmt: skip
    done = subprocess.run(argv, cwd=ROOT, env={**env, "GTM__DATABASE__URL": url},  # noqa: S603
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=1800, check=False)  # fmt: skip
    try:
        totals = json.loads(done.stdout)["totals"]
    except (ValueError, KeyError):
        totals = None
    # Exit 0 means nothing changed and nothing failed; a replay that covered
    # nothing (or could not read its raw documents) demonstrates nothing.
    ok = (done.returncode == 0 and totals is not None and totals["crawls"] == seeded
          and not totals["unreadable_pages"])  # fmt: skip
    detail = (
        f"{seeded} seeded crawls, exit {done.returncode}: {json.dumps(totals)}"
        if totals
        else (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1][:300]
    )
    return {"status": PASS if ok else FAIL, "seconds": round(time.perf_counter() - started, 2),
            "detail": detail}  # fmt: skip


def pg_dump_available(prefix: str) -> bool:
    from .backup import pg_command

    argv = [*pg_command(prefix), "pg_dump", "--version"]
    if not shutil.which(argv[0]):
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=60, check=False).returncode == 0  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return False


def tools(
    args: argparse.Namespace, env: dict[str, str], work: Path
) -> dict[str, Callable[[], dict[str, Any]]]:
    from .loadtest import SCENARIOS

    full = args.full
    out: dict[str, Callable[[], dict[str, Any]]] = {
        "corpus": lambda: run_tool(["gtm_crawler.corpus"], env, 600),
        "replay": lambda: replay_tool(args.replay_database_url, env, 300 if full else 50, work),
    }

    def drill() -> dict[str, Any]:
        if not pg_dump_available(args.pg_prefix):
            return {"status": NOT_DEMONSTRATED, "seconds": 0.0,
                    "detail": f"pg_dump not runnable via {args.pg_prefix!r}"}  # fmt: skip
        return run_tool(["gtm_crawler.drill", "--pages", "1000" if full else "200",
                         "--database-url", args.drill_database_url, "--pg-prefix", args.pg_prefix,
                         "--out", str(work / "drill.json")], env, 3600,
                        work / "drill.json")  # fmt: skip

    out["drill"] = drill
    for name, scenario in SCENARIOS.items():
        scale = scenario.scale if full else scenario.small
        argv = ["gtm_crawler.loadtest", name, "--scale", str(scale), "--database-url",
                args.load_database_url, "--out", str(work / f"load-{name}.json")]  # fmt: skip
        out[f"load:{name}"] = functools.partial(
            run_tool, argv, env, 3600, work / f"load-{name}.json"
        )
    return out


def combine(statuses: list[str]) -> str:
    if FAIL in statuses:
        return FAIL
    return NOT_DEMONSTRATED if NOT_DEMONSTRATED in statuses or not statuses else PASS


def evaluate(
    tests: dict[str, dict[str, Any]], tool_results: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The matrix: each criterion with its evidence and status."""
    rows = []
    for criterion, items in CRITERIA:
        evidence = []
        for item in items:
            if item in tool_results:
                evidence.append({"evidence": item, **tool_results[item]})
                continue
            cases = {k: v for k, v in tests.items() if covers(item, k)}
            statuses = [c["status"] for c in cases.values()]
            bad = [f"{k.split('::')[-1]}: {v['status']} {v['message']}".strip()
                   for k, v in cases.items() if v["status"] != PASS]  # fmt: skip
            evidence.append({
                "evidence": item, "status": combine(statuses),
                "seconds": round(sum(c["seconds"] for c in cases.values()), 2),
                "detail": "; ".join(bad)[:300] if bad else f"{len(cases)} tests passed"
                if cases else "no test ran (node id not collected)",
            })  # fmt: skip
        rows.append({"criterion": criterion,
                     "status": combine([e["status"] for e in evidence]),
                     "seconds": round(sum(e["seconds"] for e in evidence), 2),
                     "evidence": evidence})  # fmt: skip
    return rows


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,  # noqa: S607
                             capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=ROOT,  # noqa: S607
                               capture_output=True, text=True,
                               check=True).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + ("+uncommitted" if dirty else "")


def print_matrix(rows: list[dict[str, Any]]) -> None:
    for i, row in enumerate(rows, 1):
        print(f"{i:>2}. [{row['status']}] {row['criterion']} ({row['seconds']:.1f} s)")
        for e in row["evidence"]:
            print(f"      {e['status']:<16} {e['seconds']:>8.1f} s  {e['evidence']}"
                  + (f"  -- {e['detail']}" if e["status"] != PASS else ""))  # fmt: skip
    counts = {s: sum(r["status"] == s for r in rows) for s in (PASS, FAIL, NOT_DEMONSTRATED)}
    print(f"\n{counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[NOT_DEMONSTRATED]} NOT DEMONSTRATED"
          f" of {len(rows)} criteria")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gtm_crawler.acceptance",
        description="Demonstrate the production acceptance criteria (spec section 35).",
    )
    parser.add_argument("--full", action="store_true",
                        help="load scenarios at full scale, drill at 1000 pages")  # fmt: skip
    parser.add_argument("--out", type=Path, help="write the report as JSON here")
    for kind in DEFAULT_URLS:
        parser.add_argument(f"--{kind}-database-url",
                            default=os.environ.get(ENV_URLS[kind]) or DEFAULT_URLS[kind],
                            help=f"default: ${ENV_URLS[kind]} or {DEFAULT_URLS[kind]}")  # fmt: skip
    parser.add_argument("--pg-prefix", default=os.environ.get(
        "GTM_PG_COMMAND_PREFIX", "docker exec -i -e PGPASSWORD scraper10-postgres-1"),
        help="command prefix for pg_dump/pg_restore (default: $GTM_PG_COMMAND_PREFIX "
             "or the compose container)")  # fmt: skip
    args = parser.parse_args(argv)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
           "GTM_TEST_DATABASE_URL": args.test_database_url,
           "GTM_LOAD_DATABASE_URL": args.load_database_url,
           "GTM_DRILL_DATABASE_URL": args.drill_database_url,
           "GTM_PG_COMMAND_PREFIX": args.pg_prefix}  # fmt: skip
    env.pop("GTM_LOAD", None)  # full-scale load runs as tools below, once
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="gtm-acceptance-", ignore_cleanup_errors=True) as w:
        work = Path(w)
        items = [i for _, ev in CRITERIA for i in ev]
        runners = tools(args, env, work)
        print(f"pytest: {len(pytest_targets([i for i in items if i not in runners]))} targets ...",
              flush=True)  # fmt: skip
        tests = run_pytest([i for i in items if i not in runners], env, work)
        tool_results: dict[str, dict[str, Any]] = {}
        for name in dict.fromkeys(i for i in items if i in runners):
            print(f"{name} ...", flush=True)
            tool_results[name] = runners[name]()
            print(f"  {tool_results[name]['status']} in {tool_results[name]['seconds']} s",
                  flush=True)  # fmt: skip
    rows = evaluate(tests, tool_results)
    print()
    print_matrix(rows)
    report = {
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "commit": git_commit(), "mode": "full" if args.full else "default",
        "seconds": round(time.perf_counter() - started, 1),
        "pytest": {"tests": len(tests),
                   **{s: sum(t["status"] == s for t in tests.values())
                      for s in (PASS, FAIL, NOT_DEMONSTRATED)}},
        "passed": all(r["status"] == PASS for r in rows), "criteria": rows,
    }  # fmt: skip
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
