"""Phase 17 staging: the fail-closed staging profile, the example deploy configs,
token rotation and the deployment smoke test (run in process, end to end)."""

import asyncio
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, ConfigError, Settings, load_settings
from gtm_crawler.smoke import lineage_problems, run_smoke, stated_fields
from gtm_crawler.worker import run_worker

from .test_crawl import ROUTES, FakeSite, settings_for

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SAFE_ENV = {
    "GTM__IDENTITY__USER_AGENT_CONTACT": "https://ops.example.com/crawler",
    "GTM__API__TOKEN": "t0ken-for-staging",
    "GTM__DATABASE__URL": "postgresql+psycopg://app:pw@db.internal:5432/gtm",
}


@pytest.mark.parametrize("profile", ["staging", "prod"])
def test_staging_fails_closed_exactly_like_prod(tmp_path: Path, profile: str) -> None:
    base = tmp_path / "config.toml"
    base.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(base, environ={"GTM_ENV": profile})
    for problem in ("user_agent_contact", "api.token", "development default"):
        assert problem in str(exc.value)
    assert str(exc.value).startswith(f"{profile} profile:")
    env = {"GTM_ENV": profile, **SAFE_ENV}
    for bad in ({"GTM__LOGGING__LEVEL": "DEBUG"}, {"GTM__DATABASE__STATEMENT_TIMEOUT_MS": "0"}):
        with pytest.raises(ConfigError, match=profile):
            load_settings(base, environ={**env, **bad})
    assert load_settings(base, environ=env).app.env == profile
    with pytest.raises(ConfigError):  # built in code, too
        Settings(app=replace(Settings().app, env=profile))


@pytest.mark.parametrize("profile", ["staging", "prod"])
def test_deploy_example_configs_load_and_hold_no_secrets(tmp_path: Path, profile: str) -> None:
    example = DEPLOY / f"config.{profile}.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    secrets = {("api", "token"), ("api", "previous_token"), ("search", "api_key"),
               ("database", "url")}  # fmt: skip
    assert not {(s, k) for s, keys in data.items() for k in keys} & secrets
    (tmp_path / f"config.{profile}.toml").write_bytes(example.read_bytes())
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="api.token"):  # the file alone is not enough
        load_settings(tmp_path / "config.toml", environ={"GTM_ENV": profile})
    s = load_settings(tmp_path / "config.toml", environ={"GTM_ENV": profile, **SAFE_ENV})
    defaults = Settings()
    # Conservative initial budgets: every one is below the default.
    assert s.crawl.max_pages < defaults.crawl.max_pages
    assert s.crawl.concurrency < defaults.crawl.concurrency
    assert s.browser.max_renders < defaults.browser.max_renders
    assert s.external.max_competitor_crawls < defaults.external.max_competitor_crawls
    assert s.api.max_queued_jobs < defaults.api.max_queued_jobs
    assert s.fetch.allowed_networks == ()  # public addresses only


def test_staging_config_mirrors_prod() -> None:
    def load(name: str) -> dict[str, Any]:
        return tomllib.loads((DEPLOY / name).read_text(encoding="utf-8"))

    assert load("config.staging.toml") == load("config.prod.toml")


def test_previous_token_needs_a_token() -> None:
    with pytest.raises(ConfigError, match="previous_token"):
        ApiSettings(previous_token="old")  # noqa: S106


