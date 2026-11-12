"""Phase 18 hardening: page-write failures, browser sandbox setting, container proofs."""

import gc
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from gtm_crawler import crawl as crawl_module
from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, ConfigError, ParseSettings, load_settings
from gtm_crawler.db.store import CrawlStore
from gtm_crawler.domain import UrlState

from .test_crawl import SITE, FakeSite, crawl_urls, run, settings_for

NEVER_RETRIEVED = "exception was never retrieved"


def _failing_record_page(
    monkeypatch: pytest.MonkeyPatch, url: str, error: Callable[[], Exception]
) -> None:
    """``record_page`` raises a new ``error()`` for the page fetched from ``url``
    (a new one each time: a kept exception would keep its frames alive)."""
    original = CrawlStore.record_page

    def record_page(self: CrawlStore, fetch_id: int, page: Any, duplicate_of: Any) -> int:
        if page.url == url:
            raise error()
        return original(self, fetch_id, page, duplicate_of)

    monkeypatch.setattr(CrawlStore, "record_page", record_page)


@pytest.mark.db
def test_failed_page_write_is_observed_and_does_not_poison_its_twin(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    # /about fails to store; /about-copy (same text) must be stored as an
    # original, not dead-lettered with /about's error.
    _failing_record_page(monkeypatch, f"{SITE}/about", lambda: ValueError("bad page"))
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        summary = run(engine, settings_for(tmp_path), FakeSite())
        gc.collect()
    states = crawl_urls(engine, summary.crawl_id)
    assert states[f"{SITE}/about"].state == UrlState.DLQ
    assert states[f"{SITE}/about-copy"].state == UrlState.PROCESSED
    assert summary.stats["duplicates"] == 0
    assert NEVER_RETRIEVED not in caplog.text


@pytest.mark.db
def test_database_error_on_page_write_propagates_without_asyncio_warning(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    # The loadtest db_pressure scenario: a statement timeout while writing a page.
    _failing_record_page(
        monkeypatch, f"{SITE}/", lambda: OperationalError("INSERT", {}, Exception("timeout"))
    )
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        try:  # not pytest.raises: its traceback would keep the crawl (and the future) alive
            run(engine, settings_for(tmp_path), FakeSite())
        except OperationalError:
            raised = True
        else:
            raised = False
        gc.collect()
    assert raised
    assert NEVER_RETRIEVED not in caplog.text


def _prod_env(**extra: str) -> dict[str, str]:
    return {"GTM_ENV": "prod", "GTM__IDENTITY__USER_AGENT_CONTACT": "https://x.example/c",
            "GTM__API__TOKEN": "t", "GTM__DATABASE__URL": "postgresql+psycopg://u:p@db/x",
            "GTM__DATABASE__STATEMENT_TIMEOUT_MS": "30000", **extra}  # fmt: skip


def test_prod_refuses_browser_without_sandbox(tmp_path: Path) -> None:
    missing = tmp_path / "config.toml"
    missing.write_text("", encoding="utf-8")
    assert load_settings(missing, _prod_env()).browser.sandbox is True
    with pytest.raises(ConfigError, match="browser.sandbox"):
        load_settings(missing, _prod_env(GTM__BROWSER__SANDBOX="false"))
    acknowledged = _prod_env(GTM__BROWSER__SANDBOX="false",
                             GTM__BROWSER__ALLOW_UNSANDBOXED="true")  # fmt: skip
    assert load_settings(missing, acknowledged).browser.sandbox is False
    # Rendering off: nothing to sandbox.
    assert load_settings(missing, _prod_env(GTM__BROWSER__SANDBOX="false",
                                            GTM__BROWSER__ENABLED="false"))  # fmt: skip


def _image_available(image: str) -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    argv = [docker, "image", "inspect", image]
    return subprocess.run(argv, capture_output=True, check=False).returncode == 0  # noqa: S603


@pytest.mark.skipif(not os.environ.get("GTM_SANDBOX_IMAGE"),
                    reason="set GTM_SANDBOX_IMAGE to the built crawler image to run")  # fmt: skip
def test_sandboxed_render_in_container() -> None:
    """deploy/sandbox-test/run.sh: a real render in the image, sandbox and DNS checked."""
    image = os.environ["GTM_SANDBOX_IMAGE"]
    if not _image_available(image):
        pytest.skip(f"docker image {image} is not available")
    script = Path(__file__).parents[1] / "deploy" / "sandbox-test" / "run.sh"
    bash = shutil.which("bash") or "bash"
    done = subprocess.run([bash, str(script)], env={**os.environ, "IMAGE": image},  # noqa: S603
                          capture_output=True, text=True, timeout=900, check=False)  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert "sandbox test passed" in done.stdout


# --- G-18: a parse that does not end, a client that does not stop ------------------------


@pytest.mark.db
def test_a_parse_that_takes_too_long_fails_the_url_not_the_crawl(
    engine: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = crawl_module.parse_page

    def slow(body: bytes, url: str, *args: Any) -> Any:
        if url.endswith("/pricing"):
            time.sleep(0.5)
        return real(body, url, *args)

    monkeypatch.setattr(crawl_module, "parse_page", slow)
    settings = replace(settings_for(tmp_path), parse=ParseSettings(timeout_seconds=0.1))
    summary = run(engine, settings, FakeSite())
    rows = crawl_urls(engine, summary.crawl_id)
    assert (rows[f"{SITE}/pricing"].state, rows[f"{SITE}/pricing"].skip_reason) == (
        "failed", "parse_timeout")  # fmt: skip
    assert rows[f"{SITE}/about"].state == "processed" and summary.status != "failed"


@pytest.mark.db
def test_api_requests_per_client_are_limited(engine: Any, tmp_path: Any) -> None:
    settings = replace(settings_for(tmp_path), api=ApiSettings(rate_limit_per_minute=3))
    api = TestClient(create_app(settings, engine))
    codes = [api.get("/v1/dead-letters").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    refused = api.get("/v1/dead-letters")
    assert refused.headers["retry-after"] == "60" and "nosniff" in str(refused.headers)
    assert api.get("/livez").status_code == 200  # probes are not limited
