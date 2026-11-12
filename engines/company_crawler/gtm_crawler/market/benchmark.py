"""A small, repeatable research benchmark and its deterministic quality gates.

``python -m gtm_crawler.market.benchmark run benchmarks/market/cases.json OUT``
runs every case against the configured (live) search provider and the real
web, in this process, and stores each report plus its raw pages under
``OUT``. ``evaluate`` (also run after ``run``) re-reads them and writes
``OUT/summary.json``: per case the coverage, latency, provider usage and the
gates below. Nothing here asks a model; nothing compares the engine with
another AI report.

Gates are deterministic correctness checks, each with an exact pass rule
(docs/MARKET_BENCHMARK.md):

* provenance: every quoted excerpt re-derives from its stored raw page;
* citations: every evidence id a finding, metric, contradiction, sizing input
  or score cites is collected evidence;
* numeric: every parsed money figure re-parsed from its own text by a
  separate parser gives the same value and currency, and the text is inside
  the excerpt;
* scope: every figure behind a metric summary has that summary's scope, and
  every figure behind an evidence-derived total has the sizing's target scope;
* sizing_math: every TAM/SAM/SOM recomputed from its inputs;
* unsupported: quoted findings carry their excerpt, derived figures are
  stated by their evidence, user and hypothesis findings are LOW;
* status: the run status agrees with its evidence, issues and coverage.

Coverage (sources tried/used, questions answered) and the analyst's labels
of extracted figures are reported, not gated: their thresholds come from the
baseline (docs/MARKET_BENCHMARK.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import MarketSettings, Settings, load_settings
from ..fetch.http import build_user_agent
from ..rawstore import RawStore
from .ads import performance_claims
from .extract import table_cell_value
from .models import (
    Confidence,
    EvidenceKind,
    MetricKind,
    Quantity,
    Report,
    ResearchRequest,
    RetrievalStatus,
    RunStatus,
    SizingRequest,
    SizingResult,
)
from .runner import research
from .scope import GLOBAL, canonical_geography, canonical_segment
from .verify import VERIFIED, stored_extraction, verify_report

# --- an independent money parser (does not reuse extract._MONEY) ----------------------

_CUR = {"us$": "USD", "usd": "USD", "$": "USD", "dollars": "USD", "€": "EUR", "eur": "EUR",
        "euros": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR", "inr": "INR", "rs": "INR",
        "rs.": "INR", "¥": "JPY", "jpy": "JPY", "cny": "CNY", "a$": "AUD", "aud": "AUD",
        "c$": "CAD", "cad": "CAD"}  # fmt: skip
_SCALE = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3,
          "crore": 1e7, "lakh": 1e5}  # fmt: skip
_TOKEN = re.compile(r"us\$|a\$|c\$|rs\.?|[$€£₹¥]|[a-z]+|\d[\d,]*(?:\.\d+)?", re.I)


def reparse_money(text: str) -> tuple[float, str] | None:
    """(value, currency) of a money figure's text such as "USD 2.0 billion" or
    "2,581.22 USD Million"; None when it does not read as one amount."""
    tokens = _TOKEN.findall(text)
    numbers = [t for t in tokens if t[0].isdigit()]
    currencies = [_CUR[t.lower()] for t in tokens if t.lower() in _CUR]
    scales = [_SCALE[t.lower()] for t in tokens if t.lower() in _SCALE]
    if len(numbers) != 1 or len(set(currencies)) != 1 or len(scales) > 1:
        return None
    return round(float(numbers[0].replace(",", "")) * (scales[0] if scales else 1.0), 2), \
        currencies[0]  # fmt: skip


# A price's amount (does not reuse market/pricing.py): its first number and the currency
# written next to it ("$29/month", "S$29", "29 EUR", "30¢", "2.9%").
_PRICE_CUR = {**_CUR, "s$": "SGD", "sgd": "SGD", "hk$": "HKD", "hkd": "HKD", "nz$": "NZD",
              "nzd": "NZD", "au$": "AUD", "ca$": "CAD", "r$": "BRL", "brl": "BRL",
              "mx$": "MXN", "mxn": "MXN", "chf": "CHF", "rupees": "INR"}  # fmt: skip
_CODES = frozenset({"usd", "eur", "gbp", "inr", "sgd", "hkd", "aud", "cad", "nzd", "chf", "jpy",
                    "cny", "brl", "mxn", "euros", "rupees", "dollars"})  # fmt: skip
_PRICE_TOKEN = re.compile(r"u\.s\.\s?\$|[a-z]{1,2}\$|rs\.?|[$€£₹¥¢%]|[a-z]+|\d[\d,]*(?:\.\d+)?",
                          re.I)  # fmt: skip


def reparse_price(text: str) -> tuple[float, str] | None:
    """(amount, currency) of a price's text: "percent" for a percentage, "$" for a bare
    dollar sign, "" when no currency is written; None without a number."""
    tokens = _PRICE_TOKEN.findall(text)
    at = next((i for i, t in enumerate(tokens) if t[0].isdigit()), None)
    if at is None:
        return None
    value = float(tokens[at].replace(",", ""))
    after = [t.lower() for t in tokens[at + 1 : at + 3]]
    if after[:1] == ["%"]:
        return value, "percent"
    if after[:1] and after[0] in ("¢", "cents", "cent"):
        return round(value / 100, 6), "$"  # a cent sign says no more than "$"
    if after[:1] and after[0] in ("p", "pence"):
        return round(value / 100, 6), "GBP"
    if after[:1] and after[0] in ("paise", "paisa"):
        return round(value / 100, 6), "INR"
    if after[:1] == ["k"]:
        value *= 1000
        after = after[1:]
    if after[:1] and after[0] in ("dollars", "dollar"):
        return value, "$"
    if after[:1] and (after[0] in _CODES or after[0] in ("€", "£")):
        return value, _PRICE_CUR[after[0]]
    before = tokens[at - 1].lower().replace(" ", "") if at else ""
    if before in ("$", "¥", "rs", "rs."):
        return value, before.rstrip(".")  # the symbol alone
    return value, _PRICE_CUR.get(before, "")


# --- gates ------------------------------------------------------------------------------


def _gate(checked: int, failures: list[str]) -> dict[str, Any]:
    return {"checked": checked, "failed": len(failures), "pass": not failures,
            "examples": failures[:5]}  # fmt: skip


def provenance(
    report: Report, raw: RawStore, market: MarketSettings | None = None
) -> dict[str, Any]:
    """Every HTML and PDF quote re-checked against its stored artifacts
    (``verify.verify_report``; PDFs re-extracted when the dependency is installed).
    Verified against the stored extraction only (not re-extracted) still passes;
    the counts say how many."""
    checks = asyncio.run(verify_report(report, raw, market or MarketSettings()))
    failures = [f"{i}: {v.status.value}: {v.detail}" for i, v in checks.items()
                if v.status not in VERIFIED]  # fmt: skip
    out = _gate(len(checks), failures)
    out["by_status"] = dict(Counter(v.status.value for v in checks.values()))
    out["checks"] = {i: v.status.value for i, v in checks.items()}
    return out


def citations(report: Report) -> dict[str, Any]:
    known = {e.evidence_id for e in report.evidence}
    cited: list[str] = [i for s in report.sections.values() for f in s.findings
                        for i in f.evidence_ids]  # fmt: skip
    cited += [i for f in report.executive_summary for i in f.evidence_ids]
    cited += [i for m in report.metrics for i in m.evidence_ids]
    cited += [i for c in report.contradictions for i in c.evidence_ids]
    if report.sizing:
        cited += [i for r in report.sizing.results for q in r.inputs for i in q.evidence_ids]
        cited += [i for x in report.sizing.excluded for i in x.evidence_ids]
    if report.scoring:
        cited += [i for c in report.scoring.candidates for d in c.dimensions
                  for i in d.evidence_ids]  # fmt: skip
    return _gate(len(cited), sorted({i for i in cited if i not in known}))


def _table_cell(e: Any, source: Any, raw: RawStore, text: str) -> tuple[float, str] | None:
    """(value, currency) of a PDF table figure re-read from the stored extraction: the
    cell must be in the located row, and the table must state its unit."""
    loc = e.locator
    if loc is None or loc.type != "pdf" or loc.table is None or source is None:
        return None
    stored = stored_extraction(source, raw)
    if stored is None or loc.page is None or loc.page > len(stored["pages"]):
        return (math.nan, "missing")
    for b in stored["pages"][loc.page - 1]["blocks"]:
        t = b.get("table")
        if t and t["index"] == loc.table and t.get("unit") and loc.row is not None \
                and loc.row < len(t["rows"]) and text in t["rows"][loc.row]:  # fmt: skip
            return table_cell_value(text, t["unit"]), t["unit"]["currency"]
    return (math.nan, "not in the located row")


def numeric(report: Report, raw: RawStore | None = None) -> dict[str, Any]:
    sources = {s.source_id: s for s in report.sources}
    checked, failures = 0, []
    for e in report.evidence:
        for m in e.metrics:
            if m.kind not in (MetricKind.MARKET_SIZE, MetricKind.MARKET_FORECAST,
                              MetricKind.PRICE_POINT):  # fmt: skip
                continue
            checked += 1
            if m.price is not None:
                problem = _price_reread(e, m)
                if problem:
                    failures.append(f"{e.evidence_id}: {m.text!r} {problem}")
                continue
            again = reparse_money(m.text)
            unit = m.unit.split("/")[0]
            cell = _table_cell(e, sources.get(e.source_id), raw, m.text) if raw else None
            if cell is not None:  # a PDF table figure: its cell in the table's stated unit
                if not math.isclose(cell[0], m.value, rel_tol=1e-9) or cell[1] != unit:
                    failures.append(f"{e.evidence_id}: table cell {m.text!r} is {cell}, "
                                    f"recorded {m.value:g} {unit}")  # fmt: skip
                continue
            if m.text not in e.excerpt:
                failures.append(f"{e.evidence_id}: {m.text!r} not in the excerpt")
            elif (
                again is None
                or again[1] != unit
                or not math.isclose(again[0], m.value, rel_tol=1e-9)
            ):
                failures.append(f"{e.evidence_id}: {m.text!r} parsed {m.value:g} {unit}, "
                                f"re-parsed {again}")  # fmt: skip
    return _gate(checked, failures)


def _price_reread(e: Any, m: Any) -> str | None:
    """Why a price's recorded amount and currency do not re-read from its own text."""
    p = m.price
    if m.text not in e.excerpt:
        return "not in the excerpt"
    again = reparse_price(m.text)
    if again is None:
        return "has no amount"
    want = "percent" if p.percent else p.currency
    got = again[1]
    if got == "" and e.locator is not None and e.locator.type == "json_path":
        # A JSON-LD price: its currency is the located priceCurrency value.
        got = next((c.text.upper() for c in p.context if c.role == "json"), "")
    if got == "$":
        # A bare "$": USD (assumed, and said so) or unsettled on a page about another dollar.
        ok = want is None or (want == "USD" and p.currency_assumed)
    elif got == "¥":
        ok = want is None  # yen or yuan: never settled from the symbol
    elif got == "rs":
        ok = want == "INR" and p.currency_assumed
    else:
        ok = got == want
    if not ok or not math.isclose(again[0], m.value, rel_tol=1e-9, abs_tol=1e-9):
        return f"recorded {m.value:g} {want}, re-read {again[0]:g} {got or 'no currency'}"
    return None


