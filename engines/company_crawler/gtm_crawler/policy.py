"""The crawl policy engine (spec section 8): may this URL be fetched now?

Every fetch-time decision goes through ``PolicyEngine.check`` and is one of
ALLOW, BLOCK, DEFER or REJECT. A non-ALLOW decision always carries a reason,
and its detail names the evidence behind it (for robots.txt, the stored
snapshot that was applied), so it can be audited later. The crawl records
every non-ALLOW decision in ``crawl_event``.

* REJECT ``out_of_scope``: not on the crawled site (and not an allowed
  external page).
* BLOCK ``ad_library``: Meta's Ad Library or Google's Ads Transparency Center.
  Their terms forbid or restrict automated collection (Meta: express written
  permission; docs/ad-intelligence-design.md), whatever robots.txt says
  (adstransparency.google.com serves none). Ads are imported by an operator.
* BLOCK ``robots_unreachable``: robots.txt could not be fetched (5xx, network
  failure); RFC 9309 says to assume nothing may be crawled.
* BLOCK ``robots_disallowed``: robots.txt disallows the URL for our user agent.
* DEFER ``crawl_delay_too_long``: the origin's Crawl-delay is longer than
  ``robots.max_crawl_delay_seconds``; the delay is never shortened.
* DEFER ``circuit_open``: the host keeps failing (``breaker.CircuitBreaker``);
  the URL is retried after the cooldown (decided by the crawl, not here).

Frontier admission decisions (traps: REJECT, depth and breadth budgets:
DEFER) are made by ``frontier.Frontier`` and stored as the URL's state and
reason; the network guard's refusals (BLOCK) surface as ``blocked`` fetch
errors.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .domain import PolicyDecision
from .robots import RobotsCache, RobotsStatus
from .urls import registrable_domain


def ad_library(url: str) -> bool:
    """Whether ``url`` is a page of Meta's Ad Library or Google's Ads Transparency Center."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if host == "adstransparency.google.com":
        return True
    return registrable_domain(host) == "facebook.com" and parts.path.startswith(
        ("/ads/library", "/ads/archive")
    )


@dataclass(frozen=True)
class Decision:
    decision: PolicyDecision
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    crawl_delay: float | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW


class PolicyEngine:
    def __init__(self, robots: RobotsCache, in_scope: Callable[[str], bool]) -> None:
        self._robots = robots
        self._in_scope = in_scope

    async def check(self, url: str, *, external: bool = False) -> Decision:
        """Decide for ``url``; ``external`` pages skip the scope rule (the external
        phase chose them under its own policy) but never robots.txt."""
        if not external and not self._in_scope(url):
            return Decision(PolicyDecision.REJECT, "out_of_scope")
        if ad_library(url):
            return Decision(PolicyDecision.BLOCK, "ad_library")
        policy = await self._robots.policy_for(url)
        detail = {"robots_snapshot_id": policy.snapshot_id, "robots_origin": policy.origin,
                  "user_agent": policy.user_agent}  # fmt: skip
        if policy.status is RobotsStatus.UNREACHABLE:
            return Decision(PolicyDecision.BLOCK, "robots_unreachable", detail)
        if not policy.can_fetch(url):
            return Decision(PolicyDecision.BLOCK, "robots_disallowed", detail)
        if policy.delay_too_long:
            return Decision(PolicyDecision.DEFER, "crawl_delay_too_long",
                            {**detail, "crawl_delay": policy.crawl_delay,
                             "max_crawl_delay": policy.max_crawl_delay})  # fmt: skip
        return Decision(PolicyDecision.ALLOW, crawl_delay=policy.crawl_delay)
