"""Artifact corpus: regression-test the parser and extractors without re-crawling.

Spec section 24 ("regression-test parser/extractor changes without
re-crawling") and section 29 ("Replay: artifact corpus, expected
observations, regression comparison").

Each case is a directory holding one raw artifact exactly as the raw store
keeps it, and two JSON files:

* ``artifact.html``: the response body, byte for byte.
* ``meta.json``: ``url``, ``content_type`` (the response header),
  ``subject_domain`` (the company the crawl is about), ``first_party``,
  ``description``; optionally ``company_names`` (names learned from the
  company's other pages) and ``known_issue`` (a truthfulness bug the golden
  currently records, kept visible until it is fixed).
* ``expected.json``: the golden, a normalized, sorted rendering of the parse
  facts and every observation, with the parser and extractor versions that
  produced it.

The page is parsed with the real parser and run through every extractor as
``extract_crawl`` runs one page; every draft must pass ``check_draft``.

Version rule (spec section 24: version all logic affecting factual output):
a change in output must come with a version bump. A golden that differs while
the responsible version (the parser for parse facts; the extractor or the
parser for observations) is unchanged fails with "bump the version"; one
whose versions were bumped fails until ``--update`` accepts the new output.

    python -m gtm_crawler.corpus [--update] [--case NAME] [DIR]
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ParseSettings, UrlSettings
from .db.evidence import EvidenceError, ObservationDraft, check_draft
from .extract import EXTRACTOR_VERSIONS, EXTRACTORS
from .extract.common import Context
from .extract.company import company_names
from .extract.product import identify_products
from .parse import PARSER_VERSION, ParsedPage, parse_page
from .urls import registrable_domain, url_host

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "tests" / "corpus"
ARTIFACT, META, EXPECTED = "artifact.html", "meta.json", "expected.json"


class CorpusError(Exception):
    """A case that cannot be run, or whose extractors produced unsupported drafts."""


def run_case(case_dir: Path) -> dict[str, Any]:
    """The golden rendering of what the current code makes of the case's artifact."""
    meta = json.loads((case_dir / META).read_text(encoding="utf-8"))
    body = (case_dir / ARTIFACT).read_bytes()
    page = parse_page(body, meta["url"], meta.get("content_type"), ParseSettings(), UrlSettings())
    domain = meta["subject_domain"]
    first_party = registrable_domain(url_host(page.url)) == domain
    if first_party != meta["first_party"]:
        raise CorpusError(f"{case_dir.name}: meta first_party does not match url/subject_domain")

    # As extract_crawl builds it, from this one page (plus names from the
    # company's other pages). Product ids stand in for product rows.
    ctx = Context(domain=domain, names=set(meta.get("company_names", [])),
                  first_party=first_party)  # fmt: skip
    products: dict[str, dict[str, str]] = {}
    if first_party:
        ctx.names |= company_names(page, domain)
        for c in identify_products(page, ctx):
            if c.slug not in ctx.product_ids:
                ctx.product_ids[c.slug] = len(ctx.product_ids) + 1
                products[c.slug] = {"slug": c.slug, "name": c.name, "basis": c.basis}
    slugs = {pid: slug for slug, pid in ctx.product_ids.items()}

    observations, seen, rejected = [], set(), []
    for extractor in EXTRACTORS:
        for d in extractor(page, ctx):
            obs = _observation(d, slugs)
            # extract_crawl records a repeated statement once (its dedup key
            # without the site, which is constant within one page).
            key = json.dumps([d.field, d.product_id, d.qualifier,
                              d.related.basis if d.related else None, d.value_verbatim,
                              d.value_normalized], sort_keys=True)  # fmt: skip
            if key in seen:
                continue
            try:
                check_draft(d, page)
            except EvidenceError as exc:
                rejected.append(str(exc))
                continue
            seen.add(key)
            observations.append(obs)
    if rejected:
        raise CorpusError(f"{case_dir.name}: drafts rejected by check_draft: {rejected}")

    return {
        "versions": {"parser": PARSER_VERSION, "extractors": dict(EXTRACTOR_VERSIONS)},
        "page": _page_facts(page),
        "company_names": sorted(ctx.names),
        "products": sorted(products.values(), key=lambda p: p["slug"]),
        "observations": sorted(observations, key=lambda o: json.dumps(o, sort_keys=True)),
    }