def scope(report: Report) -> dict[str, Any]:
    index = report.evidence_index()
    checked, failures = 0, []
    for summary in report.metrics:
        key = (summary.geography, summary.segment, summary.unit, summary.subset,
               summary.off_subject)  # fmt: skip
        for i in summary.evidence_ids:
            checked += 1
            same = [
                m
                for m in index[i].metrics
                if m.kind is summary.kind
                and (m.geography, m.segment, m.unit, m.subset, m.off_subject) == key
            ]
            if not same:
                failures.append(f"{i}: no figure of scope {key} behind {summary.kind.value}")
    sizing = report.sizing
    if sizing is not None and sizing.target_scope:
        req = report.request.sizing
        assert req is not None
        requested = req.geography or report.request.geography
        geo = canonical_geography(requested) or GLOBAL
        places = {geo} if requested else {geo, None}
        seg = canonical_segment(req.segment or report.request.segment)
        for r in sizing.results:
            for q in r.inputs:
                if q.name != "market_total_from_evidence":
                    continue
                for i in q.evidence_ids:
                    checked += 1
                    ok = [
                        m
                        for m in index[i].metrics
                        if m.kind is MetricKind.MARKET_SIZE
                        and m.unit == req.currency
                        and m.segment == seg
                        and not m.subset
                        and not m.off_subject
                        and m.measure in (None, "market")
                        and m.geography in places
                        and (m.year or 9999) <= req.year
                    ]
                    if not ok:
                        failures.append(f"{i}: behind the total but not of {geo}/{seg}")
    return _gate(checked, failures)


