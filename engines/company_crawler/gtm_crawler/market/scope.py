"""The market a stated figure describes: geography, segment, measure (audit M7).

A page often states several markets at once ("the global market was A in
2023, with North America accounting for B"). Each market figure of a
sentence (sizes, forecasts, growth rates) gets the scope its own clause
states, read from that sentence only, never from the page title or another
sentence:

* geography: the place its clause names (a gazetteer of regions and major
  countries, plus the requested geography); several places = all of them,
  joined (never one picked); none = not stated;
* segment: a customer, deployment or vertical qualifier of the market noun
  ("the CRM market for SMBs", "the cloud CRM market"); words that are part of
  the research subject are not segments;
* measure: a market value, spending, or a revenue that names no market (a
  company's);
* the market phrase, and whether it names another market than the subject
  (none of its words is a subject term);
* subset: stated as a part of a larger figure ("of which", "accounted for")
  without a scope of its own.

A figure whose clause names nothing continues the previous figure of the
same sentence ("valued at A in 2023 and expected to reach B by 2030": basis
``carried``), unless its clause introduces a market of its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .models import Metric, ScopeBasis

GLOBAL = "Global"

# Case-insensitive regex variants per canonical place.
_PLACES: dict[str, tuple[str, ...]] = {
    GLOBAL: ("global", "worldwide", r"world(?!-class| class)"),
    "North America": ("north america", "north american"),
    "Latin America": ("latin america", "latin american"),
    "South America": ("south america", "south american"),
    "Americas": ("the americas",),
    "Europe": ("europe", "european"),
    "European Union": ("european union",),
    "Western Europe": ("western europe",),
    "Eastern Europe": ("eastern europe",),
    "Asia Pacific": ("asia pacific", "asia-pacific"),
    "Asia": ("asia", "asian"),
    "Southeast Asia": ("southeast asia", "south-east asia", "south east asia"),
    "Middle East and Africa": ("middle east and africa", "middle east & africa"),
    "Middle East": ("middle east",),
    "Africa": ("africa", "african"),
    "United States": ("united states", "united states of america"),
    "Canada": ("canada", "canadian"),
    "Mexico": ("mexico", "mexican"),
    "Brazil": ("brazil", "brazilian"),
    "United Kingdom": ("united kingdom", "britain", "great britain", "british"),
    "Germany": ("germany", "german"),
    "France": ("france", "french"),
    "Italy": ("italy", "italian"),
    "Spain": ("spain", "spanish"),
    "Netherlands": ("netherlands", "the netherlands", "dutch"),
    "Sweden": ("sweden", "swedish"),
    "Switzerland": ("switzerland", "swiss"),
    "Russia": ("russia", "russian"),
    "India": ("india", "indian"),
    "China": ("china", "chinese"),
    "Japan": ("japan", "japanese"),
    "South Korea": ("south korea", "south korean", "korea", "korean"),
    "Australia": ("australia", "australian"),
    "Singapore": ("singapore", "singaporean"),
    "Indonesia": ("indonesia", "indonesian"),
    "Vietnam": ("vietnam", "vietnamese"),
    "Philippines": ("philippines", "filipino"),
    "Saudi Arabia": ("saudi arabia", "saudi"),
    "United Arab Emirates": ("united arab emirates", "emirati"),
    "South Africa": ("south africa", "south african"),
    "Nigeria": ("nigeria", "nigerian"),
}  # fmt: skip
# Abbreviations, case-sensitive ("US" is a place, "us" is not; "US$" is a currency).
_ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "United States": (r"U\.S\.(?:A\.)?", "USA", "US"),
    "United Kingdom": (r"U\.K\.", "UK"),
    "United Arab Emirates": ("UAE",),
    "European Union": ("EU",),
    "Asia Pacific": ("APAC",),
    "EMEA": ("EMEA",),
    "Middle East and Africa": ("MEA",),
    "Latin America": ("LATAM", "LatAm"),
}
_SEGMENTS: dict[str, tuple[str, ...]] = {
    "SMB": (r"small and medium[- ]sized (?:businesses|enterprises|companies)",
            r"small and medium (?:businesses|enterprises)",
            r"small (?:and mid-?sized? )?business(?:es)?",
            "smbs?", "smes?", "msmes?"),
    "Mid-market": ("mid-?market", r"mid-?sized? (?:businesses|companies|enterprises)"),
    "Large enterprise": ("large enterprises?", r"large (?:businesses|companies|organi[sz]ations)",
                         "enterprise segment"),
    "Cloud": ("cloud(?:-based)?", "saas", "software[- ]as[- ]a[- ]service"),
    "On-premise": ("on-?premises?", "on-?prem"),
    "Consumer": ("consumers?", "b2c", "households?", "residential"),
    "B2B": ("b2b",),
    "Healthcare": ("health ?care", "hospitals?"),
    "Financial services": ("bfsi", "banking", "financial services", "insurance"),
    "Retail": ("retail", "e-?commerce"),
    "Manufacturing": ("manufacturing",),
    "Education": ("education(?:al)?",),
    "Government": ("government", "public sector"),
    "Real estate": ("real estate",),
    "Automotive": ("automotive",),
    "Telecom": ("telecom(?:munications?)?",),
}  # fmt: skip


def _table(table: dict[str, tuple[str, ...]], flags: int) -> tuple[re.Pattern[str], list[str]]:
    """One regex with a group per canonical name; longer variants first, so
    "Middle East and Africa" wins over "Middle East" and "Africa"."""
    items = sorted(table.items(), key=lambda kv: -max(len(v) for v in kv[1]))
    parts = [f"(?P<g{i}>{'|'.join(sorted(v, key=len, reverse=True))})"
             for i, (_, v) in enumerate(items)]  # fmt: skip
    return re.compile(rf"(?<![\w$])(?:{'|'.join(parts)})(?![\w$])", flags), [k for k, _ in items]


_PLACE_RX, _PLACE_NAMES = _table(_PLACES, re.I)
_ABBR_RX, _ABBR_NAMES = _table(_ABBREVIATIONS, 0)
_SEGMENT_RX, _SEGMENT_NAMES = _table(_SEGMENTS, re.I)

_NOUN = r"(?:markets?|industry|industries|sectors?|segments?)"
_VERB = (r"(?:is|was|were|are|has|have|had|will|stood|reached|grew|valued|accounted|"
         r"generated|expected|projected|estimated|totalled|totaled|amounted)\b")  # fmt: skip
# A market phrase: up to six words, the noun, and an optional "for / among / in ..." tail.
_PHRASE = re.compile(
    rf"(?:(?!{_NOUN}\b)[\w&'’/+.-]+\s+){{0,6}}{_NOUN}\b"
    rf"(?:\s+(?:for|among|across|in|within)\s+(?:the\s+)?(?!{_VERB})[\w&/.-]+"
    rf"(?:\s+(?!{_VERB})[\w&/.-]+){{0,2}})?",
    re.I,
)  # fmt: skip
# The figure followed by its own market phrase: "a USD 70 billion global CRM market".
_AFTER_PHRASE = re.compile(rf"\s+(?:[\w&/-]+\s+){{0,4}}?{_NOUN}\b", re.I)
# A place attached after the figure: "USD 3 billion (in 2023) in Europe", "... came from Asia".
_ATTACH = re.compile(
    r"\s*(?:(?:in|by)\s+\d{4}\s*,?\s*)?(?:(?:came|comes|coming|was|were|generated)\s+)?"
    r"(?:in|across|within|for|from)\s+(?:the\s+)?",
    re.I,
)
_SUBSET = re.compile(
    r"\b(?:account(?:ed|s|ing)? for|of which|of this|of that|share of|contribut\w+|captured|"
    r"held|generated by|came from|comes from|made up)\b",
    re.I,
)
_SPEND = re.compile(r"\bspend\w*\b", re.I)
_INVEST = re.compile(r"\b(?:invest\w*|capex|capital expenditure)\b", re.I)
_REVENUE = re.compile(r"\b(?:revenues?|sales|turnover)\b", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9&+-]*")
# The head of a market phrase starts after its last determiner ("we estimate that the
# market" -> "market"; "the India CRM software market" -> "India CRM software market").
_DETERMINER = re.compile(r"\b(?:the|a|an|this|that|these|those|its|their|our|which|of|"
                         r"in|for|and|or|while|whereas)\b", re.I)  # fmt: skip
# Words that describe a market without defining it.
_GENERIC = frozenset(
    "the a an this that these its their our overall total entire whole size value share "
    "market markets industry industries sector sectors segment segments of in for among "
    "across within s global regional domestic local international national worldwide "
    "fastest fast growing largest large biggest leading key major emerging mature booming "
    "rapidly expanding current estimated projected expected forecast addressable broader "
    "wider combined related respective and or "
    # Category words (as in extract._STOPWORDS): "CRM software", "CRM platforms", "CRM tools".
    "software solution solutions service services platform platforms product products tool "
    "tools system systems app apps technology technologies company companies business "
    "businesses".split()
)


def _found(rx: re.Pattern[str], names: list[str], text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), names[int(next(k for k, v in m.groupdict().items()
                                                 if v is not None)[1:])])
            for m in rx.finditer(text)]  # fmt: skip


def places(text: str, requested: str | None = None) -> list[tuple[int, int, str]]:
    """(start, end, canonical place) mentions in ``text``, in order."""
    found = _found(_PLACE_RX, _PLACE_NAMES, text) + _found(_ABBR_RX, _ABBR_NAMES, text)
    if requested and canonical_geography(requested) == requested.strip():
        # A requested place the gazetteer does not know ("Maharashtra") is matched as written.
        found += [
            (m.start(), m.end(), requested.strip())
            for m in re.finditer(rf"(?<!\w){re.escape(requested.strip())}(?!\w)", text, re.I)
        ]
    found.sort(key=lambda f: (f[0], -f[1]))
    out: list[tuple[int, int, str]] = []
    for f in found:  # overlaps: the earlier, longer mention wins
        if not out or f[0] >= out[-1][1]:
            out.append(f)
    return out


def canonical_geography(name: str | None) -> str | None:
    """The gazetteer's name for a requested geography ("india" -> "India"), or the text."""
    if not name or not name.strip():
        return None
    text = name.strip()
    hits = _found(_PLACE_RX, _PLACE_NAMES, text) + _found(_ABBR_RX, _ABBR_NAMES, text)
    whole = [h for h in hits if h[0] == 0 and h[1] == len(text)]
    return whole[0][2] if whole else text


