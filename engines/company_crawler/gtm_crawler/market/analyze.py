"""Industry and product analysis over collected evidence: the report's sections.

Every finding is either a quoted excerpt (``direct``), a figure summarized
across sources (``derived``), a requester's statement (``user``) or a rule's
hypothesis or recommendation (``inferred``), and says which. Sections with no
evidence are not filled in: they are listed as gaps. Nothing here invents a
fact; inferred findings are phrased as hypotheses and point at the evidence
that prompted them.
"""

from __future__ import annotations

from collections.abc import Iterable
from statistics import median

from .assessment import _fmt, confidence, independent_domains, scope_label
from .extract import subject_terms
from .models import (
    Attribution,
    Confidence,
    Dimension,
    EvidenceKind,
    EvidenceRecord,
    Finding,
    Metric,
    MetricKind,
    MetricSummary,
    PriceBound,
    PricingType,
    ProductInput,
    ResearchMode,
    ResearchRequest,
    Section,
    SourceRecord,
    SourceType,
)
from .plan import Question
from .pricing import REPORT_PRICE
from .scope import GLOBAL, canonical_geography

# 2: prices compared only within compatible terms (market/pricing.py), exclusions listed.
# 3: competitor_ads section (market/ads.py) when the product asks for captured ads.
ANALYSIS_VERSION = "market-analysis-3"
MAX_FINDINGS = 8
NO_EVIDENCE = "no evidence found for"
# Median monthly list price below which self-serve sales is the plausible motion; other
# currencies get no motion hypothesis (no conversion is made).
SELF_SERVE_MONTHLY_MAX = {"USD": 100.0, "EUR": 100.0, "GBP": 100.0}
D = Dimension

INDUSTRY_SECTIONS: tuple[tuple[str, str, tuple[Dimension, ...]], ...] = (
    ("definition", "Industry definition and scope", (D.DEFINITION,)),
    ("size_growth", "Market size and growth evidence", (D.MARKET_SIZE, D.GROWTH)),
    ("segments", "Segment analysis", (D.SEGMENT,)),
    ("demand", "Demand and customer analysis", (D.DEMAND_DRIVER, D.PAIN_POINT, D.ADOPTION,
                                                D.WILLINGNESS_TO_PAY)),
    ("competition", "Competitive structure", (D.COMPETITION, D.PRICING, D.DISTRIBUTION,
                                              D.SUBSTITUTE)),
    ("trends", "Trends and external drivers", (D.TREND, D.TECHNOLOGY, D.REGULATION)),
    ("risks", "Barriers and risks", (D.BARRIER, D.RISK)),
)  # fmt: skip
PRODUCT_SECTIONS: tuple[tuple[str, str, tuple[Dimension, ...]], ...] = (
    ("problem", "Product and problem definition", (D.PAIN_POINT,)),
    ("segments", "Target market and customer segments", (D.SEGMENT,)),
    ("demand", "Demand validation", (D.DEMAND_DRIVER, D.ADOPTION, D.WILLINGNESS_TO_PAY)),
    ("competitors", "Competitor and substitute comparison", (D.COMPETITION, D.SUBSTITUTE)),
    ("pricing", "Pricing and business-model observations", (D.PRICING, D.WILLINGNESS_TO_PAY)),
    ("size_growth", "Category size and growth", (D.MARKET_SIZE, D.GROWTH)),
    ("barriers", "Adoption barriers and risks", (D.BARRIER, D.REGULATION, D.RISK)),
)  # fmt: skip


def _rank(evidence: Iterable[EvidenceRecord], sources: dict[str, SourceRecord]) -> list[
        EvidenceRecord]:  # fmt: skip
    def key(e: EvidenceRecord) -> tuple[float, float, int]:
        s = sources.get(e.source_id)
        quality = s.quality.score if s is not None and s.quality is not None else 0.0
        return (-quality, -e.extraction_certainty, -len(e.metrics))

    return sorted(evidence, key=key)


def _finding(e: EvidenceRecord, sources: dict[str, SourceRecord]) -> Finding:
    s = sources.get(e.source_id)
    where = f" ({s.publisher or s.domain})" if s is not None else ""
    return Finding(statement=f"“{e.excerpt}”{where}", evidence_ids=[e.evidence_id],
                   confidence=confidence([e], sources), basis=e.kind,
                   independent_domains=1)  # fmt: skip


