"""Per-host politeness and adaptive rate control (spec section 7).

Each host gets a concurrency limit and a minimum gap between request starts.
Both adapt to how the server behaves (``observe``):

* 429 / 503 ("overloaded"): the limit halves and an extra delay doubles
  (at least 1 s, at most ``max_extra_delay``).
* Timeouts, connection failures and other 5xx ("failed"): the limit halves.
* A response slower than ``slow_seconds``: the limit drops by one.
* ``recover_after`` fast successes in a row: the limit grows by one (up to
  the configured maximum) and the extra delay halves.

The configured minimum delay and robots.txt Crawl-delay are floors: adaptation
only ever slows requests down from there, it never shortens a Crawl-delay.
``backoff`` holds a host for a server's Retry-After.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

Outcome = Literal["ok", "overloaded", "failed"]


@dataclass
class _Host:
    limit: float
    extra_delay: float = 0.0
    active: int = 0
    next_start: float | None = None
    last_start: float | None = None  # the latest request's actual (reserved) start
    streak: int = 0
    slowdowns: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)


class HostThrottle:
    def __init__(
        self,
        per_host_concurrency: int,
        min_delay: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        slow_seconds: float = 10.0,
        recover_after: int = 10,
        max_extra_delay: float = 60.0,
    ) -> None:
        self._concurrency = per_host_concurrency
        self._min_delay = min_delay
        self._clock = clock
        self._sleep = sleep
        self._slow_seconds = slow_seconds
        self._recover_after = recover_after
        self._max_extra_delay = max_extra_delay
        self._hosts: dict[str, _Host] = {}

    def _host(self, host: str) -> _Host:
        if host not in self._hosts:
            self._hosts[host] = _Host(float(self._concurrency))
        return self._hosts[host]

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        """Hold a request slot for ``host``; waits for the host's delay to elapse.

        The delay is the larger of the configured minimum and the site's
        robots.txt Crawl-delay, plus any extra delay the host earned by
        signalling overload.
        """
        h = self._host(host)
        async with h.cond:
            await h.cond.wait_for(lambda: h.active < max(1, int(h.limit)))
            h.active += 1
            delay = max(self._min_delay, crawl_delay or 0.0) + h.extra_delay
            now = self._clock()
            start = max(now, h.next_start if h.next_start is not None else now)
            h.next_start = start + delay
        try:
            if start > now:
                await self._sleep(start - now)
            # Starts reserved in advance wake together after an event-loop stall
            # longer than the delay (found by the Phase 16 load test), and a request
            # woken late shortened the gap to the next one (Phase 18): release only
            # `delay` after the previous request's actual release, recorded when it
            # happens. Check and record have no await between them, so two waiters
            # cannot both pass.
            while True:
                now = self._clock()
                due = now if h.last_start is None else h.last_start + delay
                if due <= now:
                    break
                await self._sleep(due - now)
            h.last_start = now
            yield
        finally:
            async with h.cond:
                h.active -= 1
                h.cond.notify_all()

    def backoff(self, host: str, seconds: float) -> None:
        """Hold every request to ``host`` for ``seconds`` (e.g. after a 429)."""
        h = self._host(host)
        now = self._clock()
        h.next_start = max(h.next_start if h.next_start is not None else now, now + seconds)

    def observe(self, host: str, outcome: Outcome, seconds: float = 0.0) -> None:
        """Adapt ``host``'s limit and delay to one response (see the module docstring)."""
        h = self._host(host)
        before = (int(h.limit), h.extra_delay)
        if outcome == "overloaded":
            h.limit = max(1.0, h.limit / 2)
            h.extra_delay = min(self._max_extra_delay, max(1.0, h.extra_delay * 2))
            h.streak = 0
        elif outcome == "failed":
            h.limit = max(1.0, h.limit / 2)
            h.streak = 0
        elif seconds > self._slow_seconds:
            h.limit = max(1.0, h.limit - 1)
            h.streak = 0
        else:
            h.streak += 1
            if h.streak >= self._recover_after:
                h.streak = 0
                h.limit = min(float(self._concurrency), h.limit + 1)
                h.extra_delay = h.extra_delay / 2 if h.extra_delay >= 0.1 else 0.0
        after = (int(h.limit), h.extra_delay)
        if after != before:
            if after[0] < before[0] or after[1] > before[1]:
                h.slowdowns += 1
            log.info("throttle.adjusted", extra={"host": host, "outcome": outcome,
                                                 "concurrency": after[0],
                                                 "extra_delay": after[1]})  # fmt: skip

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current limits of hosts that were ever slowed down (crawl stats)."""
        return {
            host: {
                "concurrency": int(h.limit),
                "extra_delay": round(h.extra_delay, 3),
                "slowdowns": h.slowdowns,
            }  # fmt: skip
            for host, h in self._hosts.items()
            if h.slowdowns
        }