def canonical_segment(name: str | None, terms: Iterable[str] = ()) -> str | None:
    """The segment label(s) a requested segment names ("small businesses" -> "SMB")."""
    if not name or not name.strip():
        return None
    return _segment_of(name, set(terms)) or name.strip()


def _segment_of(text: str, terms: set[str]) -> str | None:
    labels = {name for start, end, name in _found(_SEGMENT_RX, _SEGMENT_NAMES, text)
              if not set(_WORD.findall(text[start:end].lower())) & terms}  # fmt: skip
    return " + ".join(sorted(labels)) or None


def _geography_of(mentions: Sequence[tuple[int, int, str]]) -> str | None:
    names = list(dict.fromkeys(name for _, _, name in mentions))
    return ", ".join(sorted(names)) if names else None


def acronyms(text: str) -> set[str]:
    """Initials of consecutive words: "electric vehicle" -> "ev"; "customer relationship
    management" -> "crm"."""
    words = _WORD.findall(text.lower())
    return {"".join(w[0] for w in words[i:j]) for i in range(len(words))
            for j in range(i + 2, min(i + 4, len(words)) + 1)}  # fmt: skip


def definition(
    phrase: str, terms: set[str], vocabulary: set[str], requested: str | None
) -> tuple[bool, str | None]:
    """(names another market, qualifier): the head of the market phrase without places,
    segments and generic words; another market when none of its words is a subject
    term; a qualifier ("marketing services", "seafood") when it adds words the subject
    vocabulary does not have, so it may be a narrower or different market."""
    noun = re.search(rf"{_NOUN}\b", phrase, re.I)
    head = phrase[: noun.start()] if noun else phrase
    cut = list(_DETERMINER.finditer(head))
    head = head[cut[-1].end() :] if cut else head
    for start, end, _ in reversed(places(head, requested)):
        head = head[:start] + " " + head[end:]
    for start, end, _ in reversed(_found(_SEGMENT_RX, _SEGMENT_NAMES, head)):
        head = head[:start] + " " + head[end:]
    words = [w for w in _WORD.findall(head.lower()) if w not in _GENERIC and not w.isdigit()]
    if not words or not terms:
        return False, None
    if not set(words) & terms:
        return True, None
    extra = [w for w in words if w not in vocabulary and w.rstrip("s") not in vocabulary]
    return False, " ".join(dict.fromkeys(extra)) or None


