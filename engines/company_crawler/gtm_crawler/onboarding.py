"""The onboarding view (Integration P1): what was found about the company, for the
user to verify, and the competitors discovered.

``onboarding_view`` regroups the dossier (``dossier.build_dossier``) under the six
headings the onboarding flow verifies: company info, value proposition, market
segment, target market, business model, products and services. Nothing is
invented: a field with no observation is ``value: None`` with its state. A value
only the company states (an UNSUPPORTED claim) is shown as found, with that state:
at onboarding the user is the company and confirms or corrects it.

The user's answers (``company_verification``) are user input, never crawler
evidence: they are shown beside each item and do not change any assessment.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db.models import ACTIVE_JOB_STATUSES, CompanyVerification, CrawlJob
from .dossier import COMPETITOR_FIELDS, build_dossier

MAX_CITATIONS = 5
# (key, title, ((field, label), ...)). Product fields are gathered across products.
SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "company_info",
        "Company info",
        (
            ("company_name", "Company name"),
            ("legal_name", "Legal name"),
            ("website", "Website"),
            ("company_description", "Description"),
            ("headquarters", "Headquarters"),
            ("company_location", "Location"),
            ("founded_year", "Founded"),
            ("company_size", "Company size"),
            ("employee_count", "Employees"),
        ),
    ),
    ("value_proposition", "Value proposition", (("product_positioning", "Value proposition"),)),
    (
        "market_segment",
        "Market segment",
        (("market_domain", "Market / industry"), ("product_category", "Product category")),
    ),
    (
        "target_market",
        "Target market",
        (
            ("customer", "Named customers"),
            ("main_use_case", "Main use cases"),
            ("customer_concentration", "Customer concentration"),
        ),
    ),
    (
        "business_model",
        "Business model",
        (("business_model", "Business model"), ("plan_name", "Plans"), ("price", "Prices")),
    ),
    ("products_services", "All products / services", (("product", "Product"),)),
)
SECTION_FIELDS = {key: {f for f, _ in fields} for key, _, fields in SECTIONS}
_STATE_RANK = (
    "CORROBORATED",
    "SUPPORTED",
    "UNSUPPORTED",
    "UNVERIFIED",
    "STALE",
    "CONFLICTING",
    "UNKNOWN",
    "NOT_APPLICABLE",
)


class VerificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: str
    field: str
    item_key: str = Field("", max_length=64)
    status: Literal["confirmed", "edited", "rejected"]
    value: Any = None
    actor: str | None = Field(None, max_length=200)  # who answered, when the caller knows


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[VerificationItem] = Field(max_length=500)


def field_status(
    state: str, has_value: bool, active: bool, answer: str | None = None, access: str = "public"
) -> str:
    """A field's status for the user, from its evidence ``state``, whether it has a
    value, whether research is still running (``active``), the user's ``answer``
    (confirmed, edited, rejected) and where its value can come from (``access``)."""
    if answer in ("confirmed", "edited"):
        return "Verified"
    if answer == "rejected" or not has_value:
        # Only public research is still extracting; the user, a document or an
        # integration must supply the rest.
        return "Extracting" if active and access == "public" and answer is None else "Not found"
    return {
        "CORROBORATED": "Verified",
        "MODEL_INFERRED": "Inferred",
        "CONFLICTING": "Conflicting",
        "UNSUPPORTED": "Needs confirmation",
        "UNVERIFIED": "Needs confirmation",
        "STALE": "Needs confirmation",
    }.get(state, "Extracted")


def _entries(v: Any) -> list[dict[str, Any]]:
    return [v] if isinstance(v, dict) else list(v or [])


def _text(value: Any) -> Any:
    """A plain value for display: a structured value (a price) becomes one string."""
    if isinstance(value, dict):
        parts = [str(v) for k, v in value.items() if v not in (None, "") and k != "raw"]
        return " ".join(parts) or None
    return value


def _found(e: dict[str, Any]) -> Any:
    value = e.get("value", e.get("claimed_value"))
    if value is None and e.get("state") == "CONFLICTING":
        # No one value: show what each source says, for the user to choose.
        value = list(dict.fromkeys(c["value_verbatim"] for c in e.get("citations", []))) or None
    return _text(value)


def _item(
    field: str, label: str, entries: list[dict[str, Any]], item_key: str = "", scalar: bool = False
) -> dict[str, Any]:
    found = [e for e in entries if _found(e) is not None]
    flat = [v for e in found for v in (_found(e) if isinstance(_found(e), list) else [_found(e)])]
    values = list(dict.fromkeys(v if isinstance(v, int | float) else str(v) for v in flat))
    states = [e.get("state", "UNKNOWN") for e in (found or entries)]
    state = min(
        states, key=lambda s: _STATE_RANK.index(s) if s in _STATE_RANK else 99, default="UNKNOWN"
    )
    confidences = [e["confidence"] for e in found if e.get("confidence") is not None]
    citations = [
        {
            "url": c["url"],
            "quote": c["quote"],
            "source_type": c["source_type"],
            "tier": c["source_tier"],
            "retrieved_at": c.get("retrieved_at"),
        }
        for e in found
        for c in e.get("citations", [])
    ][:MAX_CITATIONS]
    return {
        "field": field,
        "label": label,
        "item_key": item_key,
        # A conflict stays a list of the candidates, even for a one-value field.
        "value": (values[0] if scalar and state != "CONFLICTING" else values) if values else None,
        "state": state,
        "confidence": max(confidences) if confidences else None,
        "citations": citations,
        "verification": None,
    }


def _product_item(product: dict[str, Any]) -> dict[str, Any]:
    fields = product["fields"]
    item = _item(
        "product",
        "Product",
        [{"value": product["name"], "state": "SUPPORTED"}],
        str(product["product_id"]),
        scalar=True,
    )
    for f in ("product_description", "product_positioning", "key_feature", "price"):
        item["citations"] += _item(f, f, _entries(fields.get(f)))["citations"]
    item["citations"] = item["citations"][:MAX_CITATIONS]

    def values(field: str) -> list[Any]:
        return _item(field, field, _entries(fields.get(field)))["value"] or []

    item["details"] = {
        "description": next(iter(values("product_description")), None),
        "category": next(iter(values("product_category")), None),
        "key_features": values("key_feature"),
        "prices": values("price"),
        "plans": values("plan_name"),
    }
    return item


def onboarding_view(
    engine: Engine, organization_id: int, profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The onboarding sections; with ``profile`` (``profile.stored_profile``), items the
    evidence left empty are filled from the model's profile, marked as inferred."""
    dossier = build_dossier(engine, organization_id)
    company = dossier["company"]["fields"]
    products = dossier["products"]

    def everywhere(field: str) -> list[dict[str, Any]]:
        """A field's entries for the company and for each of its products."""
        return [
            *_entries(company.get(field)),
            *(e for p in products for e in _entries(p["fields"].get(field))),
        ]

    sections: list[dict[str, Any]] = []
    for key, title, fields in SECTIONS:
        items: list[dict[str, Any]] = []
        if key == "products_services":
            items = [_product_item(p) for p in products]
            if not items:
                items = [_item("product", "Product", [])]
        elif key == "value_proposition":
            loose = _entries(company.get("product_positioning"))
            items = [
                _item(
                    "product_positioning",
                    f"Value proposition: {p['name']}",
                    _entries(p["fields"].get("product_positioning")),
                    str(p["product_id"]),
                )
                for p in products
                if any(
                    _found(e) is not None for e in _entries(p["fields"].get("product_positioning"))
                )
            ]
            if loose or not items:
                items.insert(0, _item("product_positioning", "Value proposition", loose))
        else:
            items = [_item(f, label, everywhere(f), scalar=f in _SCALAR) for f, label in fields]
        sections.append({"key": key, "title": title, "items": items})

    if profile and profile.get("profile"):
        fill_from_profile(sections, profile["profile"])
    with engine.connect() as conn:
        job = conn.execute(
            select(CrawlJob.id, CrawlJob.status, CrawlJob.root_url)
            .where(CrawlJob.organization_id == organization_id)
            .order_by(CrawlJob.id.desc())
            .limit(1)
        ).first()
    active = job is not None and job.status in ACTIVE_JOB_STATUSES
    answers = verification(engine, organization_id)
    by_key = {(a["field"], a["item_key"]): a for a in answers}
    for section in sections:
        for item in section["items"]:
            answer = by_key.get((item["field"], item["item_key"]))
            item["verification"] = answer
            item["status"] = field_status(item["state"], item["value"] is not None, active,
                                          answer["status"] if answer else None)  # fmt: skip
            dates = [c["retrieved_at"] for c in item["citations"] if c.get("retrieved_at")]
            item["extracted_at"] = max(dates, key=datetime.fromisoformat) if dates else None
    answered = {a["section"] for a in answers}
    return {
        "company_id": organization_id,
        "domain": dossier["company"]["domain"],
        "website": job.root_url if job else None,
        "crawl": {"crawl_id": job.id, "status": job.status.upper()} if job else None,
        "sections": sections,
        "competitors": [
            {
                **{
                    k: c[k]
                    for k in ("organization_id", "name", "domain", "evidence_basis", "assessed")
                },
                "fields": {
                    f: _item(f, f, _entries(c["fields"].get(f)), scalar=f not in _LIST)["value"]
                    for f in COMPETITOR_FIELDS
                },
            }
            for c in dossier["competitors"]
        ],
        "candidate_competitors": [
            {"name": c["name"], "evidence_basis": c["evidence_basis"]}
            for c in dossier["candidate_competitors"]
        ],
        "verified": all(key in answered for key, _, _ in SECTIONS),
        "profile": profile,
    }


