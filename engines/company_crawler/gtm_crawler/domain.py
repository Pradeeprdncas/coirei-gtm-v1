"""Domain vocabularies (spec sections 5 and 8), shared by code, database and API.

Values are stored lower-case in PostgreSQL (check constraints are generated
from these enums); the API shows them upper-case, as the spec writes them.

Job states::

    CREATED -> QUEUED -> RUNNING -> FINALIZING -> COMPLETED
                 ^  |       |  |        |
                 |  v       v  |        v
                PAUSED <----+  +-> FAILED / CANCELLED

``RUNNING`` covers acquisition (the site, external pages, competitors);
``FINALIZING`` covers extraction, assessment and the job result. A job whose
worker stops goes back to ``QUEUED`` (hand-back) or is reclaimed when its
lease expires. ``COMPLETED``, ``FAILED`` and ``CANCELLED`` are final. A crawl
stopped by a budget is ``COMPLETED``: the job result keeps the stop reason.

URL states::

    DISCOVERED -> ELIGIBLE -> LEASED -> FETCHING -> FETCHED -> PARSED -> PROCESSED
    alternates: DEFERRED, BLOCKED, TRAP, FAILED, RETRY (-> LEASED), DLQ

A URL is inserted ``ELIGIBLE`` once frontier admission accepts it.
``DISCOVERED``, ``DEFERRED``, ``TRAP`` and ``DLQ`` are reserved for the phases
that persist admission decisions (2, 6) and dead-lettering (13).
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINAL_JOB_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})
# Allowed moves; a move to the same state (a lease reclaim) is always allowed.
JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.QUEUED, JobState.PAUSED, JobState.CANCELLED}),
    # QUEUED -> FAILED: abandoned at claim time after max_job_attempts.
    JobState.QUEUED: frozenset(
        {JobState.RUNNING, JobState.PAUSED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.RUNNING: frozenset(
        {JobState.FINALIZING, JobState.QUEUED, JobState.PAUSED, JobState.FAILED, JobState.CANCELLED}
    ),  # fmt: skip
    JobState.FINALIZING: frozenset(
        {JobState.COMPLETED, JobState.QUEUED, JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}
    ),  # fmt: skip
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def can_move(src: JobState | str, dst: JobState | str) -> bool:
    src, dst = JobState(src), JobState(dst)
    if src in FINAL_JOB_STATES:
        return False
    return src == dst or dst in JOB_TRANSITIONS[src]


class IllegalTransition(Exception):
    pass


class UrlState(StrEnum):
    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    LEASED = "leased"
    FETCHING = "fetching"
    FETCHED = "fetched"
    PARSED = "parsed"
    PROCESSED = "processed"
    DEFERRED = "deferred"
    BLOCKED = "blocked"
    TRAP = "trap"
    FAILED = "failed"
    RETRY = "retry"
    DLQ = "dlq"


# Claimed and not finished: a dead worker's URL in one of these is reclaimable.
IN_FLIGHT_URL_STATES = frozenset(
    {UrlState.LEASED, UrlState.FETCHING, UrlState.FETCHED, UrlState.PARSED}
)
OPEN_URL_STATES = IN_FLIGHT_URL_STATES | {UrlState.ELIGIBLE, UrlState.RETRY}
FINAL_URL_STATES = frozenset(
    {UrlState.PROCESSED, UrlState.BLOCKED, UrlState.TRAP, UrlState.FAILED, UrlState.DLQ}
)


class PolicyDecision(StrEnum):
    """Centralized fetch decisions (spec section 8); every non-ALLOW has a reason."""

    ALLOW = "allow"
    BLOCK = "block"  # a rule forbids it (robots.txt, network guard)
    DEFER = "defer"  # not now (rate limit, crawl-delay, budget window)
    REJECT = "reject"  # out of scope (another site, a trap)