def _recompute(
    r: SizingResult, spec: SizingRequest
) -> dict[str, tuple[float, float | None, float | None]]:
    """TAM, SAM, SOM per scenario from the request's own inputs (top-down from
    evidence: the collected total, no filters, no share)."""
    if r.method == "bottom_up":
        assert spec.bottom_up is not None
        b = spec.bottom_up
        factors, filters, share = [b.customers, b.units_per_customer, b.price_per_unit], \
            b.sam_filters, b.penetration  # fmt: skip
    elif spec.top_down is not None:
        t = spec.top_down
        factors, filters, share = [t.market_total], t.sam_filters, t.obtainable_share
    else:
        factors, filters, share = [r.inputs[0]], [], None
    out = {}
    for level, pick in (("low", "lo"), ("base", "base"), ("high", "hi")):

        def v(q: Quantity, pick: str = pick) -> float:
            return float(getattr(q, pick))

        tam = math.prod(v(q) for q in factors)
        sam = tam * math.prod(v(q) for q in filters) if filters else None
        som = sam * v(share) if sam is not None and share is not None else None
        out[level] = (tam, sam, som)
    return out


def sizing_math(report: Report) -> dict[str, Any]:
    spec = report.request.sizing
    if report.sizing is None or spec is None:
        return _gate(0, [])
    checked, failures = 0, []
    for r in report.sizing.results:
        for level, want in _recompute(r, spec).items():
            got = getattr(r, level)
            for name, a, b in zip(("tam", "sam", "som"), want, (got.tam, got.sam, got.som),
                                  strict=True):  # fmt: skip
                checked += 1
                if (a is None) != (b is None) or (
                    a is not None
                    and b is not None
                    and not math.isclose(a, b, rel_tol=1e-12, abs_tol=0.01)
                ):
                    failures.append(f"{r.method} {level} {name}: stated {b}, recomputed {a}")
    return _gate(checked, failures)


