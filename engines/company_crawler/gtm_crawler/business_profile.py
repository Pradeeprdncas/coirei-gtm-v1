"""The Business Intelligence Profile: what COIREI knows about a company, under thirteen
headings, every field with its value, status, confidence, extraction date and sources.

It is assembled on read from what Postgres already holds: the assessed evidence
(``dossier.build_dossier``), the model's cited profile (``profile.stored_profile``) and
the user's answers (``company_verification``). Every field is the model's structured,
cited value when it gave one (the raw evidence kept beside it as ``evidence_value``;
the evidence's state still decides Verified or Conflicting, and a value no assessed
evidence backs is Inferred), else the raw evidence. The user's answer wins over both,
with the value it replaced kept beside it as ``crawler_value``.

A field's ``access`` says where its value can come from: ``public`` research,
``document`` (an uploaded document), ``private`` (only the user can say) or
``integration`` (an authorized account connection). A field no source fills yet still
appears, as Not found, so the profile always shows what is missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine, select

from .config import AssessmentSettings, Settings
from .db.models import (
    ACTIVE_JOB_STATUSES,
    CrawlJob,
    ExternalIdentity,
    IdentityDecision,
    IdentityLink,
    Organization,
)
from .dossier import build_dossier
from .onboarding import (
    MAX_CITATIONS,
    _entries,
    _inferred,
    _item,
    _leaves,
    _product_item,
    field_status,
    fill_from_profile,
    offerings_of,
    verification,
)
from .profile import stored_profile

__all__ = ["EDITABLE", "REGISTRY", "SECTIONS", "assemble", "build_business_profile",
           "field_status"]  # fmt: skip

Access = Literal["public", "document", "private", "integration"]

SECTIONS: tuple[tuple[str, str], ...] = (
    ("company_profile", "Company profile"),
    ("products_services", "Product and service catalogue"),
    ("business_model", "Business model"),
    ("target_customers", "Target customer hypotheses"),
    ("brand_positioning", "Brand positioning and messaging"),
    ("marketing_footprint", "Existing marketing footprint"),
    ("sales_commercial", "Sales and commercial information"),
    ("customer_evidence", "Customer evidence"),
    ("content_assets", "Content and brand assets"),
    ("business_objectives", "Business objectives"),
    ("operational_constraints", "Operational constraints"),
    ("missing_information", "Missing information and unresolved assumptions"),
    ("source_records", "Source and confidence records"),
)


@dataclass(frozen=True)
class Field:
    section: str
    key: str  # the dossier field's name when one feeds it, so onboarding answers apply
    label: str
    dossier: tuple[str, ...] = ()  # assessed fields, for the company and its products
    model: tuple[tuple[str, str | None], ...] = ()  # (profile group, key); None: the group
    access: Access = "public"
    scalar: bool = False


def _f(section: str, key: str, label: str, *dossier: str, **kw: Any) -> Field:
    return Field(section, key, label, dossier, **kw)


def _private(section: str, key: str, label: str) -> Field:
    return Field(section, key, label, access="private")


_CP, _BM, _TC, _BR = "company_profile", "business_model", "target_customers", "brand_positioning"
_MF, _SC, _CE = "marketing_footprint", "sales_commercial", "customer_evidence"

REGISTRY: tuple[Field, ...] = (
    _f(_CP, "company_name", "Company name", "company_name", model=(("company", "name"),),
       scalar=True),
    _f(_CP, "legal_name", "Legal name", "legal_name", model=(("company", "legal_name"),),
       scalar=True),
    _f(_CP, "website", "Website", "website", scalar=True),
    _f(_CP, "other_domains", "Other company domains"),
    _f(_CP, "market_domain", "Industry", "market_domain", model=(("market", "category"),)),
    _f(_CP, "headquarters", "Headquarters", "headquarters",
       model=(("company", "headquarters"),), scalar=True),
    _f(_CP, "company_location", "Offices and operating locations", "company_location",
       "office_location", model=(("company", "locations"),)),
    _f(_CP, "geography_served", "Countries, regions and cities served", "company_country",
       model=(("market", "geography"),)),
    _f(_CP, "company_description", "What the company does", "company_description",
       model=(("company", "description"),), scalar=True),
    _f(_CP, "company_stage", "Company stage", model=(("company", "stage"),), scalar=True),
    _f(_CP, "company_size", "Company size", "company_size", model=(("company", "size"),),
       scalar=True),
    _f(_CP, "employee_count", "Employees", "employee_count", scalar=True),
    _f(_CP, "founded_year", "Founding year", "founded_year",
       model=(("company", "founded_year"),), scalar=True),
    _f(_CP, "founders", "Founders", "founder", model=(("company", "founders"),)),
    _f(_CP, "contact_email", "Public email", "contact_email",
       model=(("company", "contact_email"),)),
    _f(_CP, "contact_phone", "Public phone", "contact_phone",
       model=(("company", "contact_phone"),)),
    _f(_CP, "contact_page", "Contact page", "contact_page"),
    _f(_BM, "business_model", "Business model", "business_model"),
    _f(_BM, "customer_type", "Customer type (B2B, B2C, D2C...)",
       model=(("business_model", "customer_type"),), scalar=True),
    _f(_BM, "revenue_model", "Pricing model", model=(("business_model", "revenue_model"),),
       scalar=True),
    _f(_BM, "delivery", "Delivery", model=(("business_model", "delivery"),), scalar=True),
    _f(_BM, "sales_motion", "Sales motion", model=(("business_model", "sales_motion"),),
       scalar=True),
    _f(_BM, "pricing_summary", "Pricing", model=(("business_model", "pricing_summary"),),
       scalar=True),
    _f(_BM, "plan_name", "Plans and packages", "plan_name",
       model=(("business_model", "plans"),)),
    _f(_BM, "price", "Published prices", "price", model=(("business_model", "price_range"),)),
    _f(_TC, "target_segments", "Stated target market",
       model=(("icp", "stated_target_market"),)),
    _f(_TC, "business_types", "Business types served", model=(("icp", "business_types"),)),
    _f(_TC, "company_sizes", "Company sizes served", model=(("icp", "company_sizes"),)),
    _f(_TC, "buyer_roles", "Target audience (roles and teams)",
       model=(("icp", "buyer_roles"),)),
    _f(_TC, "target_industries", "Target industries", model=(("icp", "industries"),)),
    _f(_TC, "main_use_case", "Use cases", "main_use_case", model=(("icp", "use_cases"),)),
    _f(_TC, "customer_concentration", "Customer concentration", "customer_concentration",
       scalar=True),
    _f(_TC, "icp_hypotheses", "Ideal customer hypotheses", model=(("icp", "hypotheses"),)),
    _private(_TC, "valuable_customers", "Most valuable existing customers"),
    # The model's statement; the home page's headline (h1) is the tagline.
    _f(_BR, "product_positioning", "Main value proposition",
       model=(("value_proposition", "statement"),), scalar=True),
    _f(_BR, "tagline", "Tagline", "product_positioning", model=(("brand", "tagline"),)),
    _f(_BR, "positioning_statement", "Positioning statement",
       model=(("brand", "positioning_statement"),), scalar=True),
    _f(_BR, "mission", "Mission", model=(("brand", "mission"),), scalar=True),
    _f(_BR, "vision", "Vision", model=(("brand", "vision"),), scalar=True),
    _f(_BR, "brand_values", "Brand values", model=(("brand", "values"),)),
    _f(_BR, "brand_promise", "Brand promise", model=(("brand", "promise"),), scalar=True),
    _f(_BR, "key_messages", "Key messaging", model=(("brand", "key_messages"),)),
    _f(_BR, "selling_points", "Primary selling points", model=(("brand", "selling_points"),)),
    _f(_BR, "customer_claims", "Claims and promises to customers", model=(("brand", "claims"),)),
    _f(_BR, "tone_of_voice", "Tone of voice", model=(("brand", "tone_of_voice"),), scalar=True),
    _f(_BR, "brand_personality", "Brand personality", model=(("brand", "personality"),)),
    _f(_BR, "calls_to_action", "Main calls to action", model=(("brand", "calls_to_action"),)),
    _f(_BR, "messaging_consistency", "Messaging consistency across website and social",
       model=(("brand", "consistency"),), scalar=True),
    _f(_BR, "differentiators", "Differentiators", model=(("differentiators", None),)),
    _f(_MF, "social_profiles", "Social profiles"),
    _private(_MF, "marketing_channels", "Marketing channels in use"),
    _private(_MF, "best_channels", "Channels that bring the best customers"),
    _private(_MF, "marketing_challenges", "Biggest marketing challenges"),
    Field(_MF, "social_analytics", "Social account analytics", access="integration"),
    _f(_SC, "annual_revenue", "Annual revenue", "annual_revenue",
       model=(("facts", "annual_revenue"),), scalar=True),
    _f(_SC, "arr", "Annual recurring revenue", "arr", model=(("facts", "arr"),),
       scalar=True),
    _private(_SC, "monthly_marketing_budget", "Monthly marketing budget"),
    _private(_SC, "average_order_value", "Average order or contract value"),
    _private(_SC, "monthly_revenue_target", "Monthly revenue target"),
    _private(_SC, "monthly_leads", "Leads received per month"),
    _private(_SC, "lead_conversion_rate", "Leads that become customers"),
    _private(_SC, "sales_team", "Existing sales team"),
    _private(_SC, "crm_platforms", "CRM and communication platforms"),
    _private(_SC, "enquiry_process", "What happens when a new enquiry arrives"),
    _f(_CE, "customer", "Named customers", "customer",
       model=(("target_customers", "named_customers"),)),
    _f(_CE, "case_study", "Case studies", "case_study",
       model=(("target_customers", "case_studies"),)),
    _f(_CE, "customer_count", "Customer count", "customer_count",
       model=(("facts", "customer_count"),), scalar=True),
    _f(_CE, "customer_proof", "Testimonials and proof", model=(("value_proposition", "proof"),)),
    Field("content_assets", "documents", "Brochures, catalogues, presentations",
          access="document"),
    _private("business_objectives", "primary_objective", "Primary business objective"),
    _private("business_objectives", "products_to_promote", "Products or services to promote"),
    _private("business_objectives", "priority_markets", "Priority geographic markets"),
    _private("operational_constraints", "customer_capacity", "Additional customers you can serve"),
)  # fmt: skip
# (section, field) pairs the user may answer (onboarding.save_verification).
EDITABLE = frozenset({(f.section, f.key) for f in REGISTRY} | {("products_services", "product")})
# Answer statuses that say something about the value (the others: dont_know, skipped).
_VALUES = frozenset({"confirmed", "edited", "rejected"})
# Fields whose model value is a hypothesis: an unresolved assumption until confirmed.
_HYPOTHESES = frozenset({"icp_hypotheses"})
_DETAILS = ("components", "benefits", "problems_solved", "use_cases", "target_users",
            "pricing_model", "maturity", "related_offerings", "proof")  # fmt: skip
# Statuses the user is asked to resolve (Inferred stays: it is labelled, not a gap).
_OPEN = ("Not found", "Conflicting", "Needs confirmation")
_REASONS = {
    "Conflicting": "sources disagree",
    "Needs confirmation": "only the company states it, or the evidence is old",
}


def _when(values: list[str | None]) -> str | None:
    dated = [v for v in values if v]
    return max(dated, key=datetime.fromisoformat) if dated else None


def _leaf(node: dict[str, Any]) -> dict[str, Any] | None:
    """A cited value: the node itself, or an item made of cited parts (a proof's kind and
    claim, a competitor's name and domain, a hypothesis' segment and why) as one value
    of those parts, citing what they cite."""
    if "value" in node:
        return node
    parts = {k: v for k, v in node.items() if isinstance(v, dict | list)}
    value = {k: [x["value"] for x in v if isinstance(x, dict)] if isinstance(v, list)
             else v.get("value") for k, v in parts.items()}  # fmt: skip
    leaves = [x for v in parts.values() for x in (v if isinstance(v, list) else [v])
              if isinstance(x, dict)]  # fmt: skip
    confidences = [c for x in leaves if (c := x.get("evidence_confidence")) is not None]
    citations = [c for x in leaves for c in x.get("citations", [])]
    if not citations:
        return None
    return {"value": value, "citations": citations,
            "evidence_confidence": max(confidences) if confidences else None}  # fmt: skip


def _offering_details(items: list[dict[str, Any]], details: dict[str, Any]) -> None:
    """The catalogue part's details of each offering, joined to it by name (any case)."""
    by_name = {str(o["name"]["value"]).casefold(): o for o in details.get("offerings") or []
               if isinstance(o.get("name"), dict)}  # fmt: skip
    for item in items:
        found = by_name.get(str(item["value"]).casefold())
        if not found:
            continue
        item.setdefault("details", {})
        for key in _DETAILS:
            leaves = _leaves(found.get(key))
            if leaves:
                values = [leaf["value"] for leaf in leaves]
                item["details"][key] = values if isinstance(found[key], list) else values[0]
                item["citations"] = [*item["citations"],
                                     *({"url": c["url"], "quote": c["quote"],
                                        "source_type": "model_cited", "tier": None}
                                       for leaf in leaves for c in leaf["citations"]
                                       if "url" in c)][:MAX_CITATIONS]  # fmt: skip


