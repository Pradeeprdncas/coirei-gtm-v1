"""Truthfulness fixes found by the Phase 15 corpus (spec section 16).

Employee counts keep "up to" / "fewer than" / "under" bounds; a figure the
page attributes to a third party is recorded as that party's report; a
billing cycle is not a price unit.
"""

from typing import Any

import pytest

from gtm_crawler.assess import assess_group
from gtm_crawler.db.evidence import check_draft
from gtm_crawler.extract.common import Context
from gtm_crawler.extract.company import extract_company
from gtm_crawler.extract.product import extract_products
from gtm_crawler.extract.traction import extract_traction

from .test_assess import obs
from .test_extract_company import page


def normalized(extract: Any, text: str, field: str) -> list[dict[str, Any] | None]:
    p = page(f"<p>{text}</p>")
    drafts = extract(p, Context("acme.example", {"Acme"}))
    for d in drafts:
        check_draft(d, p)  # the bound is in the value, so the draft is kept
    return [d.value_normalized for d in drafts if d.field == field]


@pytest.mark.parametrize(
    ("text", "qualifier", "value"),
    [
        ("We have up to 250 employees.", "lte", 250),
        ("We employ fewer than 50 employees.", "lt", 50),
        ("We have less than 40 staff members.", "lt", 40),
        ("We are under 20 FTEs.", "lt", 20),
        ("We have at most 90 employees.", "lte", 90),
        ("We have nearly 300 team members.", "approx", 300),
        ("We have almost 280 employees.", "approx", 280),
    ],
)
def test_employee_bounds_use_the_shared_qualifiers(text: str, qualifier: str, value: int) -> None:
    got = normalized(extract_company, text, "employee_count")
    assert got == [{"qualifier": qualifier, "value": value}]


def test_customer_count_fewer_than() -> None:
    got = normalized(extract_traction, "We serve fewer than 100 customers.", "customer_count")
    assert got == [{"qualifier": "lt", "value": 100}]


def test_third_party_figures_are_reported() -> None:
    by_linkedin = {"status": "reported", "reported_by": "LinkedIn"}
    assert normalized(extract_company, "LinkedIn lists Acme in the 51-200 employees band.",
                      "company_size") == [{"qualifier": "range", "min": 51, "max": 200,
                                           **by_linkedin}]  # fmt: skip
    assert normalized(extract_company, "According to Crunchbase, Acme has 120 employees.",
                      "employee_count") == [{"qualifier": "eq", "value": 120, "status": "reported",
                                             "reported_by": "Crunchbase"}]  # fmt: skip
    assert normalized(extract_traction, "According to PitchBook, Acme raised $5M in 2024.",
                      "funding_round") == [{"qualifier": "eq", "amount": 5e6, "currency": "USD",
                                            "year": 2024, "status": "reported",
                                            "reported_by": "PitchBook", "round": None}]  # fmt: skip


@pytest.mark.parametrize(
    "text",
    [
        "Acme reports 120 employees.",  # the company itself
        "Today Acme reports 120 employees.",
        "We reported 120 employees.",
        "Our CEO Jane Doe says we have 120 employees.",  # an executive speaks for the company
    ],
)
def test_the_company_citing_itself_is_not_a_report(text: str) -> None:
    assert normalized(extract_company, text, "employee_count") == [
        {"qualifier": "eq", "value": 120}
    ]


def test_billing_cycle_is_not_a_price_unit() -> None:
    p = page("<h1>Pricing</h1><h2>Pro</h2><p>$249/mo billed annually</p>", path="/pricing")
    (price,) = [d for d in extract_products(p, Context("acme.example", {"Acme"}))
                if d.field == "price"]  # fmt: skip
    assert price.value_normalized == {"qualifier": "eq", "amount": 249.0, "currency": "USD",
                                      "per": ["month"], "billing": "annual"}  # fmt: skip


def test_a_report_on_the_company_site_is_the_company_site_source() -> None:
    # LinkedIn quoted on acme.com is acme.com's claim: not LinkedIn's page, so
    # not an independent source, and a high-risk claim stays UNSUPPORTED.
    size = {"qualifier": "range", "min": 51, "max": 200, "status": "reported",
            "reported_by": "LinkedIn"}  # fmt: skip
    (a,) = assess_group("company_size", None, [obs("company_size", "51-200 employees", size,
                                                   confidence=0.9)])  # fmt: skip
    assert a.state == "UNSUPPORTED"
    assert a.components is not None and a.components["independent_sources"] == 1