def unsupported(report: Report) -> dict[str, Any]:
    index = report.evidence_index()
    checked, failures = 0, []
    for key, section in report.sections.items():
        for f in section.findings:
            checked += 1
            if f.basis is EvidenceKind.DIRECT and not (
                len(f.evidence_ids) == 1 and f.evidence_ids[0] in index
                and index[f.evidence_ids[0]].excerpt in f.statement):  # fmt: skip
                failures.append(f"{key}: quoted finding without its excerpt")
            if f.basis is EvidenceKind.USER and f.confidence is not Confidence.LOW:
                failures.append(f"{key}: user statement not LOW")
            if (f.basis is EvidenceKind.INFERRED and f.statement.startswith(("Hypothesis",
                    "Proposed")) and f.confidence is not Confidence.LOW):  # fmt: skip
                failures.append(f"{key}: hypothesis not LOW")
            if f.basis in (EvidenceKind.DIRECT, EvidenceKind.DERIVED) and not f.evidence_ids \
                    and key not in ("validation",):  # fmt: skip
                failures.append(f"{key}: {f.basis.value} finding cites nothing")
    for m in report.metrics:
        checked += 1
        stated = {x.value for i in m.evidence_ids for x in index[i].metrics}
        if not {m.low, m.high} <= stated:
            failures.append(f"summary {m.kind.value} {m.year}: {m.low}-{m.high} not stated")
    return _gate(checked, failures)