def _section(title: str, dims: tuple[Dimension, ...], evidence: list[EvidenceRecord],
             sources: dict[str, SourceRecord]) -> Section:  # fmt: skip
    mine = [e for e in evidence if set(e.dimensions) & set(dims)]
    section = Section(title=title)
    seen_domains: dict[str, int] = {}
    for e in _rank(mine, sources):
        domain = sources[e.source_id].domain if e.source_id in sources else e.source_id
        # Breadth before depth: at most two findings per domain.
        if seen_domains.get(domain, 0) >= 2:
            continue
        seen_domains[domain] = seen_domains.get(domain, 0) + 1
        section.findings.append(_finding(e, sources))
        if len(section.findings) >= MAX_FINDINGS:
            break
    if not mine:
        section.gaps.append(f"{NO_EVIDENCE}: {', '.join(d.value for d in dims)}")
    return section


def metric_findings(metrics: list[MetricSummary], geography: str | None = None) -> list[Finding]:
    """Figure summaries, each labelled with its scope (geography or "not stated",
    segment, part of a larger figure, another market) and its scope notes."""
    labels = {MetricKind.MARKET_SIZE: "Market size", MetricKind.MARKET_FORECAST: "Forecast",
              MetricKind.GROWTH_RATE: "Growth rate (CAGR)"}  # fmt: skip
    out: list[Finding] = []
    target = canonical_geography(geography) or GLOBAL
    # The requested scope's whole-market figures first (the executive summary quotes the first).
    in_scope = [m for m in metrics if m.geography == target and not m.segment and not m.subset
                and not m.off_subject and not m.qualifier and not m.ambiguous]  # fmt: skip
    for m in in_scope + [m for m in metrics if m not in in_scope]:
        if m.kind not in labels or (m.kind is not MetricKind.GROWTH_RATE and m.year is None):
            continue
        when = f" {'to ' if m.kind is MetricKind.GROWTH_RATE else ''}{m.year}" if m.year else ""
        value = (
            _fmt(m.low, m.unit)
            if m.low == m.high
            else f"{_fmt(m.low, m.unit)} to {_fmt(m.high, m.unit)}"
        )
        note = "; sources disagree" if m.contradicted else ""
        note += "".join(f"; {n}" for n in m.notes)
        label = scope_label(m.geography, m.segment, m.measure, m.subset, m.off_subject,
                            qualifier=m.qualifier)  # fmt: skip
        when += f" ({label})"
        out.append(Finding(
            statement=f"{labels[m.kind]}{when}: {value} as stated by {m.independent_domains} "
                      f"independent source(s){note}",
            evidence_ids=m.evidence_ids, confidence=m.confidence, basis=EvidenceKind.DERIVED,
            independent_domains=m.independent_domains,
        ))  # fmt: skip
    return out


def question_gaps(questions: list[Question], evidence: list[EvidenceRecord]) -> list[str]:
    found = {d for e in evidence for d in e.dimensions}
    return [f"unanswered: {q.text}" for q in questions if not found & set(q.dimensions)]


def _user(statement: str) -> Finding:
    return Finding(statement=statement, confidence=Confidence.LOW, basis=EvidenceKind.USER)


def _inferred(statement: str, evidence: list[EvidenceRecord],
              sources: dict[str, SourceRecord]) -> Finding:  # fmt: skip
    return Finding(statement=statement, evidence_ids=[e.evidence_id for e in evidence][:10],
                   confidence=Confidence.LOW, basis=EvidenceKind.INFERRED,
                   independent_domains=independent_domains(evidence, sources))  # fmt: skip


def industry_sections(evidence: list[EvidenceRecord], sources: dict[str, SourceRecord],
                      metrics: list[MetricSummary],
                      geography: str | None = None) -> dict[str, Section]:  # fmt: skip
    out = {key: _section(title, dims, evidence, sources) for key, title, dims in INDUSTRY_SECTIONS}
    figures = metric_findings(metrics, geography)
    out["size_growth"].findings[:0] = figures
    if figures:
        out["size_growth"].gaps.clear()
    return out


PriceKey = tuple[
    str, str, bool, str | None, str | None, str | None, float | None, str | None, str | None
]
MAX_EXCLUDED_SHOWN = 10


def _price_points(evidence: list[EvidenceRecord]) -> list[tuple[Metric, EvidenceRecord]]:
    return [(m, e) for e in evidence for m in e.metrics
            if m.kind in (MetricKind.PRICE_POINT, MetricKind.PRICE_ON_REQUEST)]  # fmt: skip


