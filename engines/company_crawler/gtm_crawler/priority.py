"""URL prioritization (spec section 6).

A URL's score is the sum of independent, explainable signals; ``signals``
returns each one so the breakdown can be stored with the URL
(``crawl_url.priority_signals``) and a crawl order can be explained later:

    path       = path_weight      * term_value(URL path)
    anchor     = anchor_weight    * term_value(link text)
    context    = context_weight   * term_value(text around the link)
    parent     = parent_weight    * term_value(linking page's title + description)
    sitemap    = sitemap_weight   * sitemap <priority> (0.5 if listed without one)
    freshness  = freshness_weight * recency of sitemap <lastmod> (1 today .. 0 at horizon)
    depth      = depth_weight     / (1 + depth)
    low_value  = -low_value_penalty            (if the path matches a low-value term)
    inlinks    = inlink_weight    * log(1 + inlinks)   (added later, as boosts)

``term_value`` is 1.0 for a high-value GTM term (pricing, products, customers,
about, press, ...), 0.5 for a medium-value term (docs, blog, careers, ...),
else 0. Signals order the crawl; they never exclude an in-site URL.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import PrioritySettings

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _contains(tokens: list[str], term_tokens: list[str]) -> bool:
    """True if ``term_tokens`` occur consecutively in ``tokens`` ("case-studies")."""
    n = len(term_tokens)
    return any(tokens[i : i + n] == term_tokens for i in range(len(tokens) - n + 1))


def parse_lastmod(value: str | None) -> datetime | None:
    """A sitemap <lastmod> (W3C datetime: a date or a date-time) as UTC; None if unusable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class Prioritizer:
    def __init__(self, settings: PrioritySettings) -> None:
        self.settings = settings
        self._high = [_tokens(t) for t in settings.high_value_terms]
        self._medium = [_tokens(t) for t in settings.medium_value_terms]
        self._low = [_tokens(t) for t in settings.low_value_terms]

    def term_value(self, text: str) -> float:
        tokens = _tokens(text)
        if any(_contains(tokens, t) for t in self._high):
            return 1.0
        if any(_contains(tokens, t) for t in self._medium):
            return 0.5
        return 0.0

    def freshness(self, lastmod: str | None, now: datetime | None = None) -> float:
        """1.0 for a page modified now, falling linearly to 0 at ``freshness_horizon_days``."""
        modified = parse_lastmod(lastmod)
        if modified is None:
            return 0.0
        age_days = ((now or datetime.now(UTC)) - modified).total_seconds() / 86_400
        horizon = self.settings.freshness_horizon_days
        return round(min(1.0, max(0.0, 1 - age_days / horizon)), 6)

    def signals(
        self,
        url: str,
        depth: int,
        anchor_text: str = "",
        context: str = "",
        sitemap_priority: float | None = None,
        in_sitemap: bool = False,
        lastmod: str | None = None,
        parent_topic: str = "",
        now: datetime | None = None,
    ) -> dict[str, float]:
        """Each non-zero signal's contribution to the score."""
        s = self.settings
        path = unquote(urlsplit(url).path)
        out = {
            "path": s.path_weight * self.term_value(path),
            "anchor": s.anchor_weight * self.term_value(anchor_text),
            "context": s.context_weight * self.term_value(context),
            "parent": s.parent_weight * self.term_value(parent_topic),
            "sitemap": s.sitemap_weight * (0.5 if sitemap_priority is None else sitemap_priority)
            if in_sitemap
            else 0.0,
            "freshness": s.freshness_weight * self.freshness(lastmod, now),
            "depth": s.depth_weight / (1 + depth),
            "low_value": -s.low_value_penalty
            if any(_contains(_tokens(path), t) for t in self._low)
            else 0.0,
        }
        return {k: round(v, 6) for k, v in out.items() if v}

    def score(self, *args: Any, **kwargs: Any) -> float:
        """The sum of ``signals`` (same arguments)."""
        return round(sum(self.signals(*args, **kwargs).values()), 6)

    def inlink_boost(self, inlinks: int) -> float:
        """Score increase when a URL's inlink count goes from ``inlinks - 1`` to ``inlinks``."""
        return self.settings.inlink_weight * (math.log1p(inlinks) - math.log1p(inlinks - 1))