def status(report: Report) -> dict[str, Any]:
    failures = []
    bad = [i for i in report.issues if i.severity in ("warning", "error")]
    statuses = Counter(s.status for s in report.sources)
    coverage = report.stats.get("coverage", {})
    unanswered = coverage.get("questions", 0) - coverage.get("questions_answered", 0)
    if report.status is RunStatus.COMPLETED and (
        bad or statuses[RetrievalStatus.FAILED] or statuses[RetrievalStatus.SKIPPED]
        or statuses[RetrievalStatus.UNREADABLE]
        or unanswered or not report.evidence):  # fmt: skip
        failures.append("COMPLETED with issues, failed/skipped sources or open questions")
    if report.status is RunStatus.PARTIAL and not report.stats.get("status_reasons"):
        failures.append("PARTIAL without a stated reason")
    if report.status is RunStatus.FAILED and report.evidence:
        failures.append("FAILED although evidence was collected")
    if not report.evidence and report.status in (RunStatus.COMPLETED, RunStatus.PARTIAL):
        failures.append(f"{report.status.value.upper()} without evidence")
    return _gate(1, failures)


def ad_claims(report: Report) -> dict[str, Any]:
    """No finding that cites a captured ad speaks of spend, reach, targeting or
    performance in its own words (quoting the ad is fine): public ads show none of it."""
    sections = [(k, s.findings) for k, s in report.sections.items()]
    sections.append(("executive_summary", report.executive_summary))
    return _gate(*performance_claims(sections, report.evidence, report.sources))


GATES = ("provenance", "citations", "numeric", "scope", "sizing_math", "unsupported", "status",
         "ad_claims")  # fmt: skip


def evaluate_report(report: Report, raw: RawStore, case: dict[str, Any]) -> dict[str, Any]:
    gates = {"provenance": provenance(report, raw), "citations": citations(report),
             "numeric": numeric(report, raw), "scope": scope(report),
             "sizing_math": sizing_math(report), "unsupported": unsupported(report),
             "status": status(report), "ad_claims": ad_claims(report)}  # fmt: skip
    statuses = Counter(s.status.value for s in report.sources)
    tried = sum(n for k, n in statuses.items() if k != RetrievalStatus.SKIPPED.value)
    used = statuses[RetrievalStatus.FETCHED.value] + statuses[RetrievalStatus.CACHED.value]
    excerpts = Counter(" ".join(e.excerpt.lower().split()) for e in report.evidence)
    vendor = [s for s in report.sources if s.discovered_by and
              s.discovered_by.startswith("known_competitor")]  # fmt: skip
    out: dict[str, Any] = {
        "case": case["id"],
        "mode": report.mode.value,
        "status": report.status.value,
        "status_reasons": report.stats.get("status_reasons", []),
        "duration_seconds": report.stats.get("duration_seconds"),
        "queries_sent": report.stats.get("queries_sent"),
        "queries_failed": report.stats.get("queries_failed"),
        "sources": dict(statuses),
        "sources_tried": tried,
        "sources_used": used,
        "retrieval_rate": round(used / tried, 2) if tried else None,
        "domains": report.stats.get("coverage", {}).get("domains"),
        "questions": report.stats.get("coverage", {}).get("questions"),
        "questions_answered": report.stats.get("coverage", {}).get("questions_answered"),
        "evidence": len(report.evidence),
        "duplicate_excerpts": sum(n - 1 for n in excerpts.values() if n > 1),
        "contradictions": len(report.contradictions),
        "metrics": len(report.metrics),
        "competitor_pages": {
            "tried": sum(s.status is not RetrievalStatus.SKIPPED for s in vendor),
            "fetched": sum(s.status is RetrievalStatus.FETCHED for s in vendor),
            "rendered": sum((s.status_detail or "").startswith("rendered") for s in vendor),
        },  # fmt: skip
        "price_points": sum(
            m.kind is MetricKind.PRICE_POINT for e in report.evidence for m in e.metrics
        ),  # fmt: skip
        "gates": gates,
        "gates_passed": all(g["pass"] for g in gates.values()),
    }
    if report.sizing is not None:
        out["sizing"] = {
            "target_scope": report.sizing.target_scope,
            "results": [{"method": r.method, "tam": r.base.tam, "sam": r.base.sam,
                         "som": r.base.som, "verified": r.verified} for r in report.sizing.results],
            "excluded": [x.model_dump() for x in report.sizing.excluded],
            "insufficient": report.sizing.insufficient,
            "cross_check": report.sizing.cross_check,
        }  # fmt: skip
    out["pdf"] = pdf_outcomes(report, gates["provenance"].pop("checks", {}))
    out["selection"] = selection_outcomes(report)
    out["expectations"] = expectations(report, case.get("expect", {}))
    return out


