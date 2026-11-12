"""Deployment smoke test (spec sections 30, 35; Appendix B phase 17).

Run against a deployed API (staging after every deploy, prod after a
rollout) with a site you are allowed to crawl::

    GTM_SMOKE_TOKEN=... python -m gtm_crawler.smoke --api https://gtm.staging.internal \\
        --site https://www.example.com/ [--timeout 900]

Checks, in order: ``/livez``, ``/readyz``, ``/metrics``; with a token, that
requests without it or with a wrong one are refused (401); a crawl submitted
with an ``Idempotency-Key`` and resubmitted (same job, ``created`` false);
polling until the job is final (COMPLETED passes); the dossier, quality report
and cost; then the evidence invariant: every dossier field that is not
UNKNOWN (company, products, competitors) has lineage whose every observation
ends in a stored, unpruned raw artifact matching its fetch's hash. A crawl
that yields no such field fails too: there would be nothing to verify.

Prints a JSON report (never the token). Exit 0 if every check passed, 1 if
any failed, 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

TOKEN_ENV = "GTM_SMOKE_TOKEN"  # noqa: S105 - the variable name, not a token
SITE_ENV = "GTM_SMOKE_SITE"
FINAL = ("COMPLETED", "FAILED", "CANCELLED")
# States that cite nothing by definition (NOT_APPLICABLE is only ever set by hand).
NO_EVIDENCE_STATES = ("UNKNOWN", "NOT_APPLICABLE")


class _Failed(Exception):
    pass


def run_smoke(
    client: httpx.Client,
    site: str,
    token: str = "",
    timeout: float = 900.0,
    poll: float = 5.0,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Run every check through ``client`` (whose base URL is the API); the report."""
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {"site": site, "checks": checks}
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.monotonic()

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks.append({"name": name, "ok": ok, **detail})
        if not ok:
            raise _Failed(name)

    def get(path: str, **kw: Any) -> httpx.Response:
        return client.get(path, headers=auth, **kw)

    try:
        r = client.get("/livez")
        check("livez", r.status_code == 200, status=r.status_code)
        r = client.get("/readyz")
        check("readyz", r.status_code == 200, status=r.status_code, body=_json(r))
        r = client.get("/metrics/")
        check("metrics", r.status_code == 200 and "# TYPE" in r.text, status=r.status_code)

        if token:
            codes = [
                client.post("/v1/crawls", json={"root_url": site}).status_code,
                client.get("/v1/crawls/0", headers={"Authorization": "Bearer not-it"}).status_code,
            ]
            check("auth_refuses_unauthenticated", codes == [401, 401], statuses=codes)
        else:
            checks.append({"name": "auth_refuses_unauthenticated", "ok": True,
                           "skipped": f"no token given (set {TOKEN_ENV})"})  # fmt: skip

        key = idempotency_key or f"smoke-{uuid.uuid4()}"
        body = {"root_url": site}
        first = client.post("/v1/crawls", json=body, headers={**auth, "Idempotency-Key": key})
        check("submit", first.status_code in (200, 201), status=first.status_code,
              body=_json(first))  # fmt: skip
        job = first.json()
        report["crawl_id"], report["company_id"] = job["crawl_id"], job["company_id"]
        again = client.post("/v1/crawls", json=body, headers={**auth, "Idempotency-Key": key})
        same = again.status_code == 200 and again.json().get("crawl_id") == job["crawl_id"]
        check("idempotent_resubmit", same and again.json().get("created") is False,
              status=again.status_code)  # fmt: skip

        deadline = time.monotonic() + timeout
        while True:
            r = get(f"/v1/crawls/{job['crawl_id']}")
            job = r.json() if r.status_code == 200 else job
            if (r.status_code == 200 and job["status"] in FINAL) or time.monotonic() > deadline:
                break
            time.sleep(poll)
        check("crawl_completed", job["status"] == "COMPLETED", status=job["status"],
              error_summary=job.get("error_summary"), versions=job.get("versions"),
              site_crawls=job.get("site_crawls"))  # fmt: skip

        company_id = job["company_id"]
        dossier = get(f"/v1/companies/{company_id}/dossier")
        check("dossier", dossier.status_code == 200, status=dossier.status_code)
        quality = get(f"/v1/crawls/{job['crawl_id']}/quality")
        check("quality", quality.status_code == 200, status=quality.status_code)
        cost = get(f"/v1/companies/{company_id}/cost")
        check("cost", cost.status_code == 200, status=cost.status_code)

        stated = stated_fields(dossier.json())
        problems: list[str] = []
        observations = 0
        for org, product, name in stated:
            params = {} if product is None else {"product_id": product}
            r = get(f"/v1/companies/{org}/lineage/{name}", params=params)
            where = f"{org}/{product}/{name}"
            if r.status_code != 200:
                problems.append(f"{where}: lineage HTTP {r.status_code}")
                continue
            found, bad = lineage_problems(r.json())
            observations += found
            problems += [f"{where}: {p}" for p in bad]
        if not stated:
            problems.append("no field other than UNKNOWN: nothing to verify")
        check("evidence_invariant", not problems, fields=len(stated),
              observations=observations, problems=problems[:50])  # fmt: skip
    except _Failed:
        pass
    except (httpx.HTTPError, ValueError, KeyError) as exc:  # unreachable, non-JSON, bad shape
        checks.append({"name": "error", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    report["ok"] = all(c["ok"] for c in checks)
    return report


def stated_fields(dossier: dict[str, Any]) -> list[tuple[int, int | None, str]]:
    """(organization, product, field) for every dossier entry not UNKNOWN."""
    company = dossier["company"]["organization_id"]
    groups: list[tuple[int, int | None, dict[str, Any]]] = [
        (company, None, dossier["company"]["fields"])
    ]
    groups += [(company, p["product_id"], p["fields"]) for p in dossier["products"]]
    groups += [(c["organization_id"], None, c["fields"]) for c in dossier["competitors"]]
    out: list[tuple[int, int | None, str]] = []
    for org, product, fields in groups:
        for name, value in fields.items():
            entries = value if isinstance(value, list) else [value]
            if any(e["state"] not in NO_EVIDENCE_STATES for e in entries):
                out.append((org, product, name))
    return out


def lineage_problems(lineage: dict[str, Any]) -> tuple[int, list[str]]:
    """Observations checked, and every way the lineage fails to reach a raw artifact."""
    count, problems = 0, []
    for item in lineage["items"]:
        if item["state"] in NO_EVIDENCE_STATES:
            continue
        if not item["observations"]:
            problems.append(f"{item['state']} item {item['assessment_id']} cites nothing")
        for o in item["observations"]:
            count += 1
            artifact, fetch = o.get("artifact"), o.get("fetch") or {}
            if artifact is None:
                problems.append(f"observation {o['observation_id']}: no raw artifact")
            elif artifact["sha256"] != fetch.get("raw_sha256"):
                problems.append(f"observation {o['observation_id']}: artifact hash mismatch")
            elif artifact.get("pruned_at") is not None:
                problems.append(f"observation {o['observation_id']}: artifact pruned")
            if not (o.get("evidence") or {}).get("quote"):
                problems.append(f"observation {o['observation_id']}: no evidence quote")
    return count, problems


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:500]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gtm_crawler.smoke", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    ap.add_argument("--api", required=True, help="API base URL, e.g. http://127.0.0.1:8000")
    ap.add_argument("--site", default=os.environ.get(SITE_ENV),
                    help=f"site to crawl (default: ${SITE_ENV})")  # fmt: skip
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for the crawl")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between status polls")
    args = ap.parse_args(argv)
    if not args.site:
        ap.error(f"--site or ${SITE_ENV} is required")
    # The token comes from the environment only: never argv (visible in ps / shell history).
    token = os.environ.get(TOKEN_ENV, "")
    with httpx.Client(base_url=args.api, timeout=30.0, follow_redirects=False) as client:
        report = run_smoke(client, args.site, token, args.timeout, args.poll)
    report["api"] = args.api
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
