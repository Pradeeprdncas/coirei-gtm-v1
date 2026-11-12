"""Source classification, source quality, claim confidence and contradictions.

Three things are kept apart (docs/MARKET_ENGINE_IMPLEMENTATION.md):

* source quality (``SourceQuality``): how much weight a publisher's page
  deserves, from authority, recency, methodology, geographic fit and
  specificity, each kept as its own component;
* claim confidence (``Confidence``): how well a statement is supported, by
  rule from independent domains, the best source's quality and unresolved
  contradictions;
* extraction certainty (on each ``EvidenceRecord``): how sure the rules are
  that the excerpt says what they filed it under.

None of them is a probability, and none comes from a model.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..config import MarketSettings, SourceSettings
from ..evidence import classify_source
from ..parse import ParsedPage
from ..urls import registrable_domain, url_host
from .models import (
    Confidence,
    Contradiction,
    EvidenceKind,
    EvidenceRecord,
    Metric,
    MetricKind,
    MetricSummary,
    SourceQuality,
    SourceRecord,
    SourceType,
)

AUTHORITY = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4}
# A vendor's own page about the market it sells into is self-interested.
VENDOR_AUTHORITY = 0.5
# Values of the same metric (same unit and year) further apart than this ratio
# are a contradiction; growth rates further apart than this many points.
SIZE_TOLERANCE = 1.25
RATE_TOLERANCE_POINTS = 3.0

_METHODOLOGY = re.compile(
    r"\b(methodology|research method|sample size|survey of [\d,]+|[\d,]+ respondents|"
    r"we surveyed|data (was|were) collected|primary research|secondary research|"
    r"margin of error|data sources?)\b",
    re.I,
)
_GLOBAL = re.compile(r"\b(global|worldwide|world)\b", re.I)


def _listed(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(
    url: str, sources: SourceSettings, market: MarketSettings, *, vendor: bool = False
) -> tuple[SourceType, int]:
    """Publisher class and tier of a page (tier describes the source, not the claim)."""
    host = url_host(url)
    if vendor:
        return SourceType.COMPANY_WEBSITE, 1
    if host.endswith(market.government_suffixes) or _listed(host, market.statistics_domains):
        return SourceType.GOVERNMENT, 1
    if _listed(host, market.market_research_domains):
        return SourceType.MARKET_RESEARCH, 3
    found = classify_source(url, None, sources)
    known = {s.value for s in SourceType}
    return (SourceType(found.type) if found.type in known else SourceType.UNVERIFIED), found.tier


def source_quality(
    source: SourceRecord,
    page: ParsedPage | None,
    evidence: list[EvidenceRecord],
    geography: str | None,
    now: datetime | None = None,
) -> SourceQuality:
    now = now or datetime.now(UTC)
    notes: list[str] = []
    if source.source_type is SourceType.COMPANY_WEBSITE:
        authority = VENDOR_AUTHORITY
        notes.append("vendor's own page: first-party for its product, self-interested for "
                     "market claims")  # fmt: skip
    else:
        authority = AUTHORITY[source.tier]
    recency: float | None = None
    if source.published_at is not None:
        published = source.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age = (now - published).days / 365.25
        recency = 1.0 if age <= 1 else 0.7 if age <= 3 else 0.4 if age <= 5 else 0.2
    else:
        notes.append("no publication date")
    text = page.main_text if page is not None else " ".join(e.excerpt for e in evidence)
    methodology = 1.0 if _METHODOLOGY.search(text) else 0.3
    if methodology < 1:
        notes.append("methodology not stated")
    geo: float | None = None
    if geography:
        if geography.lower() in text.lower():
            geo = 1.0
        elif _GLOBAL.search(text):
            geo = 0.6
            notes.append(f"global scope; {geography} not named")
        else:
            geo = 0.3
            notes.append(f"{geography} not named")
    specificity = (sum(1 for e in evidence if e.metrics) / len(evidence)) if evidence else None
    known = [v for v in (authority, recency, methodology, geo, specificity) if v is not None]
    return SourceQuality(
        authority=authority, recency=recency, methodology=methodology,
        geographic_relevance=geo, specificity=None if specificity is None else
        round(specificity, 2), score=round(sum(known) / len(known), 2), notes=notes,
    )  # fmt: skip


def confidence(
    evidence: list[EvidenceRecord],
    sources: dict[str, SourceRecord],
    contradicted: bool | None = None,
) -> Confidence:
    """Claim confidence by rule (see ``Confidence``). ``contradicted`` overrides
    the evidence's own marks (a sentence contradicted on one figure may be
    uncontested on another it states)."""
    if not evidence:
        return Confidence.INSUFFICIENT
    domains = {sources[e.source_id].domain for e in evidence if e.source_id in sources}
    scores = [q.score for e in evidence if e.source_id in sources
              and (q := sources[e.source_id].quality) is not None]  # fmt: skip
    best = max(scores, default=0.0)
    if contradicted is None:
        contradicted = any(e.contradicted_by for e in evidence)
    only_inferred = all(e.kind in (EvidenceKind.INFERRED, EvidenceKind.USER) for e in evidence)
    if contradicted or only_inferred:
        return Confidence.LOW
    if len(domains) >= 2 and best >= 0.7:
        return Confidence.HIGH
    if best >= 0.6 or len(domains) >= 2:
        return Confidence.MEDIUM
    return Confidence.LOW


def independent_domains(
    evidence: Iterable[EvidenceRecord], sources: dict[str, SourceRecord]
) -> int:
    return len({sources[e.source_id].domain for e in evidence if e.source_id in sources})


# kind, unit, year, geography, segment, measure, subset, off_subject, qualifier (``scope.py``).
MetricKey = tuple[MetricKind, str, int | None, str | None, str | None, str, bool, bool, str | None]
_SCOPED = (MetricKind.MARKET_SIZE, MetricKind.MARKET_FORECAST, MetricKind.GROWTH_RATE)


def _metric_groups(
    evidence: list[EvidenceRecord],
) -> dict[MetricKey, list[tuple[EvidenceRecord, Metric]]]:
    """Figures by their full scope: a global total, a regional figure, a segment,
    a part of a larger figure and another market's figure are different metrics,
    never one range and never a contradiction. Revenue that names no market (a
    company's) is not a market metric at all; its evidence is kept."""
    groups: dict[MetricKey, list[tuple[EvidenceRecord, Metric]]] = defaultdict(list)
    for e in evidence:
        for m in e.metrics:
            if m.kind not in _SCOPED or m.measure == "revenue":
                continue
            year = m.period_end if m.kind is MetricKind.GROWTH_RATE else m.year
            groups[(m.kind, m.unit, year, m.geography, m.segment, m.measure or "market",
                    m.subset, m.off_subject, m.qualifier)].append((e, m))  # fmt: skip
    return groups


def scope_label(geography: str | None, segment: str | None = None, measure: str | None = None,
                subset: bool = False, off_subject: bool = False,
                phrase: str | None = None, qualifier: str | None = None) -> str:  # fmt: skip
    parts = [geography or "geography not stated"]
    if segment:
        parts.append(f"{segment} segment")
    if qualifier:
        parts.append(f"market qualified as {qualifier!r}")
    if measure in ("spending", "investment"):
        parts.append(measure)
    if subset:
        parts.append("part of a larger figure")
    if off_subject:
        parts.append(f"another market: {phrase!r}" if phrase else "another market")
    return ", ".join(parts)


def _apart(kind: MetricKind, values: list[float]) -> bool:
    lo, hi = min(values), max(values)
    if kind is MetricKind.GROWTH_RATE:
        return hi - lo > RATE_TOLERANCE_POINTS
    return lo > 0 and hi / lo > SIZE_TOLERANCE


def find_contradictions(
    evidence: list[EvidenceRecord], sources: dict[str, SourceRecord]
) -> list[Contradiction]:
    """Same metric (kind, unit, year, geography), different domains, values too far apart.

    Both sides are kept and marked (``contradicted_by``); neither is preferred.
    Values from one domain are not compared (a page often breaks a market down
    into regions or segments). Figures without a year are never compared.
    """
    out: list[Contradiction] = []
    for key, items in sorted(_metric_groups(evidence).items(), key=str):
        kind, unit, year, geo, segment, measure, subset, off_subject, qualifier = key
        if year is None or subset or off_subject:
            continue  # parts of a larger figure and other markets are not claims about this one
        by_domain: dict[str, tuple[EvidenceRecord, float]] = {}
        for e, m in items:
            domain = sources[e.source_id].domain if e.source_id in sources else e.source_id
            by_domain.setdefault(domain, (e, m.value))
        if len(by_domain) < 2:
            continue
        values = [v for _, v in by_domain.values()]
        lo, hi = min(values), max(values)
        if not _apart(kind, values):
            continue
        involved = [e for e, _ in by_domain.values()]
        ids = [e.evidence_id for e in involved]
        for e in involved:
            e.contradicted_by = sorted(set(e.contradicted_by) | (set(ids) - {e.evidence_id}))
        causes = ["sources may define the market differently (scope, included categories)"]
        srcs = [sources[e.source_id] for e in involved if e.source_id in sources]
        if len({s.published_at.date() if s.published_at else None for s in srcs}) > 1:
            causes.append("published at different times")
        if any(s.published_at is None for s in srcs):
            causes.append("a publication date is missing")
        if geo is None:
            causes.append("the excerpts do not name a geography: some may be global")
        label = {MetricKind.MARKET_SIZE: "market size", MetricKind.MARKET_FORECAST:
                 "market forecast", MetricKind.GROWTH_RATE: "growth rate (CAGR)"}[kind]  # fmt: skip
        when = f"for {year}" if kind is not MetricKind.GROWTH_RATE else f"to {year}"
        when += f" ({scope_label(geo, segment, measure, qualifier=qualifier)})"
        shown = [_fmt(v, unit) for v in values]
        out.append(Contradiction(
            topic=f"{label} {when}, {unit}", metric=kind, unit=unit, year=year, geography=geo,
            segment=segment, measure=measure, qualifier=qualifier,
            evidence_ids=ids, values=shown,
            explanation=f"{len(by_domain)} independent sources state {label} {when} between "
                        f"{_fmt(lo, unit)} and {_fmt(hi, unit)}; both ends are kept and the "
                        "figure is reported as a range.",
            possible_causes=causes,
        ))  # fmt: skip
    return out


_KIND_ORDER = {MetricKind.MARKET_SIZE: 0, MetricKind.MARKET_FORECAST: 1,
               MetricKind.GROWTH_RATE: 2}  # fmt: skip


def metric_summaries(
    evidence: list[EvidenceRecord],
    sources: dict[str, SourceRecord],
    contradictions: list[Contradiction],
) -> list[MetricSummary]:
    """One summary per metric scope: sizes first, then forecasts, then growth
    rates; stated geographies and whole markets first, the latest year first.

    Unresolved scope lowers confidence: one source stating several values for
    one scope (``ambiguous``), a part of a larger figure or another market ->
    LOW; a geography the excerpts do not state -> at most MEDIUM."""
    disputed = {(c.metric, c.unit, c.year, c.geography, c.segment, c.measure, c.qualifier)
                for c in contradictions}  # fmt: skip
    out: list[MetricSummary] = []

    def order(key: MetricKey) -> tuple[Any, ...]:
        kind, _, year, geo, segment, _, subset, off_subject, qualifier = key
        return (_KIND_ORDER[kind], geo is None, subset, off_subject, qualifier is not None,
                segment is not None,
                -(year or 0), str(key))  # fmt: skip

    for key, items in sorted(_metric_groups(evidence).items(), key=lambda kv: order(kv[0])):
        kind, unit, year, geo, segment, measure, subset, off_subject, qualifier = key
        records = list({e.evidence_id: e for e, _ in items}.values())
        values = [m.value for _, m in items]
        contested = (kind, unit, year, geo, segment, measure, qualifier) in disputed
        per_domain: dict[str, list[float]] = defaultdict(list)
        for e, m in items:
            per_domain[sources[e.source_id].domain if e.source_id in sources
                       else e.source_id].append(m.value)  # fmt: skip
        ambiguous = any(_apart(kind, v) for v in per_domain.values())
        rating = confidence(records, sources, contradicted=contested)
        notes: list[str] = []
        if ambiguous:
            notes.append("one source states several different values for this scope (regional "
                         "or segment figures the rules did not separate?)")  # fmt: skip
        if subset:
            notes.append("stated as part of a larger figure, without a scope of its own")
        if off_subject:
            notes.append(f"names another market: {items[0][1].market_phrase!r}")
        if measure == "spending":
            notes.append("spending on the market, not a market value: definitions may differ")
        if qualifier:
            notes.append(f"the market is named {items[0][1].market_phrase!r}: possibly narrower or "
                         "different than the subject")  # fmt: skip
        if ambiguous or subset or off_subject:
            rating = Confidence.LOW
        elif geo is None:
            notes.append("geography not stated: it may be global or another region")
            if rating is Confidence.HIGH:
                rating = Confidence.MEDIUM
        out.append(MetricSummary(
            kind=kind, unit=unit, year=year, geography=geo, segment=segment, measure=measure,
            subset=subset, off_subject=off_subject, qualifier=qualifier, low=min(values),
            high=max(values),
            evidence_ids=[e.evidence_id for e in records],
            independent_domains=independent_domains(records, sources),
            confidence=rating, contradicted=contested, ambiguous=ambiguous, notes=notes,
        ))  # fmt: skip
    return out


def _fmt(value: float, unit: str) -> str:
    if unit.startswith("percent"):
        return f"{value:g}%"
    for scale, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if value >= scale:
            return f"{unit} {value / scale:.3g} {word}"
    return f"{unit} {value:,.2f}"


def domain_of(url: str) -> str:
    return registrable_domain(url_host(url))