def selection_outcomes(report: Report) -> dict[str, Any]:
    """How the source budget was spent: sources that gave evidence, attempts that gave
    none (by outcome), failures and whether a later source answered the same questions,
    and why collection stopped."""
    with_evidence = {e.source_id for e in report.evidence}
    tried = [s for s in report.sources if s.retrieved_at is not None
             and not (s.discovered_by or "").startswith("known_competitor")]  # fmt: skip
    wasted = Counter("no evidence" if s.status is RetrievalStatus.FETCHED else s.status.value
                     for s in tried if s.source_id not in with_evidence)  # fmt: skip
    failed = [s for s in tried if s.status in (RetrievalStatus.FAILED, RetrievalStatus.BLOCKED,
                                               RetrievalStatus.UNREADABLE)]  # fmt: skip

    def recovered(f: Any) -> bool:
        aims = set(f.selection.targets) if f.selection else set()
        later = [
            s
            for s in tried
            if s.source_id in with_evidence
            and s.selection
            and (s.selection.wave or 0) >= (f.selection.wave or 0 if f.selection else 0)
        ]
        return any(aims & set(s.selection.targets) for s in later if s.selection)

    sel = report.stats.get("selection", {})
    return {
        "useful_sources": sum(s.source_id in with_evidence for s in tried),
        "attempts_without_evidence": dict(wasted),
        "failures": len(failed), "failures_recovered": sum(recovered(f) for f in failed),
        "stopped": sel.get("stopped") if isinstance(sel, dict) else None,
        "used": sel.get("used") if isinstance(sel, dict) else None,
        "open_questions": [{"key": q["key"], "reason": q["reason"]}
                           for q in report.stats.get("questions", []) if not q["answered"]],
    }  # fmt: skip


def pdf_outcomes(report: Report, checks: dict[str, str]) -> list[dict[str, Any]]:
    """Every PDF the run met: URL, outcome and reason, pages read, the pages its
    evidence cites, and how its quotes verified."""

    def met(s: Any) -> bool:
        detail = s.status_detail or ""
        return bool(s.extracted_sha256 or detail.startswith(("pdf", "PDF"))
                    or re.search(r"\.pdf(?:$|[?#])", s.url, re.I))  # fmt: skip

    pdf_sources = [s for s in report.sources if met(s)]
    out = []
    for s in pdf_sources:
        mine = [e for e in report.evidence if e.source_id == s.source_id]
        out.append({
            "url": s.url, "status": s.status.value, "detail": s.status_detail,
            "evidence": len(mine),
            "figures": sum(len(e.metrics) for e in mine),
            "pages_cited": sorted({e.locator.page for e in mine if e.locator and e.locator.page}),
            "verification": dict(Counter(checks.get(e.evidence_id, "unchecked") for e in mine)),
            "limitations": s.limitations[:5],
        })  # fmt: skip
    return out


def expectations(report: Report, expect: dict[str, Any]) -> list[dict[str, Any]]:
    """The case's own expected outcomes (cases.json ``expect``)."""
    checks: list[dict[str, Any]] = []
    if "status_in" in expect:
        checks.append({"name": "status_in", "ok": report.status.value in expect["status_in"],
                       "detail": report.status.value})  # fmt: skip
    if "bottom_up" in expect and report.sizing is not None:
        r = next((r for r in report.sizing.results if r.method == "bottom_up"), None)
        for level, want in expect["bottom_up"].items():
            got = getattr(r, level) if r else None
            ok = got is not None and [got.tam, got.sam, got.som] == want
            checks.append(
                {
                    "name": f"bottom_up.{level}",
                    "ok": ok,
                    "detail": f"{[got.tam, got.sam, got.som] if got else None} vs {want}",
                }
            )
    if "min_competitor_pages_fetched" in expect:
        n = sum(s.status is RetrievalStatus.FETCHED for s in report.sources
                if (s.discovered_by or "").startswith("known_competitor"))  # fmt: skip
        checks.append(
            {
                "name": "min_competitor_pages_fetched",
                "ok": n >= expect["min_competitor_pages_fetched"],
                "detail": n,
            }
        )
    return checks