_SCALAR = frozenset(
    {
        "company_name",
        "legal_name",
        "website",
        "company_description",
        "headquarters",
        "founded_year",
        "company_size",
        "employee_count",
        "business_model",
        "customer_concentration",
    }
)


# Competitor fields shown as lists; the others as their first (best) value.
_LIST = frozenset({"key_feature", "investor", "funding_round"})

# Onboarding fields the profile (profile.py) fills when the evidence alone left them
# empty: field -> (profile section, key). The business model is its own case.
PROFILE_FIELDS = {
    "company_description": ("company", "description"),
    "headquarters": ("company", "headquarters"),
    "company_location": ("company", "locations"),
    "founded_year": ("company", "founded_year"),
    "company_size": ("company", "size"),
    "product_positioning": ("value_proposition", "statement"),
    "market_domain": ("market", "category"),
    "product_category": ("market", "sub_category"),
    "main_use_case": ("icp", "use_cases"),
    "customer": ("target_customers", "named_customers"),
    "price": ("business_model", "price_range"),
}
_BUSINESS_MODEL_PARTS = ("customer_type", "revenue_model", "delivery", "sales_motion")


def _leaves(node: Any) -> list[dict[str, Any]]:
    return [n for n in (node if isinstance(node, list) else [node]) if isinstance(n, dict)]


