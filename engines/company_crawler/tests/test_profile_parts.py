"""Phase 4: the model fills every public profile field, in parts a small model can answer:
offering details, brand and messaging, ideal-customer hypotheses, funding and traction."""

from typing import Any

from gtm_crawler import profile

from .test_business_profile import bip, cite, dossier, field
from .test_profile import REFS as _REFS
from .test_profile import leaf, stats

# The page names what the tests' answers name: a name no line has is dropped.
NAMES = (
    "Data Science, Rival, Other, Globex, Acme, Acme Platform, Engage Agent, "
    "Lead-Nurturing, Pro, Reports"
)
REFS = {**_REFS, "e2": {**_REFS["e2"], "text": f"{_REFS['e2']['text']} {NAMES}."}}

LEAF = {
    "evidence_confidence": 0.6,
    "citations": [{"observation_id": 9, "url": "https://acme.example/", "quote": "q"}],
}


def cited(value: Any) -> dict[str, Any]:
    return {"value": value, **LEAF}


def test_the_profile_is_made_of_six_parts() -> None:
    assert [(v, k) for v, _, _, k in profile.PARTS] == [
        ("profile-10", None),
        ("value-prop-2", "value_proposition"),
        ("facts-1", "facts"),
        ("offerings-5", "catalogue"),
        ("brand-1", "brand"),
        ("icp-4", "icp"),
    ]


def test_offering_details_keep_only_their_choices() -> None:
    s = stats()
    out = profile.clean(profile.OFFERINGS_SCHEMA, {"offerings": [{
        "name": leaf("Data Science", "e2"),
        "benefits": [leaf("Job-ready skills", "e2")],
        "pricing_model": leaf("custom_quote", "e2"),
        "maturity": leaf("alpha", "e2"),  # not a choice
        "target_users": [leaf("Graduates", "e2")],
    }]}, REFS, s)  # fmt: skip
    offering = out["offerings"][0]
    assert set(offering) == {"name", "benefits", "pricing_model", "target_users"}
    assert s["invalid"] == 1


def test_brand_and_icp_schemas_have_the_spec_fields() -> None:
    assert set(profile.BRAND_SCHEMA) >= {
        "mission", "vision", "values", "promise", "positioning_statement", "tagline",
        "key_messages", "selling_points", "claims", "calls_to_action", "tone_of_voice",
        "personality", "consistency", "reasoning",
    }  # fmt: skip
    assert set(profile.ICP_SCHEMA) == {"reasoning", "stated_target_market", "business_types",
                                       "company_sizes", "buyer_roles", "industries",
                                       "use_cases", "hypotheses"}  # fmt: skip
    # Offerings and who they serve are their own calls, not the main one's.
    assert "offerings" not in profile.SCHEMA
    assert set(profile.SCHEMA["target_customers"]) == {"named_customers", "case_studies"}
    # Funding and traction are their own small call: a bigger main schema made the
    # small model drop or misplace whole sections (a SaaS site lost its business model).
    assert not set(profile.SCHEMA) & {"funding", "traction"}
    assert "ownership" not in profile.SCHEMA["company"]
    assert set(profile.FACTS_SCHEMA) == {"ownership", "total_funding", "funding_rounds",
                                         "investors", "valuation", "customer_count",
                                         "annual_revenue", "arr"}  # fmt: skip
    assert "plans" in profile.SCHEMA["business_model"]
    assert "case_studies" in profile.SCHEMA["target_customers"]


def test_a_customer_count_is_any_figure_the_evidence_has() -> None:
    def count(value: Any) -> Any:
        out = profile.clean(profile.FACTS_SCHEMA, {"customer_count": leaf(value, "e1")},
                            REFS, stats())  # fmt: skip
        return out and out["customer_count"]["value"]

    # A year-sized range check once dropped every real count.
    assert count(15000) == 15000
    assert count("15,000+") == "15,000+"
    assert count("many") is None
    assert count(0) is None
    assert count(99) is None  # not in the cited line


def test_a_competitors_domain_is_a_host_name() -> None:
    out = profile.clean(profile.SCHEMA, {"competitors": [
        {"name": leaf("Rival", "e2"), "domain": leaf("Bakery", "e2")},
        {"name": leaf("Other", "e2"), "domain": leaf("www.other.co.uk", "e2")},
    ]}, REFS, stats())  # fmt: skip
    assert [(c["name"]["value"], "domain" in c) for c in out["competitors"]] == [
        ("Rival", False), ("Other", True)]  # fmt: skip


