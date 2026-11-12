"""The benchmark's quality gates (gtm_crawler.market.benchmark) on fixture runs: they pass
on a correct report and fail on one tampered in each way they check."""

import json
from pathlib import Path

import pytest

from gtm_crawler.market import benchmark
from gtm_crawler.market.models import ResearchMode, ResearchRequest, RunStatus, SizingRequest
from gtm_crawler.rawstore import RawStore

from .test_market import GLOBAL_AND_REGION, PAGES, FakeSearch, html, industry, run

CASES = Path(__file__).parent.parent / "benchmarks" / "market" / "cases.json"


@pytest.mark.parametrize(("text", "expected"), [
    ("USD 2.0 billion", (2e9, "USD")), ("2581.22 USD Million", (2_581_220_000.0, "USD")),
    ("₹1,200 crore", (1.2e10, "INR")), ("$25 per user per month", (25.0, "USD")),
    ("USD 2 to 3 billion", None), ("HK$ 5 EUR", None),
])  # fmt: skip
def test_independent_money_parser(text: str, expected: tuple[float, str] | None) -> None:
    assert benchmark.reparse_money(text) == expected


def _report(tmp_path: Path):  # type: ignore[no-untyped-def]
    req = industry(sizing=SizingRequest(market_definition="CRM, India", year=2025))
    return run(req, tmp_path)


def test_gates_pass_on_a_correct_report(tmp_path: Path) -> None:
    report = _report(tmp_path)
    result = benchmark.evaluate_report(report, RawStore(tmp_path / "raw"), {"id": "x"})
    assert result["gates_passed"], {k: g for k, g in result["gates"].items() if not g["pass"]}
    assert result["gates"]["provenance"]["checked"] == len(
        [e for e in report.evidence if e.locator and e.locator.type == "xpath"]
    )
    assert result["gates"]["sizing_math"]["checked"] == 9


def test_gates_fail_on_tampered_reports(tmp_path: Path) -> None:
    raw = RawStore(tmp_path / "raw")
    report = _report(tmp_path)
    figure = next(e for e in report.evidence if e.metrics)

    bad = report.model_copy(deep=True)
    bad.evidence[report.evidence.index(figure)].excerpt += " (edited)"
    assert not benchmark.provenance(bad, raw)["pass"]

    bad = report.model_copy(deep=True)
    bad.sections["size_growth"].findings[0].evidence_ids.append("ev_000000000000")
    assert not benchmark.citations(bad)["pass"]

    bad = report.model_copy(deep=True)
    bad.evidence[report.evidence.index(figure)].metrics[0].value *= 10
    assert not benchmark.numeric(bad)["pass"]

    bad = report.model_copy(deep=True)
    bad.metrics[0].geography = "Atlantis"
    assert not benchmark.scope(bad)["pass"]

    bad = report.model_copy(deep=True)
    assert bad.sizing is not None
    bad.sizing.results[0].base.tam += 1.0
    assert not benchmark.sizing_math(bad)["pass"]

    bad = report.model_copy(deep=True)
    bad.metrics[0].high *= 3
    assert not benchmark.unsupported(bad)["pass"]

    bad = report.model_copy(deep=True)
    bad.status = RunStatus.COMPLETED  # it has failed sources and open questions
    assert not benchmark.status(bad)["pass"]


def test_scope_gate_catches_a_regional_figure_behind_a_global_total(tmp_path: Path) -> None:
    url = "https://global.example.com/crm"
    PAGES[url] = html("CRM software market report", GLOBAL_AND_REGION[1])
    try:
        req = ResearchRequest(mode=ResearchMode.INDUSTRY, subject="CRM software",
                              sizing=SizingRequest(market_definition="CRM", year=2023))  # fmt: skip
        report = run(req, tmp_path, search=FakeSearch(urls=[url], fail=None))
    finally:
        del PAGES[url]
    assert benchmark.scope(report)["pass"]
    assert report.sizing is not None
    regional = report.sizing.excluded[0].evidence_ids
    report.sizing.results[0].inputs[0].evidence_ids = regional  # the contamination M7 prevents
    for e in report.evidence:  # make the regional figure the only one cited
        if e.evidence_id in regional:
            e.metrics = [m for m in e.metrics if m.geography == "North America"]
    assert not benchmark.scope(report)["pass"]


def test_case_file_is_valid_and_its_expected_sizing_is_the_arithmetic() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    kinds = [c["kind"] for c in cases]
    assert kinds.count("industry") >= 2 and kinds.count("product") >= 2 and "sizing" in kinds
    for case in cases:
        ResearchRequest.model_validate(case["request"])
    sizing_case = next(c for c in cases if c["kind"] == "sizing")
    b = sizing_case["request"]["sizing"]["bottom_up"]
    want = sizing_case["expect"]["bottom_up"]
    for level, pick in (("low", "low"), ("base", "base"), ("high", "high")):

        def v(q: dict[str, float], pick: str = pick) -> float:
            return q.get(pick, q["base"])

        tam = v(b["customers"]) * v(b["units_per_customer"]) * v(b["price_per_unit"])
        sam = tam * v(b["sam_filters"][0])
        som = sam * v(b["penetration"])
        assert want[level] == pytest.approx([tam, sam, som])
