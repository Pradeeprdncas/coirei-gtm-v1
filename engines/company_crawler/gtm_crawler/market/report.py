"""A report as Markdown: every finding with its confidence, basis and numbered sources.

The JSON report (``models.Report``) is the record; this is a readable export.
Source text is quoted, never interpreted as markup (``|``, ``*``, ``<`` escaped).
"""

from __future__ import annotations

import re

from .assessment import _fmt
from .models import Finding, Report

_ESCAPE = re.compile(r"([\\`*_\[\]<>|#])")


def _md(text: str) -> str:
    return _ESCAPE.sub(r"\\\1", " ".join(text.split()))


def to_markdown(report: Report) -> str:
    index = report.evidence_index()
    numbers: dict[str, int] = {}
    sources = {s.source_id: s for s in report.sources}

    def cite(f: Finding) -> str:
        refs = []
        for eid in f.evidence_ids:
            e = index.get(eid)
            if e is None:
                continue
            n = numbers.setdefault(e.source_id, len(numbers) + 1)
            # A PDF quote is cited with its page ("[3, p. 12]").
            ref = f"[{n}, p. {e.locator.page}]" if e.locator and e.locator.page else f"[{n}]"
            if ref not in refs:
                refs.append(ref)
        tag = f"_{f.confidence.value}, {f.basis.value}_"
        return f"- {_md(f.statement)} {''.join(refs)} {tag}".rstrip()

    lines = [f"# {_md(report.subject)}: {report.mode.value} market research", "",
             f"Run `{report.research_id}` · status **{report.status.value}** · generated "
             f"{report.generated_at:%Y-%m-%d %H:%M} UTC · {report.analysis_version}", "",
             "## Executive summary", ""]  # fmt: skip
    lines += [cite(f) for f in report.executive_summary]
    for section in report.sections.values():
        lines += ["", f"## {_md(section.title)}", ""]
        lines += [cite(f) for f in section.findings] or ["_No findings._"]
        lines += [f"- Gap: {_md(g)}" for g in section.gaps]
    if report.contradictions:
        lines += ["", "## Conflicting evidence", ""]
        for c in report.contradictions:
            lines.append(f"- **{_md(c.topic)}**: {', '.join(_md(v) for v in c.values)}. "
                         f"{_md(c.explanation)} Possible causes: "
                         f"{_md('; '.join(c.possible_causes))}.")  # fmt: skip
    if report.sizing:
        lines += ["", "## Market sizing", "", f"Definition: {_md(report.sizing.market_definition)}"]
        for r in report.sizing.results:
            lines += ["", f"### {r.method.replace('_', '-')} ({r.currency}, {r.year}"
                      f"{', ' + _md(r.geography) if r.geography else ''})"
                      f" {'- verified' if r.verified else '- not verified'}", ""]  # fmt: skip
            lines += [f"- `{_md(f)}`" for f in r.formula]
            lines.append("")
            lines.append("| Scenario | TAM | SAM | SOM |")
            lines.append("|---|---|---|---|")
            for name, level in (("low", r.low), ("base", r.base), ("high", r.high)):
                cells = [_fmt(v, r.currency) if v is not None else "n/a"
                         for v in (level.tam, level.sam, level.som)]  # fmt: skip
                lines.append(f"| {name} | {' | '.join(cells)} |")
            lines.append("")
            lines += [f"- Input {_md(q.name)}: {q.base:g} {_md(q.unit)} (range {q.lo:g}-{q.hi:g}; "
                      f"{q.basis.value}{'; ' + _md(q.rationale) if q.rationale else ''})"
                      for q in r.inputs]  # fmt: skip
            lines += [f"- Sensitivity: {_md(row.input)} swings the result by "
                      f"{_fmt(row.swing, r.currency)}" for row in r.sensitivity[:5]]  # fmt: skip
            lines += [f"- Warning: {_md(w)}" for w in r.warnings]
        if report.sizing.cross_check:
            lines += ["", f"Cross-check: {_md(report.sizing.cross_check)}"]
        lines += [f"- Not calculated: {_md(i)}" for i in report.sizing.insufficient]
        if report.sizing.target_scope:
            lines += ["", f"Collected totals considered for: {_md(report.sizing.target_scope)}"]
        lines += [f"- Excluded {_md(x.figure)}: {_md(x.reason)}" for x in report.sizing.excluded]
    if report.scoring:
        sc = report.scoring
        lines += [
            "",
            "## Opportunity scores",
            "",
            _md(sc.interpretation),
            "",
            "| Candidate | Score | Raw | Coverage | Rank |",
            "|---|---|---|---|---|",
        ]
        for cand in sc.candidates:
            score = cand.score if cand.score is not None else "n/a"
            raw = cand.raw_score if cand.raw_score is not None else "n/a"
            lines.append(f"| {_md(cand.candidate)} | {score} | {raw} | {cand.coverage:.0%} | "
                         f"{cand.rank or '-'} |")  # fmt: skip
        lines.append("")
        for cand in sc.candidates:
            # What each score rests on, and what it does not: a number is not confidence.
            unrated = [d.dimension for d in cand.dimensions if d.rating is None]
            if cand.score is not None and unrated:
                lines.append(f"- {_md(cand.candidate)}: provisional ({cand.coverage:.0%} of the "
                             f"weight rated); not rated: {', '.join(unrated)}")  # fmt: skip
            rated = "; ".join(f"{d.dimension} {d.rating:g} ({d.basis}: {d.rationale})"
                              for d in cand.dimensions if d.rating is not None)  # fmt: skip
            if rated:
                lines.append(f"- {_md(cand.candidate)} ratings: {_md(rated)}")
        lines += [f"- {_md(n)}" for n in sc.sensitivity]
    for title, items in (("Open questions and gaps", report.gaps),
                         ("Assumptions", report.assumptions),
                         ("Limitations", report.limitations)):  # fmt: skip
        if items:
            lines += ["", f"## {title}", ""] + [f"- {_md(i)}" for i in items]
    if report.issues:
        lines += ["", "## Run issues", ""]
        lines += [f"- {i.severity}: {i.stage}: {_md(i.message)}" for i in report.issues]
    lines += ["", "## Sources", ""]
    for source_id, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        s = sources.get(source_id)
        if s is None:
            continue
        when = f", published {s.published_at:%Y-%m-%d}" if s.published_at else ""
        quality = f", quality {s.quality.score:.2f}" if s.quality else ""
        lines.append(f"{n}. {_md(s.title or s.domain)} - <{s.url}> ({s.source_type.value}, "
                     f"tier {s.tier}{when}{quality})")  # fmt: skip
    return "\n".join(lines) + "\n"
