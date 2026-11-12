"""Company profile: the model organises the evidence; every value must cite it."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from gtm_crawler import profile
from gtm_crawler.config import LlmSettings
from gtm_crawler.crawl import crawl
from gtm_crawler.onboarding import fill_from_profile, onboarding_view
from gtm_crawler.pipeline import reextract

from .test_crawl import SITE, FakeSite, html, settings_for

LLM = LlmSettings(base_url="http://localhost:11434/v1", model="qwen3:8b")
REFS: dict[str, dict[str, Any]] = {
    "e1": {"observation_id": 11, "url": f"{SITE}/", "quote": "Fees from Rs 15,000.",
           "confidence": 0.7,
           "text": "company price: 'Rs 15,000'; quote: \"Fees from Rs 15,000.\""},
    "e2": {"url": f"{SITE}/about", "quote": "We train students in Chennai.",
           "text": "page about: We train students in Chennai."},
}  # fmt: skip


def leaf(value: Any, *ids: str) -> dict[str, Any]:
    return {"value": value, "evidence": list(ids)}


def stats() -> dict[str, int]:
    return {"uncited": 0, "invalid": 0, "number_not_in_evidence": 0}


def test_clean_keeps_only_cited_in_vocabulary_values() -> None:
    s = stats()
    out = profile.clean(profile.SCHEMA, {
        "business_model": {
            "customer_type": leaf("b2c", "e2"),
            "revenue_model": leaf("course_fees", "e1"),  # not a choice
            "price_range": leaf("Rs 15,000", "e1"),
            "pricing_summary": leaf("From Rs 20,000", "e1"),  # number not in evidence
            "delivery": leaf("offline"),  # cites nothing
            "reasoning": leaf("Students pay fees to train in Chennai.", "e1", "e2"),
        },
        "company": {"founded_year": leaf("2019", "e9")},  # unknown id
        "market": "junk",
    }, REFS, s)  # fmt: skip
    model = out["business_model"]
    assert set(model) == {"customer_type", "price_range", "reasoning"}
    assert model["reasoning"]["evidence_confidence"] == 0.7
    assert model["reasoning"]["citations"] == [
        {"observation_id": 11, "url": f"{SITE}/", "quote": "Fees from Rs 15,000."},
        {"url": f"{SITE}/about", "quote": "We train students in Chennai."},
    ]
    assert "company" not in out and "market" not in out
    assert s == {"uncited": 2, "invalid": 1, "number_not_in_evidence": 1}


def test_evidence_lines_put_facts_before_pages_within_budget() -> None:
    dossier = {
        "company": {"fields": {"company_description": {"state": "SUPPORTED", "confidence": 0.8,
            "citations": [{"observation_id": 5, "url": f"{SITE}/", "quote": "We train people.",
                           "value_verbatim": "We train people."}]}}},
        "products": [], "competitors": [{"name": "Globex", "domain": "globex.com"}],
    }  # fmt: skip
    pages = [("home", f"{SITE}/", "One. " * 400)]
    text, refs = profile.evidence_lines(dossier, 3000, pages)
    lines = text.splitlines()
    assert lines[0].startswith("[e1] company company_description (SUPPORTED): 'We train people.'")
    assert lines[1] == "[e2] competitor named on the site: Globex (globex.com)"
    assert lines[2].startswith(f"[e3] page home {SITE}/: One. One.")
    assert len(text) <= 3000 and all(len(r["quote"]) <= 300 for r in list(refs.values())[2:])
    assert refs["e1"]["observation_id"] == 5 and refs["e3"]["url"] == f"{SITE}/"


def test_fill_from_profile_fills_only_empty_items() -> None:
    sections = [
        {"key": "company_info", "items": [
            {"field": "company_description", "item_key": "", "value": "Found",
             "state": "SUPPORTED"},
            {"field": "founded_year", "item_key": "", "value": None, "state": "UNKNOWN"}]},
        {"key": "business_model", "items": [
            {"field": "business_model", "item_key": "", "value": None, "state": "UNKNOWN"}]},
        {"key": "products_services", "items": [
            {"field": "product", "item_key": "", "value": None, "state": "UNKNOWN"}]},
    ]  # fmt: skip
    cite = {"evidence_confidence": 0.6, "citations": [{"url": SITE, "quote": "q"}]}
    fill_from_profile(sections, {
        "company": {"description": {"value": "Model", **cite},
                    "founded_year": {"value": 2019, **cite}},
        "business_model": {"customer_type": {"value": "b2c", **cite},
                           "revenue_model": {"value": "one_time_fee", **cite},
                           "reasoning": {"value": "Students pay once.", **cite}},
        "catalogue": {"offerings": [{"name": {"value": "Data Science", **cite},
                                     "type": {"value": "course", **cite}}]},
    })  # fmt: skip
    info, model, products = (s["items"] for s in sections)
    assert info[0]["value"] == "Found" and info[0]["state"] == "SUPPORTED"
    assert info[1]["value"] == 2019 and info[1]["state"] == "MODEL_INFERRED"
    assert model[0]["value"] == ["b2c", "one_time_fee"]
    assert model[0]["reasoning"] == "Students pay once."
    assert model[0]["citations"][0] == {"url": SITE, "quote": "q", "source_type": "model_cited",
                                        "tier": None}  # fmt: skip
    assert [(p["value"], p["item_key"], p["details"]["type"]) for p in products] == [
        ("Data Science", "llm:Data Science", "course")
    ]


def test_value_proposition_statement_replaces_the_home_tagline() -> None:
    def section() -> list[dict[str, Any]]:
        return [{"key": "value_proposition", "items": [
            {"field": "product_positioning", "item_key": "", "value": ["From One URL"],
             "state": "UNSUPPORTED"},
            {"field": "product_positioning", "item_key": "7", "value": ["Product pitch"],
             "state": "SUPPORTED"}]}]  # fmt: skip

    cite = {"evidence_confidence": 0.5, "citations": [{"url": SITE, "quote": "q"}]}
    sections = section()
    fill_from_profile(sections, {"value_proposition": {
        "statement": {"value": "For founders who ...", **cite},
        "reasoning": {"value": "Why.", **cite}}})  # fmt: skip
    company, product = sections[0]["items"]
    assert (company["value"], company["state"]) == (["For founders who ..."], "MODEL_INFERRED")
    assert company["reasoning"] == "Why."
    assert product["value"] == ["Product pitch"]  # a product's own positioning stays
    sections = section()
    fill_from_profile(sections, {})  # no statement: the tagline stays
    assert sections[0]["items"][0]["value"] == ["From One URL"]


# Its own company: other tests leave facts on acme.example's organisation.
OWN = "https://itp-profile.example"
ROUTES: dict[str, Any] = {
    f"{OWN}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{OWN}/": ("text/html", html("Acme", "<p>We are a software training institute in "
                                   "Chennai. Course fees start at Rs 15,000.</p>")),
}  # fmt: skip


class FakeModel:
    def __init__(self) -> None:
        self.profile_calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        if user.startswith("Evidence:"):
            self.profile_calls.append(body)
            home = next(line[1:].split("]")[0] for line in user.splitlines()
                        if "] page home " in line)  # fmt: skip
            if body["messages"][0]["content"] == profile.VP_SYSTEM:
                answer = {
                    "problem": {"value": "Graduates lack the skills IT jobs ask for",
                                "evidence": [home]},
                    "basis": {"value": "inferred", "evidence": [home]},
                    # Filler that fits any company: dropped.
                    "statement": {"value": "Innovative end-to-end training solutions",
                                  "evidence": [home]},
                }  # fmt: skip
            else:
                answer = {"business_model": {
                    "customer_type": {"value": "b2c", "evidence": [home]},
                    "revenue_model": {"value": "one_time_fee", "evidence": [home]},
                    "reasoning": {"value": "Students pay course fees from Rs 15,000.",
                                  "evidence": [home]}},
                    "differentiators": [{"value": "Placements", "evidence": ["e999"]}]}  # fmt: skip
        else:
            answer = {"page_type": "home", "facts": []}
        content = json.dumps(answer)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.db
def test_profile_reasons_from_pages_and_fills_onboarding(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(settings_for(tmp_path), llm=LLM)
    summary = asyncio.run(crawl(f"{OWN}/", settings, engine,
                                transport=httpx.MockTransport(FakeSite(ROUTES))))  # fmt: skip
    model = FakeModel()
    transport = httpx.MockTransport(model)
    real = profile.llm.read_crawl
    monkeypatch.setattr("gtm_crawler.pipeline.read_crawl",
                        lambda e, s, c: real(e, s, c, transport))  # fmt: skip
    reextract(engine, settings, summary.crawl_id)
    org = summary.organization_id
    assert profile.stored_profile(engine, settings, org)["status"] == "not_built"

    built = profile.build_profile(engine, settings, org, transport)
    assert built["status"] == "ready" and not built["cached"]
    sent = model.profile_calls[0]
    assert sent["reasoning_effort"] == "medium" and sent["max_tokens"] == 8000
    assert sent["temperature"] == 0.6  # greedy decoding loops a thinking model
    bm = built["profile"]["business_model"]
    assert bm["revenue_model"]["value"] == "one_time_fee"
    assert bm["reasoning"]["citations"][0]["url"] == f"{OWN}/"
    assert "differentiators" not in built["profile"]  # cited an id never sent
    assert built["dropped"]["uncited"] == 1

    # One call per part (PARTS); the value proposition reads the pages' prose.
    def calls(system: str) -> int:
        return sum(c["messages"][0]["content"] == system for c in model.profile_calls)

    assert calls(profile.SYSTEM) == calls(profile.VP_SYSTEM) == 1
    assert len(model.profile_calls) == len(profile.PARTS)
    vp = built["profile"]["value_proposition"]
    assert vp["problem"]["value"] == "Graduates lack the skills IT jobs ask for"
    assert vp["basis"]["value"] == "inferred" and "statement" not in vp
    assert built["dropped_value_proposition"]["invalid"] == 1

    # Same evidence: the stored profile, no new call; the API reads it the same way.
    assert profile.build_profile(engine, settings, org, transport)["cached"]
    assert calls(profile.SYSTEM) == calls(profile.VP_SYSTEM) == 1
    # A part the fake answered with nothing usable was not stored: it is asked again.
    assert calls(profile.BRAND_SYSTEM) == 2
    stored = profile.stored_profile(engine, settings, org)
    assert stored["status"] == "ready" and stored["basis"] == "model_generated"
    view = onboarding_view(engine, org, stored)
    items = {i["field"]: i for s in view["sections"] for i in s["items"]}
    assert items["business_model"]["state"] == "MODEL_INFERRED"
    assert items["business_model"]["value"] == ["b2c", "one_time_fee"]
    assert view["profile"]["status"] == "ready"
    assert view["profile"]["profile"]["value_proposition"]["problem"]["value"]


def test_reasoning_keeps_only_cited_sentences() -> None:
    s = stats()
    out = profile.clean(profile.SCHEMA, {"business_model": {"reasoning": (
        "Students pay fees per course [e1]. They train in Chennai [e2][e1]. "
        "It is the best institute in India. Fees are Rs 99,000 [e1]. Bogus [e7].")}},
        REFS, s)  # fmt: skip
    r = out["business_model"]["reasoning"]
    assert r["value"] == "Students pay fees per course. They train in Chennai."
    assert [c["url"] for c in r["citations"]] == [f"{SITE}/", f"{SITE}/about"]
    assert s == {"uncited": 2, "invalid": 0, "number_not_in_evidence": 1}


def test_salary_figures_are_labelled_and_never_a_price() -> None:
    pages = [("home", f"{SITE}/", "Data Engineering 14 weeks Rs 8-18 LPA View Curriculum.")]
    text, refs = profile.evidence_lines({"company": {"fields": {}}, "products": [],
                                         "competitors": []}, 3000, pages)  # fmt: skip
    assert "8-18 LPA (salary graduates earn, not a fee)" in text
    s = stats()
    out = profile.clean(profile.SCHEMA, {"business_model": {
        "price_range": leaf("Rs 8-18 LPA", "e1"),
        "pricing_summary": leaf("Salary of Rs 8-18 lakhs", "e1"),
        "delivery": leaf("offline", "e1")}}, refs, s)  # fmt: skip
    catalogue = profile.clean(profile.OFFERINGS_SCHEMA, {
        "offerings": [{"name": leaf("Data Engineering", "e1"),
                       "price": leaf("Not explicitly stated", "e1")}]}, refs, s)  # fmt: skip
    assert set(out["business_model"]) == {"delivery"}
    assert set(catalogue["offerings"][0]) == {"name"}
    assert s["invalid"] == 3


def test_value_proposition_reads_page_prose_and_drops_filler() -> None:
    # Menus, a demo widget's cells and a repeated line are not the page's copy.
    page = "\n".join(["Home", "Pricing", "clay", "http://clay.run/", "44%",
                      "We find your next customers for you",
                      "We find your next customers for you",
                      "Save 10 hours a week on research."])  # fmt: skip
    assert profile.prose(page) == ("We find your next customers for you.\n"
                                   "Save 10 hours a week on research.")  # fmt: skip
    dossier = {"company": {"fields": {
        "price": {"state": "SUPPORTED", "citations": [
            {"observation_id": 1, "url": SITE, "quote": "Rs 99", "value_verbatim": "Rs 99"}]},
        "customer": {"state": "SUPPORTED", "citations": [
            {"observation_id": 2, "url": SITE, "quote": "Used by Globex.",
             "value_verbatim": "Globex"}]}}}, "products": [], "competitors": []}  # fmt: skip
    text, refs = profile.evidence_lines(dossier, 3000, [("home", SITE, profile.prose(page))],
                                        profile.VP_FIELDS)  # fmt: skip
    assert "price" not in text and "customer" in text
    s = stats()
    out = profile.clean(profile.VP_SCHEMA, {
        "for_whom": leaf("Sales teams at B2B startups", "e1"),
        "outcome": leaf("Save 10 hours a week on research", "e2"),
        "how": leaf("Seamless, innovative lead solutions", "e2"),  # filler: dropped
        "statement": leaf("Save 20 hours a week", "e2"),  # not the page's number
    }, refs, s)  # fmt: skip
    assert set(out) == {"for_whom", "outcome"}
    assert s == {"uncited": 0, "invalid": 1, "number_not_in_evidence": 1}


def test_statement_rests_on_its_parts_and_inline_ids_are_evidence() -> None:
    data: dict[str, Any] = {
        "problem": leaf("Students pay for courses that do not get them hired", "e2"),
        "outcome": {"value": "Fees from Rs 15,000 [e1]", "evidence": []},
        "statement": "For students in Chennai, training that costs Rs 15,000.",
    }
    profile._statement_evidence(data)
    s = stats()
    out = profile.clean(profile.VP_SCHEMA, data, REFS, s)
    assert out["outcome"]["value"] == "Fees from Rs 15,000"
    assert [c["url"] for c in out["statement"]["citations"]] == [f"{SITE}/about", f"{SITE}/"]
    assert s == stats()


def test_a_figure_cited_from_the_wrong_line_is_cited_from_the_right_one() -> None:
    trek = "page product: Trek Bikes saw a1,002%increase in reach."
    refs = {**REFS, "e3": {"url": f"{SITE}/customers", "quote": "q", "text": trek}}
    s = stats()
    out = profile.clean(profile.VP_SCHEMA, {
        "outcome": leaf("1,002% more reach", "e2"),  # the figure is on e3, not e2
        "proof": [{"kind": "statistic", "claim": leaf("Rs 99,000 saved", "e1")},  # on no line
                  {"kind": "statistic", "claim": leaf("Graduates earn 8-18 LPA", "e2")},  # salary
                  {"kind": "feature", "claim": leaf("Live classes", "e2")},  # not a kind of proof
                  {"claim": leaf("Free trial", "e2")}],  # no kind
    }, refs, s)  # fmt: skip
    assert [c["url"] for c in out["outcome"]["citations"]] == [f"{SITE}/about", f"{SITE}/customers"]
    assert "proof" not in out
    assert s == {"uncited": 0, "invalid": 2, "number_not_in_evidence": 1}
    out = profile.clean(
        profile.VP_SCHEMA,
        {"proof": [{"kind": "customer", "claim": leaf("Placed graduates at Wipro", "e2")}]},
        refs,
        s,
    )
    assert out["proof"][0]["kind"] == "customer"
    assert out["proof"][0]["claim"]["value"] == "Placed graduates at Wipro"
    assert profile.prose("Growth this quarter\n↑ 28%vs last quarter\nReach 0K↑0%") == ""


def test_prose_drops_a_demo_screens_sample_figures_not_real_claims() -> None:
    demo = ["Reach", "0K↑0%", "Engagement", "0.0%↑0.0%", "Leads", "0↑0%", "AI Recommendation",
            "LinkedIn engagement is 23% higher for your target audience.",
            "Generates ad copy tailored to each buyer persona."]  # fmt: skip
    # A pricing table's or a stat band's bare figures are not a dashboard.
    claim = ["$20", "10+", "4.8", "Trusted by 4,000 teams across Europe."]
    assert profile.prose("\n".join(demo)) == "Generates ad copy tailored to each buyer persona."
    assert profile.prose("\n".join(claim)) == "Trusted by 4,000 teams across Europe."
