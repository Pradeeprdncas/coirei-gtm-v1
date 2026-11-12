"""Phase 2: the user edits any profile field; every answer is kept, the latest wins."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from .test_business_profile import bip, dossier, field
from .test_crawl import settings_for
from .test_onboarding_api import crawled


def answer(field_: str, status: str, value: Any, when: str, actor: str | None = None) -> dict:
    return {"section": "company_profile", "field": field_, "item_key": "", "status": status,
            "value": value, "verified_at": when, "actor": actor}  # fmt: skip


def test_the_latest_answer_wins_and_earlier_ones_are_history() -> None:
    view = bip(dossier({}), answers=[
        answer("founded_year", "edited", 2018, "2026-11-10T09:00:00+00:00", "ana"),
        answer("founded_year", "edited", 2019, "2026-11-10T10:00:00+00:00", "raj"),
    ])  # fmt: skip
    founded = field(view, "founded_year")
    assert founded["value"] == 2019 and founded["status"] == "Verified"
    assert [(h["value"], h["actor"]) for h in founded["history"]] == [(2018, "ana"), (2019, "raj")]
    assert field(view, "company_name")["history"] == []


@pytest.mark.db
def test_any_profile_field_is_editable_and_kept_append_only(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "bip-edit")
    url = f"/v1/companies/{company}/verification"
    first = {"section": "company_profile", "field": "company_name", "status": "edited",
             "value": "Acme One", "actor": "ana"}  # fmt: skip
    assert api.put(url, json={"items": [first]}).status_code == 200
    second = {**first, "value": "Acme Robotics", "actor": "raj"}
    budget = {"section": "sales_commercial", "field": "monthly_marketing_budget",
              "status": "edited", "value": {"amount": 5000, "currency": "USD"}}  # fmt: skip
    saved = api.put(url, json={"items": [second, budget]})
    assert saved.status_code == 200, saved.text
    # The current answers: one per field, the latest.
    current = {i["field"]: i for i in saved.json()["items"]}
    assert current["company_name"]["value"] == "Acme Robotics"
    assert current["company_name"]["actor"] == "raj"

    view = api.get(f"/v1/companies/{company}/business-profile").json()
    name = field(view, "company_name")
    assert name["value"] == "Acme Robotics" and name["origin"] == "user"
    assert [h["value"] for h in name["history"]] == ["Acme One", "Acme Robotics"]
    spend = field(view, "monthly_marketing_budget")
    assert spend["status"] == "Verified" and spend["value"] == {"amount": 5000, "currency": "USD"}
    missing = {m["key"] for m in view["sections"][12]["items"]}
    assert "monthly_marketing_budget" not in missing

    # Onboarding answers still apply (its sections and fields stay valid).
    old = {"section": "company_info", "field": "founded_year", "status": "edited", "value": 2019}
    assert api.put(url, json={"items": [old]}).status_code == 200
    assert field(api.get(f"/v1/companies/{company}/business-profile").json(),
                 "founded_year")["value"] == 2019  # fmt: skip

    wrong_section = {**budget, "section": "company_profile"}
    assert api.put(url, json={"items": [wrong_section]}).status_code == 422
    unknown = {**budget, "field": "favourite_colour"}
    assert api.put(url, json={"items": [unknown]}).status_code == 422
