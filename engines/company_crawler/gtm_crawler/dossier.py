"""The GTM dossier: current field assessments with their citations (spec section 20).

For the company, each of its products and each competitor, every field is
reported with its state, value (absent unless the state allows one),
confidence and citations: the URL, exact quote, source tier and type,
retrieval and publication time of each observation behind it.

Website domain and subdomains are not page statements; they come from the
crawl's own records (hosts on the company's domain that answered a fetch),
and are reported with that basis.

``sections`` groups the field names under the spec's dossier headings (each
name is in the company's or a product's ``fields``); ``summary`` counts the
states; ``freshness`` dates the evidence; ``versions`` are those recorded by
the company's latest crawl; ``cost`` totals the job that ran that crawl
(its crawls, competitors included, and database time; see ``cost.py``).
Full lineage of any field: ``field_lineage`` (``GET /v1/companies/{id}/lineage/{field}``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, aliased

from .assess import COMPANY_FIELDS, METHOD_VERSION, PRODUCT_FIELDS, SCALAR
from .cost import cost_summary
from .db.identity import canonical_ids
from .db.models import (
    CostEvent,
    Crawl,
    Evidence,
    Fetch,
    FieldAssessment,
    Observation,
    Organization,
    Page,
    Product,
    Relationship,
    Url,
    is_current,
)
from .tracing import span
from .urls import url_host
from .versions import DOSSIER_SCHEMA_VERSION

SCALAR_FIELDS = SCALAR | {"number_of_products"}
# The competitor-analysis view of GTM Parameters V1.0.
COMPETITOR_FIELDS = (
    "company_name", "website", "founded_year", "funding_round", "total_funding",
    "company_size", "employee_count", "headquarters", "company_location", "company_country",
    "business_model", "investor", "ownership_type", "annual_revenue", "key_feature",
    "market_domain",
)  # fmt: skip
# Spec section 20 headings -> the fields reported under them. Every company
# and product field (and product_name, stated per product) is in exactly one.
SECTIONS: dict[str, tuple[str, ...]] = {
    "identity": ("company_name", "legal_name", "website", "founded_year", "founder"),
    "contact": ("contact_email", "contact_phone", "contact_page"),
    "description_positioning": ("company_description", "product_description",
                                "product_positioning"),
    "products_categories": ("number_of_products", "product_name", "product_category"),
    "features": ("key_feature",),
    "pricing": ("price", "plan_name"),
    "target_customers": ("business_model", "customer_concentration"),
    "industries_use_cases": ("market_domain", "main_use_case"),
    "customer_references": ("customer", "case_study"),
    "traction": ("customer_count", "arr", "mrr", "annual_revenue", "employee_count",
                 "company_size", "product_sales"),
    "funding": ("funding_round", "total_funding", "valuation", "investor", "ownership_type"),
    "partnerships_integrations": ("technology_partnership", "strategic_partnership",
                                  "partnership", "integration"),
    "competitors": ("competitor",),
    "press": ("recent_update",),
    "hiring": ("job_opening", "open_roles"),
    "geography": ("headquarters", "company_location", "company_country", "office_location"),
    "technology": ("website_technology",),
}  # fmt: skip
DOSSIER_FIELDS = frozenset(f for fs in SECTIONS.values() for f in fs)
assert sorted(DOSSIER_FIELDS) == sorted({*COMPANY_FIELDS, *PRODUCT_FIELDS, "product_name"})
assert sum(map(len, SECTIONS.values())) == len(DOSSIER_FIELDS)


def build_dossier(engine: Engine, organization_id: int) -> dict[str, Any]:
    with span("dossier.build", stage="dossier", organization_id=organization_id):
        return _build_dossier(engine, organization_id)


def _build_dossier(engine: Engine, organization_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        org = session.get(Organization, organization_id)
        if org is None:
            raise ValueError(f"no organization {organization_id}")
        products = session.execute(
            select(Product).where(Product.organization_id == organization_id).order_by(Product.id)
        ).scalars().all()  # fmt: skip
        competitors = _named(session, organization_id, "competitor")
        explicit = {c.id for c, _ in competitors}
        # Co-listed only (extract/competitor.py): never assessed or crawled as competitors.
        candidates = [
            {"name": c.name, "named_organization_id": c.id, "evidence_basis": sorted(bases)}
            for c, bases in _named(session, organization_id, "candidate_competitor")
            if c.id not in explicit
        ]  # fmt: skip
        fields = _fields(session, organization_id)
        method = session.execute(
            select(FieldAssessment.method_version)
            .where(FieldAssessment.organization_id == organization_id)
            .order_by(FieldAssessment.computed_at.desc())
            .limit(1)
        ).scalar()
        company = {
            "organization_id": org.id,
            "domain": org.registrable_domain,
            "subdomains": _subdomains(session, organization_id, org.registrable_domain),
            "fields": fields.get(None, {}),
        }
        rivals: list[dict[str, Any]] = []
        last_crawl = session.execute(
            select(Crawl.versions, Crawl.finished_at, Crawl.job_id)
            # A web crawl: the documents crawl (documents.py) has no job, versions or cost.
            .where(Crawl.organization_id == organization_id, Crawl.job_id.is_not(None))
            .order_by(Crawl.id.desc())
            .limit(1)
        ).first()  # fmt: skip
        entries = [(f, e) for fs in [fields.get(k, {}) for k in (None, *(p.id for p in products))]
                   for f, v in fs.items() for e in (v if isinstance(v, list) else [v])]  # fmt: skip
        retrieved = sorted({c["retrieved_at"] for _, e in entries for c in e["citations"]},
                           key=datetime.fromisoformat)  # fmt: skip
        out = {
            "schema_version": DOSSIER_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "method": method or METHOD_VERSION,
            "versions": last_crawl.versions if last_crawl else None,
            "summary": {"states": dict(Counter(e["state"] for _, e in entries))},
            "freshness": {
                "last_crawl_finished_at": (last_crawl.finished_at.isoformat()
                                           if last_crawl and last_crawl.finished_at else None),
                "oldest_evidence_retrieved_at": retrieved[0] if retrieved else None,
                "newest_evidence_retrieved_at": retrieved[-1] if retrieved else None,
                "stale_fields": sorted({f for f, e in entries if e["state"] == "STALE"}),
            },
            "cost": _cost(session, organization_id, last_crawl.job_id if last_crawl else None),
            "sections": {name: list(names) for name, names in SECTIONS.items()},
            "company": company,
            "products": [
                {"product_id": p.id, "name": p.name, "slug": p.slug,
                 "identified_by": p.identification_basis, "fields": fields.get(p.id, {})}
                for p in products
            ],
            "competitors": rivals,
            "candidate_competitors": candidates,
        }  # fmt: skip
        # A named competitor's facts are those of the company it was resolved to.
        linked = canonical_ids(session.connection(), [c.id for c, _ in competitors])
        for competitor, bases in competitors:
            link = linked.get(competitor.id)
            company_id = link.organization_id if link else competitor.id
            company_row = session.get(Organization, company_id)
            own = _fields(session, company_id).get(None, {})
            rivals.append({
                "organization_id": company_id, "name": competitor.name,
                "domain": company_row.registrable_domain if company_row else None,
                "evidence_basis": sorted(bases),
                "identity": ({"status": "match", "decision_id": link.decision_id,
                              "named_organization_id": competitor.id} if link else None),
                "assessed": bool(own),
                "fields": {f: own[f] for f in COMPETITOR_FIELDS if f in own},
            })  # fmt: skip
        return out


def _named(session: Session, organization_id: int, kind: str) -> list[Any]:
    """Organizations ``organization_id`` names with relationship ``kind``, with their bases."""
    return list(session.execute(
        select(Organization, func.array_agg(func.distinct(Relationship.basis)))
        .join(Relationship, Relationship.object_org_id == Organization.id)
        .join(Observation, Observation.id == Relationship.observation_id)
        .where(Relationship.subject_org_id == organization_id,
               Relationship.type == kind, is_current())
        .group_by(Organization.id)
        .order_by(Organization.name)
    ).all())  # fmt: skip


def _values(entries: Any) -> list[Any]:
    """The facts among a field's entries (a claim or a conflict has no ``value``), once each."""
    if isinstance(entries, dict):
        entries = [entries]
    seen = {str(e["value"]).casefold(): e["value"] for e in reversed(entries or [])
            if "value" in e}  # fmt: skip
    return list(reversed(seen.values()))