def _measure(before: str, window: str, tail: str, terms: set[str]) -> str | None:
    """What the figure measures, if its clause says: spending, investment, a market
    value, or a revenue that names no market (a company's)."""
    if _SPEND.search(before):
        return "spending"
    if _INVEST.search(before):
        return "investment"
    revenue = list(_REVENUE.finditer(tail if window else before))
    if window:
        if not revenue:
            return "market"
        # "the market generated a revenue of" vs "in the CRM market, Acme's revenue was"
        between = tail[: revenue[-1].start()]
        return "revenue" if re.search(r"[,;]|['’]s\b", between) else "market"
    if revenue:
        lead = _WORD.findall(before[: revenue[-1].start()].lower())[-4:]
        named = bool(set(lead) & terms) and not re.search(
            r"['’]s\s*$", before[: revenue[-1].start()]
        )
        return "market" if named else "revenue"  # "CRM revenue" vs "Salesforce revenue"
    return None


def assign(
    sentence: str,
    figures: Sequence[tuple[int, int, Metric]],
    terms: set[str],
    requested: str | None = None,
    vocabulary: set[str] | None = None,
) -> None:
    """Set the scope of each market figure of ``sentence`` (in sentence order).

    ``terms``: the subject's distinctive words; ``vocabulary``: every word that may
    name the subject market (subject, industry, their acronyms; default ``terms``)."""
    vocabulary = (vocabulary or set()) | terms
    prev: Metric | None = None
    start_at = 0
    for i, (start, end, metric) in enumerate(figures):
        nxt = figures[i + 1][0] if i + 1 < len(figures) else len(sentence)
        before, after = sentence[start_at:start], sentence[end:nxt]
        consumed = 0
        attached: str | None = None
        if (m := _ATTACH.match(after)) is not None:
            hit = [p for p in places(after[m.end() :], requested) if p[0] == 0]
            if hit:
                attached, consumed = hit[0][2], m.end() + hit[0][1]
        # The figure's own market phrase: right after it, else the last one before it.
        window, tail = "", ""
        if (a := _AFTER_PHRASE.match(after)) is not None:
            window = after[: a.end()]
        elif phrases := [p for p in _PHRASE.finditer(before) if p.group(0).strip()]:
            window, tail = phrases[-1].group(0), before[phrases[-1].end() :]
        own = bool(window)
        markers = list(_SUBSET.finditer(before))
        marker = markers[-1] if markers else None
        geography = (attached or _geography_of(places(window, requested))
                     or _geography_of(places(before, requested)[-1:]))  # fmt: skip
        # A segment named after the market phrase is the figure's own ("... share of the
        # market, while SMEs are expected to grow at"), else the phrase's qualifier.
        segment = (_segment_of(tail, terms) or _segment_of(window, terms)) if own else None
        if segment is None and marker is not None:  # "of which cloud accounted for ..."
            segment = _segment_of(before[: marker.start()], terms)
        metric.subset = marker is not None and geography is None and segment is None
        metric.scope_basis = ScopeBasis.STATED if geography else ScopeBasis.NOT_STATED
        continuation = prev if prev is not None and not own else None
        if continuation is not None:
            if geography is None and continuation.geography is not None:
                geography, metric.scope_basis = continuation.geography, ScopeBasis.CARRIED
            segment = segment or (None if metric.subset else continuation.segment)
        metric.geography, metric.segment = geography, segment
        metric.measure = _measure(before, window, tail, terms) or (
            continuation.measure if continuation is not None and continuation.measure
            else "market")  # fmt: skip
        if own:
            metric.market_phrase = " ".join(window.split())[:200]
            metric.off_subject, metric.qualifier = definition(window, terms, vocabulary,
                                                               requested)  # fmt: skip
        elif continuation is not None:
            metric.market_phrase = continuation.market_phrase
            metric.off_subject = continuation.off_subject
            metric.qualifier = continuation.qualifier
        elif metric.measure in ("spending", "investment") and terms:
            # "spending on cloud IT infrastructure reached ...": what is spent on is named.
            metric.off_subject = not set(_WORD.findall(before.lower())) & terms
        prev, start_at = metric, end + consumed
