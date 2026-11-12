"""Questions for what research could not establish, and the user's answers.

Once research has found something (``generate``), the user is asked:

* the fixed questions of three groups (``BANK``): business fundamentals, commercial
  information, and existing sales and marketing; a question whose field the user
  already answered or research verified is not asked, and its choices come from the
  profile where it has them (the offerings to promote, the differentiators found);
* the catalogue question (``catalogue``): which of the products and services found the
  company sells, with any research missed; each one found is then confirmed or rejected;
* for a public fact the profile is missing, needs confirmed, or has sources
  disagreeing on, a gap question (``gap:<field>``): "which is right?" for a conflict;
  and for what the model inferred about whom and how the company sells (``CONFIRM``),
  "is this right?".

Every question allows "I don't know": COIREI never makes the user invent a figure.
An answer is stored as the user's answer to the field it fills
(``company_verification``, append-only): ``answered`` as ``edited``, and
``dont_know`` / ``skipped`` with no value, so the field stays in the profile's
missing information.

The wording comes from the language model (``word_questions``, run after the
profile is built), specific to the company and cached per question; without a
model, or for a question it has not worded, the template wording here is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .business_profile import REGISTRY, build_business_profile
from .config import Settings
from .db.models import CompanyVerification, LlmPageResult
from .extract.llm import LlmError, PageReader

log = logging.getLogger(__name__)

PROMPT_VERSION = "questions-2"
TYPES = ("text", "number", "money", "percent", "choice", "multi_choice", "yes_no")
MAX_GAP_QUESTIONS = 15
MAX_TEXT = 2000
# Statuses of a field research has something for: questions can then be asked.
_FOUND = frozenset({"Extracted", "Verified", "Inferred", "Conflicting", "Needs confirmation"})
# Gap questions, most pressing first: sources disagree, then unconfirmed, then what the
# model inferred (CONFIRM fields only: the plan rests on them), then missing.
_GAP_ORDER = ("Conflicting", "Needs confirmation", "Inferred", "Not found")
# The core public facts worth asking the user about when research leaves them open; the
# rest (brand wording, funding, stage...) stay in the missing information.
GAP_FIELDS = ("company_name", "legal_name", "website", "market_domain", "company_description",
              "headquarters", "company_location", "geography_served", "founded_year",
              "founders", "contact_email", "contact_phone", "product_positioning",
              "target_segments", "business_types", "company_sizes", "buyer_roles",
              "target_industries", "main_use_case", "customer_type", "revenue_model",
              "sales_motion", "delivery", "pricing_summary", "plan_name", "price",
              "company_size", "customer", "case_study", "customer_count")  # fmt: skip
# Model inferences the go-to-market plan rests on: asked to confirm, not only labelled.
CONFIRM = frozenset({"target_segments", "business_types", "company_sizes", "buyer_roles",
                     "target_industries", "customer_type", "revenue_model", "sales_motion",
                     "product_positioning", "market_domain"})  # fmt: skip
CATALOGUE = "catalogue"
_SECTION = {f.key: f.section for f in REGISTRY}
_FIELD = {f.key: f for f in REGISTRY}

CHANNELS = ("Website / SEO", "Google Ads", "Meta (Facebook, Instagram)", "LinkedIn",
            "Email", "WhatsApp", "Events", "Referrals", "Partners", "Outbound sales",
            "Marketplaces", "Print, radio or TV")  # fmt: skip
CRMS = ("HubSpot", "Salesforce", "Zoho", "Pipedrive", "Freshworks", "Microsoft Dynamics",
        "Spreadsheets", "WhatsApp Business", "None")  # fmt: skip
OBJECTIVES = ("More leads", "More sales", "Brand awareness", "Enter a new market",
              "Launch a product", "Keep existing customers")  # fmt: skip


@dataclass(frozen=True)
class Question:
    id: str
    group: str
    text: str
    type: str
    fills: str  # the Business Intelligence Profile field the answer gives
    choices: tuple[str, ...] = ()


_A, _B, _C = "business_fundamentals", "commercial", "sales_marketing"
BANK: tuple[Question, ...] = (
    Question("primary_objective", _A, "What is your primary business objective?", "choice",
             "primary_objective", OBJECTIVES),
    Question("products_to_promote", _A, "Which products or services do you want to promote?",
             "multi_choice", "products_to_promote"),
    Question("priority_markets", _A, "Which geographic markets are your priorities?",
             "multi_choice", "priority_markets"),
    Question("valuable_customers", _A, "Who are your most valuable existing customers?",
             "text", "valuable_customers"),
    Question("differentiators", _A, "What makes your offering different?", "multi_choice",
             "differentiators"),
    Question("monthly_marketing_budget", _B, "What is your monthly marketing budget?",
             "money", "monthly_marketing_budget"),
    Question("average_order_value", _B, "What is your average order or contract value?",
             "money", "average_order_value"),
    Question("monthly_revenue_target", _B, "What is your monthly revenue target?", "money",
             "monthly_revenue_target"),
    Question("monthly_leads", _B, "How many leads do you currently receive each month?",
             "number", "monthly_leads"),
    Question("lead_conversion_rate", _B, "What percentage of leads become customers?",
             "percent", "lead_conversion_rate"),
    Question("customer_capacity", _B, "How many additional customers can you serve?",
             "number", "customer_capacity"),
    Question("marketing_channels", _C, "Which marketing channels are you using?",
             "multi_choice", "marketing_channels", CHANNELS),
    Question("best_channels", _C, "Which channels generate the best customers?",
             "multi_choice", "best_channels", CHANNELS),
    Question("marketing_challenges", _C, "What are your biggest marketing challenges?", "text",
             "marketing_challenges"),
    Question("sales_team", _C, "Do you have an existing sales team?", "yes_no", "sales_team"),
    Question("crm_platforms", _C, "Which CRM and communication platforms do you use?",
             "multi_choice", "crm_platforms", CRMS),
    Question("enquiry_process", _C, "What happens when a new enquiry arrives?", "text",
             "enquiry_process"),
)  # fmt: skip
BY_ID = {q.id: q for q in BANK}
_BANK_FIELDS = frozenset(q.fills for q in BANK)
# Bank questions whose choices are what the profile found for another field.
_CHOICES_FROM = {"products_to_promote": None, "priority_markets": "geography_served",
                 "differentiators": "differentiators"}  # fmt: skip


def _texts(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [] if value is None else [value]
    return list(dict.fromkeys(str(v) for v in values if v not in (None, "")))


def generate(bip: dict[str, Any], wording: dict[str, str] | None = None) -> dict[str, Any]:
    """The questions to ask now about the profile ``bip``
    (``business_profile.assemble``); ``wording``: question id -> the model's text."""
    fields = {f["key"]: f for s in bip["sections"] for f in s.get("fields", [])}
    if not any(f["status"] in _FOUND for f in fields.values()):
        return {"ready": False, "questions": []}
    offerings = [i["value"] for i in bip["sections"][1]["items"] if i["value"]]

    def open_(f: dict[str, Any]) -> bool:
        return f["status"] != "Verified" and f.get("answer_status") not in ("dont_know",
                                                                             "skipped")  # fmt: skip

    out: list[dict[str, Any]] = []
    for q in BANK:
        f = fields[q.fills]
        if not open_(f):
            continue
        choices = list(q.choices)
        if q.id in _CHOICES_FROM:
            source = _CHOICES_FROM[q.id]
            choices = _texts(offerings if source is None else fields[source]["value"])
        out.append({"id": q.id, "group": q.group, "text": q.text, "type": q.type,
                    "fills": q.fills, "choices": choices, "suggested": f["value"]})  # fmt: skip
    found = [i for i in bip["sections"][1]["items"] if i["value"]]
    if found and not any(i.get("answer_status") for i in found):
        out.append({"id": CATALOGUE, "group": _A, "type": "multi_choice",
                    "text": "Which of these do you sell as products or services? Add any "
                            "we missed.",
                    "fills": "product", "choices": _texts(offerings),
                    "suggested": _texts(offerings)})  # fmt: skip
    gaps = sorted(
        (f for key in GAP_FIELDS if open_(f := fields[key]) and f["status"] in _GAP_ORDER
         and (f["status"] != "Inferred" or key in CONFIRM)),
        key=lambda f: _GAP_ORDER.index(f["status"]),
    )  # fmt: skip
    for f in gaps[:MAX_GAP_QUESTIONS]:
        spec = _FIELD[f["key"]]
        if f["status"] == "Conflicting":
            candidates = _texts(f.get("evidence_value", f["value"]))
            # The label quoted, so any label reads right ("Named customers", "What the
            # company does"); the model words it for the company.
            out.append({"id": f"gap:{spec.key}", "group": "gaps", "type": "choice",
                        "text": f'Sources disagree on "{spec.label}". Which is right?',
                        "fills": spec.key, "choices": candidates, "suggested": None})  # fmt: skip
        else:
            have = f["status"] in ("Needs confirmation", "Inferred")
            text = (f'We found this for "{spec.label}" but could not confirm it. Is it right?'
                    if f["status"] == "Needs confirmation"
                    else f'We worked this out for "{spec.label}" from your website. Is it '
                         "right?" if have
                    else f'We could not find "{spec.label}". What should it say?')  # fmt: skip
            out.append({"id": f"gap:{spec.key}", "group": "gaps", "type": "text", "text": text,
                        "fills": spec.key, "choices": [],
                        "suggested": f["value"] if have else None})  # fmt: skip
    for asked in out:
        asked["allow_dont_know"] = True
        if wording and asked["id"] in wording:
            asked["text"] = wording[asked["id"]]
    return {"ready": True, "questions": out}