def _page_facts(page: ParsedPage) -> dict[str, Any]:
    return {
        "url": page.url,
        "encoding": page.encoding,
        "title": page.title,
        "language": page.language,
        "canonical_url": page.canonical_url,
        "description": page.description,
        "headings": [list(h) for h in page.headings],
        "published_at": page.published_at.isoformat() if page.published_at else None,
        "json_ld_nodes": len(page.json_ld_nodes),
        "json_ld_errors": len(page.json_ld_errors),
        "blocks": len(page.blocks),
        "links": [link.url for link in page.links],
        "text_sha256": page.text_sha256,
        "parse_error": page.parse_error,
    }


def _observation(d: ObservationDraft, slugs: dict[int, str]) -> dict[str, Any]:
    return {
        "extractor": d.extractor,
        "field": d.field,
        "value_verbatim": d.value_verbatim,
        "value_normalized": d.value_normalized,
        "qualifier": d.qualifier,
        "kind": d.kind,
        "as_of": d.as_of.isoformat() if d.as_of else None,
        "product": slugs.get(d.product_id) if d.product_id is not None else None,
        "related": asdict(d.related) if d.related else None,
        "certainty": d.extraction_certainty,
        "quote": asdict(d.quote),
    }


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Problems with ``actual`` against the golden; empty when they match exactly."""
    if expected == actual:
        return []
    problems = []
    old_v, new_v = expected.get("versions", {}), actual["versions"]
    parser_bumped = old_v.get("parser") != new_v["parser"]
    if expected.get("page") != actual["page"] and not parser_bumped:
        problems.append(f"parse output changed but PARSER_VERSION is still {new_v['parser']!r}: "
                        "bump it (gtm_crawler/parse/__init__.py)")  # fmt: skip
    old_x = old_v.get("extractors", {})
    for name, version in new_v["extractors"].items():
        before = [o for o in expected.get("observations", []) if o["extractor"] == name]
        after = [o for o in actual["observations"] if o["extractor"] == name]
        if before != after and old_x.get(name) == version and not parser_bumped:
            problems.append(f"{name} extractor output changed but its VERSION is still "
                            f"{version!r}: bump it (gtm_crawler/extract/{name}.py)")  # fmt: skip
    if not problems:
        problems.append("output changed with a version bump: review the diff, then --update")
    return problems


def dumps(golden: dict[str, Any]) -> str:
    return json.dumps(golden, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def cases(root: Path, name: str | None = None) -> list[Path]:
    found = sorted(p.parent for p in root.glob(f"*/{META}"))
    if name is not None:
        found = [p for p in found if p.name == name]
        if not found:
            raise CorpusError(f"no case {name!r} in {root}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m gtm_crawler.corpus",
        description="Compare the artifact corpus with its goldens.",
    )
    ap.add_argument("dir", nargs="?", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--update", action="store_true", help="rewrite the goldens")
    ap.add_argument("--case", help="run only this case")
    args = ap.parse_args(argv)
    failed = 0
    for case_dir in cases(args.dir, args.case):
        path = case_dir / EXPECTED
        try:
            actual = run_case(case_dir)
        except CorpusError as exc:
            print(f"FAIL {case_dir.name}: {exc}")
            failed += 1
            continue
        if args.update:
            path.write_text(dumps(actual), encoding="utf-8", newline="\n")
            print(f"updated {case_dir.name}")
            continue
        if not path.exists():
            print(f"FAIL {case_dir.name}: no {EXPECTED} (run with --update)")
            failed += 1
            continue
        expected = json.loads(path.read_text(encoding="utf-8"))
        problems = compare(expected, actual)
        if not problems:
            print(f"ok   {case_dir.name}")
            continue
        failed += 1
        print(f"FAIL {case_dir.name}: " + "; ".join(problems))
        sys.stdout.writelines(difflib.unified_diff(
            dumps(expected).splitlines(keepends=True), dumps(actual).splitlines(keepends=True),
            f"{case_dir.name}/{EXPECTED}", "current output"))  # fmt: skip
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