def _catalogue(found: list[dict[str, Any]], model: dict[str, Any],
               answered: set[str]) -> list[dict[str, Any]]:  # fmt: skip
    """The model's offerings as the catalogue. The rule extractor takes pricing-page
    features, plan bullets and slogans for products; the model reads what is sold. An
    offering keeps the key of the crawled product of its name (so answers on it hold); a
    crawled product the model left out stays only when the user answered it."""
    fresh: list[dict[str, Any]] = [{"key": "products_services", "items": []}]
    fill_from_profile(fresh, model)
    by_name = {str(i["value"]).casefold(): i for i in found}
    out: list[dict[str, Any]] = []
    for item in fresh[0]["items"]:
        crawled = by_name.pop(str(item["value"]).casefold(), None)
        if crawled:
            item["item_key"] = crawled["item_key"]
            item["citations"] = [*item["citations"], *crawled["citations"]][:MAX_CITATIONS]
            item["details"] = {**crawled["details"],
                               **{k: v for k, v in item["details"].items() if v}}  # fmt: skip
        out.append(item)
    return out + [i for i in by_name.values() if i["item_key"] in answered]


def _model_leaves(
    model: dict[str, Any], paths: tuple[tuple[str, str | None], ...]
) -> list[dict[str, Any]]:
    out = []
    for group, key in paths:
        node = model.get(group)
        if key is not None:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        out += [leaf for n in _leaves(node) if (leaf := _leaf(n))]
    return out