def not_comparable(m: Metric) -> str | None:
    """Why a price cannot be set beside others, or None. Nothing is converted: no
    currency conversion, no monthly/annual conversion, no period assumed."""
    p = m.price
    if m.kind is MetricKind.PRICE_ON_REQUEST or p is not None and p.pricing_type is \
            PricingType.QUOTE:  # fmt: skip
        return "priced on request: no amount"
    if p is None:
        return "recorded without price terms (type and billing not known)"
    if REPORT_PRICE in p.ambiguities:
        return REPORT_PRICE
    if not p.percent and p.currency is None:
        return "currency not stated or unclear (no conversion is made)"
    if p.pricing_type is PricingType.UNKNOWN:
        return "what the amount pays for is not stated"
    if p.pricing_type is PricingType.SUBSCRIPTION and p.period is None:
        return "no billing period stated (none is assumed)"
    if p.promotional:
        return "a promotional price"
    if p.bound in (PriceBound.UP_TO, PriceBound.ABOUT):
        return "an upper bound or approximate amount"
    return None


def price_key(m: Metric) -> PriceKey:
    """Prices with the same key are in the same terms: type, currency (or percent),
    period, billing commitment, basis and its quantity, tax, included allowance."""
    p = m.price
    assert p is not None
    return (p.pricing_type.value, "percent" if p.percent else p.currency or "", p.percent,
            p.period, p.billed, p.basis, p.basis_quantity, p.tax, p.included)  # fmt: skip


def describe_terms(key: PriceKey) -> str:
    kind, cur, percent, period, billed, basis, quantity, tax, included = key
    unit = "%" if percent else cur
    per = "".join(f" per {x}" for x in (f"{quantity:g} {basis}" if quantity else basis, period)
                  if x)  # fmt: skip
    return (
        f"{unit}{per}, {kind.replace('_', ' ')}"
        + (f", billed {billed}" if billed else "")
        + (f", tax {tax}" if tax else "")
        + (f", for {included}" if included else "")
    )


def _who(m: Metric, e: EvidenceRecord, sources: dict[str, SourceRecord]) -> str:
    p = m.price
    s = sources.get(e.source_id)
    names = [p.vendor, p.product, p.plan] if p is not None else []
    if not (p and (p.vendor or p.product)):
        names.insert(0, s.publisher or s.domain if s is not None else e.source_id)
    out: list[str] = []
    for name in (n for n in names if n):
        if any(name.lower() in x.lower() for x in out):
            continue
        out = [x for x in out if x.lower() not in name.lower()] + [name]
    if p is not None and p.attribution is Attribution.UNCERTAIN:
        out.append("(plan not identified)")
    return " ".join(out)


def price_comparison(evidence: list[EvidenceRecord], sources: dict[str, SourceRecord]
                     ) -> tuple[list[Finding], list[str]]:  # fmt: skip
    """Findings comparing prices within compatible terms (one per set of terms, each
    price named by vendor or product and plan; amounts that sources state differently
    for the same plan are flagged), and the prices left out, with why."""
    groups: dict[PriceKey, list[tuple[Metric, EvidenceRecord]]] = {}
    excluded: list[str] = []
    for m, e in _price_points(evidence):
        why = not_comparable(m)
        if why is not None:
            excluded.append(f"price not compared: “{m.text}” ({_who(m, e, sources)}): {why}")
            continue
        groups.setdefault(price_key(m), []).append((m, e))
    out = []
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        unit = "percent" if key[2] else key[1]
        named: dict[str, set[float]] = {}
        for m, e in items:
            named.setdefault(_who(m, e, sources), set()).add(m.value)
        listed = "; ".join(
            f"{who} {' / '.join(_fmt(v, unit) for v in sorted(vs))}" for who, vs in named.items()
        )
        values = [m.value for m, _ in items]
        spread = (
            f"{_fmt(min(values), unit)} to {_fmt(max(values), unit)}, median "
            f"{_fmt(median(values), unit)}"
            if len(values) > 1
            else _fmt(values[0], unit)
        )
        starting = sum(m.price is not None and m.price.bound is PriceBound.FROM for m, _ in items)
        notes = [f"{starting} of them starting prices"] if starting else []
        assumed = sum(m.price is not None and m.price.currency_assumed for m, _ in items)
        if assumed:
            notes.append(f"{assumed} with the currency read from a bare symbol")
        conflicts = [who for who, vs in named.items() if len(vs) > 1]
        if conflicts:
            notes.append("different amounts stated for " + ", ".join(conflicts) +
                         " (another plan, date or source; check the excerpts)")  # fmt: skip
        out.append(Finding(
            statement=f"Prices in {describe_terms(key)}: {listed} ({spread}; "
                      f"{len(items)} price point(s)" + "".join(f"; {n}" for n in notes) + ")",
            evidence_ids=list(dict.fromkeys(e.evidence_id for _, e in items))[:20],
            confidence=confidence([e for _, e in items], sources), basis=EvidenceKind.DERIVED,
            independent_domains=independent_domains((e for _, e in items), sources),
        ))  # fmt: skip
    if len(excluded) > MAX_EXCLUDED_SHOWN:
        more = len(excluded) - MAX_EXCLUDED_SHOWN
        excluded = [*excluded[:MAX_EXCLUDED_SHOWN], f"{more} more price(s) not compared"]
    if not groups and excluded:
        excluded.append("no prices in comparable terms: none compared")
    return out, excluded


