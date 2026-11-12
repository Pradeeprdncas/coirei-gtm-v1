"""Acceptance evidence the earlier phases lacked (spec section 35; Appendix B phase 18).

"Versions are reproducible": the same content crawled with the same code and
configuration yields identical observations and field states, a replay of the
stored artifacts reproduces the recorded extraction run exactly, and the
version record (including the configuration hash) does not depend on the
process that computed it.
"""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from gtm_crawler.config import Settings
from gtm_crawler.db.models import Crawl, Extraction
from gtm_crawler.pipeline import run_pipeline
from gtm_crawler.replay import field_states, replay_crawl, stored_observations
from gtm_crawler.versions import config_hash, current_versions

from .test_crawl import FakeSite, html, settings_for

ROOT = Path(__file__).resolve().parent.parent


def routes(site: str) -> dict[str, Any]:
    """A small company site with facts that carry qualifiers, a product and a price."""
    return {
        f"{site}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{site}/": ("text/html", html(
            "Acme", '<a href="/about">About</a> <a href="/pricing">Pricing</a>'
            '<a href="/careers">Careers</a><p>Acme builds inventory software for retailers.</p>',
            '<meta property="og:site_name" content="Acme">')),
        f"{site}/about": ("text/html", html(
            "About", "<p>Acme Inc was founded in 2019 in Austin.</p>"
            "<p>Acme has more than 200 employees.</p>")),
        f"{site}/pricing": ("text/html", html("Pricing", "<p>Starter $29 per month.</p>")),
        f"{site}/careers": ("text/html", html("Careers", "<p>We have 12 open roles.</p>")),
    }  # fmt: skip


def portable(value: Any, site: str) -> str:
    """``value`` as JSON with the site's host replaced, to compare two domains."""
    return json.dumps(value, sort_keys=True, default=str).replace(site.split("//")[1], "SITE")


def crawled(engine: Engine, tmp_path: Path) -> tuple[str, Settings, dict[str, Any]]:
    site = f"https://v{uuid.uuid4().hex[:10]}.example"
    s = settings_for(tmp_path, concurrency=4)
    t = httpx.MockTransport(FakeSite(routes(site)))
    result = asyncio.run(run_pipeline(f"{site}/", s, engine, transport=t))
    assert result["status"] == "completed"
    return site, s, result


def recorded(engine: Engine, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The crawl's stored versions and configuration snapshot."""
    with engine.connect() as conn:
        row = conn.execute(select(Crawl.versions, Crawl.config)
                           .where(Crawl.id == result["crawl_id"])).one()  # fmt: skip
    return row.versions, row.config


@pytest.mark.db
def test_same_content_and_versions_give_identical_observations_and_states(
    engine: Engine, tmp_path: Path
) -> None:
    site_a, s, a = crawled(engine, tmp_path / "a")
    site_b, _, b = crawled(engine, tmp_path / "b")
    obs_a = stored_observations(engine, a["extraction"]["extraction_id"])
    obs_b = stored_observations(engine, b["extraction"]["extraction_id"])
    assert len(obs_a) >= 4  # founded year, employees (with qualifier), price, roles...
    assert any("gt" in json.dumps(o["value_normalized"]) for o in obs_a)  # "more than 200"
    assert sorted(portable(o, site_a) for o in obs_a) == sorted(portable(o, site_b) for o in obs_b)
    states = [{k: portable(v, site) for k, v in field_states(engine, r["organization_id"]).items()}
              for site, r in ((site_a, a), (site_b, b))]  # fmt: skip
    assert states[0] == states[1] and states[0]

    # Both crawls record the same versions, and they are the current ones, although
    # their raw stores live in different directories (not output-affecting).
    versions_a, config_a = recorded(engine, a)
    versions_b, config_b = recorded(engine, b)
    assert config_a["rawstore"] != config_b["rawstore"]
    assert versions_a == versions_b == current_versions(s)
    # The configuration hash can be recomputed from the stored snapshot.
    assert config_hash(config_a) == config_hash(config_b) == versions_a["config"]


@pytest.mark.db
def test_replay_of_stored_artifacts_reproduces_the_extraction_run(
    engine: Engine, tmp_path: Path
) -> None:
    _, s, result = crawled(engine, tmp_path)
    first = result["extraction"]["extraction_id"]
    report = replay_crawl(engine, s, result["crawl_id"], write=True)
    assert report["changed"] is False and report["field_states"] == []
    replayed = stored_observations(engine, report["extraction_id"])
    assert replayed == stored_observations(engine, first) and replayed
    with engine.connect() as conn:
        versions = dict(conn.execute(select(Extraction.id, Extraction.versions)
                                     .where(Extraction.id.in_([first, report["extraction_id"]])))
                        .tuples().all())  # fmt: skip
    assert versions[first] == versions[report["extraction_id"]] == current_versions(s)


def test_versions_do_not_depend_on_the_process() -> None:
    """Hash randomization (set order) must not leak into the recorded versions."""
    code = ("import json; from gtm_crawler.config import Settings; "
            "from gtm_crawler.versions import current_versions; "
            "print(json.dumps(current_versions(Settings()), sort_keys=True))")  # fmt: skip
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,  # noqa: S603
                             text=True, check=True, cwd=ROOT).stdout  # fmt: skip
        outputs.add(out.strip())
    assert outputs == {json.dumps(current_versions(Settings()), sort_keys=True)}


