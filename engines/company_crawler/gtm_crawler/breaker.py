"""Circuit breakers (spec section 7): stop calling a dependency that keeps failing.

One breaker tracks many keys (hosts, providers). A key's circuit is:

* closed: calls go through; ``threshold`` failures in a row open it.
* open: calls are refused for ``cooldown`` seconds.
* half-open: after the cooldown one probe call goes through. Success closes
  the circuit; failure opens it again for twice as long (at most
  ``max_cooldown``). A probe that never reports back is replaced after one
  cooldown, so a lost probe cannot hold the circuit half-open forever.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .metrics import CIRCUIT_OPENED

log = logging.getLogger(__name__)

# How long other calls wait while a half-open probe is in flight.
_PROBE_WAIT = 5.0


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    cooldown: float = 0.0
    probe_at: float | None = None
    trips: int = 0


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        threshold: int,
        cooldown: float,
        max_cooldown: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = threshold
        self._cooldown = cooldown
        self._max_cooldown = max_cooldown
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    def state(self, key: str) -> str:
        c = self._circuits.get(key)
        if c is None or c.opened_at is None:
            return "closed"
        return "open" if self._clock() < c.opened_at + c.cooldown else "half_open"

    def check(self, key: str) -> float:
        """0 if a call to ``key`` may go now, else seconds to wait before asking again."""
        c = self._circuits.get(key)
        if c is None or c.opened_at is None:
            return 0.0
        now = self._clock()
        reopens = c.opened_at + c.cooldown
        if now < reopens:
            return reopens - now
        if c.probe_at is not None and now < c.probe_at + c.cooldown:
            return min(_PROBE_WAIT, c.probe_at + c.cooldown - now)
        c.probe_at = now  # this call is the probe
        return 0.0

    def record(self, key: str, ok: bool) -> None:
        c = self._circuits.setdefault(key, _Circuit())
        if ok:
            if c.opened_at is not None:
                log.info("circuit.closed", extra={"breaker": self.name, "key": key})
            c.failures, c.opened_at, c.probe_at, c.cooldown = 0, None, None, 0.0
            return
        c.failures += 1
        if c.opened_at is not None:
            if c.probe_at is None:
                return  # a call started before the circuit opened
            c.cooldown = min(self._max_cooldown, c.cooldown * 2)  # the probe failed
        elif c.failures >= self._threshold:
            c.cooldown = self._cooldown
        else:
            return
        c.opened_at, c.probe_at = self._clock(), None
        c.trips += 1
        CIRCUIT_OPENED.labels(self.name).inc()
        log.warning("circuit.opened", extra={"breaker": self.name, "key": key,
                                             "failures": c.failures,
                                             "cooldown": c.cooldown})  # fmt: skip

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Keys whose circuit ever opened (crawl stats)."""
        return {
            key: {"state": self.state(key), "trips": c.trips, "failures": c.failures}
            for key, c in self._circuits.items()
            if c.trips
        }