def product_sections(req: ResearchRequest, evidence: list[EvidenceRecord],
                     sources: dict[str, SourceRecord],
                     metrics: list[MetricSummary]) -> dict[str, Section]:  # fmt: skip
    assert req.product is not None
    p = req.product
    out = {key: _section(title, dims, evidence, sources) for key, title, dims in PRODUCT_SECTIONS}
    stated = [f"Product: {p.name}" + (f" ({p.stage.value})" if p.stage else "")]
    for label, value in (("Description", p.description), ("Problem", p.problem),
                         ("Target customer", p.target_customer),
                         ("Business model", p.business_model), ("Price", p.price)):  # fmt: skip
        if value:
            stated.append(f"{label}: {value}")
    out["problem"].findings[:0] = [_user(f"{s} (as stated by the requester)") for s in stated]
    for label, value in (("problem", p.problem), ("target customer", p.target_customer),
                         ("price", p.price)):  # fmt: skip
        if not value:
            out["problem"].gaps.append(f"{label} not given: conclusions about it rest on "
                                       "search results only")  # fmt: skip
    figures = metric_findings(metrics, req.geography)
    out["size_growth"].findings[:0] = figures
    if figures:
        out["size_growth"].gaps.clear()

    # Pricing: prices side by side only in the same terms; the rest listed with why not.
    compared, left_out = price_comparison(evidence, sources)
    out["pricing"].findings[:0] = compared
    out["pricing"].gaps += left_out

    # What competitors say about themselves belongs in the comparison, whatever it is about.
    rivals = out["competitors"]
    shown = {i for f in rivals.findings for i in f.evidence_ids}
    for e in _rank(_vendor(evidence, sources), sources):
        if len(rivals.findings) >= MAX_FINDINGS * 2:
            break
        if e.evidence_id not in shown:
            rivals.findings.append(_finding(e, sources))
            rivals.gaps.clear()
    if p.known_competitors:
        rivals.findings.insert(0, competitor_coverage(p, evidence, sources, rivals.gaps))
    out["differentiation"] = differentiation(req, evidence, sources)
    out["go_to_market"] = go_to_market(req, evidence, sources)
    out["validation"] = validation_experiments(req, out)
    return out


def competitor_coverage(product: ProductInput, evidence: list[EvidenceRecord],
                        sources: dict[str, SourceRecord], gaps: list[str]) -> Finding:  # fmt: skip
    """How many known competitors were actually collected and priced, so a comparison of
    one is never read as a comparison of all; each one not collected is added to ``gaps``
    with why (its pages' retrieval status, or no url)."""
    collected: list[str] = []
    priced: list[str] = []
    missing: list[str] = []
    for c in product.known_competitors:
        pages = [s for s in sources.values() if s.discovered_by == f"known_competitor:{c.name}"]
        ids = {s.source_id for s in pages}
        found = [e for e in evidence if e.source_id in ids]
        if not found:
            missing.append(c.name)
            why = ", ".join(sorted({s.status.value for s in pages})) or "no url given"
            gaps.append(f"known competitor {c.name}: no evidence collected ({why})")
            continue
        collected.append(c.name)
        if any(m.kind is MetricKind.PRICE_POINT and not_comparable(m) is None
               for e in found for m in e.metrics):  # fmt: skip
            priced.append(c.name)
        else:
            gaps.append(f"known competitor {c.name}: no price in comparable terms collected")
    statement = (
        f"Known competitors: {len(product.known_competitors)} given; {len(collected)} with "
        f"collected evidence ({', '.join(collected) or 'none'}); {len(priced)} with prices in "
        f"comparable terms ({', '.join(priced) or 'none'})"
        + (f"; not collected: {', '.join(missing)}" if missing else "")
    )
    return Finding(statement=statement, confidence=Confidence.HIGH, basis=EvidenceKind.DERIVED)


