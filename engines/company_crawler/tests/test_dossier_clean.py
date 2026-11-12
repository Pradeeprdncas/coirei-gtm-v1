"""The clean view of a dossier: the GTM Parameters V1.0 headings, plain values, facts only."""

from typing import Any

from gtm_crawler.dossier import clean_dossier


def entry(state: str, value: Any = None, url: str = "https://acme.example/", **details: Any) -> Any:
    out: dict[str, Any] = {"state": state, "confidence": 0.7,
                           "citations": [{"url": url, "value_verbatim": str(value)}]}  # fmt: skip
    if value is not None:
        out["claimed_value" if state == "UNSUPPORTED" else "value"] = value
        out["details"] = details
    return out


def test_clean_dossier() -> None:
    fields = {
        "company_name": [entry("SUPPORTED", "Acme")],
        "headquarters": entry("SUPPORTED", "Pune, India"),
        "founded_year": entry("UNSUPPORTED", 2013, "https://other.example/acme"),
        "arr": {"state": "CONFLICTING", "citations": [
            {"url": "https://a.example/", "value_verbatim": "$10M"},
            {"url": "https://b.example/", "value_verbatim": "$7M"}]},
        "total_funding": entry("UNSUPPORTED", 5_000_000),
        "investor": [entry("SUPPORTED", "Sequoia"), entry("SUPPORTED", "sequoia")],
        "price": [entry("SUPPORTED", "$50/mo", qualifier="Pro"),
                  entry("SUPPORTED", "$50/mo", qualifier="Pro")],
        "key_feature": [entry("SUPPORTED", "Robot fleets", "https://acme.example/features")],
    }  # fmt: skip
    product = {"product_id": 1, "name": "Stock", "fields": {
        "product_name": [entry("SUPPORTED", "Stock")],
        "product_sales": entry("SUPPORTED", 120),
        "key_feature": [{"state": "UNKNOWN", "citations": []}]}}  # fmt: skip
    clean = clean_dossier({
        "generated_at": "2026-10-03T00:00:00+00:00",
        "company": {"organization_id": 7, "domain": "acme.example",
                    "subdomains": {"state": "SUPPORTED", "value": ["docs.acme.example"]},
                    "fields": fields},
        "products": [product],
        "competitors": [{"name": "Globex", "domain": None,
                         "fields": {"founded_year": entry("SUPPORTED", 1999)}}],
    })  # fmt: skip

    # Exactly the headings of GTM Parameters V1.0, nothing else.
    assert list(clean) == ["name_of_company", "company_information", "product_information",
                           "traction", "competitor_analysis"]  # fmt: skip
    assert clean["name_of_company"] == "Acme"
    assert clean["company_information"] == {
        "headquarters": "Pune, India", "website_domain": "acme.example", "company_size": None,
        "employee_count": None, "ownership_type": None, "partnership": [],
        "sub_domains": ["docs.acme.example"], "business_model": [], "market_domain": [],
        "company_location": [],
    }  # fmt: skip
    # The only product gets what the site states without naming a product.
    assert clean["product_information"]["products"] == [{
        "name": "Stock", "description": [],
        "pricing_and_plans": {"plans": [], "prices": [{"plan": "Pro", "price": "$50/mo"}]},
        "key_features": ["Robot fleets"], "main_use_case": [], "product_positioning": [],
        "product_evidence": ["https://acme.example/", "https://acme.example/features"],
        "product_category": [],
    }]  # fmt: skip
    traction = clean["traction"]
    # A claim or a conflict is never a value.
    assert traction["arr"] is None and traction["total_funding"] is None
    assert traction["investors"] == ["Sequoia"]  # no duplicates
    assert traction["annual_sales_per_product"] == {"Stock": 120}
    assert clean["competitor_analysis"][0]["name"] == "Globex"
    assert clean["competitor_analysis"][0]["founded_year"] == 1999
