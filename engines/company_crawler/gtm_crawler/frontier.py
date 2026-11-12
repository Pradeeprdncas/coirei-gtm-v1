"""Crawl frontier admission for one site.

The frontier decides which URLs may be queued: deduplication, depth and
breadth budgets, crawler-trap heuristics and the same-site restriction. The
queue itself is durable: accepted entries are ``crawl_url`` rows, claimed by
priority through ``CrawlStore.claim`` (leases, ``FOR UPDATE SKIP LOCKED``).
The frontier tracks which URLs are still queued so callers can raise their
score as more inlinks are found, and ``restore`` rebuilds it after a restart.

Trap rules (spec section 13) are bounded counters, each named in the
rejection reason: over-long URLs, deep paths, too many parameters, repeated
segments (relative-link loops), per-shape budgets (calendars, pagination,
facets), per-path query budgets (sort/filter combinations), session ids left
in the URL, and low unique-content yield: a URL shape or section whose fetched
pages keep bringing nothing new (duplicates, redirects to known pages, error
pages) is closed, which also stops pagination loops, template duplication
and repeated redirects.

Translated copies of the site are not crawled: the seed's language is the
site's main one, and once a fetched page in another language sits under a
first path segment naming that language (``/fr/``, ``/de-de/``), every URL
under that segment, on any host of the site, is refused. Only a page's own
declared language teaches a segment, so an English ``/hr/`` section stays.

URLs must already be normalized with ``urls.normalize_url``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import SplitResult, parse_qsl, urlsplit

from .config import FrontierSettings
from .parse import primary_language
from .urls import registrable_domain

log = logging.getLogger(__name__)

_NUMERIC_SEGMENT = re.compile(r"[\d._-]*\d[\d._-]*")
_ID_SEGMENT = re.compile(r"(?=.*\d)(?:[0-9a-f]{8,}|[0-9a-f-]{32,36})", re.IGNORECASE)
# Session ids that normalization's strip_params did not remove: parameter names
# such as sid / sessid / session_id with a long random value, and ASP.NET
# cookieless sessions in the path: /(S(abcdef...))/.
_SESSION_KEY = re.compile(r"(?:^|[_-])(?:s_?id|sess(?:ion)?(?:_?id)?)$", re.IGNORECASE)
_SESSION_VALUE = re.compile(r"(?=.*\d)(?=.*[a-z])[a-z0-9_-]{16,}", re.IGNORECASE)
_ASPNET_SESSION = re.compile(r"\([A-Z]\([a-z0-9]{16,}\)\)", re.IGNORECASE)


class Rejection(StrEnum):
    DUPLICATE = "duplicate"
    EXTERNAL = "external"
    MAX_DEPTH = "max_depth"
    QUEUE_FULL = "queue_full"
    URL_TOO_LONG = "trap:url_too_long"
    PATH_TOO_DEEP = "trap:path_too_deep"
    TOO_MANY_PARAMS = "trap:too_many_params"
    REPEATED_SEGMENTS = "trap:repeated_segments"
    PATTERN_BUDGET = "trap:pattern_budget"
    PREFIX_BUDGET = "prefix_budget"
    HOST_BUDGET = "host_budget"
    QUERY_EXPLOSION = "trap:query_explosion"
    SESSION_ID = "trap:session_id"
    LOW_YIELD = "trap:low_yield"
    OTHER_LANGUAGE = "other_language"


# Rejections that depend on how the URL was reached, not on the URL itself:
# the same URL may be admissible later (shallower link, freed queue space).
_RETRYABLE = frozenset({Rejection.MAX_DEPTH, Rejection.QUEUE_FULL})


@dataclass(frozen=True)
class FrontierEntry:
    url: str
    depth: int
    score: float
    discovered_via: str
    parent_url: str | None = None
    anchor_text: str | None = None
    # How ``score`` was computed (priority.Prioritizer.signals), stored with the URL.
    signals: dict[str, float] | None = field(default=None, compare=False, hash=False)


class Frontier:
    def __init__(self, site_domain: str, settings: FrontierSettings) -> None:
        self.site_domain = site_domain
        self.settings = settings
        self.rejections: Counter[Rejection] = Counter()
        self._seen: set[str] = set()
        self._queued: set[str] = set()
        self._pattern_counts: Counter[str] = Counter()
        self._prefix_counts: Counter[str] = Counter()
        self._host_counts: Counter[str] = Counter()
        self._query_counts: Counter[str] = Counter()
        # Per URL shape and section: pages fetched, and those that brought new content.
        self._fetched: Counter[str] = Counter()
        self._useful: Counter[str] = Counter()
        # The seed's primary language, and first path segments of translated copies.
        self.main_language: str | None = None
        self.translated: set[str] = set()

    def __len__(self) -> int:
        return len(self._queued)

    def add(self, entry: FrontierEntry) -> Rejection | None:
        """Queue ``entry``; return the reason it was rejected, or ``None`` if accepted."""
        reason = self._admission_check(entry)
        if reason is None:
            self._admit(entry.url, queued=True)
            log.debug(
                "url.scheduled",
                extra={"url": entry.url, "depth": entry.depth, "score": entry.score},
            )
            return None

        if reason not in _RETRYABLE:
            self._seen.add(entry.url)
        self.rejections[reason] += 1
        log.debug("url.skipped", extra={"url": entry.url, "reason": str(reason)})
        return reason

    def is_queued(self, url: str) -> bool:
        return url in self._queued

    def claimed(self, url: str) -> None:
        """``url`` left the queue (claimed by a worker)."""
        self._queued.discard(url)

    def restore(self, url: str, queued: bool) -> None:
        """Re-admit a URL a previous run of this crawl already accepted."""
        if url not in self._seen:
            self._admit(url, queued)

    def _admit(self, url: str, queued: bool) -> None:
        self._seen.add(url)
        if queued:
            self._queued.add(url)
        parts = urlsplit(url)
        self._pattern_counts[_pattern_key(parts)] += 1
        self._prefix_counts[_prefix_key(parts)] += 1
        self._host_counts[parts.hostname or ""] += 1
        if parts.query:
            self._query_counts[_path_key(parts)] += 1

    def record_yield(self, url: str, useful: bool) -> str | None:
        """Count a fetched page of ``url``'s shape and section; ``useful``: it
        brought new content. Returns the key that just closed, if any."""
        closed = None
        for key in _yield_keys(urlsplit(url)):
            was_open = not self._low_yield(key)
            self._fetched[key] += 1
            self._useful[key] += useful
            if was_open and self._low_yield(key):
                closed = key
                log.info("frontier.low_yield", extra={"key": key, "fetched": self._fetched[key],
                                                      "useful": self._useful[key]})  # fmt: skip
        return closed

    def low_yield(self, url: str) -> bool:
        """True if ``url``'s shape or section stopped yielding new content."""
        return any(self._low_yield(key) for key in _yield_keys(urlsplit(url)))

    def _low_yield(self, key: str) -> bool:
        fetched = self._fetched[key]
        return fetched >= self.settings.yield_min_fetches and (
            self._useful[key] < self.settings.min_unique_yield * fetched
        )

    def learn_language(self, url: str, language: str | None, *, seed: bool) -> str | None:
        """Note a fetched page's declared language. The seed's is the site's
        main one; another page in another language whose first path segment
        names that language marks the segment as a translated copy (returned
        when newly learned)."""
        lang = primary_language(language)
        if seed:
            self.main_language = lang
            return None
        segment = _first_segment(urlsplit(url))
        known = lang in (None, self.main_language) or segment in self.translated
        if self.main_language and not known and primary_language(segment) == lang:
            self.translated.add(segment)
            log.info("frontier.translated_section", extra={"segment": segment, "language": lang})
            return segment
        return None

    def other_language(self, url: str) -> bool:
        """True if ``url`` is under a section learned to be a translated copy."""
        return bool(self.translated) and _first_segment(urlsplit(url)) in self.translated

    def mark_seen(self, url: str) -> bool:
        """Record a URL reached another way (e.g. a redirect target). True if it was new."""
        if url in self._seen:
            return False
        self._seen.add(url)
        return True

    def _admission_check(self, entry: FrontierEntry) -> Rejection | None:
        s = self.settings
        url = entry.url
        if url in self._seen:
            return Rejection.DUPLICATE
        parts = urlsplit(url)
        if registrable_domain(parts.hostname or "") != self.site_domain:
            return Rejection.EXTERNAL
        if entry.depth > s.max_depth:
            return Rejection.MAX_DEPTH
        if len(url) > s.max_url_length:
            return Rejection.URL_TOO_LONG
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) > s.max_path_segments:
            return Rejection.PATH_TOO_DEEP
        if len(parse_qsl(parts.query, keep_blank_values=True)) > s.max_query_params:
            return Rejection.TOO_MANY_PARAMS
        if _has_repeated_segments(segments, s.max_segment_repeats):
            return Rejection.REPEATED_SEGMENTS
        if _has_session_id(parts):
            return Rejection.SESSION_ID
        if self.low_yield(url):
            return Rejection.LOW_YIELD
        if self.other_language(url):
            return Rejection.OTHER_LANGUAGE
        if parts.query and self._query_counts[_path_key(parts)] >= s.max_query_urls_per_path:
            return Rejection.QUERY_EXPLOSION
        if self._pattern_counts[_pattern_key(parts)] >= s.max_urls_per_pattern:
            return Rejection.PATTERN_BUDGET
        if self._prefix_counts[_prefix_key(parts)] >= s.max_urls_per_prefix:
            return Rejection.PREFIX_BUDGET
        if self._host_counts[parts.hostname or ""] >= s.max_urls_per_host:
            return Rejection.HOST_BUDGET
        if len(self._queued) >= s.max_queue_size:
            return Rejection.QUEUE_FULL
        return None


