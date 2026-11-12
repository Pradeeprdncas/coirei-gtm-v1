"""Field assessment fa-v3: one test per approved rule (fa-v1 rules, spec states), plus the DB."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from gtm_crawler.assess import Obs, assess_group
from gtm_crawler.db.models import FieldAssessment
from gtm_crawler.pipeline import run_pipeline

from .test_crawl import FakeSite, html, settings_for

_ids = iter(range(1, 10_000))


def obs(field: str, value: str, normalized: dict[str, Any] | None = None, *,
        source: str = "acme.com", confidence: float = 0.81, tier: int = 1,
        first_party: bool = True, period: int | None = None,
        qualifier: str | None = None) -> Obs:  # fmt: skip
    return Obs(next(_ids), field, None, value, normalized, qualifier, confidence, tier,
               first_party, source, period)  # fmt: skip


def one(field: str, *observations: Obs) -> Any:
    (a,) = assess_group(field, None, list(observations))
    return a


def eq(v: float, **kw: Any) -> dict[str, Any]:
    return {"qualifier": "eq", "value": v, **kw}


def test_nothing_observed_is_unknown() -> None:
    a = one("valuation")
    assert (a.state, a.value, a.confidence, a.observation_ids) == ("UNKNOWN", None, None, [])


def test_same_site_repeats_are_one_source() -> None:
    a = one("headquarters", obs("headquarters", "Austin"), obs("headquarters", "Austin"),
            obs("headquarters", "Austin"))  # fmt: skip
    assert a.state == "SUPPORTED" and a.value["sources"] == ["acme.com"]
    assert a.confidence == 0.81  # not boosted by repetition


def test_two_independent_sources_verify_a_low_risk_field() -> None:
    a = one("headquarters", obs("headquarters", "Austin, Texas"),
            obs("headquarters", "Austin", source="crunchbase.com", tier=2, first_party=False,
                confidence=0.72))  # fmt: skip
    assert a.state == "CORROBORATED" and a.value["value"] == "Austin, Texas"
    assert a.confidence == round(1 - (1 - 0.81) * (1 - 0.72), 2)


def test_a_legal_form_spelt_two_ways_is_one_legal_name() -> None:
    a = one("legal_name", obs("legal_name", "Coirei Innovations Pvt Ltd"),
            obs("legal_name", "COIREI Innovations Private Limited"),
            obs("legal_name", "Coirei", source="theorg.com", tier=2,
                first_party=False))  # fmt: skip
    assert a.state != "CONFLICTING" and a.value["value"] == "Coirei Innovations Pvt Ltd"
    # A different company is still a conflict.
    b = one("legal_name", obs("legal_name", "Coirei Innovations Pvt Ltd"),
            obs("legal_name", "Coirei Labs Private Limited", source="x.com"))  # fmt: skip
    assert b.state == "CONFLICTING"


def test_bound_and_value_are_compatible_and_the_exact_value_is_reported() -> None:
    a = one("customer_count",
            obs("customer_count", "more than 500 customers", {"qualifier": "gt", "value": 500}),
            obs("customer_count", "620 customers", eq(620), source="news.com", tier=2,
                first_party=False))  # fmt: skip
    assert a.state == "CORROBORATED" and a.value["value"] == 620 and a.value["qualifier"] == "eq"


def test_incompatible_values_conflict_without_a_value() -> None:
    a = one("founded_year", obs("founded_year", "2019", eq(2019)),
            obs("founded_year", "2017", eq(2017), source="crunchbase.com", tier=2,
                first_party=False))  # fmt: skip
    assert a.state == "CONFLICTING" and a.value is None and len(a.observation_ids) == 2


def test_different_currencies_are_not_compared() -> None:
    a = one("total_funding",
            obs("total_funding", "$20M", {"qualifier": "eq", "amount": 2e7, "currency": "USD"}),
            obs("total_funding", "€20M", {"qualifier": "eq", "amount": 2e7, "currency": "EUR"},
                source="x.com"))  # fmt: skip
    assert a.state == "CONFLICTING"


def test_newer_dated_figure_supersedes_older_one() -> None:
    old = obs("employee_count", "120 employees", eq(120, year=2023), period=2023)
    new = obs("employee_count", "250 employees", eq(250, year=2025), period=2025)
    a = one("employee_count", old, new)
    assert a.state != "CONFLICTING" and a.value["value"] == 250
    assert a.value["as_of_year"] == 2025 and a.observation_ids == [new.id]  # old is history
    # An undated disagreement cannot be placed in time: conflict.
    undated = obs("employee_count", "90 employees", eq(90), source="x.com")
    assert one("employee_count", old, new, undated).state == "CONFLICTING"


def test_founding_year_is_not_time_varying() -> None:
    a = one("founded_year", obs("founded_year", "2019", eq(2019), period=2024),
            obs("founded_year", "2018", eq(2018), period=2025))  # fmt: skip
    assert a.state == "CONFLICTING"


def test_high_risk_claims_made_only_by_the_company_stay_unverified() -> None:
    # Two first-party pages on different company domains are still only the company.
    a = one("arr", obs("arr", "$10M", {"qualifier": "eq", "amount": 1e7, "currency": "USD"},
                       confidence=0.9),
            obs("arr", "$10M", {"qualifier": "eq", "amount": 1e7, "currency": "USD"},
                source="acme.io", confidence=0.9))  # fmt: skip
    assert a.state == "UNSUPPORTED" and a.value["value"] == 1e7
    # The same with an independent third-party source is verified.
    b = one("arr", obs("arr", "$10M", {"qualifier": "eq", "amount": 1e7, "currency": "USD"},
                       confidence=0.9),
            obs("arr", "$10M ARR", {"qualifier": "eq", "amount": 1e7, "currency": "USD"},
                source="techcrunch.com", tier=2, first_party=False, confidence=0.8))  # fmt: skip
    assert b.state == "CORROBORATED"


def test_low_confidence_is_unverified() -> None:
    a = one("headquarters", obs("headquarters", "Austin", confidence=0.45, tier=4,
                                first_party=False, source="blog.com"))  # fmt: skip
    assert a.state == "UNSUPPORTED"


def test_list_fields_are_assessed_per_item() -> None:
    items = assess_group("investor", None, [
        obs("investor", "Sequoia Capital", first_party=True),
        obs("investor", "Sequoia Capital", source="crunchbase.com", tier=2, first_party=False),
        obs("investor", "Accel"),
    ])  # fmt: skip
    got = {a.value["value"]: a.state for a in items}
    assert got == {
        "Sequoia Capital": "CORROBORATED",
        "Accel": "UNSUPPORTED",
    }  # investors: high risk


def test_ownership_compatibility() -> None:
    def o(v: str, src: str) -> Obs:
        return obs("ownership_type", v, {"qualifier": "eq", "value": v}, source=src)

    assert one("ownership_type", o("private", "a.com"), o("venture_backed", "b.com")).state != (
        "CONFLICTING"
    )
    assert one("ownership_type", o("public", "a.com"), o("private", "b.com")).state == "CONFLICTING"


# --- database ------------------------------------------------------------------------

# Its own company: evidence from other tests' acme.example crawls stays current
# for that organization and would add products to number_of_products.
ASSESS_SITE = "https://assess-acme.example"
ROUTES: dict[str, Any] = {
    f"{ASSESS_SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{ASSESS_SITE}/": ("text/html", html(
        "Acme", "<p>Acme was founded in 2019. We have more than 50 employees.</p>"
        '<a href="/products/stock">Stock</a>',
        '<meta property="og:site_name" content="Acme">',
    )),
    f"{ASSESS_SITE}/products/stock": ("text/html", html(
        "Stock", "<h1>Acme Stock</h1><h2>Features</h2><ul><li>Scanning</li></ul>")),
}  # fmt: skip


@pytest.mark.db
def test_pipeline_writes_assessments_the_database_accepts(engine: Engine, tmp_path: Path) -> None:
    result = asyncio.run(run_pipeline(f"{ASSESS_SITE}/", settings_for(tmp_path), engine,
                                      transport=httpx.MockTransport(FakeSite(ROUTES))))  # fmt: skip
    assert result["assessment"]["method"] == "fa-v3"
    with engine.connect() as conn:
        rows = conn.execute(
            select(FieldAssessment.field, FieldAssessment.product_id, FieldAssessment.state,
                   FieldAssessment.value)
            .where(FieldAssessment.organization_id == result["organization_id"])
            .order_by(FieldAssessment.id.desc())
        ).all()  # fmt: skip
    latest: dict[tuple[str, int | None], Any] = {}
    for r in rows:
        latest.setdefault((r.field, r.product_id), r)
    assert latest[("founded_year", None)].state == "SUPPORTED"
    assert latest[("employee_count", None)].state == "UNSUPPORTED"  # high risk, company only
    assert latest[("employee_count", None)].value["value"] == 50
    assert latest[("valuation", None)].state == "UNKNOWN"
    assert latest[("number_of_products", None)].value["value"] == 1
    product_rows = [r for (f, p), r in latest.items() if p is not None]
    assert {r.field for r in product_rows} >= {"key_feature", "price", "product_description"}
    assert next(r for r in product_rows if r.field == "price").state == "UNKNOWN"


def test_thresholds_are_configurable_and_recorded() -> None:
    from gtm_crawler.assess import method_version
    from gtm_crawler.config import AssessmentSettings

    weak = obs("headquarters", "Austin", {"value": "Austin"}, confidence=0.65)
    (default,) = assess_group("headquarters", None, [weak])
    assert default.state == "SUPPORTED"
    strict = AssessmentSettings(verified_confidence=0.9, found_confidence=0.7)
    (a,) = assess_group("headquarters", None, [weak], strict)
    assert a.state == "UNSUPPORTED"
    assert method_version() == "fa-v3"
    assert method_version(strict) == "fa-v3(verified=0.9,found=0.7,stale_after_days=730)"
