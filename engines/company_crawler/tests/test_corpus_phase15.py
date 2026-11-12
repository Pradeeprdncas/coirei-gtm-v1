"""Artifact corpus (spec sections 24, 29): the current parser and extractors reproduce
every golden exactly, and a changed output without a version bump is refused."""

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gtm_crawler import corpus
from gtm_crawler.extract import EXTRACTOR_VERSIONS, traction

CASES = corpus.cases(corpus.DEFAULT_DIR)


def test_corpus_is_broad() -> None:
    assert len(CASES) >= 15
    extractors = {o["extractor"] for c in CASES for o in _golden(c)["observations"]}
    # The llm extractor reads stored model answers, which the corpus has none of
    # (tests/test_extract_llm.py covers it).
    assert extractors == set(EXTRACTOR_VERSIONS) - {"llm"}


@pytest.mark.parametrize("case_dir", CASES, ids=[c.name for c in CASES])
def test_golden_reproduced(case_dir: Path) -> None:
    assert corpus.run_case(case_dir) == _golden(case_dir)


def test_precision_goldens() -> None:
    """Spec section 16 cases, stated here too so a careless --update cannot hide them."""
    obs = {c.name: _golden(c)["observations"] for c in CASES}

    def values(case: str, field: str) -> list[tuple[str, Any]]:
        return [(o["value_verbatim"], o["value_normalized"]) for o in obs[case]
                if o["field"] == field]  # fmt: skip

    assert ("more than 500 customers", {"qualifier": "gt", "value": 500}) in values(
        "precision-customer-count-qualifiers", "customer_count")  # fmt: skip
    assert [v for v, n in values("precision-at-least-total-funding", "total_funding")] == [
        "at least $20M to date"]  # fmt: skip
    assert values("precision-raised-is-not-valuation", "valuation") == []
    assert values("precision-raised-is-not-valuation", "funding_round")
    prices = dict(values("precision-pricing-starting-at", "price"))
    assert prices["Starting at $99 per month"]["qualifier"] == "gte"
    assert not any("40" in v for v in prices)
    assert [v for v, _ in values("precision-up-to-concentration", "customer_concentration")] == [
        "Our largest customer accounted for up to 40% of revenue"]  # fmt: skip
    for case in ("negative-plausible-unsupported", "negative-search-snippet-teaser"):
        # Only the company's own name (the shared footer's "© Acme Robotics Inc.").
        assert {o["field"] for o in obs[case]} <= {"company_name", "legal_name"}
        assert all(o["value_verbatim"].startswith("Acme") for o in obs[case])


def test_unbumped_change_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    case = corpus.DEFAULT_DIR / "precision-customer-count-qualifiers"

    def sloppy(page: Any, ctx: Any) -> Any:  # drops every qualifier
        drafts = traction.extract_traction(page, ctx)
        return [replace(d, value_normalized={**d.value_normalized, "qualifier": "eq"})
                if d.value_normalized else d for d in drafts]  # fmt: skip

    monkeypatch.setattr(corpus, "EXTRACTORS", tuple(
        sloppy if e is traction.extract_traction else e for e in corpus.EXTRACTORS))  # fmt: skip
    actual = corpus.run_case(case)
    problems = corpus.compare(_golden(case), actual)
    assert len(problems) == 1
    assert "traction extractor output changed" in problems[0] and "bump it" in problems[0]

    monkeypatch.setitem(EXTRACTOR_VERSIONS, "traction", "999")
    problems = corpus.compare(_golden(case), corpus.run_case(case))
    assert problems == ["output changed with a version bump: review the diff, then --update"]


def test_parser_change_needs_parser_bump() -> None:
    case = CASES[0]
    golden = _golden(case)
    changed = json.loads(json.dumps(golden))
    changed["page"]["title"] = "something else"
    assert "PARSER_VERSION" in corpus.compare(changed, golden)[0]


def test_update_round_trips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for c in CASES[:3]:
        shutil.copytree(c, tmp_path / c.name)
    first = CASES[0].name
    (tmp_path / first / corpus.EXPECTED).unlink()
    assert corpus.main([str(tmp_path)]) == 1  # a case without a golden fails
    assert corpus.main([str(tmp_path), "--update", "--case", first]) == 0
    assert corpus.main([str(tmp_path)]) == 0
    for c in CASES[:3]:
        assert (tmp_path / c.name / corpus.EXPECTED).read_bytes() == (
            c / corpus.EXPECTED).read_bytes()  # fmt: skip

    golden = tmp_path / first / corpus.EXPECTED
    edited = json.loads(golden.read_text(encoding="utf-8"))
    edited["observations"].pop()
    golden.write_text(corpus.dumps(edited), encoding="utf-8", newline="\n")
    capsys.readouterr()
    assert corpus.main([str(tmp_path), "--case", first]) == 1
    assert "bump it" in capsys.readouterr().out
    assert corpus.main([str(tmp_path), "--update"]) == 0
    assert golden.read_bytes() == (CASES[0] / corpus.EXPECTED).read_bytes()


def test_rejected_draft_fails_the_case(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(page: Any, ctx: Any) -> Any:
        return [replace(d, value_verbatim="9,999 customers")
                for d in traction.extract_traction(page, ctx)]  # fmt: skip

    monkeypatch.setattr(corpus, "EXTRACTORS", (unsupported,))
    with pytest.raises(corpus.CorpusError, match="rejected by check_draft"):
        corpus.run_case(corpus.DEFAULT_DIR / "precision-customer-count-qualifiers")


def _golden(case_dir: Path) -> dict[str, Any]:
    return json.loads((case_dir / corpus.EXPECTED).read_text(encoding="utf-8"))