def _finish(
    item: dict[str, Any],
    access: str,
    active: bool,
    answers: list[dict[str, Any]],
    retrieved: dict[int, str],
    leaves: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """An item (``onboarding._item`` shape) as a profile field."""
    if leaves is not None:  # the model's value: what it cites, then the field's evidence
        cited = [{"url": c["url"], "quote": c["quote"], "source_type": "model_cited",
                  "tier": None, "retrieved_at": retrieved.get(c.get("observation_id", -1))}
                 for leaf in leaves for c in leaf["citations"] if "url" in c]  # fmt: skip
        seen = {(c["url"], c["quote"]) for c in cited}
        cited += [c for c in item.get("evidence_citations", [])
                  if (c["url"], c["quote"]) not in seen]  # fmt: skip
        sources = cited[:MAX_CITATIONS]
    else:
        sources = item["citations"]
    value, state = item["value"], item["state"]
    if leaves is not None:
        origin: str | None = "model"
    elif sources and all(s["url"].startswith("upload://") for s in sources):
        origin = "document"
    else:
        origin = "crawler" if value is not None else None
    out = {
        "key": item["field"], "label": item["label"], "item_key": item["item_key"],
        "value": value, "evidence_state": state, "confidence": item["confidence"],
        "extracted_at": _when([s.get("retrieved_at") for s in sources]),
        "sources": sources, "origin": origin, "access": access,
        "verified_by": "sources" if state == "CORROBORATED" else None,
    }  # fmt: skip
    for k in ("reasoning", "details", "evidence_value"):
        if k in item:
            out[k] = item[k]
    answer = answers[-1] if answers else None
    out["history"] = [{k: a.get(k) for k in ("status", "value", "actor", "verified_at")}
                      for a in answers]  # fmt: skip
    out["answer_status"] = answer["status"] if answer else None
    if answer and answer["status"] not in _VALUES:
        answer = None  # "I don't know" or skipped: no value from the user, nothing changes
    status = answer["status"] if answer else None
    if answer:
        out["crawler_value"] = value
        out["verified_by"] = "user" if status != "rejected" else None
        if status == "edited":
            out.update(value=answer["value"], origin="user", extracted_at=answer["verified_at"])
        elif status == "rejected":
            out["value"] = None
    out["status"] = field_status(state, out["value"] is not None, active, status, access)
    if status in ("confirmed", "edited"):
        out["confidence"] = 1.0  # the company said so
    elif out["value"] is None:
        out["confidence"] = None
    elif out["confidence"] is None:
        out["confidence"] = _cited_confidence(sources)
    return out


# What one cited page counts for when no assessment scored the value (a model's reading of
# the company's pages): pages combine as assessment combines sources (assess.py), kept
# under the Supported threshold, so an unassessed value never reads as established.
PAGE_CONFIDENCE = 0.3
_CEILING = AssessmentSettings().found_confidence - 0.01


def _cited_confidence(sources: list[dict[str, Any]]) -> float | None:
    pages = {src["url"] for src in sources}
    if not pages:
        return None
    return round(min(_CEILING, 1 - (1 - PAGE_CONFIDENCE) ** len(pages)), 2)


def assemble(
    organization_id: int,
    dossier: dict[str, Any],
    profile: dict[str, Any] | None,
    answers: list[dict[str, Any]],
    job: dict[str, Any] | None,
    identities: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The profile from its parts; pure, so it is tested without a database.
    ``identities``: the company's matched profiles on other platforms and its other
    domains (``kind`` profile or domain; ``identities`` below).

    ``profile``: ``profile.stored_profile``; ``answers``: every answer
    (``onboarding.verification(history=True)``, oldest first: the latest wins);
    ``job``: the latest crawl job's id and status."""
    active = job is not None and job["status"] in ACTIVE_JOB_STATUSES
    company, products = dossier["company"]["fields"], dossier["products"]
    model = (profile or {}).get("profile") or {}
    retrieved = {c["observation_id"]: c["retrieved_at"]
                 for fields in [company, *(p["fields"] for p in products)]
                 for v in fields.values() for e in _entries(v)
                 for c in e.get("citations", []) if "observation_id" in c}  # fmt: skip
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in answers:  # oldest first: the last one is the answer
        by_key.setdefault((a["field"], a["item_key"]), []).append(a)

    def entries(field: str) -> list[dict[str, Any]]:
        return [*_entries(company.get(field)),
                *(e for p in products for e in _entries(p["fields"].get(field)))]  # fmt: skip

    fields: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in SECTIONS}
    for f in REGISTRY:
        item = _item(f.key, f.label, [e for d in f.dossier for e in entries(d)], scalar=f.scalar)
        if f.key in _IDENTITY_FIELDS:
            _identity_item(item, f.key, identities, dossier["company"].get("subdomains"))
        leaves = _model_leaves(model, f.model)
        used = None
        if leaves:
            # The model's structured value wins; the evidence behind the field still
            # decides its state (corroborated, conflicting...), else it is inferred.
            found = {k: item[k] for k in ("value", "state", "confidence", "citations")}
            _inferred(item, leaves, scalar=f.scalar)
            used = leaves
            if found["value"] is not None:
                item.update(evidence_value=found["value"], state=found["state"],
                            evidence_citations=found["citations"],
                            confidence=found["confidence"] or item["confidence"])  # fmt: skip
        fields[f.section].append(
            _finish(item, f.access, active, by_key.get((f.key, ""), []), retrieved, used)
        )

    items = [_product_item(p) for p in products]
    catalogue: list[dict[str, Any]] = [{"key": "products_services", "items": items}]
    if model and offerings_of(model):
        answered = {k for f, k in by_key if f == "product"}
        catalogue[0]["items"] = _catalogue(items, model, answered)
    elif model:
        fill_from_profile(catalogue, model)
    if model:
        _offering_details(catalogue[0]["items"], {"offerings": offerings_of(model)})
    # What the user added in the catalogue question (questions.py): sold, though research
    # did not find it.
    catalogue[0]["items"] += [
        _item("product", "Product", [], key, scalar=True)
        for f, key in by_key
        if f == "product" and key.startswith("user:")
    ]
    offerings = [
        _finish(i, "public", active, by_key.get(("product", i["item_key"]), []), retrieved, None)
        for i in catalogue[0]["items"]
    ]

    missing = [{"key": f["key"], "label": f["label"], "section": s, "status": f["status"],
                "access": f["access"],
                "reason": ("you said you don't know yet" if f["answer_status"] == "dont_know"
                           else "you skipped this question" if f["answer_status"] == "skipped"
                           else "you rejected the value found"
                           if f.get("crawler_value") is not None and f["value"] is None
                           else "only you can tell us" if f["access"] == "private"
                           else "needs an authorized account connection"
                           if f["access"] == "integration"
                           else "upload a document" if f["access"] == "document"
                           else _REASONS.get(f["status"], "not found in public sources"))}
               for s, _ in SECTIONS for f in fields[s] if f["status"] in _OPEN]  # fmt: skip
    missing += [{"key": f["key"], "label": f["label"], "section": s, "status": f["status"],
                 "access": f["access"],
                 "reason": "hypothesis until validated against your customer data"}
                for s, _ in SECTIONS for f in fields[s]
                if f["key"] in _HYPOTHESES and f["status"] == "Inferred"]  # fmt: skip
    records = [{"key": f["key"] if f["key"] != "product" else f"product:{f['item_key']}",
                "section": s, "status": f["status"], "confidence": f["confidence"],
                "extracted_at": f["extracted_at"], "sources": f["sources"]}
               for s, fs in [*((s, fields[s]) for s, _ in SECTIONS),
                             ("products_services", offerings)]
               for f in fs if f["sources"]]  # fmt: skip
    sections: list[dict[str, Any]] = []
    for key, title in SECTIONS:
        if key == "products_services":
            sections.append({"key": key, "title": title, "items": offerings})
        elif key == "missing_information":
            sections.append({"key": key, "title": title, "items": missing})
        elif key == "source_records":
            sections.append({"key": key, "title": title, "items": records})
        else:
            sections.append({"key": key, "title": title, "fields": fields[key]})
    return {
        "company_id": organization_id,
        "domain": dossier["company"]["domain"],
        "crawl": {"crawl_id": job["crawl_id"], "status": job["status"].upper()} if job else None,
        "profile_status": (profile or {}).get("status"),
        "sections": sections,
    }


_IDENTITY_FIELDS = ("social_profiles", "other_domains")


def _identity_item(item: dict[str, Any], key: str, identities: Sequence[dict[str, Any]],
                   subdomains: dict[str, Any] | None) -> None:  # fmt: skip
    """Social profiles and other domains: what identity resolution matched to the
    company (``db/identity.py``), each cited by the decision's rationale."""
    kind = "profile" if key == "social_profiles" else "domain"
    rows = [i for i in identities if i["kind"] == kind]
    if kind == "profile":
        value: list[Any] = [{"platform": i["platform"], "url": i["url"]} for i in rows]
    else:
        value = sorted({i["url"] for i in rows} | set((subdomains or {}).get("value") or []))
    item.update(
        value=value or None, state="SUPPORTED" if value else "UNKNOWN",
        citations=[{"url": i["url"] if kind == "profile" else f"https://{i['url']}/",
                    "quote": i["rationale"], "source_type": "identity_match", "tier": None,
                    "retrieved_at": i["decided_at"]} for i in rows][:MAX_CITATIONS],
    )  # fmt: skip


def identities(engine: Engine, organization_id: int) -> list[dict[str, Any]]:
    """The company's matched profiles on other platforms, and the other domains of
    organizations matched to it."""
    link, decision = IdentityLink, IdentityDecision
    with engine.connect() as conn:
        own = conn.execute(select(Organization.registrable_domain)
                           .where(Organization.id == organization_id)).scalar()  # fmt: skip
        profiles = conn.execute(
            select(ExternalIdentity.source_type, ExternalIdentity.profile_url,
                   decision.rationale, decision.created_at)
            .join(link, link.external_identity_id == ExternalIdentity.id)
            .join(decision, decision.id == link.decision_id)
            .where(link.organization_id == organization_id, link.status == "match")
            .order_by(ExternalIdentity.id)
        ).all()  # fmt: skip
        domains = conn.execute(
            select(Organization.registrable_domain, decision.rationale, decision.created_at)
            .join(link, link.member_org_id == Organization.id)
            .join(decision, decision.id == link.decision_id)
            .where(link.organization_id == organization_id, link.status == "match",
                   Organization.registrable_domain.is_not(None),
                   Organization.registrable_domain != own)
        ).all()  # fmt: skip
    return [
        *({"kind": "profile", "platform": r.source_type, "url": r.profile_url,
           "rationale": r.rationale, "decided_at": r.created_at.isoformat()} for r in profiles),
        *({"kind": "domain", "platform": None, "url": r.registrable_domain,
           "rationale": r.rationale, "decided_at": r.created_at.isoformat()} for r in domains),
    ]  # fmt: skip


def build_business_profile(
    engine: Engine, settings: Settings, organization_id: int
) -> dict[str, Any]:
    """The company's profile now. Raises ValueError for an unknown company."""
    dossier = build_dossier(engine, organization_id)
    with engine.connect() as conn:
        job = conn.execute(
            select(CrawlJob.id, CrawlJob.status)
            .where(CrawlJob.organization_id == organization_id)
            .order_by(CrawlJob.id.desc())
            .limit(1)
        ).first()
    return assemble(
        organization_id,
        dossier,
        stored_profile(engine, settings, organization_id),
        verification(engine, organization_id, history=True),
        {"crawl_id": job.id, "status": job.status} if job else None,
        identities(engine, organization_id),
    )