def _get(fields: dict[str, Any], field: str, one: bool | None = None) -> Any:
    values = _values(fields.get(field))
    if field in SCALAR_FIELDS if one is None else one:
        return values[0] if values else None
    return values


def _clean_product(name: str | None, fields: dict[str, Any]) -> dict[str, Any]:
    prices = [e for e in fields.get("price", []) if "value" in e]
    return {
        "name": name,
        "description": _get(fields, "product_description"),
        "pricing_and_plans": {
            "plans": _get(fields, "plan_name"),
            "prices": _values([{"value": {"plan": e.get("details", {}).get("qualifier"),
                                          "price": e["value"]}} for e in prices]),
        },
        "key_features": _get(fields, "key_feature"),
        "main_use_case": _get(fields, "main_use_case"),
        "product_positioning": _get(fields, "product_positioning"),
        "product_evidence": sorted({c["url"] for f, es in fields.items() if f in PRODUCT_FIELDS
                                    for e in (es if isinstance(es, list) else [es])
                                    if "value" in e for c in e["citations"]}),
        "product_category": _get(fields, "product_category"),
        "annual_sales": _get(fields, "product_sales"),
    }  # fmt: skip


def clean_dossier(dossier: dict[str, Any]) -> dict[str, Any]:
    """The dossier as GTM Parameters V1.0 lists it: those headings, plain values.

    Facts only: a field that is unknown, an unsupported claim or in conflict is
    ``None`` (or an empty list). No states, confidence, citations or run metadata.
    """
    company = dossier["company"]
    fields = company.get("fields", {})
    products = [_clean_product(p["name"], p["fields"]) for p in dossier["products"]]
    # What the site states without naming a product belongs to the only product.
    loose = {f: fields[f] for f in PRODUCT_FIELDS if f in fields}
    if len(products) == 1:
        only = dossier["products"][0]
        merged = {f: [*_entries(only["fields"].get(f)), *_entries(loose.get(f))]
                  for f in PRODUCT_FIELDS}  # fmt: skip
        products = [_clean_product(only["name"], merged)]
    elif any(_values(v) for v in loose.values()):
        products.append(_clean_product(None, loose))
    return {
        "name_of_company": _get(fields, "company_name", one=True),
        "company_information": {
            "headquarters": _get(fields, "headquarters"),
            "website_domain": company.get("domain"),
            "company_size": _get(fields, "company_size"),
            "employee_count": _get(fields, "employee_count"),
            "ownership_type": _get(fields, "ownership_type"),
            "partnership": _get(fields, "partnership"),
            "sub_domains": company.get("subdomains", {}).get("value", []),
            "business_model": _get(fields, "business_model"),
            "market_domain": _get(fields, "market_domain"),
            "company_location": _get(fields, "company_location"),
        },
        "product_information": {
            "number_of_products": _get(fields, "number_of_products"),
            "recent_updates": _get(fields, "recent_update"),
            "products": [{k: v for k, v in p.items() if k != "annual_sales"} for p in products],
        },
        "traction": {
            "arr": _get(fields, "arr"),
            "mrr": _get(fields, "mrr"),
            "annual_revenue": _get(fields, "annual_revenue"),
            "customer_concentration": _get(fields, "customer_concentration"),
            "technology_partnerships": _get(fields, "technology_partnership"),
            "strategic_partnerships": _get(fields, "strategic_partnership"),
            "annual_sales_per_product": {p["name"]: p["annual_sales"] for p in products
                                         if p["name"] and p["annual_sales"] is not None},
            "total_funding": _get(fields, "total_funding"),
            "valuation": _get(fields, "valuation"),
            "investors": _get(fields, "investor"),
        },
        "competitor_analysis": [
            {
                "name": c["name"],
                "founded_year": _get(c["fields"], "founded_year"),
                "funding_stage": _get(c["fields"], "funding_round"),
                "total_raised": _get(c["fields"], "total_funding"),
                "company_size": _get(c["fields"], "company_size"),
                "hq_location": _get(c["fields"], "headquarters"),
                "business_model": _get(c["fields"], "business_model"),
                "key_markets": _get(c["fields"], "company_country"),
                "key_investors": _get(c["fields"], "investor"),
                "ownership": _get(c["fields"], "ownership_type"),
                # ponytail: competitors are assessed at company level only, so the
                # category is their market domain; crawl their products if that is too coarse.
                "product_category": _get(c["fields"], "market_domain"),
                "revenue": _get(c["fields"], "annual_revenue"),
                "core_features": _get(c["fields"], "key_feature"),
            }
            for c in dossier["competitors"]
        ],
    }  # fmt: skip


