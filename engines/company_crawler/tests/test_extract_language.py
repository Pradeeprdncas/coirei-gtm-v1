"""The company's own site is read in its main language: translations are not a second
source (live crawl of agorapulse.com: 422 French and German pages to 369 English)."""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from gtm_crawler.api import create_app
from gtm_crawler.worker import run_worker

from .test_crawl import FakeSite, a, settings_for

pytestmark = pytest.mark.db

SITE = "https://lang.example"


def page(lang: str | None, body: str) -> tuple[str, bytes]:
    attr = f' lang="{lang}"' if lang else ""
    return (
        "text/html",
        f"<html{attr}><head><title>T</title></head><body>{body}</body></html>".encode(),
    )


def crawl(engine: Engine, tmp_path: Path, home_lang: str | None) -> dict[str, object]:
    routes = {
        f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\n"),
        f"{SITE}/": page(home_lang, a("/about", "About") + a("/fr/about", "FR")),
        f"{SITE}/about": page(home_lang, "<p>We were founded in 2019 in Austin.</p>"),
        f"{SITE}/fr/about": page("fr-FR", "<p>We were founded in 2021 in Austin.</p>"),
    }
    settings = settings_for(tmp_path)
    api = TestClient(create_app(settings, engine))
    company = api.post("/v1/crawls", json={"root_url": f"{SITE}/"}).json()["company_id"]
    transport = httpx.MockTransport(FakeSite(routes))
    assert asyncio.run(run_worker(settings, engine, once=True, transport=transport)) == 1
    founded = api.get(f"/v1/companies/{company}/dossier").json()["company"]["fields"]
    return founded["founded_year"]  # type: ignore[no-any-return]


def test_translations_of_the_site_are_not_read(engine: Engine, tmp_path: Path) -> None:
    founded = crawl(engine, tmp_path, "en-US")
    assert founded["value"] == 2019 and founded["state"] == "SUPPORTED"
    assert all("/fr/" not in c["url"] for c in founded["citations"])
