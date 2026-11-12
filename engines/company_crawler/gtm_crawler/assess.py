"""Field assessment: from observations to one answer per field (approved 2026-09-29).

Method ``fa-v3``: ``fa-v2`` (production spec section 19: the approved
``fa-v1`` rules with the spec's field states (mapping approved 2026-09-29: VERIFIED ->
CORROBORATED, FOUND -> SUPPORTED, UNVERIFIED -> UNSUPPORTED), STALE, stored
confidence components and evidence roles), with a legal name's legal form
read as one word whatever its spelling ("Pvt Ltd" = "Private Limited").
Earlier rows keep their fa-v1 states as history.

1. Observations from the same site (registrable domain of the page) are one
   source; a repeated statement is not corroboration.
2. Equal normalized values agree. A bound and a value inside it are
   compatible ("more than 500" and "620" -> 620); anything else conflicts.
   Numbers are compared as intervals (``approx`` is +/-10%) in one currency;
   places and names by word containment ("Austin" vs "Austin, Texas"), a
   legal form spelt any way ("Acme Pvt Ltd" = "ACME Private Limited"); ownership by a
   fixed list of incompatible pairs (public vs private, ...).
3. For figures that change over time, a newer dated statement supersedes an
   older one (the older is history, not cited); only disagreements within the
   latest period, or involving undated statements, are conflicts.
4. Confidence = 1 - prod(1 - best observation confidence per source), <= 0.99.
5. No observations -> UNKNOWN. Unresolved disagreement -> CONFLICTING (no
   value; every observation recorded as contradicting). >= 2 independent
   sources, one of tier 1-2, confidence >= 0.80 -> CORROBORATED. Confidence
   >= 0.60 -> SUPPORTED. Otherwise UNSUPPORTED: the claim is kept with its
   citations, labelled a claim, never presented as a fact.
6. High-risk fields also need a source other than the company itself to be
   CORROBORATED, and a claim only the company makes is at most UNSUPPORTED.
7. A SUPPORTED or CORROBORATED figure that changes over time is STALE when
   its newest evidence (the year it states, else publication, else retrieval
   date) is older than ``stale_after_days`` (730); the value stays, dated.
8. NOT_APPLICABLE is never set automatically.

Each assessment stores its confidence components (per-source best
confidence, tiers, independence, age) and, in ``corroboration``, the role of
every observation it considered: ``supports``, ``qualifies`` (a bound
compatible with the value), ``contradicts`` or ``superseded`` (an older
dated figure).

List fields (investors, competitors, features, ...) are assessed per item.
Assessments are appended with ``method_version``; the latest per field is
current, earlier ones remain as history.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session, aliased

from .config import AssessmentSettings
from .db.models import (
    Corroboration,
    Evidence,
    Fetch,
    FieldAssessment,
    Observation,
    Page,
    Product,
    Url,
    is_current,
)
from .metrics import FIELD_CONFLICT, FIELD_STATES, FIELD_UNKNOWN
from .urls import registrable_domain, url_host

METHOD_VERSION = "fa-v3"
# States that state a fact (the dossier presents their value as one).
FACT_STATES = frozenset({"SUPPORTED", "CORROBORATED", "STALE"})
DEFAULT_THRESHOLDS = AssessmentSettings()


def method_version(thresholds: AssessmentSettings = DEFAULT_THRESHOLDS) -> str:
    """``fa-v3``, or ``fa-v3`` plus the thresholds when they differ from the approved ones."""
    if thresholds == DEFAULT_THRESHOLDS:
        return METHOD_VERSION
    return (f"{METHOD_VERSION}(verified={thresholds.verified_confidence},"
            f"found={thresholds.found_confidence},"
            f"stale_after_days={thresholds.stale_after_days})")  # fmt: skip


MAX_CONFIDENCE = 0.99
APPROX = 0.10

HIGH_RISK = frozenset(
    {"arr", "mrr", "annual_revenue", "funding_round", "total_funding", "valuation",
     "customer_count", "customer_concentration", "employee_count", "company_size", "investor",
     "ownership_type", "product_sales"}
)  # fmt: skip
NUMERIC = frozenset(
    {"employee_count", "company_size", "customer_count", "arr", "mrr", "annual_revenue",
     "total_funding", "valuation", "founded_year", "product_sales", "open_roles"}
)  # fmt: skip
TIME_VARYING = NUMERIC - {"founded_year"}
TEXT_SCALAR = frozenset({"legal_name", "website", "headquarters"})
SCALAR = NUMERIC | TEXT_SCALAR | {"ownership_type"}

# Fields reported for every company / product, UNKNOWN when nothing was observed.
COMPANY_FIELDS = (
    "company_name", "legal_name", "website", "headquarters", "company_location",
    "company_country", "founded_year", "employee_count", "company_size", "ownership_type",
    "business_model", "market_domain", "company_description", "number_of_products",
    "recent_update", "arr", "mrr", "annual_revenue", "customer_count",
    "customer_concentration", "funding_round", "total_funding", "valuation", "investor",
    "technology_partnership", "strategic_partnership", "partnership", "competitor",
    # Production spec section 17 (extract/signals.py).
    "customer", "case_study", "integration", "job_opening", "open_roles", "office_location",
    "website_technology",
    # Business Intelligence Profile (business_profile.py).
    "contact_email", "contact_phone", "contact_page", "founder",
)  # fmt: skip
PRODUCT_FIELDS = (
    "product_description", "product_category", "product_positioning", "key_feature",
    "main_use_case", "price", "plan_name", "product_sales",
)  # fmt: skip

_OWNERSHIP_CONFLICTS = frozenset(
    frozenset(p) for p in (("public", "private"), ("public", "venture_backed"),
                           ("public", "employee_owned"), ("public", "nonprofit"),
                           ("venture_backed", "nonprofit"))
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")
# A legal form's abbreviations, as words of a legal name ("Pvt Ltd" = "Private Limited").
_LEGAL_FORM = {"pvt": "private", "ltd": "limited", "inc": "incorporated",
               "corp": "corporation", "co": "company"}  # fmt: skip


@dataclass(frozen=True)
class Obs:
    id: int
    field: str
    product_id: int | None
    value_verbatim: str
    normalized: dict[str, Any] | None
    qualifier: str | None
    confidence: float
    tier: int
    first_party: bool
    source: str  # registrable domain of the page it was read from
    period: int | None  # year the figure refers to, if dated
    # When the evidence dates from: the stated year's end, else publication,
    # else retrieval (staleness).
    evidence_date: date | None = None


@dataclass
class Assessment:
    field: str
    product_id: int | None
    state: str
    value: dict[str, Any] | None = None
    confidence: float | None = None
    observation_ids: list[int] = dc_field(default_factory=list)
    components: dict[str, Any] | None = None
    # (observation id, source, role) for every observation considered.
    roles: list[tuple[int, str, str]] = dc_field(default_factory=list)


# --- the method --------------------------------------------------------------------


def assess_group(
    field_name: str,
    product_id: int | None,
    obs: list[Obs],
    thresholds: AssessmentSettings = DEFAULT_THRESHOLDS,
    today: date | None = None,
) -> list[Assessment]:
    """Assess one field for one subject: one row for a scalar, one per item for a list."""
    if not obs:
        return [Assessment(field_name, product_id, "UNKNOWN")]
    today = today or datetime.now(UTC).date()
    if field_name not in SCALAR:
        items: dict[str, list[Obs]] = defaultdict(list)
        for o in obs:
            items[_item_key(o)].append(o)
        return [_supported(field_name, product_id, group, _item_value(group), thresholds, today)
                for _, group in sorted(items.items())]  # fmt: skip

    current, period = obs, None
    if not _compatible(field_name, obs) and field_name in TIME_VARYING:
        dated = [o.period for o in obs if o.period is not None]
        if dated:
            period = max(dated)
            current = [o for o in obs if o.period in (period, None)]
    elif field_name in TIME_VARYING:
        dated = [o.period for o in obs if o.period is not None]
        period = max(dated) if dated else None
    superseded = [(o.id, o.source, "superseded") for o in obs if o not in current]
    if not _compatible(field_name, current):
        conflict = Assessment(
            field_name, product_id, "CONFLICTING",
            observation_ids=sorted(o.id for o in current),
            components={"sources": _source_components(current)},
            roles=[(o.id, o.source, "contradicts") for o in current] + superseded,
        )  # fmt: skip
        return [conflict]
    value = _combined_value(field_name, current)
    if period is not None:
        value["as_of_year"] = period
    assessment = _supported(field_name, product_id, current, value, thresholds, today)
    assessment.roles += superseded
    return [assessment]


def _source_components(obs: list[Obs]) -> list[dict[str, Any]]:
    by_source: dict[str, list[Obs]] = defaultdict(list)
    for o in obs:
        by_source[o.source].append(o)
    return [
        {"source": source, "tier": min(o.tier for o in group),
         "first_party": all(o.first_party for o in group),
         "best_confidence": max(o.confidence for o in group), "observations": len(group)}
        for source, group in sorted(by_source.items())
    ]  # fmt: skip


def _supported(
    field_name: str,
    product_id: int | None,
    obs: list[Obs],
    value: dict[str, Any],
    thresholds: AssessmentSettings = DEFAULT_THRESHOLDS,
    today: date | None = None,
) -> Assessment:
    best: dict[str, float] = {}
    for o in obs:
        best[o.source] = max(best.get(o.source, 0.0), o.confidence)
    confidence = min(MAX_CONFIDENCE, 1 - math.prod(1 - c for c in best.values()))
    independent = len(best) >= 2
    strong_source = any(o.tier <= 2 for o in obs)
    beyond_company = any(not o.first_party for o in obs)
    high_risk = field_name in HIGH_RISK
    if (independent and strong_source and confidence >= thresholds.verified_confidence
            and (beyond_company or not high_risk)):  # fmt: skip
        state = "CORROBORATED"
    elif high_risk and not beyond_company:
        state = "UNSUPPORTED"  # only the company says so
    elif confidence >= thresholds.found_confidence:
        state = "SUPPORTED"
    else:
        state = "UNSUPPORTED"
    dates = [o.evidence_date for o in obs if o.evidence_date is not None]
    newest = max(dates) if dates else None
    age = (today - newest).days if newest is not None and today is not None else None
    if (state in ("CORROBORATED", "SUPPORTED") and field_name in TIME_VARYING
            and age is not None and age > thresholds.stale_after_days):  # fmt: skip
        state = "STALE"
    value["verbatim"] = sorted({o.value_verbatim for o in obs})
    value["sources"] = sorted(best)
    if newest is not None:
        value["evidence_date"] = newest.isoformat()
    components = {
        "method": "1 - prod(1 - best confidence per source)",
        "sources": _source_components(obs),
        "independent_sources": len(best), "strong_source": strong_source,
        "beyond_company": beyond_company, "high_risk": high_risk,
        "newest_evidence": newest.isoformat() if newest else None, "age_days": age,
        "thresholds": {"corroborated": thresholds.verified_confidence,
                       "supported": thresholds.found_confidence,
                       "stale_after_days": thresholds.stale_after_days},
    }  # fmt: skip
    roles = [(o.id, o.source, "qualifies" if _is_bound(o) else "supports") for o in obs]
    return Assessment(field_name, product_id, state, value, round(confidence, 2),
                      sorted(o.id for o in obs), components, roles)  # fmt: skip


def _is_bound(o: Obs) -> bool:
    q = (o.normalized or {}).get("qualifier")
    return isinstance(q, str) and q not in ("eq", "range")


def _item_key(o: Obs) -> str:
    n = o.normalized or {}
    value = n.get("value")
    if isinstance(value, str) and o.field in ("business_model", "market_domain"):
        return value
    return f"{_fold(o.qualifier or '')}|{_fold(o.value_verbatim)}"


def _item_value(group: list[Obs]) -> dict[str, Any]:
    best = max(group, key=lambda o: o.confidence)
    value: dict[str, Any] = {"value": best.value_verbatim}
    if best.normalized:
        value["normalized"] = best.normalized
    if best.qualifier:
        value["qualifier"] = best.qualifier
    return value


def _compatible(field_name: str, obs: list[Obs]) -> bool:
    if field_name in NUMERIC:
        intervals = [_interval(o) for o in obs]
        if all(i is not None for i in intervals):
            currencies = {i[2] for i in intervals if i is not None}
            lo = max(i[0] for i in intervals if i is not None)
            hi = min(i[1] for i in intervals if i is not None)
            return len(currencies) <= 1 and lo <= hi
    if field_name == "ownership_type":
        kinds = {_category(o) for o in obs}
        return not any(frozenset((a, b)) in _OWNERSHIP_CONFLICTS for a in kinds for b in kinds)
    keys = [_words(o) for o in obs]
    return all(a <= b or b <= a for a in keys for b in keys)


def _combined_value(field_name: str, obs: list[Obs]) -> dict[str, Any]:
    if field_name in NUMERIC and all(_interval(o) for o in obs):
        intervals = [i for o in obs if (i := _interval(o))]
        currency = intervals[0][2]
        exact = [o for o in obs if (o.normalized or {}).get("qualifier") == "eq"]
        if exact:
            value: dict[str, Any] = {"qualifier": "eq", "value": _number(exact[0])}
        else:
            lo = max(i[0] for i in intervals)
            hi = min(i[1] for i in intervals)
            if hi == math.inf:
                value = {"qualifier": "gte", "value": lo}
            elif lo == -math.inf:
                value = {"qualifier": "lte", "value": hi}
            else:
                value = {"qualifier": "range", "min": lo, "max": hi}
        if currency:
            value["currency"] = currency
        return value
    if field_name == "ownership_type":
        return {"value": sorted({_category(o) for o in obs})}
    longest = max(obs, key=lambda o: (len(_words(o)), o.confidence))
    return {"value": longest.value_verbatim}


def _number(o: Obs) -> float | None:
    n = o.normalized or {}
    v = n.get("value", n.get("amount"))
    # Counts stay integers; amounts stay as stated.
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


def _interval(o: Obs) -> tuple[float, float, str | None] | None:
    n = o.normalized
    if not n:
        return None
    currency = n.get("currency")
    q = n.get("qualifier")
    low, high = n.get("min"), n.get("max")
    if q == "range" and isinstance(low, int | float) and isinstance(high, int | float):
        return float(low), float(high), currency
    v = _number(o)
    if v is None:
        return None
    bounds = {"eq": (v, v), "gt": (v, math.inf), "gte": (v, math.inf),
              "lt": (-math.inf, v), "lte": (-math.inf, v),
              "approx": (v * (1 - APPROX), v * (1 + APPROX))}  # fmt: skip
    return (*bounds[q], currency) if q in bounds else None


def _category(o: Obs) -> str:
    v = (o.normalized or {}).get("value")
    return v if isinstance(v, str) else _fold(o.value_verbatim)


def _words(o: Obs) -> frozenset[str]:
    if o.field == "website":
        host = re.sub(r"^[a-z]+://", "", o.value_verbatim.strip().lower()).split("/")[0]
        return frozenset({registrable_domain(host)}) if host else frozenset()
    words = _WORD.findall(o.value_verbatim.lower())
    if o.field == "legal_name":
        words = [_LEGAL_FORM.get(w, w) for w in words]
    return frozenset(words)


def _fold(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


# --- database ------------------------------------------------------------------------


def assess_organization(
    engine: Engine, organization_id: int, thresholds: AssessmentSettings = DEFAULT_THRESHOLDS
) -> dict[str, Any]:
    """Append a fresh assessment of every field for the organization and its products."""
    obs = load_observations(engine, organization_id)
    with engine.connect() as conn:
        products = conn.execute(
            select(Product.id).where(Product.organization_id == organization_id)
        ).scalars().all()  # fmt: skip
    by_subject: dict[tuple[int | None, str], list[Obs]] = defaultdict(list)
    for o in obs:
        by_subject[(o.product_id, o.field)].append(o)

    subjects: set[tuple[int | None, str]] = {(None, f) for f in COMPANY_FIELDS}
    subjects |= {(p, f) for p in products for f in PRODUCT_FIELDS}
    subjects |= set(by_subject)
    subjects.discard((None, "number_of_products"))
    today = datetime.now(UTC).date()
    assessments = [
        a
        for pid, f in sorted(subjects, key=lambda k: (k[0] or 0, k[1]))
        for a in assess_group(f, pid, by_subject.get((pid, f), []), thresholds, today)
    ]
    assessments.append(_number_of_products(products, by_subject))

    rows = [
        dict(organization_id=organization_id, product_id=a.product_id, field=a.field,
             state=a.state, value=a.value,
             confidence=Decimal(str(a.confidence)) if a.confidence is not None else None,
             observation_ids=a.observation_ids, method_version=method_version(thresholds),
             confidence_components=a.components)
        for a in assessments
    ]  # fmt: skip
    with engine.begin() as conn:
        ids = conn.execute(
            insert(FieldAssessment).returning(FieldAssessment.id, sort_by_parameter_order=True),
            rows,
        ).scalars().all()  # fmt: skip
        roles = [
            {"assessment_id": aid, "observation_id": oid, "source": source, "role": role}
            for aid, a in zip(ids, assessments, strict=True)
            for oid, source, role in a.roles
        ]
        if roles:
            conn.execute(insert(Corroboration), roles)
    states: dict[str, int] = defaultdict(int)
    for a in assessments:
        states[a.state] += 1
    FIELD_UNKNOWN.inc(states["UNKNOWN"])
    FIELD_CONFLICT.inc(states["CONFLICTING"])
    for state, count in states.items():
        FIELD_STATES.labels(state).inc(count)
    return {"method": method_version(thresholds), "assessments": len(rows),
            "states": dict(states)}  # fmt: skip


def field_gaps(engine: Engine, organization_id: int) -> set[str]:
    """Company fields the latest assessment left open: no SUPPORTED or
    CORROBORATED item.

    UNSUPPORTED counts as open (a claim only the company makes, or a weak one,
    which outside sources may confirm), and so does STALE (newer evidence may
    exist).
    """
    with engine.connect() as conn:
        latest = select(func.max(FieldAssessment.computed_at)).where(
            FieldAssessment.organization_id == organization_id
        ).scalar_subquery()  # fmt: skip
        rows = conn.execute(
            select(FieldAssessment.field, FieldAssessment.state).where(
                FieldAssessment.organization_id == organization_id,
                FieldAssessment.product_id.is_(None),
                FieldAssessment.computed_at == latest,
            )
        ).all()
    fields = {f for f, _ in rows}
    return fields - {f for f, state in rows if state in ("SUPPORTED", "CORROBORATED")}


_RANK = {"CORROBORATED": 0, "SUPPORTED": 1, "STALE": 2, "UNSUPPORTED": 3}


def field_values(
    engine: Engine, organization_id: int, fields: Iterable[str]
) -> dict[str, list[str]]:
    """Values the latest assessment holds for ``fields`` (company or any product),
    strongest first: CORROBORATED, SUPPORTED, STALE, UNSUPPORTED, then confidence."""
    with engine.connect() as conn:
        latest = select(func.max(FieldAssessment.computed_at)).where(
            FieldAssessment.organization_id == organization_id
        ).scalar_subquery()  # fmt: skip
        rows = conn.execute(
            select(FieldAssessment.field, FieldAssessment.state, FieldAssessment.value,
                   FieldAssessment.confidence)
            .where(FieldAssessment.organization_id == organization_id,
                   FieldAssessment.computed_at == latest,
                   FieldAssessment.field.in_(list(fields)),
                   FieldAssessment.state.in_(list(_RANK)))
        ).all()  # fmt: skip
    out: dict[str, list[str]] = {}
    for field, _, value, _ in sorted(rows, key=lambda r: (_RANK[r.state], -(r.confidence or 0))):
        verbatim = (value or {}).get("verbatim") or []
        if verbatim and verbatim[0] not in out.setdefault(field, []):
            out[field].append(verbatim[0])
    return out


def _number_of_products(
    products: Iterable[int], by_subject: dict[tuple[int | None, str], list[Obs]]
) -> Assessment:
    # Each counted product must itself be named by an observation.
    named = {p: obs for p in products if (obs := by_subject.get((p, "product_name")))}
    if not named:
        return Assessment("number_of_products", None, "UNKNOWN")
    cited = [min(obs, key=lambda o: o.id) for obs in named.values()]
    assessment = _supported("number_of_products", None, cited, {"value": len(named)})
    return assessment


def load_observations(engine: Engine, organization_id: int) -> list[Obs]:
    final = aliased(Url)
    with Session(engine) as session:
        rows = session.execute(
            select(Observation, func.coalesce(final.url, Url.url).label("page_url"))
            .join(Evidence, Evidence.id == Observation.evidence_id)
            .join(Page, Page.id == Evidence.page_id)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .join(Url, Url.id == Fetch.url_id)
            .outerjoin(final, final.id == Fetch.final_url_id)
            .where(Observation.organization_id == organization_id, is_current())
            .order_by(Observation.id)
        ).all()
        return [_obs(row.Observation, row.page_url) for row in rows]


def _obs(o: Any, page_url: str) -> Obs:
    n = o.value_normalized
    stated = n.get("year") if isinstance(n, dict) else None
    year = stated if isinstance(stated, int) else None
    if year is None:
        year = o.published_at.year if o.published_at else None
    if isinstance(stated, int) and 1900 <= stated <= 9999:
        evidence_date: date | None = date(stated, 12, 31)
    elif o.published_at is not None:
        evidence_date = o.published_at.date()
    else:
        evidence_date = o.retrieved_at.date() if o.retrieved_at else None
    return Obs(
        id=o.id, field=o.field, product_id=o.product_id, value_verbatim=o.value_verbatim,
        normalized=n, qualifier=o.qualifier, confidence=float(o.confidence),
        tier=o.source_tier, first_party=o.is_first_party,
        source=registrable_domain(url_host(page_url)), period=year,
        evidence_date=evidence_date,
    )  # fmt: skip