# --- running ----------------------------------------------------------------------------


async def run_cases(cases: list[dict[str, Any]], out: Path, settings: Settings) -> None:
    for case in cases:
        raw_root = out / "raw"
        s = replace(settings, rawstore=replace(settings.rawstore, root=str(raw_root)))
        req = ResearchRequest.model_validate(case["request"])
        started = time.monotonic()
        report = await research(req, s, None)
        wall = round(time.monotonic() - started, 1)
        (out / f"{case['id']}.json").write_text(
            json.dumps({"wall_seconds": wall, "report": report.model_dump(mode="json")},
                       ensure_ascii=False), encoding="utf-8")  # fmt: skip
        print(f"{case['id']}: {report.status.value} in {wall}s", file=sys.stderr)


def run_via_service(cases: list[dict[str, Any]], out: Path, settings: Settings) -> None:
    """Each case through the service: ``POST /v1/market/research`` on the API, one
    ``gtm-worker`` pass (``database.url`` must point at an empty, migrated database:
    a queued crawl job would be run first), then ``GET .../report``."""
    from fastapi.testclient import TestClient

    from ..api import create_app
    from ..db import make_engine
    from ..worker import run_worker

    s = replace(settings, rawstore=replace(settings.rawstore, root=str(out / "raw")))
    engine = make_engine(s.database)
    api = TestClient(create_app(s, engine))
    for case in cases:
        started = time.monotonic()
        created = api.post("/v1/market/research", json=case["request"])
        created.raise_for_status()
        research_id = created.json()["research_id"]
        asyncio.run(run_worker(s, engine, once=True))
        view = api.get(f"/v1/market/research/{research_id}").json()
        report = api.get(f"/v1/market/research/{research_id}/report")
        report.raise_for_status()
        wall = round(time.monotonic() - started, 1)
        record = {"wall_seconds": wall, "via": "api+worker", "api_status": view["status"],
                  "report": report.json()}  # fmt: skip
        (out / f"{case['id']}.json").write_text(json.dumps(record, ensure_ascii=False),
                                                encoding="utf-8")  # fmt: skip
        print(f"{case['id']}: {view['status']} via API + worker in {wall}s", file=sys.stderr)


def evaluate_dir(cases: list[dict[str, Any]], out: Path, settings: Settings) -> dict[str, Any]:
    raw = RawStore(out / "raw")
    prices = settings.cost.search_prices()
    results = []
    for case in cases:
        path = out / f"{case['id']}.json"
        if not path.exists():
            results.append({"case": case["id"], "missing": True})
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        report = Report.model_validate(data["report"])
        r = evaluate_report(report, raw, case)
        r["wall_seconds"] = data.get("wall_seconds")
        price = prices.get("exa")  # the configured provider (search.py)
        r["search_cost_usd"] = (round(price * (r["queries_sent"] or 0), 4) if price
                                else "unknown (set cost.search_query_usd)")  # fmt: skip
        results.append(r)
    summary = {"cases": results, "gates_passed": all(r.get("gates_passed") for r in results)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                      encoding="utf-8")  # fmt: skip
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gtm_crawler.market.benchmark")
    parser.add_argument("command", choices=("run", "evaluate"))
    parser.add_argument("cases", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--only", action="append", help="case id (repeatable)")
    parser.add_argument("--via-service", action="store_true",
                        help="run through the API and a worker (needs database.url)")  # fmt: skip
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "run":
        build_user_agent(settings.identity)
        if args.via_service:
            run_via_service(cases, args.out, settings)
        else:
            asyncio.run(run_cases(cases, args.out, settings))
    summary = evaluate_dir(cases, args.out, settings)
    for r in summary["cases"]:
        failed = [k for k, g in r.get("gates", {}).items() if not g["pass"]]
        print(f"{r['case']}: {r.get('status')} gates {'ok' if not failed else failed}")
    return 0 if summary["gates_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
