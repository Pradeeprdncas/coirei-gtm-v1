"""TAM / SAM / SOM, calculated here from explicit inputs (docs/MARKET_SIZING_METHODOLOGY.md).

* Top-down: TAM = a market total; SAM = TAM x each SAM filter; SOM = SAM x
  the obtainable share.
* Bottom-up: TAM = customers x units per customer x price per unit (a year);
  SAM and SOM as above, with a penetration rate for SOM.

Every input is a ``Quantity`` with a low/base/high range and a basis
(sourced with evidence ids, or an assumption with a rationale). The low and
high scenarios take every input at its low or high end at once: bounds, not
probabilities. Sensitivity moves one input to each end with the others at
base. A result is ``verified`` only when every input is sourced and each is
corroborated by independent sources; otherwise it says what it rests on.
SAM is not defined without a SAM filter and SOM not without a share: they
are reported missing rather than set equal to the level above.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from .assessment import _fmt, scope_label
from .models import (
    Basis,
    BottomUpInput,
    Confidence,
    ExcludedFigure,
    MetricKind,
    MetricSummary,
    Quantity,
    Scenario,
    SensitivityRow,
    SizingReport,
    SizingRequest,
    SizingResult,
    TopDownInput,
)
from .scope import GLOBAL, canonical_geography, canonical_segment

FRACTION = "fraction"
# Two methods whose base TAMs differ by more than this factor get a warning.
CROSS_CHECK_FACTOR = 2.0

Pick = Callable[[Quantity], float]


class SizingError(ValueError):
    pass


def _base(q: Quantity) -> float:
    return q.base


def _lo(q: Quantity) -> float:
    return q.lo


def _hi(q: Quantity) -> float:
    return q.hi


def _check_currency(q: Quantity, currency: str) -> None:
    if q.unit.split("/")[0].upper() != currency:
        raise SizingError(f"{q.name}: unit {q.unit!r} is not in {currency} (no conversion is made)")


_NOT_ANNUAL = frozenset({"month", "mo", "week", "wk", "day", "quarter", "qtr", "hour", "hr"})
_ANNUAL = frozenset({"year", "yr", "annum", "a"})


def _annual_price(q: Quantity) -> list[str]:
    """Bottom-up TAM is per year: refuse a price per month/week/...; warn if no period."""
    periods = {p.strip().lower() for p in q.unit.split("/")[1:]}
    if periods & _NOT_ANNUAL:
        raise SizingError(f"{q.name}: unit {q.unit!r} is not per year; give the annual price "
                          "(e.g. 12 x a monthly price) in a '/year' unit")  # fmt: skip
    if not periods & _ANNUAL:
        return [f"{q.name}: unit {q.unit!r} names no period; assumed per year"]
    return []


def _check_filters(filters: list[Quantity], share: Quantity | None) -> None:
    dims: dict[str, str] = {}
    for f in [*filters, *([share] if share else [])]:
        if f.unit != FRACTION or f.hi > 1:
            raise SizingError(f"{f.name}: a filter or share must be a fraction between 0 and 1")
    for f in filters:
        dim = (f.dimension or f.name).strip().lower()
        if dim in dims:
            raise SizingError(f"{f.name} and {dims[dim]} both narrow '{dim}': that double-counts; "
                              "combine them into one filter")  # fmt: skip
        dims[dim] = f.name


def _levels(tam: float, filters: list[Quantity], share: Quantity | None, pick: Pick) -> Scenario:
    sam = tam * math.prod(pick(f) for f in filters) if filters else None
    som = sam * pick(share) if sam is not None and share is not None else None
    return Scenario(tam=round(tam, 2), sam=None if sam is None else round(sam, 2),
                    som=None if som is None else round(som, 2))  # fmt: skip


def _scenarios(
    tam_of: Callable[[Pick], float],
    filters: list[Quantity],
    share: Quantity | None,
    inputs: list[Quantity],
) -> tuple[Scenario, Scenario, Scenario, list[SensitivityRow]]:
    low = _levels(tam_of(_lo), filters, share, _lo)
    base = _levels(tam_of(_base), filters, share, _base)
    high = _levels(tam_of(_hi), filters, share, _hi)

    def deepest(s: Scenario) -> float:
        return s.som if s.som is not None else s.sam if s.sam is not None else s.tam

    rows: list[SensitivityRow] = []
    for q in inputs:
        if q.lo == q.hi:
            continue

        def only(end: Pick, q: Quantity = q) -> Pick:
            return lambda x: end(x) if x is q else x.base

        at_low = deepest(_levels(tam_of(only(_lo)), filters, share, only(_lo)))
        at_high = deepest(_levels(tam_of(only(_hi)), filters, share, only(_hi)))
        rows.append(SensitivityRow(input=q.name, som_at_low=at_low, som_at_high=at_high,
                                   swing=round(abs(at_high - at_low), 2)))  # fmt: skip
    rows.sort(key=lambda r: -r.swing)
    return low, base, high, rows


def top_down(
    spec: TopDownInput,
    req: SizingRequest,
    corroborated: Callable[[list[str]], bool],
    known: set[str] | None = None,
) -> SizingResult:
    _check_currency(spec.market_total, req.currency)
    _check_filters(spec.sam_filters, spec.obtainable_share)
    inputs = [spec.market_total, *spec.sam_filters,
              *([spec.obtainable_share] if spec.obtainable_share else [])]  # fmt: skip
    low, base, high, rows = _scenarios(lambda pick: pick(spec.market_total), spec.sam_filters,
                                       spec.obtainable_share, inputs)  # fmt: skip
    formula = [f"TAM = {spec.market_total.name} = {_fmt(base.tam, req.currency)}"]
    return _result("top_down", req, inputs, formula, spec.sam_filters, spec.obtainable_share,
                   low, base, high, rows, corroborated, known)  # fmt: skip


def bottom_up(
    spec: BottomUpInput,
    req: SizingRequest,
    corroborated: Callable[[list[str]], bool],
    known: set[str] | None = None,
) -> SizingResult:
    _check_currency(spec.price_per_unit, req.currency)
    period_notes = _annual_price(spec.price_per_unit)
    _check_filters(spec.sam_filters, spec.penetration)
    for q in (spec.customers, spec.units_per_customer):
        if q.unit.split("/")[0].upper() == req.currency:
            raise SizingError(f"{q.name}: a count, not money (price goes in price_per_unit)")
    inputs = [spec.customers, spec.units_per_customer, spec.price_per_unit, *spec.sam_filters,
              *([spec.penetration] if spec.penetration else [])]  # fmt: skip

    def tam(pick: Pick) -> float:
        return pick(spec.customers) * pick(spec.units_per_customer) * pick(spec.price_per_unit)

    low, base, high, rows = _scenarios(tam, spec.sam_filters, spec.penetration, inputs)
    formula = [
        f"TAM = {spec.customers.name} ({spec.customers.base:,.0f} {spec.customers.unit}) x "
        f"{spec.units_per_customer.name} ({spec.units_per_customer.base:g} "
        f"{spec.units_per_customer.unit}) x {spec.price_per_unit.name} "
        f"({spec.price_per_unit.base:,.2f} {spec.price_per_unit.unit}) = "
        f"{_fmt(base.tam, req.currency)} per year"
    ]
    result = _result("bottom_up", req, inputs, formula, spec.sam_filters, spec.penetration,
                     low, base, high, rows, corroborated, known)  # fmt: skip
    result.warnings[:0] = period_notes
    return result


def _result(
    method: str,
    req: SizingRequest,
    inputs: list[Quantity],
    formula: list[str],
    filters: list[Quantity],
    share: Quantity | None,
    low: Scenario,
    base: Scenario,
    high: Scenario,
    rows: list[SensitivityRow],
    corroborated: Callable[[list[str]], bool],
    known: set[str] | None,
) -> SizingResult:
    warnings: list[str] = []
    # In a research run (``known`` = its evidence ids) a sourced input must cite that evidence.
    unknown = [q.name for q in inputs if known is not None and set(q.evidence_ids) - known]
    if unknown:
        warnings.append("cites evidence not collected in this run: " + ", ".join(unknown))
    if filters and base.sam is not None:
        parts = " x ".join(f"{f.name} ({f.base:g})" for f in filters)
        formula.append(f"SAM = TAM x {parts} = {_fmt(base.sam, req.currency)}")
    else:
        warnings.append("SAM not calculated: no SAM filter (geography, segment, capability) given")
    if share is not None and base.som is not None:
        formula.append(f"SOM = SAM x {share.name} ({share.base:g}) = "
                       f"{_fmt(base.som, req.currency)}")  # fmt: skip
    else:
        warnings.append("SOM not calculated: needs SAM and an obtainable share / penetration")
    assumed = [q.name for q in inputs if q.basis is not Basis.SOURCED]
    weak = [q.name for q in inputs if q.basis is Basis.SOURCED and not corroborated(q.evidence_ids)]
    if assumed:
        warnings.append("rests on assumptions: " + ", ".join(assumed))
    if weak:
        warnings.append("sourced but not independently corroborated: " + ", ".join(weak))
    return SizingResult(
        method=method, currency=req.currency, year=req.year, geography=req.geography,
        segment=req.segment, formula=formula, inputs=inputs, low=low, base=base, high=high,
        sensitivity=rows, verified=not assumed and not weak and not unknown, warnings=warnings,
    )  # fmt: skip


def target_scope(req: SizingRequest) -> tuple[str, str | None]:
    """(geography, segment) a collected total must state: the requested geography
    (Global when none is requested) and the requested segment (None = whole market)."""
    return canonical_geography(req.geography) or GLOBAL, canonical_segment(req.segment)


def _exclusion(m: MetricSummary, req: SizingRequest, geo: str, seg: str | None) -> str | None:
    """Why a market-size summary cannot be the top-down total; None if it can."""
    if m.unit != req.currency:
        return f"currency {m.unit}, sizing in {req.currency} (no conversion is made)"
    if m.year is None:
        return "no year stated"
    if m.year > req.year:
        return f"for {m.year}, after the sizing year {req.year}"
    if m.measure not in (None, "market"):
        return f"{m.measure} figure, not a market value"
    if m.off_subject:
        return "names another market"
    if m.subset:
        return "part of a larger figure; its own scope is not stated"
    defined = set(re.findall(r"[a-z0-9]+", req.market_definition.lower()))
    if m.qualifier and not set(m.qualifier.split()) <= defined:
        return (
            f"the market is qualified as {m.qualifier!r}: possibly narrower or different "
            "than the sizing's market definition"
        )
    if m.ambiguous:
        return "one source states several values for this scope"
    if m.segment != seg:
        return (f"{m.segment} segment: part of the market, not all of it" if seg is None
                else f"{m.segment or 'whole market'}, not the {seg} segment")  # fmt: skip
    if m.geography is None:
        return (
            None
            if req.geography is None
            else (f"geography not stated: it may be global or another region than {geo}")
        )
    if m.geography != geo:
        return f"geography {m.geography}, not {geo}"
    return None


def evidence_total(
    metrics: list[MetricSummary], req: SizingRequest
) -> tuple[Quantity | None, list[str], list[ExcludedFigure]]:
    """A top-down total from collected market-size figures of the target scope
    (``target_scope``) in the request's currency: the year nearest ``req.year``
    (not after it), range = the figures' spread, base = its midpoint.

    Figures of another geography, segment, measure or market, parts of larger
    figures and ambiguous ones are never mixed in: each is returned in the
    exclusions with its reason. A figure whose geography is not stated is
    used only when no geography was requested and no figure states "Global",
    and the total is then marked unverified."""
    geo, seg = target_scope(req)
    sizes = [m for m in metrics if m.kind is MetricKind.MARKET_SIZE]
    reasons = {id(m): _exclusion(m, req, geo, seg) for m in sizes}
    eligible = [m for m in sizes if reasons[id(m)] is None]
    stated = [m for m in eligible if m.geography is not None]
    pool = stated or eligible
    best = max(pool, key=lambda m: (m.year or 0, m.independent_domains)) if pool else None
    excluded = [
        ExcludedFigure(
            figure=_describe(m), evidence_ids=m.evidence_ids,
            reason=reasons[id(m)] or f"not selected: the {best.year if best else '-'} figure "
                                     f"stating {best.geography if best else '-'} was used",
        )
        for m in sizes if m is not best
    ]  # fmt: skip
    if best is None:
        return None, [], excluded
    notes = [
        f"market total taken from {len(best.evidence_ids)} collected figure(s) for "
        f"{best.year}; their market definitions may differ from: {req.market_definition!r}"
    ]
    unstated = best.geography is None
    if unstated:
        notes.append(f"no collected figure states {geo}; using figures whose geography is not "
                     "stated (they may describe one region)")  # fmt: skip
    if best.year is not None and best.year < req.year:
        notes.append(f"the figure is for {best.year}, the sizing year is {req.year}: not "
                     "adjusted for growth")  # fmt: skip
    if best.confidence is not Confidence.HIGH or best.contradicted or unstated:
        notes.append("the collected total is not independently verified "
                     f"(confidence {best.confidence.value}"
                     f"{', contradicted' if best.contradicted else ''})")  # fmt: skip
    q = Quantity(
        name="market_total_from_evidence", base=(best.low + best.high) / 2, low=best.low,
        high=best.high, unit=req.currency, basis=Basis.SOURCED, evidence_ids=best.evidence_ids,
    )  # fmt: skip
    return q, notes, excluded


def _describe(m: MetricSummary) -> str:
    lo, hi = _fmt(m.low, m.unit), _fmt(m.high, m.unit)
    scope = scope_label(
        m.geography, m.segment, m.measure, m.subset, m.off_subject, qualifier=m.qualifier
    )
    return f"{lo if lo == hi else f'{lo} to {hi}'} for {m.year} ({scope})"


def size_market(
    req: SizingRequest,
    metrics: list[MetricSummary],
    corroborated: Callable[[list[str]], bool],
    known: set[str] | None = None,
) -> SizingReport:
    """``known``: the run's evidence ids; sourced inputs citing others are flagged (None:
    no run to check against, e.g. ``POST /v1/market/sizing``)."""
    report = SizingReport(market_definition=req.market_definition)
    td = req.top_down
    notes: list[str] = []
    if td is None and req.use_evidence_total:
        geo, seg = target_scope(req)
        report.target_scope = f"{geo}{f', {seg} segment' if seg else ''}, {req.currency}, " \
                              f"{req.year} or earlier"  # fmt: skip
        total, notes, report.excluded = evidence_total(metrics, req)
        td = TopDownInput(market_total=total) if total is not None else None
    if td is None:
        report.insufficient.append(
            "top_down: no inputs, and no collected market-size figure matches "
            f"{report.target_scope or 'the sizing scope'}"
            + (f" ({len(report.excluded)} excluded: see 'excluded')" if report.excluded else "")
        )
    else:
        try:
            result = top_down(td, req, corroborated, known)
            result.warnings[:0] = notes
            result.verified = result.verified and not notes[1:]
            report.results.append(result)
        except SizingError as exc:
            report.insufficient.append(f"top_down: {exc}")
    if req.bottom_up is None:
        report.insufficient.append("bottom_up: no inputs")
    else:
        try:
            report.results.append(bottom_up(req.bottom_up, req, corroborated, known))
        except SizingError as exc:
            report.insufficient.append(f"bottom_up: {exc}")
    if len(report.results) == 2:
        a, b = (r.base.tam for r in report.results)
        ratio = max(a, b) / min(a, b) if min(a, b) > 0 else math.inf
        report.cross_check = (
            f"top-down and bottom-up TAM differ by {ratio:.1f}x"
            + (": the market definitions or inputs disagree; review before use"
               if ratio > CROSS_CHECK_FACTOR else ": consistent within a factor of 2")
        )  # fmt: skip
    return report