def test_a_section_the_model_nested_in_another_is_put_back() -> None:
    s = stats()
    out = profile.clean(profile.SCHEMA, profile.lift(profile.SCHEMA, {
        "company": {"name": leaf("Acme", "e2")},
        "business_model": {
            "customer_type": leaf("b2b", "e2"),
            # The model closed business_model too late: these belong at the top.
            "competitors": [{"name": leaf("Globex", "e2")}],
            "market": {"category": leaf("Training", "e2")},
        },
    }), REFS, s)  # fmt: skip
    assert out["business_model"]["customer_type"]["value"] == "b2b"
    assert out["competitors"][0]["name"]["value"] == "Globex"
    assert out["market"]["category"]["value"] == "Training"
    # A key the section itself has stays where it is.
    nested = {"business_model": {"plans": [leaf("Pro", "e2")]}, "plans": "junk"}
    assert profile.lift(profile.SCHEMA, nested)["business_model"]["plans"]


def test_every_part_fills_the_profile() -> None:
    product = {"product_id": 4, "name": "Arm", "fields": {
        "product_description": {"state": "SUPPORTED", "value": "A robot arm",
                                "citations": [cite(9, "2026-11-01T00:00:00+00:00")]}}}  # fmt: skip
    model = {
        "brand": {"mission": cited("Robots for every warehouse"),
                  "tone_of_voice": cited("plain and confident"),
                  "calls_to_action": [cited("Book a demo")]},
        "icp": {"hypotheses": [{"segment": cited("Mid-size 3PL warehouses"),
                                "why": cited("Case studies are all 3PLs")}]},
        "catalogue": {"offerings": [{"name": cited("arm"),
                                            "benefits": [cited("Fewer picking errors")],
                                            "maturity": cited("beta")}]},
        "facts": {"investors": [cited("Northwind Ventures")], "customer_count": cited(120)},
        "value_proposition": {"proof": [{"kind": {"value": "award"},
                                         "claim": cited("Best robot 2025")}]},
    }  # fmt: skip
    view = bip(dossier({}, [product]), model)
    assert field(view, "mission")["value"] == "Robots for every warehouse"
    assert field(view, "tone_of_voice")["status"] == "Inferred"
    assert field(view, "calls_to_action")["value"] == ["Book a demo"]
    assert field(view, "customer_count")["value"] == 120
    # A composite item keeps its parts.
    assert field(view, "customer_proof")["value"] == [{"kind": "award",
                                                       "claim": "Best robot 2025"}]  # fmt: skip
    icp = field(view, "icp_hypotheses")
    assert icp["value"] == [{"segment": "Mid-size 3PL warehouses",
                             "why": "Case studies are all 3PLs"}]  # fmt: skip
    assert icp["status"] == "Inferred"
    # A hypothesis stays an unresolved assumption until the user confirms it.
    open_items = {m["key"]: m for m in view["sections"][11]["items"]}
    assert open_items["icp_hypotheses"]["reason"].startswith("hypothesis")
    # Offering details join their offering by name, whatever its case.
    arm = view["sections"][1]["items"][0]
    assert arm["details"]["benefits"] == ["Fewer picking errors"]
    assert arm["details"]["maturity"] == "beta"


def test_the_catalogue_lists_offerings_with_their_components() -> None:
    s = stats()
    out = profile.clean(profile.OFFERINGS_SCHEMA, {
        "reasoning": "One platform with modules [e2].",
        "offerings": [
            {"name": leaf("Acme Platform", "e2"), "type": leaf("product", "e2"),
             "components": [leaf("Engage Agent", "e2"), leaf("Lead Nurturing", "e2")]},
            {"name": leaf("Pro", "e2"), "type": leaf("plan", "e2")},  # plan: not a type
        ],
    }, REFS, s)  # fmt: skip
    platform, pro = out["offerings"]
    assert [c["value"] for c in platform["components"]] == ["Engage Agent", "Lead Nurturing"]
    assert "type" not in pro and s["invalid"] == 1
    assert out["reasoning"]["value"] == "One platform with modules."


def test_a_segment_names_someone_in_particular() -> None:
    s = stats()
    out = profile.clean(profile.ICP_SCHEMA, {
        "business_types": [leaf("Businesses", "e2"), leaf("B2B SaaS startups", "e2")],
        "company_sizes": [leaf("Small businesses", "e2"), leaf("all companies", "e2")],
        "industries": [{"name": leaf("Various industries", "e2"), "why": leaf("x", "e2")},
                       {"name": leaf("Real estate developers", "e2")},
                       {"why": leaf("no name", "e2")}],
    }, REFS, s)  # fmt: skip
    assert [b["value"] for b in out["business_types"]] == ["B2B SaaS startups"]
    assert [b["value"] for b in out["company_sizes"]] == ["Small businesses"]
    assert [i["name"]["value"] for i in out["industries"]] == ["Real estate developers"]
    assert s["invalid"] == 3
    market = profile.clean(profile.SCHEMA, {"market": {
        "category": leaf("Technology", "e2"),
        "sub_category": leaf("Sales engagement software", "e2")}}, REFS, stats())  # fmt: skip
    assert set(market["market"]) == {"sub_category"}


