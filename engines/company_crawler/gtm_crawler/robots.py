"""robots.txt handling per RFC 9309.

Rules are per origin (scheme, host, port), fetched once per origin and kept
for at most ``max_age_seconds`` (RFC 9309 section 2.4: 24 hours by default),
after which the next request reloads them. Each load is a snapshot: the
``on_load`` callback stores it and returns its id, which the policy carries so
every decision can name the exact rules it applied.

Availability semantics follow RFC 9309 section 2.3.1:

* 2xx: parse the file (first ``max_bytes`` only).
* 4xx "unavailable": no restrictions apply. 429 is treated as unreachable
  instead, since it signals load rather than absence.
* 5xx, network errors and other failures "unreachable": everything is disallowed.

Redirects are followed by the fetcher (up to its redirect limit, which is
within the RFC's minimum of five).

Each origin is loaded once even when many requests ask at the same time, and
``on_load`` receives every fetch result so the crawl can keep a snapshot of
the rules it obeyed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from protego import Protego

from .config import RobotsSettings
from .fetch.http import FetchResult, HttpFetcher

log = logging.getLogger(__name__)


class RobotsStatus(StrEnum):
    PARSED = "parsed"
    UNAVAILABLE = "unavailable"  # allow all
    UNREACHABLE = "unreachable"  # disallow all


@dataclass
class RobotsPolicy:
    origin: str
    status: RobotsStatus
    user_agent: str
    _parser: Protego | None = None
    snapshot_id: int | None = None
    loaded_at: float = field(default=0.0, compare=False)
    # robots.max_crawl_delay_seconds when the policy was loaded.
    max_crawl_delay: float | None = field(default=None, compare=False)

    @property
    def delay_too_long(self) -> bool:
        """Crawl-delay exceeds what the crawl will wait. It is never shortened:
        the origin is not crawled (policy DEFER) instead."""
        delay = self.crawl_delay
        return (
            delay is not None
            and self.max_crawl_delay is not None
            and (delay > self.max_crawl_delay)
        )

    def can_fetch(self, url: str) -> bool:
        if self.status is RobotsStatus.UNAVAILABLE:
            return True
        if self.status is RobotsStatus.UNREACHABLE or self._parser is None:
            return False
        return bool(self._parser.can_fetch(url, self.user_agent))

    @property
    def crawl_delay(self) -> float | None:
        if self._parser is None:
            return None
        delay = self._parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    @property
    def sitemaps(self) -> list[str]:
        return list(self._parser.sitemaps) if self._parser is not None else []


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RobotsCache:
    """Fetches and caches one ``RobotsPolicy`` per origin."""

    def __init__(
        self,
        fetcher: HttpFetcher,
        settings: RobotsSettings,
        user_agent_token: str,
        on_load: Callable[[str, RobotsStatus, FetchResult], Awaitable[int | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetcher = fetcher
        self._settings = settings
        self._token = user_agent_token
        self._on_load = on_load
        self._clock = clock
        self._policies: dict[str, asyncio.Future[RobotsPolicy]] = {}

    async def policy_for(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        future = self._policies.get(origin)
        if (
            future is not None
            and future.done()
            and not future.cancelled()
            and (
                future.exception() is None
                and self._clock() - future.result().loaded_at > self._settings.max_age_seconds
            )
        ):
            future = None  # too old: reload (a new snapshot)
        if future is None:
            future = self._policies[origin] = asyncio.ensure_future(self._load(origin))
        # Shielded: one caller being cancelled must not cancel the shared load.
        return await asyncio.shield(future)

    async def can_fetch(self, url: str) -> bool:
        return (await self.policy_for(url)).can_fetch(url)

    async def _load(self, origin: str) -> RobotsPolicy:
        result = await self._fetcher.fetch(f"{origin}/robots.txt")
        status = result.status
        if result.error is None and status is not None and 200 <= status < 300:
            text = (result.body or b"")[: self._settings.max_bytes].decode("utf-8", "replace")
            policy = RobotsPolicy(origin, RobotsStatus.PARSED, self._token, Protego.parse(text))
        elif result.error is None and status is not None and 400 <= status < 500 and status != 429:
            policy = RobotsPolicy(origin, RobotsStatus.UNAVAILABLE, self._token)
        else:
            policy = RobotsPolicy(origin, RobotsStatus.UNREACHABLE, self._token)
        log.info(
            "robots.loaded",
            extra={"origin": origin, "status": str(policy.status), "http_status": status,
                   "error": result.error, "sitemaps": len(policy.sitemaps)},
        )  # fmt: skip
        policy.loaded_at = self._clock()
        policy.max_crawl_delay = self._settings.max_crawl_delay_seconds
        if self._on_load is not None:
            policy.snapshot_id = await self._on_load(origin, policy.status, result)
        return policy