def test_config_hash_follows_output_affecting_settings_only() -> None:
    """Regression (Phase 18): the hash used to change with the raw-store path, the
    environment name, the log level, prices and the contact address."""
    base = Settings()
    same = replace(base, app=replace(base.app, env="test"),
                   logging=replace(base.logging, level="DEBUG"),
                   rawstore=replace(base.rawstore, root="/elsewhere", s3_bucket="b"),
                   cost=replace(base.cost, http_request_usd=1.0),
                   identity=replace(base.identity,
                                    user_agent_contact="ops@example.com"))  # fmt: skip
    assert current_versions(same)["config"] == current_versions(base)["config"]
    for changed in (replace(base, identity=replace(base.identity, product_token="Other")),  # noqa: S106
                    replace(base, crawl=replace(base.crawl, max_pages=7))):  # fmt: skip
        assert current_versions(changed)["config"] != current_versions(base)["config"]


def test_acceptance_runner_maps_every_criterion_to_existing_evidence() -> None:
    from gtm_crawler.acceptance import (
        CRITERIA,
        FAIL,
        NOT_DEMONSTRATED,
        PASS,
        combine,
        covers,
        pytest_targets,
    )
    from gtm_crawler.loadtest import SCENARIOS

    assert len(CRITERIA) == 17  # spec section 35
    tools = {"corpus", "replay", "drill", *(f"load:{n}" for n in SCENARIOS)}
    for criterion, items in CRITERIA:
        assert items, criterion
        for item in items:
            if item in tools:
                continue
            path, _, name = item.partition("::")
            source = (ROOT / path).read_text(encoding="utf-8")
            assert not name or f"def {name}(" in source, item
    assert pytest_targets(["tests/a.py::t", "tests/a.py", "tests/b.py::u"]) == [
        "tests/a.py", "tests/b.py::u"]  # fmt: skip
    assert covers("tests/a.py::t", "tests/a.py::t[1]") and not covers(
        "tests/a.py::t", "tests/a.py::tt"
    )
    assert covers("tests/a.py", "tests/a.py::anything")
    assert combine([PASS, NOT_DEMONSTRATED]) == NOT_DEMONSTRATED
    assert combine([NOT_DEMONSTRATED, FAIL]) == FAIL
    assert combine([]) == NOT_DEMONSTRATED and combine([PASS]) == PASS


def test_throttle_spacing_survives_a_late_wakeup() -> None:
    """Regression (found by the Phase 18 full-scale many_domain run: releases 6.8 ms
    apart at a 50 ms delay): a request whose timer fired early slept again until its
    reserved start; if the loop then stalled, it was released late while the next
    request kept its reservation, so the gap after it shrank by the stall."""
    import time

    from gtm_crawler.throttle import HostThrottle

    calls: dict[asyncio.Task[Any] | None, int] = {}

    async def sleep(seconds: float) -> None:
        n = calls[asyncio.current_task()] = calls.get(asyncio.current_task(), 0) + 1
        if task_index[asyncio.current_task()] != 1:
            await asyncio.sleep(seconds)
        elif n == 1:
            await asyncio.sleep(max(0.0, seconds - 0.03))  # the timer fires early
        else:
            await asyncio.sleep(seconds)
            time.sleep(0.06)  # noqa: ASYNC251 - the loop stalls before it is resumed

    throttle = HostThrottle(3, 0.1, sleep=sleep)
    task_index: dict[asyncio.Task[Any] | None, int] = {}
    starts: list[float] = []

    async def request(i: int) -> None:
        task_index[asyncio.current_task()] = i
        await asyncio.sleep(0.001 * i)  # enter in order
        async with throttle.slot("a.example"):
            starts.append(time.monotonic())

    async def go() -> None:
        await asyncio.gather(*(request(i) for i in range(3)))

    asyncio.run(go())
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert len(gaps) == 2 and min(gaps) >= 0.095, gaps