def valid(qtype: str, value: Any) -> bool:
    """Whether ``value`` is an answer of type ``qtype``."""

    def number(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and v >= 0

    def text(v: Any) -> bool:
        return isinstance(v, str) and 0 < len(v.strip()) <= MAX_TEXT

    if qtype == "money":
        return (isinstance(value, dict) and set(value) == {"amount", "currency"}
                and number(value["amount"]) and isinstance(value["currency"], str)
                and bool(re.fullmatch(r"[A-Z]{3}", value["currency"])))  # fmt: skip
    if qtype == "number":
        return number(value)
    if qtype == "percent":
        return number(value) and value <= 100
    if qtype == "yes_no":
        return isinstance(value, bool)
    if qtype == "multi_choice":
        return isinstance(value, list) and 0 < len(value) <= 50 and all(map(text, value))
    return text(value)  # text, choice


# --- answers -------------------------------------------------------------------


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(max_length=80)
    status: Literal["answered", "dont_know", "skipped"]
    value: Any = None
    actor: str | None = Field(None, max_length=200)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[Answer] = Field(max_length=100)


def _question(question_id: str) -> tuple[str, str]:
    """(type, field) of a question id. Raises ValueError for an unknown one."""
    if question_id in BY_ID:
        q = BY_ID[question_id]
        return q.type, q.fills
    if question_id == CATALOGUE:
        return "multi_choice", "product"
    key = question_id.removeprefix("gap:")
    spec = _FIELD.get(key)
    if question_id.startswith("gap:") and spec and spec.access in ("public", "document"):
        return "text", key  # a gap's choice is text too
    raise ValueError(f"no question {question_id!r}")


def save_answers(
    engine: Engine,
    organization_id: int,
    request: AnswerRequest,
    catalogue: Callable[[], list[dict[str, Any]]] = list,
) -> list[dict[str, Any]]:
    """Record answers as the user's answers to the fields they fill. ``catalogue``: the
    profile's products and services now (``business_profile``), for the catalogue answer.

    Raises ValueError for an unknown question or a value that is not of its type."""
    rows = []
    for a in request.answers:
        qtype, key = _question(a.question_id)
        if a.status == "answered" and not valid(qtype, a.value):
            raise ValueError(f"{a.question_id}: not a {qtype} answer")
        if a.question_id == CATALOGUE:
            rows += _catalogue_rows(organization_id, a, catalogue())
            continue
        rows.append({"organization_id": organization_id, "section": _SECTION[key],
                     "field": key, "item_key": "",
                     "status": "edited" if a.status == "answered" else a.status,
                     "value": a.value if a.status == "answered" else None,
                     "actor": a.actor})  # fmt: skip
    if rows:
        with engine.begin() as conn:
            conn.execute(pg_insert(CompanyVerification).values(rows))
    return [{"question_id": a.question_id, "status": a.status} for a in request.answers]


def _catalogue_rows(organization_id: int, a: Answer,
                    items: list[dict[str, Any]]) -> list[dict[str, Any]]:  # fmt: skip
    """Each product found confirmed (chosen) or rejected (not chosen), and each name the
    user added as a product of theirs; "I don't know" or skipped on each one found."""
    found = [i for i in items if i["value"]]
    chosen = {str(v).strip().casefold(): str(v).strip() for v in a.value or []}
    known = {str(i["value"]).casefold() for i in found}

    def row(item_key: str, status: str, value: Any = None) -> dict[str, Any]:
        return {"organization_id": organization_id, "section": "products_services",
                "field": "product", "item_key": item_key[:64], "status": status,
                "value": value, "actor": a.actor}  # fmt: skip

    if a.status != "answered":
        return [row(i["item_key"], a.status) for i in found]
    return [*(row(i["item_key"], "confirmed" if str(i["value"]).casefold() in chosen
                  else "rejected", i["value"]) for i in found),
            *(row(f"user:{name}", "edited", name) for k, name in chosen.items()
              if k not in known)]  # fmt: skip


# --- wording -------------------------------------------------------------------

SYSTEM = """You word onboarding questions for the person setting up one company, in plain,
friendly English. You get what research found about the company (its name, what it does,
whom it sells to, its stage and size, its offerings) and a JSON list of questions (id, text,
type, choices). Answer with JSON only: {"questions": [{"id": ..., "text": ...}]}, one entry
per question, with the same ids. Keep each question's meaning and its answer type; no new
questions; at most 30 words each.
Make each one fit this company: use its name, and its own words for what it sells and for
its buyers (students, shoppers, patients, clients, accounts...). The company may be a
one-person shop or a global enterprise: for a large one ask about the business unit,
brand or region the person is responsible for; for a small one keep it simple. Never
assume a figure or a fact the research did not give."""
# What research found, for the wording: these fields, then the first offerings.
_CONTEXT_FIELDS = ("company_name", "company_description", "customer_type", "market_domain",
                   "company_stage", "company_size", "employee_count", "ownership_type",
                   "geography_served")  # fmt: skip
MAX_CONTEXT_OFFERINGS = 8


def _digest(company: str, q: dict[str, Any]) -> str:
    key = json.dumps([company, q["id"], q["text"], q["type"], q["choices"]], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()


def _context(bip: dict[str, Any]) -> str:
    """What research found about the company, as lines for the wording model; the
    questions are worded again when it changes."""
    fields = {f["key"]: f for s in bip["sections"] for f in s.get("fields", [])}
    lines = [f"{fields[k]['label']}: {', '.join(_texts(fields[k]['value']))}"
             for k in _CONTEXT_FIELDS if k in fields and _texts(fields[k]["value"])]  # fmt: skip
    offerings = [str(i["value"]) for i in bip["sections"][1]["items"] if i["value"]]
    if offerings:
        lines.append("Offerings: " + ", ".join(offerings[:MAX_CONTEXT_OFFERINGS]))
    return "\n".join(lines) or f"Website: {bip['domain'] or 'unknown'}"


def _stored_wording(engine: Engine, model: str, digests: dict[str, str]) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(LlmPageResult.text_sha256, LlmPageResult.result).where(
                LlmPageResult.text_sha256.in_(digests.values()), LlmPageResult.model == model,
                LlmPageResult.prompt_version == PROMPT_VERSION)
        ).all()  # fmt: skip
    by_digest = {r.text_sha256: r.result["text"] for r in rows}
    return {qid: by_digest[d] for qid, d in digests.items() if d in by_digest}


def questions_view(engine: Engine, settings: Settings, organization_id: int) -> dict[str, Any]:
    """The questions to ask now, in the model's wording where it has worded them."""
    bip = build_business_profile(engine, settings, organization_id)
    plain = generate(bip)
    if not settings.llm.model or not plain["questions"]:
        return plain
    company = _context(bip)
    digests = {q["id"]: _digest(company, q) for q in plain["questions"]}
    return generate(bip, _stored_wording(engine, settings.llm.model, digests))


def word_questions(
    engine: Engine,
    settings: Settings,
    organization_id: int,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Have the model word the current questions it has not worded yet: ``disabled``,
    ``no_questions``, ``cached``, ``worded`` or ``failed`` (templates are used)."""
    s = settings.llm
    if not s.model:
        return "disabled"
    bip = build_business_profile(engine, settings, organization_id)
    asked = generate(bip)["questions"]
    if not asked:
        return "no_questions"
    company = _context(bip)
    digests = {q["id"]: _digest(company, q) for q in asked}
    done = _stored_wording(engine, s.model, digests)
    todo = [q for q in asked if q["id"] not in done]
    if not todo:
        return "cached"
    payload = [{k: q[k] for k in ("id", "text", "type", "choices")} for q in todo]
    reader = PageReader(s, transport)
    try:
        answer = reader.complete(SYSTEM, f"Company:\n{company}\nQuestions: {json.dumps(payload)}",
                                 2000, "none")  # fmt: skip
        data = json.loads(re.sub(r"<think>.*?</think>", "", answer, flags=re.S))
    except (LlmError, ValueError) as exc:
        log.warning("questions.failed", extra={"organization_id": organization_id,
                                               "error": str(exc)})  # fmt: skip
        return "failed"
    finally:
        reader.close()
    worded = {e["id"]: e["text"].strip() for e in (data.get("questions") or [])
              if isinstance(e, dict) and e.get("id") in digests and isinstance(e.get("text"), str)
              and 0 < len(e["text"].strip()) <= 300}  # fmt: skip
    if not worded:
        return "failed"
    # A question the model left out keeps its template wording, so it is not asked again.
    rows = [{"text_sha256": digests[q["id"]], "model": s.model, "prompt_version": PROMPT_VERSION,
             "result": {"id": q["id"], "text": worded.get(q["id"], q["text"])}}
            for q in todo]  # fmt: skip
    with engine.begin() as conn:
        conn.execute(pg_insert(LlmPageResult).values(rows).on_conflict_do_nothing())
    return "worded"
