"""Phase 11: spec field states, STALE, confidence components, evidence roles."""

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, insert, select

from gtm_crawler.assess import Obs, assess_group, method_version
from gtm_crawler.config import AssessmentSettings
from gtm_crawler.db.models import Corroboration, FieldAssessment, Organization
from gtm_crawler.dossier import _claim_note, build_dossier
from gtm_crawler.pipeline import run_pipeline

from .test_assess import ASSESS_SITE, ROUTES, eq
from .test_crawl import FakeSite, settings_for

TODAY = date(2026, 9, 29)
_ids = iter(range(50_000, 60_000))


def obs(field: str, value: str, normalized: dict[str, Any] | None = None, *,
        source: str = "acme.com", tier: int = 1, first_party: bool = True,
        evidence_date: date | None = None, period: int | None = None,
        confidence: float = 0.81) -> Obs:  # fmt: skip
    return Obs(next(_ids), field, None, value, normalized, None, confidence, tier, first_party,
               source, period, evidence_date)  # fmt: skip


def one(field: str, *observations: Obs, **kw: Any) -> Any:
    (a,) = assess_group(field, None, list(observations), today=TODAY, **kw)
    return a


def test_old_time_varying_figure_is_stale_but_keeps_its_value() -> None:
    old = [obs("customer_count", "620 customers", eq(620), source=s, tier=t, first_party=fp,
               evidence_date=date(2023, 12, 31))
           for s, t, fp in (("acme.com", 1, True), ("sec.gov", 1, False))]  # fmt: skip
    a = one("customer_count", *old)
    assert a.state == "STALE" and a.value["value"] == 620
    assert a.value["evidence_date"] == "2023-12-31"
    assert a.components["age_days"] == (TODAY - date(2023, 12, 31)).days

    recent = [replace(o, evidence_date=date(2025, 6, 1)) for o in old]
    assert one("customer_count", *recent).state == "CORROBORATED"


def test_staleness_needs_support_and_a_time_varying_field() -> None:
    long_ago = date(2010, 1, 1)
    # Founding years do not go stale.
    assert one("founded_year", obs("founded_year", "2019", eq(2019),
                                   evidence_date=long_ago)).state == "SUPPORTED"  # fmt: skip
    # A claim stays a claim: STALE never upgrades an unsupported value.
    claim = obs("employee_count", "45 employees", eq(45), evidence_date=long_ago)
    assert one("employee_count", claim).state == "UNSUPPORTED"


def test_stale_threshold_is_configurable_and_recorded() -> None:
    o = [obs("open_roles", "12 open roles", eq(12), evidence_date=date(2026, 1, 1))]
    strict = AssessmentSettings(stale_after_days=90)
    assert one("open_roles", *o).state == "SUPPORTED"
    assert one("open_roles", *o, thresholds=strict).state == "STALE"
    assert "stale_after_days=90" in method_version(strict)


def test_confidence_components_are_recorded() -> None:
    a = one("headquarters",
            obs("headquarters", "Austin", source="acme.com", confidence=0.8),
            obs("headquarters", "Austin", source="acme.com", confidence=0.6),
            obs("headquarters", "Austin, Texas", source="crunchbase.com", tier=3,
                first_party=False, confidence=0.7))  # fmt: skip
    c = a.components
    assert c["independent_sources"] == 2 and c["beyond_company"] and c["strong_source"]
    assert c["sources"] == [
        {"source": "acme.com", "tier": 1, "first_party": True, "best_confidence": 0.8,
         "observations": 2},
        {"source": "crunchbase.com", "tier": 3, "first_party": False, "best_confidence": 0.7,
         "observations": 1},
    ]  # fmt: skip
    assert a.confidence == round(1 - (1 - 0.8) * (1 - 0.7), 2)
    assert c["thresholds"] == {"corroborated": 0.8, "supported": 0.6, "stale_after_days": 730}