def _entries(entries: Any) -> list[dict[str, Any]]:
    return [entries] if isinstance(entries, dict) else list(entries or [])


def _cost(session: Session, organization_id: int, job_id: int | None) -> dict[str, Any] | None:
    if job_id is None:
        return None
    where = (CostEvent.job_id == job_id) & (CostEvent.organization_id == organization_id)
    return {"job_id": job_id, **cost_summary(session.connection(), where)}


# Assessed but not supported by the evidence (fa-v2 UNSUPPORTED, fa-v1 UNVERIFIED).
_CLAIM_STATES = frozenset({"UNSUPPORTED", "UNVERIFIED"})


def _claim_note(components: dict[str, Any] | None) -> str:
    """Why an assessed value is only a claim (fa-v1 rows record no reason)."""
    c = components or {}
    if c.get("high_risk") and not c.get("beyond_company", True):
        return "claim, not fact: only the company states it; no independent source confirms it"
    if "high_risk" in c:
        return "claim, not fact: the evidence is too weak to support it"
    return "claim, not fact: the evidence does not support it"


def _fields(session: Session, organization_id: int) -> dict[int | None, dict[str, Any]]:
    """Latest assessment run's rows, grouped by product and field (lists as item lists)."""
    latest = session.execute(
        select(func.max(FieldAssessment.computed_at)).where(
            FieldAssessment.organization_id == organization_id,
        )
    ).scalar()
    if latest is None:
        return {}
    rows = session.execute(
        select(FieldAssessment)
        .where(FieldAssessment.organization_id == organization_id,
               FieldAssessment.computed_at == latest)
        .order_by(FieldAssessment.id)
    ).scalars().all()  # fmt: skip
    citations = _citations(session, {i for r in rows for i in r.observation_ids})
    grouped: dict[int | None, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in rows:
        entry: dict[str, Any] = {"state": r.state}
        if r.value is not None:
            # An UNSUPPORTED value is a claim, never a fact: it is not under "value".
            key = "claimed_value" if r.state in _CLAIM_STATES else "value"
            entry[key] = r.value.get("value")
            entry["details"] = {k: v for k, v in r.value.items() if k != "value"}
            if key == "claimed_value":
                entry["note"] = _claim_note(r.confidence_components)
        if r.confidence is not None:
            entry["confidence"] = float(r.confidence)
        entry["citations"] = [citations[i] for i in r.observation_ids if i in citations]
        grouped[r.product_id][r.field].append(entry)
    # A scalar field has exactly one row; a list field one row per item (or one UNKNOWN).
    return {
        product: {f: entries[0] if f in SCALAR_FIELDS else entries
                  for f, entries in by_field.items()}
        for product, by_field in grouped.items()
    }  # fmt: skip


def _citations(session: Session, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    final = aliased(Url)
    rows = session.execute(
        select(
            Observation,
            Evidence,
            Page.parser_version,
            Fetch.id.label("fetch_id"),
            Fetch.raw_sha256,
            func.coalesce(final.url, Url.url).label("url"),
        )
        .join(Evidence, Evidence.id == Observation.evidence_id)
        .join(Page, Page.id == Evidence.page_id)
        .join(Fetch, Fetch.id == Page.fetch_id)
        .join(Url, Url.id == Fetch.url_id)
        .outerjoin(final, final.id == Fetch.final_url_id)
        .where(Observation.id.in_(ids))
    ).all()
    return {
        row.Observation.id: {
            "observation_id": row.Observation.id, "url": row.url,
            "quote": row.Evidence.quote, "value_verbatim": row.Observation.value_verbatim,
            # Where the quote is, and what produced it (spec section 15).
            "evidence_id": row.Evidence.id, "page_id": row.Evidence.page_id,
            "locator": {"type": row.Evidence.locator_type, "value": row.Evidence.locator,
                        "char_start": row.Evidence.char_start,
                        "char_end": row.Evidence.char_end},
            "fetch_id": row.fetch_id, "artifact_sha256": row.raw_sha256,
            "parser_version": row.parser_version,
            "extractor": f"{row.Observation.extractor}@{row.Observation.extractor_version}",
            "source_tier": row.Observation.source_tier,
            "source_type": row.Observation.source_type,
            "first_party": row.Observation.is_first_party,
            "retrieved_at": row.Observation.retrieved_at.isoformat(),
            "published_at": (row.Observation.published_at.isoformat()
                             if row.Observation.published_at else None),
        }
        for row in rows
    }  # fmt: skip


def _subdomains(session: Session, organization_id: int, domain: str | None) -> dict[str, Any]:
    if domain is None:
        return {"state": "UNKNOWN"}
    urls = session.execute(
        select(Url.url)
        .join(Fetch, Fetch.url_id == Url.id)
        .join(Crawl, Crawl.id == Fetch.crawl_id)
        .where(Crawl.organization_id == organization_id, Url.registrable_domain == domain,
               Fetch.status_code.between(200, 299))
    ).scalars().all()  # fmt: skip
    hosts = sorted({url_host(u) for u in urls} - {domain, f"www.{domain}"})
    return {"state": "SUPPORTED" if hosts else "UNKNOWN", "value": hosts,
            "basis": "hosts on the company's domain that answered a fetch"}  # fmt: skip