def _first_segment(parts: SplitResult) -> str:
    return next((seg for seg in parts.path.lower().split("/") if seg), "")


def _has_repeated_segments(segments: list[str], max_repeats: int) -> bool:
    """Detect relative-link loops such as /a/b/a/b/... or /x/x/x/x."""
    if any(count > max_repeats for count in Counter(segments).values()):
        return True
    # A block of two or more segments immediately repeated: /docs/api/docs/api
    n = len(segments)
    for size in range(2, n // 2 + 1):
        for start in range(n - 2 * size + 1):
            if segments[start : start + size] == segments[start + size : start + 2 * size]:
                return True
    return False


def _generalize(segment: str) -> str:
    if _NUMERIC_SEGMENT.fullmatch(segment):
        return "{n}"
    if _ID_SEGMENT.fullmatch(segment):
        return "{id}"
    return segment


def _pattern_key(parts: SplitResult) -> str:
    """Shape of a URL with numbers, ids and query values abstracted away."""
    path = "/".join(_generalize(seg) for seg in parts.path.split("/"))
    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return f"{parts.hostname}{path}?{'&'.join(keys)}"


def _has_session_id(parts: SplitResult) -> bool:
    if _ASPNET_SESSION.search(parts.path):
        return True
    return any(
        _SESSION_KEY.search(key) and _SESSION_VALUE.fullmatch(value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )


def _path_key(parts: SplitResult) -> str:
    path = "/".join(_generalize(seg) for seg in parts.path.split("/"))
    return f"{parts.hostname}{path}"


def _yield_keys(parts: SplitResult) -> tuple[str, str]:
    return f"shape:{_pattern_key(parts)}", f"section:{_prefix_key(parts)}"


def _prefix_key(parts: SplitResult) -> str:
    first = parts.path.lstrip("/").split("/", 1)[0]
    return f"{parts.hostname}/{first}"