def _vendor(evidence: list[EvidenceRecord],
            sources: dict[str, SourceRecord]) -> list[EvidenceRecord]:  # fmt: skip
    return [e for e in evidence if e.source_id in sources and sources[e.source_id].source_type
            in (SourceType.COMPANY_WEBSITE, SourceType.CRAWLED_DOSSIER)]  # fmt: skip


def differentiation(req: ResearchRequest, evidence: list[EvidenceRecord],
                    sources: dict[str, SourceRecord]) -> Section:  # fmt: skip
    """Proposed differentiators checked against what was collected.

    On a known competitor's own page (or the crawled dossier): likely parity.
    Elsewhere (search results): the idea is already discussed or sold by
    someone; those pages are named, not assumed to be competitors. Not found
    at all: absence of evidence, not proof of uniqueness."""
    assert req.product is not None
    section = Section(title="Differentiation opportunities")
    vendor = _vendor(evidence, sources)
    vendor_ids = {e.evidence_id for e in vendor}
    if not req.product.differentiators:
        section.gaps.append("no proposed differentiators given to check")

    def matches(claim_terms: set[str], e: EvidenceRecord) -> bool:
        need = max(1, (len(claim_terms) + 1) // 2)
        return bool(claim_terms) and len(claim_terms & subject_terms(e.excerpt)) >= need

    def names(found: list[EvidenceRecord]) -> str:
        return ", ".join(sorted({sources[e.source_id].publisher or sources[e.source_id].domain
                                 for e in found if e.source_id in sources}))  # fmt: skip

    for claim in req.product.differentiators:
        terms = subject_terms(claim)
        on_vendor = [e for e in vendor if matches(terms, e)]
        elsewhere = [e for e in evidence if e.evidence_id not in vendor_ids and matches(terms, e)]
        if on_vendor:
            text = (
                f"Proposed differentiator “{claim}”: comparable claims appear on competitor "
                f"pages ({names(on_vendor)}) - likely feature parity, not differentiation"
            )
        elif elsewhere:
            text = (
                f"Proposed differentiator “{claim}”: not on the known competitors' pages, but "
                f"discussed on {len(elsewhere)} other collected excerpt(s) "
                f"({names(elsewhere)}) - check whether these offer it before claiming it"
            )
        else:
            text = (
                f"Proposed differentiator “{claim}”: not found in {len(evidence)} collected "
                "excerpt(s); absence of evidence, not proof it is unique - validate with "
                "buyers"
            )
        section.findings.append(_inferred(text, on_vendor or elsewhere, sources))
    if not vendor:
        section.gaps.append("no competitor pages collected (add known_competitors or a crawled "
                            "company_id)")  # fmt: skip
    return section


def go_to_market(req: ResearchRequest, evidence: list[EvidenceRecord],
                 sources: dict[str, SourceRecord]) -> Section:  # fmt: skip
    section = _section("Go-to-market observations and hypotheses", (D.DISTRIBUTION,), evidence,
                       sources)  # fmt: skip
    # Monthly subscription prices in one set of terms only (the largest): one currency,
    # one basis, one billing; no conversion is made.
    monthly: dict[PriceKey, list[tuple[float, EvidenceRecord]]] = {}
    for m, e in _price_points(evidence):
        if not_comparable(m) is None and m.price is not None and m.price.period == "month" \
                and m.price.pricing_type is PricingType.SUBSCRIPTION:  # fmt: skip
            monthly.setdefault(price_key(m), []).append((m.value, e))
    if monthly:
        key, same = max(monthly.items(), key=lambda kv: len(kv[1]))
        cur = key[1]
        mid = median(v for v, _ in same)
        limit = SELF_SERVE_MONTHLY_MAX.get(cur)
        motion = ("" if limit is None else
                  ", so self-serve or product-led sales is plausible" if mid < limit else
                  ", so a sales-assisted motion is likely needed")  # fmt: skip
        section.findings.append(
            _inferred(
                f"Hypothesis: comparable products list a median of {cur} {mid:g} per month "
                f"({describe_terms(key)}; {len(same)} price point(s)){motion}; test before "
                "committing",
                [e for _, e in same],
                sources,
            )
        )
    enterprise = [e for e in evidence if "enterprise" in e.excerpt.lower()
                  and D.PRICING in e.dimensions]  # fmt: skip
    if enterprise:
        section.findings.append(
            _inferred(
                "Hypothesis: competitors gate enterprise tiers behind sales contact, "
                "suggesting longer sales cycles for larger buyers",
                enterprise,
                sources,
            )
        )
    return section


def validation_experiments(req: ResearchRequest, sections: dict[str, Section]) -> Section:
    """Experiments for what web research could not settle (recommendations, not findings)."""
    assert req.product is not None
    who = req.product.target_customer or req.segment or "target buyers"
    section = Section(title="Recommended validation experiments")
    plan = [
        ("demand", f"Interview 10-15 {who} about how often the problem occurs, what it costs and "
                   "what they use today; demand counts only if they describe recent, concrete "
                   "episodes."),
        ("pricing", "Test willingness to pay: a pricing page with plans (fake door) or a "
                    "Van Westendorp survey with qualified buyers."),
        ("competitors", "Talk to 5+ users of the named competitors or workarounds about what "
                        "would make them switch."),
        ("go_to_market", "Run small, capped tests on two acquisition channels and compare cost "
                         "per qualified lead."),
    ]  # fmt: skip
    for key, text in plan:
        weak = key not in sections or all(f.confidence in (Confidence.LOW, Confidence.INSUFFICIENT)
                                          for f in sections[key].findings)  # fmt: skip
        prefix = "Priority - evidence is thin: " if weak else ""
        section.findings.append(Finding(statement=prefix + text, confidence=Confidence.LOW,
                                        basis=EvidenceKind.INFERRED))  # fmt: skip
    section.findings.append(Finding(
        statement="Web research alone cannot establish product-market fit; it indicates where "
                  "demand evidence exists and where it is missing.",
        confidence=Confidence.HIGH, basis=EvidenceKind.INFERRED))  # fmt: skip
    return section


def executive_summary(req: ResearchRequest, sections: dict[str, Section],
                      sources: list[SourceRecord], evidence: list[EvidenceRecord],
                      contradictions: int, gaps: list[str]) -> list[Finding]:  # fmt: skip
    used = [s for s in sources if s.status.value in ("fetched", "cached")]
    domains = len({s.domain for s in used})
    out = [Finding(
        statement=f"{len(evidence)} evidence excerpt(s) from {len(used)} source(s) across "
                  f"{domains} domain(s) on {req.subject}"
                  + (f" ({req.geography})" if req.geography else ""),
        confidence=Confidence.HIGH, basis=EvidenceKind.DERIVED, independent_domains=domains,
    )]  # fmt: skip
    first_key = "size_growth"
    order = [first_key] + [k for k in sections if k != first_key]
    used_ids: set[str] = set()
    for key in order:
        section = sections.get(key)
        if section is None:
            continue
        best = next((f for f in section.findings
                     if f.basis in (EvidenceKind.DIRECT, EvidenceKind.DERIVED)
                     and not used_ids & set(f.evidence_ids)), None)  # fmt: skip
        if best is not None:
            used_ids.update(best.evidence_ids)
            out.append(best.model_copy(update={"statement": f"{section.title}: {best.statement}"}))
        if len(out) >= 7:
            break
    if contradictions:
        out.append(Finding(statement=f"{contradictions} contradiction(s) between sources are "
                                     "reported with both values kept", confidence=Confidence.HIGH,
                           basis=EvidenceKind.DERIVED))  # fmt: skip
    if gaps:
        out.append(Finding(statement=f"{len(gaps)} research question(s) remain open: "
                                     + "; ".join(g.removeprefix("unanswered: ") for g in gaps[:4]),
                           confidence=Confidence.HIGH, basis=EvidenceKind.DERIVED))  # fmt: skip
    if req.mode is ResearchMode.PRODUCT:
        out.append(Finding(statement="This is desk research: it cannot establish product-market "
                                     "fit", confidence=Confidence.HIGH,
                           basis=EvidenceKind.INFERRED))  # fmt: skip
    return out
