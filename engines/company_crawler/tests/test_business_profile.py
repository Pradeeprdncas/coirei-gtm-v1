"""The Business Intelligence Profile: every field with its value, status, confidence,
extraction date and sources; what is missing; the user's answers over the evidence."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from gtm_crawler.business_profile import REGISTRY, SECTIONS, assemble, field_status

SITE = "https://acme.example"


def cite(obs: int, when: str, url: str = f"{SITE}/") -> dict[str, Any]:
    return {
        "observation_id": obs,
        "url": url,
        "quote": f"quote {obs}",
        "value_verbatim": "v",
        "source_type": "company_website",
        "source_tier": 2,
        "retrieved_at": when,
    }


def dossier(company: dict[str, Any], products: list[dict[str, Any]] | None = None) -> dict:
    return {
        "company": {"domain": "acme.example", "fields": company},
        "products": products or [],
        "competitors": [],
        "candidate_competitors": [],
    }


def bip(
    d: dict, profile: dict | None = None, answers: list | None = None, job: str = "completed"
) -> dict[str, Any]:
    return assemble(
        1,
        d,
        {"status": "ready", "profile": profile} if profile else None,
        answers or [],
        {"crawl_id": 7, "status": job},
    )


def field(view: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for s in view["sections"] for f in s.get("fields", []) if f["key"] == key)


@pytest.mark.parametrize(
    ("state", "has_value", "active", "answer", "access", "expected"),
    [
        ("SUPPORTED", True, False, None, "public", "Extracted"),
        ("CORROBORATED", True, False, None, "public", "Verified"),
        ("MODEL_INFERRED", True, False, None, "public", "Inferred"),
        ("CONFLICTING", True, False, None, "public", "Conflicting"),
        ("UNSUPPORTED", True, False, None, "public", "Needs confirmation"),
        ("STALE", True, False, None, "public", "Needs confirmation"),
        ("UNKNOWN", False, True, None, "public", "Extracting"),
        ("UNKNOWN", False, False, None, "public", "Not found"),
        # Only the user can say: research never "extracts" it.
        ("UNKNOWN", False, True, None, "private", "Not found"),
        ("MODEL_INFERRED", True, False, "confirmed", "public", "Verified"),
        ("UNKNOWN", False, False, "edited", "private", "Verified"),
        ("SUPPORTED", True, False, "rejected", "public", "Not found"),
    ],
)
def test_field_status(
    state: str, has_value: bool, active: bool, answer: str | None, access: str, expected: str
) -> None:
    assert field_status(state, has_value, active, answer, access) == expected


def test_registry_has_the_thirteen_sections_and_the_spec_fields() -> None:
    assert [k for k, _ in SECTIONS] == [
        "company_profile",
        "products_services",
        "business_model",
        "target_customers",
        "brand_positioning",
        "marketing_footprint",
        "sales_commercial",
        "customer_evidence",
        "content_assets",
        "business_objectives",
        "operational_constraints",
        "missing_information",
        "source_records",
    ]
    keys = {f.key for f in REGISTRY}
    assert len(keys) == len(REGISTRY)
    for key in (
        "company_name",
        "website",
        "other_domains",
        "market_domain",
        "headquarters",
        "company_location",
        "geography_served",
        "company_description",
        "company_stage",
        "employee_count",
        "founded_year",
        "founders",
        "contact_email",
        "contact_phone",
        "contact_page",
        "customer_type",
        "revenue_model",
        "price",
        "target_segments",
        "icp_hypotheses",
        "product_positioning",
        "mission",
        "vision",
        "brand_values",
        "brand_promise",
        "positioning_statement",
        "tagline",
        "key_messages",
        "selling_points",
        "customer_claims",
        "tone_of_voice",
        "brand_personality",
        "calls_to_action",
        "messaging_consistency",
        "differentiators",
        "business_types",
        "company_sizes",
        "buyer_roles",
        "social_profiles",
        "marketing_channels",
        "monthly_marketing_budget",
        "average_order_value",
        "monthly_revenue_target",
        "monthly_leads",
        "lead_conversion_rate",
        "sales_team",
        "crm_platforms",
        "enquiry_process",
        "customer",
        "case_study",
        "customer_proof",
        "documents",
        "primary_objective",
        "products_to_promote",
        "priority_markets",
        "customer_capacity",
    ):
        assert key in keys, key
    assert {f.access for f in REGISTRY} <= {"public", "document", "private", "integration"}
    assert all(f.section in dict(SECTIONS) for f in REGISTRY)


def test_evidence_fields_carry_status_date_and_sources() -> None:
    view = bip(
        dossier(
            {
                "company_name": {
                    "state": "SUPPORTED",
                    "value": "Acme",
                    "confidence": 0.7,
                    "citations": [
                        cite(1, "2026-11-01T10:00:00+00:00"),
                        cite(2, "2026-11-03T10:00:00+00:00"),
                    ],
                },
                "founded_year": {"state": "UNKNOWN", "citations": []},
            }
        )
    )
    name = field(view, "company_name")
    assert name["value"] == "Acme" and name["status"] == "Extracted"
    assert name["confidence"] == 0.7 and name["origin"] == "crawler"
    assert name["extracted_at"] == "2026-11-03T10:00:00+00:00"
    assert name["sources"][0] == {
        "url": f"{SITE}/",
        "quote": "quote 1",
        "tier": 2,
        "source_type": "company_website",
        "retrieved_at": "2026-11-01T10:00:00+00:00",
    }
    founded = field(view, "founded_year")
    assert founded["value"] is None and founded["status"] == "Not found"
    assert founded["sources"] == [] and founded["extracted_at"] is None


def test_a_document_source_is_the_origin() -> None:
    doc = cite(3, "2026-11-02T00:00:00+00:00", "upload://acme.example/documents/ab/deck.pdf")
    view = bip(dossier({"founded_year": {"state": "SUPPORTED", "value": 2019, "citations": [doc]}}))
    assert field(view, "founded_year")["origin"] == "document"


def test_model_values_are_inferred_and_dated_by_the_evidence_they_cite() -> None:
    d = dossier(
        {
            "company_name": {
                "state": "SUPPORTED",
                "value": "Acme",
                "citations": [cite(5, "2026-10-30T00:00:00+00:00")],
            }
        }
    )
    leaf = {
        "value": "Robotics",
        "evidence_confidence": 0.6,
        "citations": [{"observation_id": 5, "url": f"{SITE}/", "quote": "quote 5"}],
    }
    view = bip(
        d,
        {
            "market": {"category": leaf, "geography": [leaf]},
            "differentiators": [{**leaf, "value": "Fastest arms"}],
            "business_model": {"customer_type": {**leaf, "value": "b2b"}},
            "value_proposition": {
                "proof": [{"kind": {"value": "award"}, "claim": {**leaf, "value": "Best 2025"}}]
            },
        },
    )
    industry = field(view, "market_domain")
    assert industry["value"] == ["Robotics"] and industry["status"] == "Inferred"
    assert industry["origin"] == "model" and industry["confidence"] == 0.6
    assert industry["extracted_at"] == "2026-10-30T00:00:00+00:00"
    assert field(view, "geography_served")["value"] == ["Robotics"]
    assert field(view, "differentiators")["value"] == ["Fastest arms"]
    assert field(view, "customer_type")["value"] == "b2b"
    assert field(view, "customer_proof")["value"] == [{"kind": "award", "claim": "Best 2025"}]


def test_the_model_s_structured_value_wins_over_the_raw_evidence() -> None:
    d = dossier(
        {
            "company_name": {
                "state": "CORROBORATED",
                "value": "coirei",
                "confidence": 0.9,
                "citations": [cite(1, "2026-11-01T00:00:00+00:00")],
            },
            "legal_name": {
                "state": "CONFLICTING",
                "value": None,
                "citations": [
                    cite(2, "2026-11-01T00:00:00+00:00"),
                    {**cite(3, "2026-11-01T00:00:00+00:00"), "value_verbatim": "w"},
                ],
            },
        }
    )
    leaf = {
        "value": "Coirei",
        "evidence_confidence": 0.8,
        "citations": [{"observation_id": 1, "url": f"{SITE}/", "quote": "quote 1"}],
    }
    view = bip(
        d,
        {"company": {"name": leaf, "legal_name": {**leaf, "value": "Coirei Innovations Pvt Ltd"}}},
    )
    name = field(view, "company_name")
    # The model's clean value, still backed by the corroborated evidence it rests on.
    assert name["value"] == "Coirei" and name["evidence_value"] == "coirei"
    assert name["status"] == "Verified" and name["origin"] == "model"
    assert name["confidence"] == 0.9 and name["sources"][0]["quote"] == "quote 1"
    legal = field(view, "legal_name")
    # The model picked one; the sources still disagree, so the user is asked.
    assert legal["value"] == "Coirei Innovations Pvt Ltd" and legal["status"] == "Conflicting"
    assert legal["evidence_value"] == ["v", "w"]


def test_the_user_s_answer_wins_and_the_crawler_value_is_kept() -> None:
    d = dossier(
        {
            "company_name": {
                "state": "SUPPORTED",
                "value": "acme",
                "citations": [cite(1, "2026-11-01T00:00:00+00:00")],
            },
            "headquarters": {
                "state": "SUPPORTED",
                "value": "Paris",
                "citations": [cite(2, "2026-11-01T00:00:00+00:00")],
            },
            "founded_year": {
                "state": "CORROBORATED",
                "value": 2019,
                "citations": [cite(3, "2026-11-01T00:00:00+00:00")],
            },
        }
    )
    answers = [
        {
            "section": "company_info",
            "field": "company_name",
            "item_key": "",
            "status": "edited",
            "value": "Acme Robotics",
            "verified_at": "2026-11-10T09:00:00+00:00",
        },
        {
            "section": "company_info",
            "field": "headquarters",
            "item_key": "",
            "status": "rejected",
            "value": None,
            "verified_at": "2026-11-10T09:00:00+00:00",
        },
    ]
    view = bip(d, answers=answers)
    name = field(view, "company_name")
    assert name["value"] == "Acme Robotics" and name["status"] == "Verified"
    assert name["origin"] == "user" and name["crawler_value"] == "acme"
    assert name["verified_by"] == "user" and name["extracted_at"] == "2026-11-10T09:00:00+00:00"
    hq = field(view, "headquarters")
    assert hq["value"] is None and hq["status"] == "Not found" and hq["crawler_value"] == "Paris"
    founded = field(view, "founded_year")
    assert founded["status"] == "Verified" and founded["verified_by"] == "sources"


def test_missing_information_lists_open_fields_once_research_is_done() -> None:
    d = dossier(
        {
            "company_name": {
                "state": "SUPPORTED",
                "value": "Acme",
                "citations": [cite(1, "2026-11-01T00:00:00+00:00")],
            },
            "legal_name": {
                "state": "CONFLICTING",
                "value": None,
                "citations": [
                    cite(2, "2026-11-01T00:00:00+00:00"),
                    {**cite(3, "2026-11-01T00:00:00+00:00"), "value_verbatim": "w"},
                ],
            },
        }
    )
    done = bip(d)
    missing = {m["key"]: m for m in done["sections"][11]["items"]}
    assert "company_name" not in missing
    assert missing["legal_name"]["status"] == "Conflicting"
    assert missing["founded_year"]["status"] == "Not found"
    assert missing["monthly_marketing_budget"]["access"] == "private"
    # While research runs, a public field not found yet is still being extracted.
    running = bip(d, job="running")
    assert field(running, "founded_year")["status"] == "Extracting"
    open_keys = {m["key"] for m in running["sections"][11]["items"]}
    assert "founded_year" not in open_keys and "monthly_marketing_budget" in open_keys


def test_source_records_list_every_field_with_sources() -> None:
    d = dossier(
        {
            "company_name": {
                "state": "SUPPORTED",
                "value": "Acme",
                "confidence": 0.7,
                "citations": [cite(1, "2026-11-01T00:00:00+00:00")],
            }
        }
    )
    records = bip(d)["sections"][12]["items"]
    assert records == [
        {
            "key": "company_name",
            "section": "company_profile",
            "status": "Extracted",
            "confidence": 0.7,
            "extracted_at": "2026-11-01T00:00:00+00:00",
            "sources": field(bip(d), "company_name")["sources"],
        }
    ]


def test_products_come_from_the_evidence_or_the_model() -> None:
    product = {
        "product_id": 4,
        "name": "Arm",
        "fields": {
            "product_description": {
                "state": "SUPPORTED",
                "value": "A robot arm",
                "citations": [cite(9, "2026-11-01T00:00:00+00:00")],
            }
        },
    }
    found = bip(dossier({}, [product]))["sections"][1]["items"]
    assert [(p["value"], p["status"], p["item_key"]) for p in found] == [("Arm", "Extracted", "4")]
    assert found[0]["details"]["description"] == "A robot arm"
    leaf = {
        "value": "Gripper",
        "evidence_confidence": None,
        "citations": [{"url": f"{SITE}/", "quote": "q"}],
    }
    model = {"catalogue": {"offerings": [{"name": leaf}]}}
    inferred = bip(dossier({}), model)["sections"][1]["items"]
    assert [(p["value"], p["status"]) for p in inferred] == [("Gripper", "Inferred")]


def test_the_models_offerings_replace_crawled_product_names() -> None:
    def crawled(pid: int, name: str) -> dict[str, Any]:
        return {"product_id": pid, "name": name, "fields": {}}

    def offering(name: str) -> dict[str, Any]:
        return {"name": {"value": name, "evidence_confidence": None,
                         "citations": [{"url": f"{SITE}/", "quote": "q"}]}}  # fmt: skip

    # A pricing page's bullets ("Unlimited runs") are not products; the user's answer holds.
    products = [crawled(4, "Arm"), crawled(5, "Unlimited runs"), crawled(6, "Kept")]
    answers = [{"section": "products_services", "field": "product", "item_key": "6",
                "status": "confirmed", "value": "Kept",
                "verified_at": "2026-11-10T09:00:00+00:00"}]  # fmt: skip
    model = {"catalogue": {"offerings": [offering("arm"), offering("Gripper")]}}
    view = bip(dossier({}, products), model, answers)
    items = view["sections"][1]["items"]
    assert [(i["value"], i["item_key"]) for i in items] == [
        ("arm", "4"),
        ("Gripper", "llm:Gripper"),
        ("Kept", "6"),
    ]


@pytest.mark.db
def test_business_profile_endpoint(engine: Engine, tmp_path: Path) -> None:
    from .test_crawl import settings_for
    from .test_onboarding_api import crawled

    api, company = crawled(engine, settings_for(tmp_path), "bip-view")
    view = api.get(f"/v1/companies/{company}/business-profile").json()
    assert [s["key"] for s in view["sections"]] == [k for k, _ in SECTIONS]
    assert view["crawl"]["status"] == "COMPLETED" and view["domain"] == "bip-view.example"
    price = field(view, "price")
    assert price["value"] and price["status"] in {"Extracted", "Verified", "Needs confirmation"}
    assert price["extracted_at"] and price["sources"]
    assert field(view, "founded_year")["status"] == "Not found"
    assert api.get("/v1/companies/999999/business-profile").status_code == 404
    # The onboarding view gains the status and the date beside its evidence state.
    onboarding = api.get(f"/v1/companies/{company}/onboarding").json()
    first = onboarding["sections"][0]["items"][0]
    assert "status" in first and "extracted_at" in first


def test_modules_and_plans_are_not_products() -> None:
    """One platform with modules (an agent, a feature) is one product, its modules among
    its components; a plan is not a product either, even when the model lists one."""

    def named(value: str) -> dict[str, Any]:
        return {"value": value, "evidence_confidence": None,
                "citations": [{"url": f"{SITE}/", "quote": "q"}]}  # fmt: skip

    modules = ["Marketing Intelligence", "Lead Nurturing", "Engage Agent"]
    # The page extraction found each module as a product.
    crawled = [{"product_id": i, "name": m, "fields": {}} for i, m in enumerate(modules, 1)]
    model = {
        "business_model": {"plans": [named("Pro")]},
        "catalogue": {"offerings": [
            {"name": named("Coirei GTM"), "type": named("product"),
             "components": [named(m) for m in modules]},
            {"name": named("engage agent")},  # a module the model also listed: dropped
            {"name": named("Pro")},  # a plan
        ]},
    }  # fmt: skip
    items = bip(dossier({}, crawled), model)["sections"][1]["items"]
    assert [i["value"] for i in items] == ["Coirei GTM"]
    assert items[0]["details"]["components"] == modules
    assert items[0]["details"]["type"] == "product"


def test_target_customers_come_from_the_icp_part() -> None:
    def cited(value: Any) -> dict[str, Any]:
        return {"value": value, "evidence_confidence": 0.5,
                "citations": [{"url": f"{SITE}/", "quote": "q"}]}  # fmt: skip

    view = bip(dossier({}), {"icp": {
        "business_types": [cited("B2B SaaS startups")],
        "company_sizes": [cited("Small businesses")],
        "buyer_roles": [cited("Founders"), cited("Sales teams")],
        "industries": [{"name": cited("Real estate developers"),
                        "why": cited("Case study with a Pune developer")}],
        "use_cases": [cited("Finding leads")],
    }})  # fmt: skip
    assert field(view, "business_types")["value"] == ["B2B SaaS startups"]
    assert field(view, "company_sizes")["value"] == ["Small businesses"]
    assert field(view, "buyer_roles")["value"] == ["Founders", "Sales teams"]
    assert field(view, "target_industries")["value"] == [
        {"name": "Real estate developers", "why": "Case study with a Pune developer"}
    ]
    assert field(view, "main_use_case")["value"] == ["Finding leads"]


def test_every_value_has_a_confidence() -> None:
    def pages(*urls: str) -> dict[str, Any]:
        return {"value": "x", "evidence_confidence": None,
                "citations": [{"url": u, "quote": "q"} for u in urls]}  # fmt: skip

    view = bip(dossier({}), {"icp": {"business_types": [pages(f"{SITE}/", f"{SITE}/a")],
                                     "buyer_roles": [pages(*(f"{SITE}/{i}" for i in "abcd"))]},
                             "brand": {"mission": pages(f"{SITE}/")}})  # fmt: skip
    # A model's reading of pages nothing assessed: pages combine, below Supported (0.60).
    assert field(view, "mission")["confidence"] == 0.3
    assert field(view, "business_types")["confidence"] == 0.51
    assert field(view, "buyer_roles")["confidence"] == 0.59
    assert field(view, "vision")["confidence"] is None  # no value, no confidence
    answers = [
        {
            "section": "brand_positioning",
            "field": "mission",
            "item_key": "",
            "status": "edited",
            "value": "Ours",
            "verified_at": "2026-11-10T09:00:00+00:00",
        }
    ]
    edited = bip(dossier({}), {"brand": {"mission": pages(f"{SITE}/")}}, answers)
    assert field(edited, "mission")["confidence"] == 1.0