@pytest.mark.db
def test_both_tokens_work_during_rotation(engine: Engine, tmp_path: Path) -> None:
    api_settings = ApiSettings(token="new-t0ken", previous_token="old-t0ken")  # noqa: S106
    api = TestClient(create_app(replace(settings_for(tmp_path), api=api_settings), engine))
    for token, status in (("new-t0ken", 404), ("old-t0ken", 404), ("other", 401), ("", 401)):
        r = api.get("/v1/crawls/999999", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == status, token
    assert api.get("/v1/crawls/999999").status_code == 401
    non_ascii = {"Authorization": "Bearer ünicode".encode()}
    assert api.get("/v1/crawls/1", headers=non_ascii).status_code == 401


def test_lineage_check_flags_every_broken_link() -> None:
    def obs(i: int, **kw: Any) -> dict[str, Any]:
        o = {"observation_id": i, "evidence": {"quote": "q"}, "fetch": {"raw_sha256": "a"},
             "artifact": {"sha256": "a", "pruned_at": None}}  # fmt: skip
        return {**o, **kw}

    good = {"items": [{"state": "SUPPORTED", "assessment_id": 1, "observations": [obs(1)]},
                      {"state": "UNKNOWN", "assessment_id": 2, "observations": []}]}  # fmt: skip
    assert lineage_problems(good) == (1, [])
    bad = {"items": [
        {"state": "SUPPORTED", "assessment_id": 3, "observations": [
            obs(2, artifact=None), obs(3, fetch={"raw_sha256": "b"}),
            obs(4, artifact={"sha256": "a", "pruned_at": "2026-01-01"}),
            obs(5, evidence={"quote": ""})]},
        {"state": "STALE", "assessment_id": 4, "observations": []},
    ]}  # fmt: skip
    count, problems = lineage_problems(bad)
    assert count == 4 and len(problems) == 5
    dossier = {
        "company": {"organization_id": 1, "fields": {
            "founded_year": {"state": "SUPPORTED"}, "arr": {"state": "UNKNOWN"},
            "customer": [{"state": "UNKNOWN"}, {"state": "UNSUPPORTED"}]}},
        "products": [{"product_id": 7, "fields": {"price": [{"state": "SUPPORTED"}]}}],
        "competitors": [{"organization_id": 9, "fields": {"website": {"state": "SUPPORTED"}}}],
    }  # fmt: skip
    assert stated_fields(dossier) == [
        (1, None, "founded_year"), (1, None, "customer"), (1, 7, "price"), (9, None, "website")
    ]  # fmt: skip


def test_smoke_reports_an_unreachable_api() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="http://api.invalid", transport=httpx.MockTransport(refuse))
    report = run_smoke(client, "https://example.com/", "t", timeout=1, poll=0)
    assert not report["ok"] and report["checks"][-1]["error"].startswith("ConnectError")


HOST = "smoke-acme.example"


@pytest.mark.db
def test_smoke_passes_end_to_end_in_process(engine: Engine, tmp_path: Path) -> None:
    """The real API and a real worker (in a thread) on a simulated site."""
    routes = {url.replace("acme.example", HOST): (route if isinstance(route, int) else
              (route[0], route[1].replace(b"acme.example", HOST.encode())))
              for url, route in ROUTES.items()}  # fmt: skip
    settings = replace(settings_for(tmp_path), api=ApiSettings(token="sm0ke"))  # noqa: S106
    stop = threading.Event()

    def work() -> None:  # one job, then exit
        while not stop.is_set():
            site = httpx.MockTransport(FakeSite(routes))
            ran = asyncio.run(run_worker(settings, engine, once=True, transport=site))
            if ran:
                return
            time.sleep(0.1)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        report = run_smoke(TestClient(create_app(settings, engine)), f"https://{HOST}/",
                           "sm0ke", timeout=120, poll=0.2)  # fmt: skip
    finally:
        stop.set()
        worker.join(30)
    assert report["ok"], report
    names = [c["name"] for c in report["checks"]]
    assert names == ["livez", "readyz", "metrics", "auth_refuses_unauthenticated", "submit",
                     "idempotent_resubmit", "crawl_completed", "dossier", "quality", "cost",
                     "evidence_invariant"]  # fmt: skip
    invariant = report["checks"][-1]
    assert invariant["fields"] > 0 and invariant["observations"] >= invariant["fields"]
    assert "sm0ke" not in str(report)

    # A wrong token: the probes pass, the submission is refused.
    wrong = run_smoke(TestClient(create_app(settings, engine)), f"https://{HOST}/", "nope")
    assert not wrong["ok"] and wrong["checks"][-1] == {
        "name": "submit", "ok": False, "status": 401,
        "body": {"detail": "missing or invalid bearer token"},
    }  # fmt: skip


@pytest.mark.db
def test_readiness_during_a_rolling_deploy(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old code on a schema a newer release migrated (additively) stays ready;
    new code on a schema still behind it is not."""
    import gtm_crawler.api as api_module
    from gtm_crawler.db.migrate import migration_head

    current = migration_head()
    monkeypatch.setattr(api_module, "migration_head", lambda: "0001_old_release")
    monkeypatch.setattr(api_module, "known_revisions", lambda: frozenset({"0001_old_release"}))
    ahead = TestClient(create_app(settings_for(tmp_path), engine)).get("/readyz")
    assert ahead.status_code == 200 and ahead.json()["schema_ahead"] is True
    assert ahead.json()["schema"] == current
    monkeypatch.setattr(api_module, "migration_head", lambda: "0099_next_release")
    newer = frozenset({current, "0099_next_release"})
    monkeypatch.setattr(api_module, "known_revisions", lambda: newer)
    behind = TestClient(create_app(settings_for(tmp_path), engine)).get("/readyz")
    assert behind.status_code == 503 and behind.json()["status"] == "migrations_pending"
