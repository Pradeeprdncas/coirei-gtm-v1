"""Opportunity scoring: configurable, explainable, honest about what is unknown.

Each dimension is rated 1-5 for *favourability* (5 = most favourable to
entering: strong demand, high growth, low competitive intensity, low
regulatory barriers...). A rating is either given by the user (basis
``user``) or derived from collected evidence by the rules in ``_derive``
(basis ``evidence``); otherwise the dimension is ``unknown`` and excluded.

* raw score = sum(weight x rating) / sum(weight of rated dimensions)
* coverage = rated weight / total weight
* score = max(1, raw score - (1 - coverage) x MISSING_PENALTY), one decimal

Scores are a structured judgement, not a measurement; compare them only
between candidates scored with the same ``config_id``. Derived ratings from
evidence *volume* (demand, pain, competition) are proxies and say so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from statistics import median

from .assessment import independent_domains
from .extract import subject_terms
from .models import (
    Dimension,
    DimensionScore,
    EvidenceRecord,
    MetricKind,
    OpportunityScore,
    ScoringReport,
    ScoringRequest,
    SourceRecord,
)

METHOD_VERSION = "score-1"
MISSING_PENALTY = 1.0
SENSITIVITY_STEP = 0.25
DEFAULT_WEIGHTS: dict[str, float] = {
    "demand_strength": 0.15,
    "market_growth": 0.12,
    "pain_severity": 0.10,
    "willingness_to_pay": 0.10,
    "competitive_intensity": 0.10,
    "segment_accessibility": 0.10,
    "distribution_feasibility": 0.08,
    "regulatory_barriers": 0.08,
    "execution_complexity": 0.07,
    "potential_margins": 0.05,
    "strategic_fit": 0.05,
}
MEANING = {
    "demand_strength": "evidence of demand (drivers, adoption); 5 = strong and broad",
    "market_growth": "market CAGR; 5 = above 20% a year",
    "pain_severity": "how acute the customer problem is; 5 = severe and widely reported",
    "willingness_to_pay": "evidence buyers pay for solutions; 5 = clear prices and budgets",
    "competitive_intensity": "5 = few competitors (favourable), 1 = crowded",
    "segment_accessibility": "5 = buyers easy to identify and reach",
    "distribution_feasibility": "5 = proven, affordable channels exist",
    "regulatory_barriers": "5 = little regulation in the way, 1 = heavy",
    "execution_complexity": "5 = simple to build and operate, 1 = capital- or expertise-heavy",
    "potential_margins": "5 = high gross margins likely",
    "strategic_fit": "5 = fits the requester's strengths (user input only)",
}


def _volume(domains: int) -> float | None:
    return None if domains == 0 else {1: 2.0, 2: 3.0, 3: 4.0, 4: 4.0}.get(domains, 5.0)


def _inverse_volume(domains: int) -> float | None:
    return None if domains == 0 else {1: 4.0, 2: 3.0, 3: 3.0}.get(domains, 2.0)


def _growth_rating(cagr: float) -> float:
    for limit, rating in ((3, 1.0), (7, 2.0), (12, 3.0), (20, 4.0)):
        if cagr < limit:
            return rating
    return 5.0


def _derive(
    evidence: list[EvidenceRecord], sources: dict[str, SourceRecord]
) -> dict[str, tuple[float, str, list[str]]]:
    """Ratings the evidence supports: dimension -> (rating, rationale, evidence ids)."""
    out: dict[str, tuple[float, str, list[str]]] = {}

    def about(*dims: Dimension) -> list[EvidenceRecord]:
        return [e for e in evidence if set(e.dimensions) & set(dims)]

    # Not parts of a larger figure, other markets or company revenue (``scope.py``).
    rates = [(m.value, e.evidence_id) for e in evidence for m in e.metrics
             if m.kind is MetricKind.GROWTH_RATE and not m.subset and not m.off_subject
             and m.measure != "revenue"]  # fmt: skip
    if rates:
        mid = median(v for v, _ in rates)
        out["market_growth"] = (_growth_rating(mid), f"median stated CAGR {mid:g}% "
                                f"({len(rates)} figure(s))", [i for _, i in rates])  # fmt: skip
    rules: tuple[tuple[str, tuple[Dimension, ...], Callable[[int], float | None], str], ...] = (
        ("demand_strength", (Dimension.DEMAND_DRIVER, Dimension.ADOPTION), _volume,
         "proxy: demand evidence from {n} independent domain(s)"),
        ("pain_severity", (Dimension.PAIN_POINT,), _volume,
         "proxy: pain-point evidence from {n} independent domain(s)"),
        ("willingness_to_pay", (Dimension.WILLINGNESS_TO_PAY, Dimension.PRICING), _volume,
         "proxy: pricing / budget evidence from {n} independent domain(s)"),
        ("competitive_intensity", (Dimension.COMPETITION,), _inverse_volume,
         "proxy: competition named by {n} independent domain(s); more = more crowded"),
        ("regulatory_barriers", (Dimension.REGULATION,), _inverse_volume,
         "proxy: regulation discussed by {n} independent domain(s); more = heavier"),
    )  # fmt: skip
    for name, dims, rate, why in rules:
        found = about(*dims)
        n = independent_domains(found, sources)
        rating = rate(n)
        if rating is not None:
            out[name] = (rating, why.format(n=n), [e.evidence_id for e in found][:10])
    return out


def config_id(weights: dict[str, float]) -> str:
    text = json.dumps({"method": METHOD_VERSION, "weights": weights}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _normalized(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    return {k: round(v / total, 4) for k, v in sorted(weights.items())}


def score_candidate(
    candidate: str,
    weights: dict[str, float],
    derived: dict[str, tuple[float, str, list[str]]],
    user: dict[str, float],
) -> OpportunityScore:
    dims: list[DimensionScore] = []
    ids: list[str]
    for name, weight in weights.items():
        if name in user:
            rating, basis, why, ids = user[name], "user", "rated by the requester", []
        elif name in derived:
            rating, why, ids = derived[name]
            basis = "evidence"
        else:
            dims.append(DimensionScore(dimension=name, rating=None, weight=weight,
                                       contribution=None, basis="unknown",
                                       rationale="no rating and no evidence"))  # fmt: skip
            continue
        dims.append(DimensionScore(dimension=name, rating=rating, weight=weight,
                                   contribution=round(weight * rating, 3), basis=basis,
                                   rationale=why, evidence_ids=ids))  # fmt: skip
    rated = [d for d in dims if d.rating is not None]
    known = sum(d.weight for d in rated)
    total = sum(weights.values())
    coverage = round(known / total, 2) if total else 0.0
    if not rated:
        return OpportunityScore(candidate=candidate, score=None, raw_score=None,
                                coverage=0.0, penalty=0.0, dimensions=dims)  # fmt: skip
    raw = sum(d.contribution or 0 for d in rated) / known
    penalty = round((1 - coverage) * MISSING_PENALTY, 2)
    return OpportunityScore(candidate=candidate, score=round(max(1.0, raw - penalty), 1),
                            raw_score=round(raw, 2), coverage=coverage, penalty=penalty,
                            dimensions=dims)  # fmt: skip


def _ranked(scores: list[OpportunityScore]) -> list[str]:
    return [s.candidate for s in sorted(scores, key=lambda s: -(s.score or 0))]


def score_opportunities(
    candidates: list[str],
    evidence: list[EvidenceRecord],
    sources: dict[str, SourceRecord],
    request: ScoringRequest | None,
    subject: str,
) -> ScoringReport:
    """Score each candidate (a segment, or the subject itself) and rank them.

    A segment candidate is rated from the evidence that names it; the subject
    from all evidence."""
    req = request or ScoringRequest()
    weights = _normalized(req.weights or DEFAULT_WEIGHTS)
    rated = {d for ratings in req.ratings.values() for d in ratings}
    unknown = sorted((set(weights) | rated) - set(MEANING))
    if unknown:
        raise ValueError(f"unknown scoring dimensions: {', '.join(unknown)}")
    derived: dict[str, dict[str, tuple[float, str, list[str]]]] = {}
    for c in candidates:
        terms = subject_terms(c)
        mine = evidence if c == subject else [
            e for e in evidence if terms & subject_terms(e.excerpt)
        ]  # fmt: skip
        derived[c] = _derive(mine, sources)
    scores = [score_candidate(c, weights, derived[c], req.ratings.get(c, {})) for c in candidates]
    order = _ranked(scores)
    for s in scores:
        s.rank = order.index(s.candidate) + 1 if s.score is not None else None
    notes: list[str] = []
    if len(candidates) > 1:
        for dim in weights:
            for factor in (1 - SENSITIVITY_STEP, 1 + SENSITIVITY_STEP):
                w = _normalized({**weights, dim: weights[dim] * factor})
                alt = [score_candidate(c, w, derived[c], req.ratings.get(c, {}))
                       for c in candidates]  # fmt: skip
                if _ranked(alt) != order:
                    notes.append(f"ranking changes when the weight of {dim} is "
                                 f"{'lowered' if factor < 1 else 'raised'} by "
                                 f"{SENSITIVITY_STEP:.0%}: {' > '.join(_ranked(alt))}")  # fmt: skip
    return ScoringReport(
        config_id=config_id(weights), weights=weights, candidates=scores, sensitivity=notes,
        interpretation="1-5 favourability, weighted; unrated dimensions lower the score by "
                       f"(1 - coverage) x {MISSING_PENALTY:g}. A structured judgement over the "
                       "collected evidence, not a measurement. Compare only equal config_id.",
    )  # fmt: skip
