"""Which source to collect next: the research questions still open decide.

Search ranking, source selection, extraction success and evidence quality
are four different things: a URL's search position is one input here; what
is selected is decided before a page is fetched, from what is known about it
then (its search title, publication date, URL, the queries and questions that
found it, its publisher class, its type); what extraction yields and how good
the evidence is are judged after collection (``engine``, ``assessment``). No
page's contents are assumed before it is read.

``value(candidate) = need_fit x quality``:

* ``need_fit``: the open questions the candidate may answer, each weighted
  by how open it is (independent domains with evidence for it: none 1.0, one
  0.5, two 0.2, three or more 0: answered and corroborated); a requested
  sizing without an eligible total yet adds 1.0 to the size question. The
  fit is the best question's weight x match plus ``BREADTH`` (0.05) of each
  other one: a question matches 1.0 when one of the candidate's queries was
  written for it and its title shows that question's cues ("market size",
  "pricing", "alternatives"...), 0.6 by the query alone, 0.5 by the title
  alone (a page every query returns is usually general, not an answer to
  each). Capped at 2.
* ``quality`` (0-1): relevance of the title and URL to the subject (0.35),
  requested scope named or another place named (0.15), publisher authority
  (0.15), recency (0.10), search rank and agreement across queries (0.15),
  cost (0.10: a PDF costs more and an earlier failure on the same domain
  makes another attempt there less likely to work). A title that names none
  of the subject's terms divides quality by three.

Collection runs in waves (``WAVE`` candidates at a time); each wave is picked
greedily, halving the need of the questions a pick targets so one wave does
not spend itself on one question. After each wave the evidence collected
updates the needs. Collection stops when budgets are used or the best
remaining value is under ``MIN_VALUE`` (little left to learn); every
candidate not collected keeps its last value and the reason.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urlsplit

from .models import EvidenceRecord, SourceRecord, SourceSelection
from .plan import Plan, Question
from .scope import GLOBAL, canonical_geography, places

WAVE = 4
MIN_VALUE = 0.08
# Need of a question by the independent domains with evidence for it: 0, 1, 2, 3+.
NEED_BY_DOMAINS = (1.0, 0.5, 0.2, 0.0)
TOTAL_NEED = 1.0
BREADTH = 0.05
AUTHORITY = {1: 1.0, 2: 0.85, 3: 0.75, 4: 0.6}
WEIGHTS = {"relevance": 0.35, "scope": 0.15, "authority": 0.15, "recency": 0.10,
           "rank": 0.15, "cost": 0.10}  # fmt: skip
PDF_URL = re.compile(r"\.pdf(?:$|[?#])", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9&+-]*")
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")
# Title cues per plan question key (industry and product plans).
CUES: dict[str, re.Pattern[str]] = {k: re.compile(v, re.I) for k, v in {
    "size": r"market size|market value|market share|outlook|forecast|cagr|billion|million|"
            r"statistic|market report|valued|revenue",
    "statistics": r"statistic|census|survey|data|indicator|release|bureau|national|official",
    "definition": r"what is|definition|overview|explained|guide|introduction|value chain",
    "demand": r"demand|adoption|survey|consumer|customer|driver|uptake|growth",
    "competition": r"companies|players|competitor|market share|leading|top \d+|vendors|landscape",
    "external": r"regulation|policy|law|directive|trend|technolog|mandate",
    "risks": r"challenge|barrier|risk|problem|obstacle",
    "business_models": r"business model|pricing|revenue model|distribution|channel",
    "alternatives": r"alternative|\bvs\b|versus|competitor|best|top \d+|review|compar",
    "pricing": r"pricing|price|plans?\b|cost",
    "workarounds": r"spreadsheet|manual|template|how to|workaround",
    "go_to_market": r"go-to-market|channel|sales|marketing|acquisition",
    "barriers": r"barrier|challenge|adoption|complian|risk",
}.items()}  # fmt: skip


@dataclass
class Candidate:
    """A URL search found, with what is known before fetching it."""

    url: str
    domain: str
    tier: int
    title: str | None = None
    published: str | None = None
    position: int = 0  # best position in any result list
    queries: list[str] = field(default_factory=list)  # query texts that found it
    questions: list[str] = field(default_factory=list)  # their plan question keys

    @property
    def is_pdf(self) -> bool:
        return bool(PDF_URL.search(self.url))


@dataclass
class Needs:
    """How open each plan question is, from the evidence collected so far."""

    plan: Plan
    total_missing: bool = False  # a requested sizing has no eligible total yet
    domains: dict[str, set[str]] = field(default_factory=dict)

    def update(self, evidence: Iterable[EvidenceRecord], sources: dict[str, SourceRecord],
               total_missing: bool) -> None:  # fmt: skip
        self.total_missing = total_missing
        for q in self.plan.questions:
            dims = set(q.dimensions)
            self.domains[q.key] = {
                sources[e.source_id].domain
                for e in evidence
                if e.source_id in sources and dims & set(e.dimensions)
            }

    def weight(self, q: Question) -> float:
        n = len(self.domains.get(q.key, ()))
        w = NEED_BY_DOMAINS[min(n, len(NEED_BY_DOMAINS) - 1)]
        return w + (TOTAL_NEED if self.total_missing and q.key == "size" else 0.0)

    def open_questions(self) -> list[Question]:
        return [q for q in self.plan.questions if not self.domains.get(q.key)]


def _words(text: str | None) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _url_words(url: str) -> set[str]:
    parts = urlsplit(url)
    return _words(re.sub(r"[-_/.+%]", " ", unquote(parts.netloc + " " + parts.path)))


def _year(c: Candidate) -> int | None:
    if c.published and (m := _YEAR.search(c.published)):
        return int(m.group(1))
    years = [int(y) for y in _YEAR.findall(f"{c.title or ''} {unquote(c.url)}")]
    return max(years) if years else None


def quality(c: Candidate, terms: set[str], geography: str | None, failed_domains: set[str],
            today: date) -> tuple[float, dict[str, float], list[str]]:  # fmt: skip
    """(quality 0-1, components, notes) of a candidate before fetching it."""
    notes: list[str] = []
    title_words = _words(c.title)
    words = title_words | _url_words(c.url)
    if not terms:
        relevance = 1.0
    elif c.title:
        relevance = len(terms & words) / len(terms)
    else:  # no title: the URL alone, and an unknown is not a miss
        relevance = max(0.5, len(terms & words) / len(terms))
    target = canonical_geography(geography) if geography else None
    named = {p[2] for p in places(f"{c.title or ''} {unquote(c.url)}", geography)}
    if target:
        scope = 1.0 if target in named else 0.3 if named - {GLOBAL} else 0.6
    else:
        scope = 0.9 if GLOBAL in named else 0.5 if named else 0.7
    if target and named and target not in named:
        notes.append(f"names {', '.join(sorted(named))}, not {target}")
    year = _year(c)
    age = None if year is None else today.year - year
    recency = 0.6 if age is None else 1.0 if age <= 1 else 0.8 if age <= 3 else 0.5 if age <= 6 \
        else 0.3  # fmt: skip
    rank = min(1.0, (1 / (1 + 0.3 * c.position)) * (1 + 0.15 * (len(c.queries) - 1)))
    cost = 0.7 if c.is_pdf else 1.0
    if c.domain in failed_domains:
        cost *= 0.3
        notes.append("an earlier request to this domain failed")
    parts = {"relevance": round(relevance, 3), "scope": scope, "authority": AUTHORITY[c.tier],
             "recency": recency, "rank": round(rank, 3), "cost": cost}  # fmt: skip
    q = sum(WEIGHTS[k] * v for k, v in parts.items())
    if c.title and terms and not terms & title_words:
        q /= 3
        notes.append("its title names none of the subject's terms")
    return q, parts, notes


def need_fit(c: Candidate, needs: Needs, extra: dict[str, float] | None = None) -> tuple[
        float, list[str]]:  # fmt: skip
    """(fit, targeted question keys): the open question the candidate most plausibly
    answers, plus a little (``BREADTH``) for each other one. Being returned for a
    question's query is a weaker sign (0.6) than its title showing that question's
    cues (1.0; 0.5 when no query of that question found it): a page every query
    returns is usually a general one, not an answer to each."""
    parts: list[float] = []
    targets: list[str] = []
    for q in needs.plan.questions:
        by_query = q.key in c.questions
        cue = bool(c.title and (rule := CUES.get(q.key)) and rule.search(c.title))
        match = 1.0 if by_query and cue else 0.6 if by_query else 0.5 if cue else 0.0
        if match:
            w = needs.weight(q) * (extra or {}).get(q.key, 1.0)
            if w > 0:
                parts.append(w * match)
                targets.append(q.key)
    if not parts:
        return 0.0, []
    parts.sort(reverse=True)
    return min(parts[0] + BREADTH * sum(parts[1:]), 2.0), targets


def evaluate(c: Candidate, needs: Needs, terms: set[str], geography: str | None,
             failed_domains: set[str], today: date,
             extra: dict[str, float] | None = None) -> SourceSelection:  # fmt: skip
    fit, targets = need_fit(c, needs, extra)
    q, parts, notes = quality(c, terms, geography, failed_domains, today)
    value = round(fit * q, 4)
    why = f"targets {', '.join(targets)}" if targets else "targets no open question"
    if notes:
        why += "; " + "; ".join(notes)
    return SourceSelection(value=value, need_fit=round(fit, 3), quality=round(q, 3),
                           components=parts, targets=targets, reason=why)  # fmt: skip


def pick_wave(
    ranked: list[tuple[Candidate, SourceSelection]],
    needs: Needs,
    allowed: Callable[[Candidate, list[Candidate]], bool],
    k: int,
) -> list[int]:
    """Indices of up to ``k`` candidates (``ranked`` best first; ``allowed(candidate,
    picked so far)`` applies the budgets), greedy: each pick halves the need of the
    questions it targets, so a wave spreads over the open questions."""
    extra: dict[str, float] = {}
    chosen: list[int] = []
    for _ in range(k):
        best, best_value = None, 0.0
        for i, (c, sel) in enumerate(ranked):
            if i in chosen or not allowed(c, [ranked[j][0] for j in chosen]):
                continue
            fit, _ = need_fit(c, needs, extra)
            v = fit * sel.quality
            if v >= MIN_VALUE and v > best_value:  # ties: the better-ranked one
                best, best_value = i, v
        if best is None:
            break
        chosen.append(best)
        for key in ranked[best][1].targets:
            extra[key] = extra.get(key, 1.0) * 0.5
    return chosen