def _inferred(item: dict[str, Any], leaves: list[dict[str, Any]], scalar: bool,
              reasoning: dict[str, Any] | None = None) -> None:  # fmt: skip
    """Fill an empty onboarding ``item`` from profile ``leaves`` (model-inferred)."""
    values = [leaf["value"] for leaf in leaves]
    if reasoning:
        leaves = [*leaves, reasoning]
    confidences = [c for leaf in leaves if (c := leaf.get("evidence_confidence")) is not None]
    item.update({
        "value": values[0] if scalar else values,
        "state": "MODEL_INFERRED",
        "confidence": max(confidences) if confidences else None,
        "citations": [{"url": c["url"], "quote": c["quote"], "source_type": "model_cited",
                       "tier": None}
                      for leaf in leaves for c in leaf["citations"] if "url" in c][:MAX_CITATIONS],
    })  # fmt: skip
    if reasoning:
        item["reasoning"] = reasoning["value"]


def offerings_of(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The catalogue part's offerings (profile.OFFERINGS_SCHEMA) sold on their own: not a
    module another offering lists among its components, nor a plan of the business
    model ("Pro"). With a company selling one product, the other products the model
    listed are that product's components. The model is told all this; this holds when
    it slips."""
    catalogue = profile.get("catalogue") or {}
    offerings = [o for o in catalogue.get("offerings") or []
                 if isinstance(o.get("name"), dict)]  # fmt: skip

    def name(node: dict[str, Any]) -> str:
        return str(node["value"]).casefold()

    def product(o: dict[str, Any]) -> bool:
        return (o.get("type") or {}).get("value") in (None, "product")

    main = catalogue.get("main_offering")
    # The main offering by name; else the first product, which the model lists first.
    lead = next((o for o in offerings if main and name(o["name"]) == name(main)),
                next((o for o in offerings if product(o)), None))  # fmt: skip
    if catalogue.get("sold_as") == "one_product" and lead is not None:
        folded = [o for o in offerings if o is not lead and product(o)]
        have = {name(c) for c in _leaves(lead.get("components"))} | {name(lead["name"])}
        extra: dict[str, dict[str, Any]] = {}
        for o in folded:  # each once, as first written
            if name(o["name"]) not in have:
                extra.setdefault(name(o["name"]), o["name"])
        rest = [o for o in offerings if o is not lead and all(o is not f for f in folded)]
        lead = {**lead, "components": [*_leaves(lead.get("components")), *extra.values()]}
        offerings = [lead, *rest]

    # One platform (sold as one product, or the only offering, with modules beneath it) is
    # sold under the company's own name: a model can head it with one of its modules
    # ("Activation Ads" for Clay), which then becomes a component. A name that already
    # carries the brand ("Coirei GTM") stays.
    if catalogue.get("sold_as") == "one_product":
        platform = lead
    else:
        platform = (offerings[0] if len(offerings) == 1
                    and len(_leaves(offerings[0].get("components"))) >= 2 else None)  # fmt: skip
    brand = (profile.get("company") or {}).get("name")
    if platform is not None:
        fixed = dict(platform)
        if (
            isinstance(brand, dict)
            and brand.get("value")
            and not re.search(rf"\b{re.escape(name(brand))}\b", name(platform["name"]))
        ):
            fixed.update(
                name=brand, components=[platform["name"], *_leaves(platform.get("components"))]
            )
        # Sold by subscription or use, a platform is a product, not a service.
        recurring = ((profile.get("business_model") or {}).get("revenue_model") or {}).get(
            "value"
        ) in ("subscription", "freemium", "usage_based")
        kind = platform.get("type")
        if (
            isinstance(kind, dict)
            and kind.get("value") == "service"
            and (recurring or catalogue.get("sold_as") == "one_product")
        ):
            fixed["type"] = {**kind, "value": "product"}
        offerings = [fixed if o is platform else o for o in offerings]  # fmt: skip

    plans = {name(p) for p in _leaves((profile.get("business_model") or {}).get("plans"))}
    out = []
    for o in offerings:
        others = {name(c) for x in offerings if x is not o
                  for c in _leaves(x.get("components"))}  # fmt: skip
        if name(o["name"]) not in others | plans:
            out.append(o)
    return out


def fill_from_profile(sections: list[dict[str, Any]], profile: dict[str, Any]) -> None:
    """Fill the items the evidence left empty from the model's profile, marked
    ``MODEL_INFERRED`` with the evidence it cited. Found values are never replaced, except
    the company's value proposition (a tagline) by the model's statement."""
    value_prop = profile.get("value_proposition") or {}
    model = profile.get("business_model") or {}
    for section in sections:
        items = section["items"]
        if section["key"] == "products_services" and not any(i["value"] for i in items):
            offerings = offerings_of(profile)
            section["items"] = items = []
            for o in offerings:
                item: dict[str, Any] = {"field": "product", "label": "Product",
                                        "item_key": f"llm:{o['name']['value']}",
                                        "verification": None}  # fmt: skip
                _inferred(item, [o["name"]], scalar=True)
                item["details"] = {
                    "type": (o.get("type") or {}).get("value"),
                    "description": (o.get("description") or {}).get("value"),
                    "key_features": [f["value"] for f in _leaves(o.get("key_features"))],
                    "prices": [o["price"]["value"]] if o.get("price") else [],
                }
                items.append(item)
            continue
        for item in items:
            # The company's own value proposition is the model's statement when it has
            # one: a found product_positioning there is the home page's tagline (h1),
            # which says what the company calls itself, not what it does for whom.
            tagline = item["field"] == "product_positioning" and value_prop.get("statement")
            if (item["value"] is not None and not tagline) or item["item_key"]:
                continue
            field = item["field"]
            if field == "business_model":
                parts = [model[k] for k in _BUSINESS_MODEL_PARTS if k in model]
                if parts:
                    _inferred(item, parts, scalar=False, reasoning=model.get("reasoning"))
                continue
            if field not in PROFILE_FIELDS:
                continue
            group, key = PROFILE_FIELDS[field]
            leaves = _leaves((profile.get(group) or {}).get(key))
            if leaves:
                reasoning = value_prop.get("reasoning") if field == "product_positioning" else None
                _inferred(item, leaves, scalar=field in _SCALAR, reasoning=reasoning)


def verification(
    engine: Engine, organization_id: int, history: bool = False
) -> list[dict[str, Any]]:
    """The user's answers, oldest first: the latest per item, or with ``history``
    every answer ever given (the table is append-only; the latest one wins)."""
    with engine.connect() as conn:
        v = CompanyVerification
        rows = conn.execute(
            select(v.section, v.field, v.item_key, v.status, v.value, v.actor, v.verified_at)
            .where(v.organization_id == organization_id)
            .order_by(v.id)
        ).all()
    answers = [
        {
            "section": r.section,
            "field": r.field,
            "item_key": r.item_key,
            "status": r.status,
            "value": r.value,
            "actor": r.actor,
            "verified_at": r.verified_at.isoformat(),
        }
        for r in rows
    ]
    if history:
        return answers
    latest = {(a["field"], a["item_key"]): a for a in answers}
    return list(latest.values())  # in the order the items were first answered


def save_verification(
    engine: Engine, organization_id: int, request: VerificationRequest
) -> list[dict[str, Any]]:
    """Record the user's answers; every one is kept and the latest per item wins.

    Raises ValueError for a section and field neither the onboarding view nor the
    Business Intelligence Profile (``business_profile.REGISTRY``) has."""
    from .business_profile import EDITABLE  # it builds on this module

    for item in request.items:
        if (item.field not in SECTION_FIELDS.get(item.section, ())
                and (item.section, item.field) not in EDITABLE):  # fmt: skip
            raise ValueError(f"no field {item.field!r} in section {item.section!r}")
    if request.items:
        rows = [{"organization_id": organization_id, **i.model_dump()} for i in request.items]
        with engine.begin() as conn:
            conn.execute(pg_insert(CompanyVerification).values(rows))
    return verification(engine, organization_id)
