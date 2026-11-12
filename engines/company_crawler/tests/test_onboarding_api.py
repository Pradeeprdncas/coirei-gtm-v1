"""Integration P1: company documents, the onboarding view and the user's verification."""

import asyncio
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from gtm_crawler.api import create_app
from gtm_crawler.config import ApiSettings, Settings
from gtm_crawler.market import pdf
from gtm_crawler.worker import run_worker

from .pdfs import Text, make_pdf
from .test_crawl import ROUTES, SITE, FakeSite, settings_for

pytestmark = pytest.mark.db

SECTION_KEYS = [
    "company_info",
    "value_proposition",
    "market_segment",
    "target_market",
    "business_model",
    "products_services",
]


def crawled(engine: Engine, settings: Settings, name: str) -> tuple[TestClient, int]:
    """A client and the company id of a completed crawl of the fake Acme site, served
    under its own domain ``<name>.example`` so other tests' evidence never mixes in."""
    site = f"https://{name}.example"

    def moved(route: Any) -> Any:
        if isinstance(route, tuple):
            return route[0], route[1].replace(SITE.encode(), site.encode())
        return route

    routes = {url.replace(SITE, site): moved(r) for url, r in ROUTES.items()}
    api = TestClient(create_app(settings, engine))
    created = api.post("/v1/crawls", json={"root_url": f"{site}/"}).json()
    transport = httpx.MockTransport(FakeSite(routes))
    # Older queued jobs (a document upload's refresh) may be claimed first: run until
    # this one is done.
    for _ in range(10):
        assert asyncio.run(run_worker(settings, engine, once=True, transport=transport)) == 1
        if api.get(f"/v1/crawls/{created['crawl_id']}").json()["status"] != "QUEUED":
            break
    return api, created["company_id"]


def item(view: dict[str, Any], section: str, field: str) -> dict[str, Any]:
    found = next(s for s in view["sections"] if s["key"] == section)
    return next(i for i in found["items"] if i["field"] == field)