def test_an_answer_cut_off_mid_list_keeps_what_was_complete() -> None:
    # The model repeated a list until its token budget ran out, mid-string.
    text = ('{"sold_as": "one_product", "offerings": [{"name": {"value": "Acme", '
            '"evidence": ["e2"]}, "components": [{"value": "Reports", "evidence": ["e2"]}, '
            '{"value": "Reports", "evidence": ["e2"]}, {"value": "Rep')  # fmt: skip
    data = profile._json(text)
    out = profile.clean(profile.OFFERINGS_SCHEMA, data, REFS, stats())
    assert out["sold_as"] == "one_product"
    (acme,) = out["offerings"]
    # Each value once: the repetition is gone.
    assert [c["value"] for c in acme["components"]] == ["Reports"]


def test_one_product_folds_the_other_products_into_its_components() -> None:
    from gtm_crawler.onboarding import offerings_of

    def offering(name: str, kind: str | None = None, *parts: str) -> dict[str, Any]:
        o: dict[str, Any] = {"name": cited(name)}
        if kind:
            o["type"] = cited(kind)
        if parts:
            o["components"] = [cited(p) for p in parts]
        return o

    catalogue = {"sold_as": "one_product", "main_offering": cited("Acme GTM"),
                 "offerings": [offering("Engage Agent", "product"),
                               offering("Acme GTM", "product", "Reports"),
                               offering("Setup", "service"),
                               offering("engage agent")]}  # fmt: skip
    out = offerings_of({"catalogue": catalogue})
    assert [o["name"]["value"] for o in out] == ["Acme GTM", "Setup"]
    assert [c["value"] for c in out[0]["components"]] == ["Reports", "Engage Agent"]
    # Several products: each stays.
    several = offerings_of({"catalogue": {**catalogue, "sold_as": "several_products"}})
    assert [o["name"]["value"] for o in several] == ["Engage Agent", "Acme GTM", "Setup",
                                                     "engage agent"]  # fmt: skip


def test_a_name_no_line_has_is_dropped() -> None:
    s = stats()
    out = profile.clean(profile.SCHEMA, {"competitors": [
        {"name": leaf("Competitors in AI search strategy", "e2")},
        {"name": leaf("globex", "e2")},
    ]}, REFS, s)  # fmt: skip
    assert [c["name"]["value"] for c in out["competitors"]] == ["globex"]
    assert s["invalid"] == 1
    catalogue = profile.clean(profile.OFFERINGS_SCHEMA, {"offerings": [
        {"name": leaf("Acme", "e2"),
         "components": [leaf("lead nurturing", "e2"), leaf("AI-powered magic", "e2")]},
    ]}, REFS, stats())  # fmt: skip
    assert [c["value"] for c in catalogue["offerings"][0]["components"]] == ["lead nurturing"]


def test_the_catalogue_reads_pricing_right_after_home() -> None:
    pages = [("home", "/", ""), ("home", "/aeo", ""), ("about", "/about", ""),
             ("pricing", "/pricing", ""), ("product", "/product", "")]  # fmt: skip
    assert [u for _, u, _ in profile._for_catalogue(pages)] == [
        "/", "/pricing", "/product", "/aeo", "/about"]  # fmt: skip


def test_a_platform_is_sold_under_the_companys_name() -> None:
    from gtm_crawler.onboarding import offerings_of

    def offering(name: str, kind: str, *parts: str) -> dict[str, Any]:
        return {"name": cited(name), "type": cited(kind),
                "components": [cited(p) for p in parts]}  # fmt: skip

    subscription = {"revenue_model": cited("subscription")}
    # Clay: the model headed the one platform with a module, typed it a service.
    ads = offering("Activation Ads", "service", "Sequencer", "Reverse ETL")
    clay = offerings_of({"company": {"name": cited("Clay")}, "business_model": subscription,
                         "catalogue": {"sold_as": "services", "offerings": [ads]}})  # fmt: skip
    (platform,) = clay
    assert platform["name"]["value"] == "Clay" and platform["type"]["value"] == "product"
    assert [c["value"] for c in platform["components"]] == [
        "Activation Ads", "Sequencer", "Reverse ETL"]  # fmt: skip
    # A name carrying the brand stays; so do several separate offerings.
    gtm = offering("Coirei GTM", "product", "Engage Agent")
    one = {"sold_as": "one_product", "offerings": [gtm]}
    coirei = offerings_of({"company": {"name": cited("coirei")}, "catalogue": one})
    assert coirei[0]["name"]["value"] == "Coirei GTM"
    hubs = [offering("Sales Hub", "product", "a", "b"), offering("Service Hub", "product")]
    several = {"sold_as": "several_products", "offerings": hubs}
    suite = offerings_of({"company": {"name": cited("Acme")}, "catalogue": several})
    assert [o["name"]["value"] for o in suite] == ["Sales Hub", "Service Hub"]