def test_evidence_roles() -> None:
    bound = obs("customer_count", "more than 500 customers", {"qualifier": "gt", "value": 500})
    exact = obs("customer_count", "620 customers", eq(620), source="sec.gov", first_party=False)
    a = one("customer_count", bound, exact)
    assert sorted(r for _, _, r in a.roles) == ["qualifies", "supports"]

    a = one("customer_count", obs("customer_count", "620", eq(620)),
            obs("customer_count", "90", eq(90), source="news.com", first_party=False))  # fmt: skip
    assert a.state == "CONFLICTING" and {r for _, _, r in a.roles} == {"contradicts"}
    assert a.value is None and len(a.components["sources"]) == 2

    older = obs("customer_count", "300", eq(300), period=2022)
    newer = obs("customer_count", "620", eq(620), period=2025)
    a = one("customer_count", older, newer)
    roles = {oid: r for oid, _, r in a.roles}
    assert roles[older.id] == "superseded" and roles[newer.id] == "supports"
    assert a.observation_ids == [newer.id]


# Its own company, so other tests' evidence for the same organization never applies.
P11_SITE = "https://p11-acme.example"
P11_ROUTES = {url.replace(ASSESS_SITE, P11_SITE): page for url, page in ROUTES.items()}


@pytest.mark.db
def test_assessments_store_components_and_corroboration(engine: Engine, tmp_path: Path) -> None:
    transport = httpx.MockTransport(FakeSite(P11_ROUTES))
    result = asyncio.run(
        run_pipeline(f"{P11_SITE}/", settings_for(tmp_path), engine, transport=transport)
    )
    org = result["organization_id"]
    with engine.connect() as conn:
        latest = conn.execute(
            select(FieldAssessment.id, FieldAssessment.state, FieldAssessment.observation_ids,
                   FieldAssessment.confidence_components)
            .where(FieldAssessment.organization_id == org, FieldAssessment.field == "founded_year")
            .order_by(FieldAssessment.id.desc()).limit(1)
        ).one()  # fmt: skip
        roles = conn.execute(
            select(Corroboration.observation_id, Corroboration.source, Corroboration.role)
            .where(Corroboration.assessment_id == latest.id)
        ).all()  # fmt: skip
    assert latest.state == "SUPPORTED"
    assert latest.confidence_components["independent_sources"] == 1
    assert {r.observation_id for r in roles} == set(latest.observation_ids)
    assert {(r.source, r.role) for r in roles} == {("p11-acme.example", "supports")}

    dossier = build_dossier(engine, org)["company"]["fields"]
    claim = dossier["employee_count"]
    assert claim["state"] == "UNSUPPORTED" and "value" not in claim
    assert claim["claimed_value"] == 50 and "only the company states it" in claim["note"]


def test_claim_note_names_the_reason() -> None:
    weak = {"high_risk": False, "beyond_company": True}
    assert "too weak" in _claim_note(weak)
    assert "only the company" in _claim_note({"high_risk": True, "beyond_company": False})
    assert _claim_note(None) == "claim, not fact: the evidence does not support it"  # fa-v1


@pytest.mark.db
def test_database_accepts_spec_and_legacy_states_only(engine: Engine) -> None:
    from sqlalchemy.exc import IntegrityError

    with engine.connect() as conn:
        org = conn.execute(insert(Organization).values(name="States Inc")
                           .returning(Organization.id)).scalar_one()  # fmt: skip

        def row(state: str, value: Any = None) -> None:
            conn.execute(insert(FieldAssessment).values(
                organization_id=org, field="website", state=state, value=value,
                confidence=0.7 if value else None, observation_ids=[1] if value else [],
                method_version="t"))  # fmt: skip

        row("STALE", {"value": "x"})
        row("FOUND", {"value": "x"})  # fa-v1 history stays valid
        row("UNKNOWN")
        with pytest.raises(IntegrityError):
            row("UNSUPPORTED")  # a claim without its value
        conn.rollback()
        with pytest.raises(IntegrityError):
            row("PROBABLE", {"value": "x"})
        conn.rollback()
