"""Phase 3: public contact details and founders, quoted from the company's own pages."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from .test_extract_company import ORG, fields, ld, page


def test_email_and_phone_from_the_company_s_page() -> None:
    p = page(
        "<main><p>Write to hello@acme.example or sales@acme.example.</p>"
        "<p>Phone: +1 (512) 555-0134</p><p>Order 12345678 shipped.</p></main>",
        path="/contact",
    )
    found = fields(p)
    assert found["contact_email"] == {"hello@acme.example", "sales@acme.example"}
    assert found["contact_phone"] == {"+1 (512) 555-0134"}  # a number with no phone cue is not
    assert found["contact_page"] == {f"{p.url}"}


def test_another_domain_s_email_and_an_image_name_are_not_contact_details() -> None:
    p = page("<p>Logo by jane@designstudio.example. See icon@2x.png for details.</p>")
    assert "contact_email" not in fields(p)
    # Not the contact page, so no contact_page either.
    assert "contact_page" not in fields(p)


def test_founders_from_json_ld() -> None:
    org = {**ORG, "founder": [{"@type": "Person", "name": "Ana Silva"},
                              {"@type": "Person", "name": "Raj Mehta"}],
           "email": "info@acme.example", "telephone": "+1-512-555-0100"}  # fmt: skip
    found = fields(page("<p>About us</p>", head=ld(org)))
    assert found["founder"] == {"Ana Silva", "Raj Mehta"}
    assert "info@acme.example" in found["contact_email"]
    assert "+1-512-555-0100" in found["contact_phone"]


def test_contact_founders_and_stage_reach_the_profile() -> None:
    from .test_business_profile import bip, cite, dossier, field

    d = dossier({
        "contact_email": [{"state": "SUPPORTED", "value": "hello@acme.example",
                           "citations": [cite(1, "2026-11-01T00:00:00+00:00")]}],
        "founder": [{"state": "SUPPORTED", "value": "Ana Silva",
                     "citations": [cite(2, "2026-11-01T00:00:00+00:00")]}],
    })  # fmt: skip
    leaf = {"value": "startup", "evidence_confidence": 0.5,
            "citations": [{"observation_id": 2, "url": "https://acme.example/",
                           "quote": "q"}]}  # fmt: skip
    view = bip(d, {"company": {"stage": leaf, "contact_phone": [{**leaf, "value": "+1 512"}],
                               "founders": [{**leaf, "value": "Ana Silva"}]}})  # fmt: skip
    assert field(view, "contact_email")["value"] == ["hello@acme.example"]
    assert field(view, "contact_email")["status"] == "Extracted"
    assert field(view, "contact_phone")["value"] == ["+1 512"]
    founders = field(view, "founders")
    assert founders["value"] == ["Ana Silva"] and founders["status"] == "Extracted"
    stage = field(view, "company_stage")
    assert stage["value"] == "startup" and stage["status"] == "Inferred"


def test_new_fields_are_assessed_for_every_company() -> None:
    from gtm_crawler.assess import COMPANY_FIELDS
    from gtm_crawler.dossier import SECTIONS

    for name in ("contact_email", "contact_phone", "contact_page", "founder"):
        assert name in COMPANY_FIELDS
        assert any(name in fields for fields in SECTIONS.values())


def test_the_profile_model_gives_contact_founders_and_a_stage_choice() -> None:
    from gtm_crawler import profile

    from .test_profile import REFS, leaf, stats

    s = stats()
    out = profile.clean(profile.SCHEMA, {"company": {
        "legal_name": leaf("Acme Robotics Pvt Ltd", "e2"),
        "founders": [leaf("Ana Silva", "e2")],
        "contact_email": [leaf("hello@acme.example", "e2")],
        "stage": leaf("unicorn", "e2"),  # not a choice
    }}, REFS, s)  # fmt: skip
    company = out["company"]
    assert set(company) == {"legal_name", "founders", "contact_email"}
    assert s["invalid"] == 1
    assert profile.PROMPT_VERSION.startswith("profile-")
    assert profile.clean(profile.SCHEMA, {"company": {"stage": leaf("growing", "e2")}},
                         REFS, stats())["company"]["stage"]["value"] == "growing"  # fmt: skip


def test_the_page_model_names_founders_only_with_the_name_in_its_quote() -> None:
    from gtm_crawler.extract import llm
    from gtm_crawler.extract.common import Context

    from .test_extract_llm import fact
    from .test_extract_llm import page as llm_page

    p = llm_page("/about", "<p>Ana Silva founded Acme in 2019 with a friend.</p>")
    reading = {"page_type": "about", "facts": [
        fact("founder", "Ana Silva", "Ana Silva founded Acme in 2019 with a friend."),
        fact("founder", "Raj Mehta", "Ana Silva founded Acme in 2019 with a friend."),
    ]}  # fmt: skip
    drafts = llm.page_drafts(p, reading, Context("acme.example", {"Acme"}), lambda s, n: 1)
    assert [(d.field, d.value_verbatim, d.kind) for d in drafts] == [
        ("founder", "Ana Silva", "stated")
    ]
    assert llm.PROMPT_VERSION == "page-llm-3" and "founder" in llm.FACT_FIELDS


def test_matched_profiles_and_domains_are_the_social_footprint() -> None:
    from gtm_crawler.business_profile import assemble

    from .test_business_profile import dossier, field

    d = dossier({})
    d["company"]["subdomains"] = {"state": "SUPPORTED", "value": ["docs.acme.example"]}
    identities = [
        {"kind": "profile", "platform": "linkedin",
         "url": "https://www.linkedin.com/company/acme", "rationale": "links to the site",
         "decided_at": "2026-11-02T00:00:00+00:00"},
        {"kind": "domain", "platform": None, "url": "acme.io",
         "rationale": "same company", "decided_at": "2026-11-03T00:00:00+00:00"},
    ]  # fmt: skip
    view = assemble(1, d, None, [], None, identities)
    social = field(view, "social_profiles")
    assert social["value"] == [{"platform": "linkedin",
                                "url": "https://www.linkedin.com/company/acme"}]  # fmt: skip
    assert social["status"] == "Extracted" and social["sources"][0]["quote"] == "links to the site"
    assert social["extracted_at"] == "2026-11-02T00:00:00+00:00"
    assert field(view, "other_domains")["value"] == ["acme.io", "docs.acme.example"]


def pptx(*slides: list[str]) -> bytes:
    import io
    import zipfile

    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("ppt/presentation.xml", "<p:presentation/>")
        # Slide 10 sorts after slide 2 by number, not by name.
        for n, paragraphs in enumerate(slides, start=1):
            body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paragraphs)
            z.writestr(f"ppt/slides/slide{n * 5}.xml",
                       f'<p:sld xmlns:a="{a}" xmlns:p="p"><a:txBody>{body}</a:txBody></p:sld>')
    return out.getvalue()


def test_a_presentation_is_read_slide_by_slide() -> None:
    import asyncio

    from gtm_crawler.config import Settings
    from gtm_crawler.documents import read_document

    doc = asyncio.run(read_document(pptx(["Acme Robotics", "Founded in 2019"],
                                         ["Our customers: Northwind"]), Settings()))  # fmt: skip
    assert doc.kind == "pptx" and doc.pages == 2
    assert doc.paragraphs == ["Acme Robotics", "Founded in 2019", "Our customers: Northwind"]


@pytest.mark.db
def test_uploaded_documents_are_key_pages_for_the_profile(engine: Engine, tmp_path: Path) -> None:
    from gtm_crawler.profile import key_pages

    from .test_crawl import settings_for
    from .test_onboarding_api import crawled, upload

    settings = settings_for(tmp_path)
    api, company = crawled(engine, settings, "bip-docs")
    assert upload(api, company, "deck.pptx", pptx(["Acme sells to hospitals."])).status_code == 201
    pages = key_pages(engine, settings, company)
    docs = [(t, u, x) for t, u, x in pages if u.startswith("upload://")]
    assert [(t, x) for t, _, x in docs] == [("document", "Acme sells to hospitals.")]
