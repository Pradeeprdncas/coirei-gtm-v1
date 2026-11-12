"""Phase 16 load scenarios (spec sections 29 and 35) against a simulated web.

Every scenario runs at a small scale in the default suite; ``GTM_LOAD=1`` also
runs them at full scale (``-m load``). They use a scratch database of their own
(``GTM_LOAD_DATABASE_URL``, default ``gtm_crawler_load_scratch``), recreated
once per module, because their workers claim any queued job in the database.
"""

import asyncio
import os
import time

import pytest
from sqlalchemy.exc import OperationalError

from gtm_crawler.loadtest import DEFAULT_DATABASE_URL, SCENARIOS, run_scenario, scratch_database
from gtm_crawler.throttle import HostThrottle

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def load_db() -> str:
    try:
        scratch_database(DEFAULT_DATABASE_URL)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable ({exc.orig}); run `docker compose up -d`")
    return DEFAULT_DATABASE_URL


def _assert_passed(result: dict) -> None:
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"] and not failed, failed
    assert result["stored"]["pages"] > 0


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_small_scale(load_db: str, name: str) -> None:
    result = asyncio.run(run_scenario(name, SCENARIOS[name].small, load_db, fresh=False))
    _assert_passed(result)


@pytest.mark.load
@pytest.mark.skipif(os.environ.get("GTM_LOAD") != "1", reason="full-scale load: set GTM_LOAD=1")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_full_scale(load_db: str, name: str) -> None:
    _assert_passed(asyncio.run(run_scenario(name, None, load_db, fresh=False)))


def test_scratch_database_refuses_other_names() -> None:
    with pytest.raises(ValueError, match="must contain 'load'"):
        scratch_database("postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler")


def test_throttle_respaces_starts_after_an_event_loop_stall() -> None:
    """Regression (found by many_domain): starts reserved min_delay apart fired together
    after the event loop stalled for longer than the delay."""
    throttle = HostThrottle(2, 0.1)
    starts: list[float] = []

    async def request() -> None:
        async with throttle.slot("a.example"):
            starts.append(time.perf_counter())
            await asyncio.sleep(0.01)

    async def go() -> None:
        tasks = [asyncio.create_task(request()) for _ in range(3)]
        await asyncio.sleep(0.001)
        time.sleep(0.3)  # noqa: ASYNC251 - the stall under test
        await asyncio.gather(*tasks)

    asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert len(gaps) == 2 and min(gaps) >= 0.06, gaps  # Windows timers may fire ~32 ms early
