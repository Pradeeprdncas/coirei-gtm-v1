"""Phase 5: questions for what research could not establish; "I don't know" is an answer."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from gtm_crawler import questions
from gtm_crawler.business_profile import REGISTRY

from .test_business_profile import bip, cite, dossier, field

KEYS = {f.key for f in REGISTRY}
CITED = {
    "evidence_confidence": 0.6,
    "citations": [{"observation_id": 1, "url": "https://acme.example/", "quote": "q"}],
}


def found_profile() -> dict[str, Any]:
    return dossier({
        "company_name": {"state": "SUPPORTED", "value": "Acme",
                         "citations": [cite(1, "2026-11-01T00:00:00+00:00")]},
        "legal_name": {"state": "CONFLICTING", "value": None, "citations": [
            cite(2, "2026-11-01T00:00:00+00:00"),
            {**cite(3, "2026-11-01T00:00:00+00:00"), "value_verbatim": "w"}]},
    })  # fmt: skip


def by_id(qs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {q["id"]: q for q in qs["questions"]}


def test_the_bank_has_the_three_groups_and_each_question_fills_a_field() -> None:
    groups: dict[str, int] = {}
    for q in questions.BANK:
        groups[q.group] = groups.get(q.group, 0) + 1
        assert q.fills in KEYS, q.id
        assert q.type in questions.TYPES
    assert groups == {"business_fundamentals": 5, "commercial": 6, "sales_marketing": 6}


def test_nothing_is_asked_before_research_has_found_anything() -> None:
    qs = questions.generate(bip(dossier({}), job="running"))
    assert qs == {"ready": False, "questions": []}


def test_questions_prefill_from_the_profile_and_ask_what_is_missing() -> None:
    model = {"catalogue": {"offerings": [{"name": {"value": "Arm", **CITED}}]},
             "differentiators": [{"value": "Fastest arms", **CITED}]}  # fmt: skip
    qs = by_id(questions.generate(bip(found_profile(), model)))
    assert qs["products_to_promote"]["choices"] == ["Arm"]
    assert qs["products_to_promote"]["type"] == "multi_choice"
    # What research found is offered to confirm, not asked from scratch.
    assert qs["differentiators"]["choices"] == ["Fastest arms"]
    assert qs["monthly_marketing_budget"]["type"] == "money"
    assert all(q["allow_dont_know"] for q in qs.values())
    # A conflict asks which source is right; a missing public fact asks for it.
    assert qs["gap:legal_name"]["type"] == "choice"
    assert qs["gap:legal_name"]["choices"] == ["v", "w"]
    assert qs["gap:founded_year"]["fills"] == "founded_year"
    # Found and unchallenged: not asked.
    assert "gap:company_name" not in qs


def test_an_answered_or_unknown_question_is_not_asked_again() -> None:
    answers = [
        {"section": "sales_commercial", "field": "monthly_marketing_budget", "item_key": "",
         "status": "edited", "value": {"amount": 500, "currency": "USD"},
         "verified_at": "2026-11-10T09:00:00+00:00"},
        {"section": "sales_commercial", "field": "monthly_leads", "item_key": "",
         "status": "dont_know", "value": None, "verified_at": "2026-11-10T09:00:00+00:00"},
    ]  # fmt: skip
    view = bip(found_profile(), answers=answers)
    qs = by_id(questions.generate(view))
    assert "monthly_marketing_budget" not in qs and "monthly_leads" not in qs
    leads = field(view, "monthly_leads")
    assert leads["value"] is None and leads["status"] == "Not found"
    missing = {m["key"]: m for m in view["sections"][11]["items"]}
    assert missing["monthly_leads"]["reason"] == "you said you don't know yet"


@pytest.mark.parametrize(
    ("qtype", "value", "ok"),
    [
        ("money", {"amount": 500, "currency": "INR"}, True),
        ("money", {"amount": -1, "currency": "INR"}, False),
        ("money", 500, False),
        ("number", 40, True),
        ("number", "forty", False),
        ("percent", 12.5, True),
        ("percent", 140, False),
        ("yes_no", True, True),
        ("yes_no", "yes", False),
        ("multi_choice", ["Email", "LinkedIn"], True),
        ("multi_choice", "Email", False),
        ("text", "We call back within an hour.", True),
        ("text", "", False),
    ],
)
def test_answers_are_checked_against_their_type(qtype: str, value: Any, ok: bool) -> None:
    assert questions.valid(qtype, value) is ok


class Phraser:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        asked = json.loads(request.content)["messages"][1]["content"]
        assert "monthly_marketing_budget" in asked
        answer = {"questions": [
            {"id": "monthly_marketing_budget",
             "text": "Roughly how much does Acme spend on marketing each month?"},
            {"id": "not_a_question", "text": "Ignored"},
        ]}  # fmt: skip
        content = json.dumps(answer)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.db
def test_the_model_words_the_questions_once_and_answers_reach_the_profile(
    engine: Engine, tmp_path: Path
) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from gtm_crawler.api import create_app

    from .test_crawl import settings_for
    from .test_onboarding_api import crawled
    from .test_profile import LLM

    # Crawled without a model (no page reads); the model then words the questions.
    _, company = crawled(engine, settings_for(tmp_path), "bip-questions")
    settings = replace(settings_for(tmp_path), llm=LLM)
    api = TestClient(create_app(settings, engine))
    phraser = Phraser()
    transport = httpx.MockTransport(phraser)
    assert questions.word_questions(engine, settings, company, transport) == "worded"
    assert questions.word_questions(engine, settings, company, transport) == "cached"
    assert phraser.calls == 1

    asked = by_id(api.get(f"/v1/companies/{company}/questions").json())
    budget = asked["monthly_marketing_budget"]
    assert budget["text"] == "Roughly how much does Acme spend on marketing each month?"
    assert asked["monthly_leads"]["text"] == questions.BY_ID["monthly_leads"].text

    url = f"/v1/companies/{company}/answers"
    saved = api.put(url, json={"answers": [
        {"question_id": "monthly_marketing_budget", "status": "answered",
         "value": {"amount": 2000, "currency": "USD"}, "actor": "ana"},
        {"question_id": "monthly_leads", "status": "dont_know"},
    ]})  # fmt: skip
    assert saved.status_code == 200, saved.text
    view = api.get(f"/v1/companies/{company}/business-profile").json()
    spend = field(view, "monthly_marketing_budget")
    assert spend["value"] == {"amount": 2000, "currency": "USD"} and spend["status"] == "Verified"
    assert field(view, "monthly_leads")["status"] == "Not found"
    left = by_id(api.get(f"/v1/companies/{company}/questions").json())
    assert "monthly_marketing_budget" not in left and "monthly_leads" not in left

    bad = {"answers": [{"question_id": "monthly_leads", "status": "answered", "value": "many"}]}
    assert api.put(url, json=bad).status_code == 422
    unknown = {"answers": [{"question_id": "favourite_colour", "status": "dont_know"}]}
    assert api.put(url, json=unknown).status_code == 422


def test_the_catalogue_is_confirmed_and_what_research_missed_is_added() -> None:
    model = {"catalogue": {"offerings": [{"name": {"value": "Arm", **CITED}},
                                         {"name": {"value": "Gripper", **CITED}}]}}  # fmt: skip
    view = bip(found_profile(), model)
    qs = by_id(questions.generate(view))
    assert qs["catalogue"]["type"] == "multi_choice"
    assert qs["catalogue"]["choices"] == qs["catalogue"]["suggested"] == ["Arm", "Gripper"]
    answer = questions.Answer(question_id="catalogue", status="answered",
                              value=["arm", "Lasers"])  # fmt: skip
    rows = questions._catalogue_rows(7, answer, view["sections"][1]["items"])
    assert [(r["item_key"], r["status"], r["value"]) for r in rows] == [
        ("llm:Arm", "confirmed", "Arm"), ("llm:Gripper", "rejected", "Gripper"),
        ("user:Lasers", "edited", "Lasers")]  # fmt: skip
    answers = [{**r, "verified_at": "2026-11-10T09:00:00+00:00"} for r in rows]
    after = bip(found_profile(), model, answers)
    items = {i["value"]: i for i in after["sections"][1]["items"] if i["value"]}
    assert set(items) == {"Arm", "Lasers"}
    assert items["Lasers"]["status"] == "Verified" and items["Lasers"]["confidence"] == 1.0
    # Answered: not asked again.
    assert "catalogue" not in by_id(questions.generate(after))


def test_what_the_model_inferred_about_buyers_is_asked_to_confirm() -> None:
    model = {"icp": {"business_types": [{"value": "Marketing agencies", **CITED}]},
             "brand": {"mission": {"value": "Robots for all", **CITED}}}  # fmt: skip
    qs = by_id(questions.generate(bip(found_profile(), model)))
    gap = qs["gap:business_types"]
    assert gap["suggested"] == ["Marketing agencies"] and "Is it right?" in gap["text"]
    assert "gap:mission" not in qs  # inferred brand wording is labelled, not asked