def docx(*paragraphs: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", xml)
    return out.getvalue()


def upload(api: TestClient, company: int, name: str, data: bytes, refresh: bool = False) -> Any:
    """Upload a document. Its refresh job is cancelled unless ``refresh``: a queued job
    would be claimed by the next test's worker instead of that test's own job."""
    response = api.post(
        f"/v1/companies/{company}/documents",
        params={"filename": name},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    if response.status_code == 201 and not refresh:
        api.post(f"/v1/crawls/{response.json()['refresh_job_id']}/cancel")
    return response


def test_onboarding_view_has_the_six_sections_and_invents_nothing(
    engine: Engine, tmp_path: Path
) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "p1-view")
    view = api.get(f"/v1/companies/{company}/onboarding").json()
    assert [s["key"] for s in view["sections"]] == SECTION_KEYS
    assert view["domain"] == "p1-view.example" and view["crawl"]["status"] == "COMPLETED"
    prices = item(view, "business_model", "price")
    assert prices["value"] and prices["state"] == "SUPPORTED"
    assert prices["citations"] and all(
        c["url"].startswith("https://p1-view.example/") for c in prices["citations"]
    )
    # The fake site never states these: not found, never filled in.
    for section, field in (
        ("market_segment", "market_domain"),
        ("target_market", "customer"),
        ("company_info", "founded_year"),
    ):
        missing = item(view, section, field)
        assert missing["value"] is None and missing["citations"] == []
    assert view["verified"] is False and isinstance(view["competitors"], list)
    assert api.get("/v1/companies/999999/onboarding").status_code == 404


def test_documents_become_cited_evidence(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "p1-docs")
    text = upload(api, company, "about.txt", b"We were founded in 2019 in Austin.\n")
    assert text.status_code == 201, text.text
    body = text.json()
    assert body["kind"] == "text" and body["url"].startswith("upload://p1-docs.example/documents/")
    founded = item(api.get(f"/v1/companies/{company}/onboarding").json(), "company_info",
                   "founded_year")  # fmt: skip
    assert founded["value"] == 2019
    uploads = [c for c in founded["citations"] if c["url"].startswith("upload://")]
    assert uploads and uploads[0]["source_type"] == "company_document"
    # The web crawl is still the dossier's crawl; the documents crawl has no job.
    assert api.get(f"/v1/companies/{company}/dossier").json()["cost"] is not None

    word = upload(api, company, "C:\\docs\\deck.docx", docx("We were founded in 2019."))
    assert word.status_code == 201 and word.json()["kind"] == "docx"
    assert word.json()["filename"] == "deck.docx"


@pytest.mark.skipif(not pdf.available()[0], reason="pip install -e .[pdf]")
def test_pdf_document(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "p1-pdf")
    data = make_pdf([[Text(72, 700, 12, "We were founded in 2019 in Austin.")]])
    got = upload(api, company, "deck.pdf", data)
    assert got.status_code == 201, got.text
    assert got.json()["kind"] == "pdf" and got.json()["pages_read"] == 1


def test_documents_are_refused_with_a_reason(engine: Engine, tmp_path: Path) -> None:
    settings = replace(settings_for(tmp_path), api=ApiSettings(max_document_bytes=1_000))
    api, company = crawled(engine, settings, "p1-refused")
    assert upload(api, company, "x.bin", b"\x00\x01binary").status_code == 415
    assert upload(api, company, "x.zip", docx("a")[:0] + b"PK\x03\x04junk").status_code == 415
    assert upload(api, company, "empty.txt", b"").status_code == 422
    assert upload(api, company, "blank.txt", b"\n\n  \n").status_code == 422
    assert upload(api, company, "big.txt", b"a" * 1_001).status_code == 413
    assert upload(api, 999999, "a.txt", b"hello").status_code == 404


def test_verification_is_recorded_and_shown(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "p1-verify")
    url = f"/v1/companies/{company}/verification"
    assert api.get(url).json() == {"items": []}
    answers = [{"section": key, "field": field, "status": "confirmed"}
               for key, field in zip(SECTION_KEYS, ["company_name", "product_positioning",
                                     "market_domain", "customer", "business_model",
                                     "product"], strict=True)]  # fmt: skip
    answers[2] = {**answers[2], "status": "edited", "value": "first try"}
    answers.append({**answers[2], "value": "Robotics"})  # the later answer wins
    saved = api.put(url, json={"items": answers})
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["items"]) == 6

    view = api.get(f"/v1/companies/{company}/onboarding").json()
    market = item(view, "market_segment", "market_domain")
    assert market["verification"]["status"] == "edited"
    assert market["verification"]["value"] == "Robotics"
    assert market["value"] is None  # user input is shown beside the evidence, not as it
    assert view["verified"] is True

    bad = {"items": [{"section": "market_segment", "field": "price", "status": "confirmed"}]}
    assert api.put(url, json=bad).status_code == 422
    wrong = {"items": [{"section": "company_info", "field": "website", "status": "maybe"}]}
    assert api.put(url, json=wrong).status_code == 422
    assert api.put("/v1/companies/999999/verification", json={"items": []}).status_code == 404


def test_a_conflict_shows_each_source_s_value() -> None:
    from gtm_crawler.onboarding import _item

    cite = {"url": "https://acme.example/", "quote": "q", "source_type": "official_website",
            "source_tier": 1}  # fmt: skip
    entry = {"state": "CONFLICTING", "citations": [
        {**cite, "value_verbatim": "Acme Robotics Inc"},
        {**cite, "value_verbatim": "Acme"},
        {**cite, "value_verbatim": "Acme"},
    ]}  # fmt: skip
    item = _item("legal_name", "Legal name", [entry], scalar=True)
    assert item["value"] == ["Acme Robotics Inc", "Acme"]
    assert item["state"] == "CONFLICTING" and len(item["citations"]) == 3
