"""Competitor ads (gtm_crawler.market.ads, docs/ad-intelligence-design.md).

Units and an offline product run need no database; storage, identity links and
append-only history run against PostgreSQL (skipped when it is not reachable).
"""

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text

from gtm_crawler.config import UrlSettings
from gtm_crawler.db.models import Ad, AdObservation, IdentityLink, Organization
from gtm_crawler.domain import PolicyDecision
from gtm_crawler.market import ads as A
from gtm_crawler.market import runner
from gtm_crawler.market.benchmark import ad_claims, citations, provenance, unsupported
from gtm_crawler.market.engine import run_research
from gtm_crawler.market.models import (
    Competitor,
    Confidence,
    Dimension,
    EvidenceKind,
    Finding,
    ProductInput,
    ResearchMode,
    ResearchRequest,
    SourceType,
)
from gtm_crawler.market.verify import QuoteCheck, verify_evidence
from gtm_crawler.policy import ad_library
from gtm_crawler.rawstore import RawStore
from tests.helpers import response

from .test_market import TODAY, FakeSearch, handler, html, settings
from .test_policy import engine_for

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def record(ad_id: str = "1234567890123", advertiser: str = "987654321",
           domain: str = "acme.example", **fields: Any) -> dict[str, Any]:  # fmt: skip
    base: dict[str, Any] = {
        "headline": {"state": "present", "verbatim": ["Switch from MyCRM in 7 days"]},
        "body": {"state": "present", "verbatim": [
            "Stop losing leads in spreadsheets. Start a free trial, no credit card."]},
        "cta": {"state": "present", "verbatim": "Sign up"},
        "format": {"state": "present", "verbatim": "Image"},
        "destination_url": {"state": "present",
                            "verbatim": f"https://{domain}/crm?utm_source=fb&gclid=abc"},
        "platform_start": {"state": "present", "verbatim": "Started running on 1 Oct 2026",
                           "normalized": "2026-10-01"},
        "regions": {"state": "not_collected"},
    }  # fmt: skip
    base.update(fields)
    return {
        "platform": "meta", "platform_ad_id": ad_id, "observed_at": NOW.isoformat(),
        "advertiser": {"advertiser_id": advertiser, "name": "Acme",
                       "profile_url": "https://www.facebook.com/ads/library/?view_all_page_id="
                                      f"{advertiser}", "competitor_domain": domain},
        "fields": base,
    }  # fmt: skip


def payload(*records: dict[str, Any]) -> bytes:
    return json.dumps({"ads": list(records)}).encode()


def view(rec: dict[str, Any], raw: RawStore | None = None,
         verification: str = "operator_transcribed") -> A.AdView:  # fmt: skip
    """An AdView as ``load_ads`` returns it, without a database."""
    ad = A.ImportedAd.model_validate(rec)
    digest = raw.put(A._canonical(ad.model_dump(mode="json"))) if raw else "0" * 64
    return A.AdView(ad.platform, ad.platform_ad_id, ad.advertiser.advertiser_id,
                    ad.advertiser.profile_url, ad.advertiser.competitor_domain, [{
                        "observed_at": ad.observed_at, "import_id": "imp_test", "operator": "op",
                        "raw_sha256": digest, "content_sha256": "1" * 64,
                        "fields": A._normalized(ad, UrlSettings()), "media": [],
                        "verification": verification}])  # fmt: skip


# --- import validation ----------------------------------------------------------------


def _mutate(path: str, value: Any) -> dict[str, Any]:
    rec = record()
    *keys, last = path.split(".")
    target = rec
    for k in keys:
        target = target[k]
    if value is KeyError:
        del target[last]
    else:
        target[last] = value
    return rec


@pytest.mark.parametrize("path, value, why", [
    ("platform_ad_id", "abc", "not in the platform's format"),
    ("advertiser.advertiser_id", "AR123", "advertiser id"),
    ("advertiser.profile_url", "https://adstransparency.google.com/advertiser/AR1234567",
     "not a meta ad library page"),
    ("advertiser.profile_url", "https://acme.example/ads", "not a meta ad library page"),
    ("observed_at", (datetime.now(UTC) + timedelta(days=2)).isoformat(), "in the future"),
    ("observed_at", "2026-11-01T10:00:00", "time zone"),
    ("fields.spend", {"state": "not_collected"}, "unknown fields"),
    ("fields.headline", {"state": "present"}, "verbatim is given exactly"),
    ("fields.cta", {"state": "redacted", "verbatim": "Buy"}, "verbatim is given exactly"),
    ("screenshots", ["../secret.png"], "no paths"),
])  # fmt: skip
def test_import_refuses_what_is_not_exact(path: str, value: Any, why: str) -> None:
    with pytest.raises(A.AdImportError, match=why):
        A.parse_import(payload(_mutate(path, value)))


def test_an_ad_needs_a_text_field() -> None:
    rec = record()
    for f in ("headline", "body", "cta"):
        rec["fields"][f] = {"state": "absent_on_source"}
    with pytest.raises(A.AdImportError, match="at least one text field"):
        A.parse_import(payload(rec))


def test_fields_are_normalized_and_missing_ones_not_collected() -> None:
    ad = A.parse_import(payload(record())).ads[0]
    fields = A._normalized(ad, UrlSettings())
    assert set(fields) == set(A.FIELDS)
    assert fields["destination_url"]["normalized"] == "https://acme.example/crm"  # trackers gone
    assert fields["format"]["normalized"] == "image"
    assert fields["platform_start"] == {
        "state": "present",
        "normalized": "2026-10-01",
        "verbatim": "Started running on 1 Oct 2026",
    }
    assert fields["languages"]["state"] == "not_collected"
    assert fields["advertiser_display_name"]["verbatim"] == "Acme"
    bad = A.parse_import(payload(record(format={"state": "present", "verbatim": "Banner"})))
    with pytest.raises(A.AdImportError, match="format must be one of"):
        A._normalized(bad.ads[0], UrlSettings())
    bad = A.parse_import(payload(record(cta={"state": "present", "verbatim": "Go",
                                             "normalized": "go"})))  # fmt: skip
    with pytest.raises(A.AdImportError, match="takes no normalized value"):
        A._normalized(bad.ads[0], UrlSettings())


def test_ocr_corroborates_only_a_matching_transcription() -> None:
    texts = ["Switch from MyCRM in 7 days", "Sign up"]
    assert A.corroborated(texts, "ACME  Switch from MyCRM in 7 days!\nSign up")
    assert not A.corroborated(texts, "Switch to Acme today. Sign up")
    assert not A.corroborated(texts, "")


def test_ad_libraries_are_never_fetched_whatever_robots_says() -> None:
    assert ad_library("https://www.facebook.com/ads/library/?id=123")
    assert ad_library("https://adstransparency.google.com/advertiser/AR1/creative/CR1")
    assert not ad_library("https://www.facebook.com/acme")
    assert not ad_library("https://acme.example/ads/library")
    policy, requested = engine_for({})
    decision = asyncio.run(policy.check("https://adstransparency.google.com/?region=IN",
                                        external=True))  # fmt: skip
    assert (decision.decision, decision.reason) == (PolicyDecision.BLOCK, "ad_library")
    assert requested == []  # decided before robots.txt is even asked for


# --- evidence, verification, section ----------------------------------------------------


def test_latest_capture_becomes_messaging_evidence_and_own_site_landing_pages() -> None:
    off = record(
        ad_id="2234567890123",
        destination_url={
            "state": "present",
            "verbatim": "https://l.facebook.com/l.php?u=https://acme.example",
        },
    )
    found = A.ad_evidence([view(record()), view(off)], {"acme.example": "Acme"})
    assert [s.source_type for s in found.sources] == [SourceType.AD_LIBRARY] * 2
    assert all(e.dimensions == [Dimension.MESSAGING] for e in found.evidence)
    headline = next(e for e in found.evidence if e.locator and e.locator.value == "headline")
    assert (headline.locator.type, headline.locator.row) == ("ad", 0)
    assert headline.claim == "Acme (meta ad 1234567890123) headline: Switch from MyCRM in 7 days"
    cta = next(e for e in found.evidence if e.locator and e.locator.value == "cta")
    assert cta.locator is not None and cta.locator.row is None
    assert not any(e.locator and e.locator.value == "regions" for e in found.evidence)
    # Only the advertiser's own site is fetched: a click tracker would count a paid click.
    assert found.landing == [("https://acme.example/crm", "known_competitor:Acme")]
    assert found.notes and "l.facebook.com" in found.notes[0]
    assert found.sources[0].url == "https://www.facebook.com/ads/library/?id=1234567890123"


def test_ad_quotes_verify_against_the_stored_capture(tmp_path: Path) -> None:
    raw = RawStore(tmp_path)
    found = A.ad_evidence([view(record(), raw)], {"acme.example": "Acme"})
    src = found.sources[0]
    checks = [verify_evidence(e, src, raw).status for e in found.evidence]
    assert checks and set(checks) == {QuoteCheck.VERIFIED_STORED}
    forged = found.evidence[0].model_copy(update={"excerpt": "Rated #1 CRM"})
    assert verify_evidence(forged, src, raw).status is QuoteCheck.MISMATCH


def _section(*records: dict[str, Any]) -> tuple[Any, A.AdEvidence]:
    found = A.ad_evidence([view(r) for r in records], {"acme.example": "Acme"})
    sources = {s.source_id: s for s in found.sources}
    return A.ad_section(found.evidence, sources, {}, ["MyCRM", "Acme"], found.notes), found


def test_section_separates_observations_conclusions_and_hypotheses() -> None:
    second = record(
        ad_id="3234567890123",
        headline={"state": "present", "verbatim": ["The CRM small teams love"]},
        format={"state": "present", "verbatim": "Video"},
    )
    section, found = _section(record(), second)
    first = section.findings[0]
    assert first.basis is EvidenceKind.DERIVED and first.evidence_ids
    assert first.statement.startswith("Acme: 2 captured ad(s) (meta 2); formats: image 1, "
                                      "video 1; calls to action: Sign up 2")  # fmt: skip
    quoted = [f for f in section.findings if f.basis is EvidenceKind.DIRECT]
    assert [f.statement for f in quoted] == [
        "“Switch from MyCRM in 7 days” (Acme, meta ad 1234567890123 headline)",
        "“The CRM small teams love” (Acme, meta ad 3234567890123 headline)",
    ]
    assert any(
        f.statement.startswith(
            "Offers stated in Acme's ads: free trial (2 of 2), no credit card (2 of 2)"
        )
        for f in section.findings
    )
    hyp = [f for f in section.findings if f.basis is EvidenceKind.INFERRED]
    assert hyp and all(f.statement.startswith("Hypothesis: ") for f in hyp)
    assert all(f.confidence is Confidence.LOW for f in hyp)
    assert any("positions itself against MyCRM (named in 1 of 2" in f.statement for f in hyp)
    assert section.gaps[-1] == A.CAVEAT
    named = {e.evidence_id for e in found.evidence}
    assert all(set(f.evidence_ids) <= named for f in section.findings)
    sections = [("competitor_ads", section.findings)]
    assert A.performance_claims(sections, found.evidence, found.sources)[1] == []


def test_inferring_performance_from_ads_is_refused_but_quoting_them_is_not() -> None:
    reach = record(body={"state": "present", "verbatim": ["Reach more customers on WhatsApp."]})
    found = A.ad_evidence([view(reach)], {"acme.example": "Acme"})
    body = next(e for e in found.evidence if e.locator and e.locator.value == "body")
    bad = [Finding(statement="Acme's long-running ad is clearly performing well",
                   evidence_ids=[body.evidence_id], confidence=Confidence.LOW,
                   basis=EvidenceKind.INFERRED),
           Finding(statement="Acme targets small teams with a big budget",
                   evidence_ids=[body.evidence_id], confidence=Confidence.LOW,
                   basis=EvidenceKind.DERIVED)]  # fmt: skip
    quote = Finding(statement=f"“{body.excerpt}” (Acme)", evidence_ids=[body.evidence_id],
                    confidence=Confidence.LOW, basis=EvidenceKind.DIRECT)  # fmt: skip
    other = Finding(statement="Buyers' budgets are rising", evidence_ids=[],
                    confidence=Confidence.LOW, basis=EvidenceKind.INFERRED)  # fmt: skip
    checked, failures = A.performance_claims([("s", [*bad, quote, other])], found.evidence,
                                             found.sources)  # fmt: skip
    assert checked == 3  # the finding citing no ad is not this gate's business
    assert len(failures) == 2 and "'performing'" in failures[0] and "'targets'" in failures[1]


# --- an offline product run -------------------------------------------------------------

LANDING = "https://acme.example/crm"


async def with_landing(request: httpx.Request) -> httpx.Response:
    if str(request.url) == LANDING:
        body = html("Acme CRM", "Switch from MyCRM in 7 days and stop losing leads.",
                    "Start a free trial today, no credit card needed.")  # fmt: skip
        return response(200, body, **{"content-type": "text/html; charset=utf-8"})
    return await handler(request)


def product(include_ads: bool = True, **kw: Any) -> ResearchRequest:
    kw.setdefault("known_competitors", [Competitor(name="Acme", url="https://acme.example/")])
    return ResearchRequest(mode=ResearchMode.PRODUCT, subject="MyCRM", industry="CRM software",
                           geography="India", product=ProductInput(
                               name="MyCRM", include_ads=include_ads, **kw))  # fmt: skip


def test_product_run_reports_captured_ads_and_their_landing_pages(tmp_path: Path) -> None:
    s = settings(tmp_path)
    raw = RawStore(s.rawstore.root)
    views = [view(record(), raw), view(record(ad_id="3234567890123"), raw)]
    asked: list[list[str]] = []

    def ads(domains: list[str]) -> list[A.AdView]:
        asked.append(domains)
        return views

    report = asyncio.run(
        run_research(
            product(),
            s,
            search=FakeSearch(),
            today=TODAY,
            ads=ads,
            transport=httpx.MockTransport(with_landing),
        )
    )
    assert asked == [["acme.example"]]
    section = report.sections["competitor_ads"]
    assert section.findings[0].statement.startswith("Acme: 2 captured ad(s)")
    landing = next(src for src in report.sources if src.url == LANDING)
    assert landing.source_type is SourceType.COMPANY_WEBSITE and landing.status.value == "fetched"
    aligned = [f for f in section.findings if "landing page" in f.statement]
    assert aligned and f"of its headline and text appear on its landing page {LANDING}" in \
        aligned[0].statement  # fmt: skip
    # Ad text is what the advertiser says, never market evidence for another section.
    ad_ids = {e.evidence_id for e in report.evidence
              if e.dimensions == [Dimension.MESSAGING]}  # fmt: skip
    assert ad_ids
    for key, sec in report.sections.items():
        if key != "competitor_ads":
            assert not {i for f in sec.findings for i in f.evidence_ids} & ad_ids, key
    assert any("not every ad an advertiser runs" in x for x in report.limitations)
    assert report.stats["ads"] == {"competitors": 1, "ads": 2, "evidence": len(ad_ids),
                                   "landing_pages": 1}  # fmt: skip
    # unsupported: the ad section only (the competitors section's coverage finding cites
    # nothing, with or without ads; a separate, pre-existing issue).
    ads_only = report.model_copy(update={"sections": {"competitor_ads": section}, "metrics": []})
    for gate in (citations(report), unsupported(ads_only), ad_claims(report)):
        assert gate["pass"], gate
    checks = provenance(report, raw)
    ad_checks = {i: v for i, v in checks["checks"].items() if i in ad_ids}
    assert set(ad_checks.values()) == {"verified_stored"} and len(ad_checks) == len(ad_ids)


def test_asking_for_ads_without_competitors_or_store_is_reported(tmp_path: Path) -> None:
    no_store = asyncio.run(
        run_research(
            product(),
            settings(tmp_path),
            search=FakeSearch(),
            transport=httpx.MockTransport(handler),
            today=TODAY,
        )
    )
    assert any(i.stage == "ads" and "no ad store" in i.message for i in no_store.issues)
    assert "no captured ads" in no_store.sections["competitor_ads"].gaps[0]
    none = asyncio.run(
        run_research(
            product(known_competitors=[Competitor(name="Acme")]),
            settings(tmp_path),
            search=FakeSearch(),
            ads=lambda d: [],
            transport=httpx.MockTransport(handler),
            today=TODAY,
        )
    )
    assert any("needs competitors with a domain" in i.message for i in none.issues)
    without = asyncio.run(run_research(product(include_ads=False), settings(tmp_path),
                                       search=FakeSearch(), today=TODAY,
                                       transport=httpx.MockTransport(handler)))  # fmt: skip
    assert "competitor_ads" not in without.sections


def test_import_cli_needs_an_operator(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        runner.main(["--import-ads", "ads.json"])
    assert exc.value.code == 2 and "--operator" in capsys.readouterr().err


# --- from a crawled company: what to capture, and its competitors' ads in the report --------

DOSSIER = {
    "company": {"domain": "mycrm.example", "fields": {}},
    "products": [],
    "competitors": [
        {"name": "Acme", "domain": "acme.example", "fields": {}},
        {"name": "Globex", "domain": "globex.example", "fields": {}},
        {"name": "Initech", "domain": None, "fields": {}},  # named, not resolved
    ],
    "candidate_competitors": [{"name": "Hooli", "named_organization_id": 9}],
}


def test_checklist_lists_every_competitor_with_links_and_what_is_captured() -> None:
    checklist = A.capture_checklist(DOSSIER, [view(record())], "IN")
    rows = {r["name"]: r for r in checklist["competitors"]}
    assert list(rows) == ["Acme", "Globex", "Initech", "Hooli"]
    assert rows["Acme"]["links"] == {
        "meta": "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=IN"
        "&q=Acme&search_type=keyword_unordered&media_type=all",
        "google": "https://adstransparency.google.com/?region=IN&domain=acme.example",
    }
    assert rows["Acme"]["captured"] == {"ads": 1, "platforms": ["meta"],
                                        "latest": NOW.isoformat()}  # fmt: skip
    assert rows["Globex"]["captured"]["ads"] == 0
    # Without a domain only a name search is possible, and imports need a domain.
    assert set(rows["Initech"]["links"]) == set(rows["Hooli"]["links"]) == {"meta"}
    assert rows["Hooli"]["role"] == "candidate_competitor"
    assert all(ad_library(u) for r in rows.values() for u in r["links"].values())
    md = A.checklist_markdown(checklist)
    assert "- **Acme**, acme.example: 1 captured (meta; latest 2026-11-01)" in md
    assert "- **Hooli** (candidate: co-listed, not confirmed), no domain known: none" in md
    assert "never fetches ad libraries" in md


def test_a_crawled_companys_competitors_bring_their_captured_ads(tmp_path: Path) -> None:
    asked: list[list[str]] = []

    def ads(domains: list[str]) -> list[A.AdView]:
        asked.append(domains)
        return [view(record())]

    req = product(known_competitors=[], company_id=7)
    report = asyncio.run(run_research(req, settings(tmp_path), search=FakeSearch(),
                                      dossier=lambda cid: DOSSIER, ads=ads, today=TODAY,
                                      transport=httpx.MockTransport(with_landing)))  # fmt: skip
    assert asked == [["acme.example", "globex.example"]]  # resolved competitors only
    section = report.sections["competitor_ads"]
    assert section.findings[0].statement.startswith("Acme: 1 captured ad(s)")
    ads_issues = [(i.severity, i.message) for i in report.issues if i.stage == "ads"]
    assert ads_issues == [("info", "dossier competitors without captured ads: Globex")]
    assert any(s.url == LANDING and s.status.value == "fetched" for s in report.sources)


def test_a_failing_dossier_is_reported_once_and_ads_still_load(tmp_path: Path) -> None:
    def broken(cid: int) -> dict[str, Any]:
        raise RuntimeError("database gone")

    report = asyncio.run(run_research(product(company_id=7), settings(tmp_path),
                                      search=FakeSearch(), dossier=broken, today=TODAY,
                                      ads=lambda d: [view(record())],
                                      transport=httpx.MockTransport(with_landing)))  # fmt: skip
    msgs = [i.message for i in report.issues if i.stage == "competitors"]
    assert msgs == ["dossier 7 not usable: RuntimeError: database gone"]
    assert report.sections["competitor_ads"].findings


def test_checklist_cli(monkeypatch: pytest.MonkeyPatch,
                       capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "load_settings", lambda path: runner.Settings())
    monkeypatch.setattr(runner, "make_engine", lambda db: "engine")
    monkeypatch.setattr(runner, "build_dossier", lambda engine, cid: DOSSIER)
    monkeypatch.setattr(
        runner, "load_ads", lambda engine, domains: seen.setdefault("domains", domains) and []
    )
    assert runner.main(["--ad-checklist", "7", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Ads to capture: competitors of mycrm.example (IN)")
    assert seen["domains"] == ["acme.example", "globex.example"]
    with pytest.raises(SystemExit):
        runner.main(["--ad-checklist", "7", "--country", "india"])


# --- storage (PostgreSQL) -----------------------------------------------------------------


def unique() -> tuple[str, str, str]:
    n = str(uuid.uuid4().int)
    return n[:16], n[16:28], f"acme{n[:8]}.example"


class FakeOcr:
    available = True

    def __init__(self, text: str) -> None:
        self.text = text

    def read_bytes(self, data: bytes) -> str:
        return self.text


def test_import_appends_observations_and_links_the_advertiser(
    engine: Engine, tmp_path: Path
) -> None:
    ad_id, adv, domain = unique()
    raw = RawStore(tmp_path)
    data = payload(record(ad_id, adv, domain))
    first = A.import_ads(engine, raw, data, operator="Asha")
    assert first["observations_added"] == 1 and first["verification"] == {"operator_transcribed": 1}
    again = A.import_ads(engine, raw, data, operator="Asha")
    assert (again["observations_added"], again["already_imported"]) == (0, 1)
    later = deepcopy(
        record(
            ad_id, adv, domain, headline={"state": "present", "verbatim": ["New: AI lead scoring"]}
        )
    )
    later["observed_at"] = (NOW + timedelta(days=3)).isoformat()
    A.import_ads(engine, raw, payload(later), operator="Ravi")
    with engine.connect() as conn:
        link = conn.execute(
            select(IdentityLink.status, Organization.registrable_domain)
            .join(Organization, Organization.id == IdentityLink.organization_id)
            .join(Ad, Ad.external_identity_id == IdentityLink.external_identity_id)
            .where(Ad.platform_ad_id == ad_id)).one()  # fmt: skip
    assert tuple(link) == ("match", domain)
    (stored,) = A.load_ads(engine, [domain])
    assert [o["operator"] for o in stored.observations] == ["Asha", "Ravi"]  # oldest first
    assert stored.observations[0]["fields"]["headline"]["verbatim"] == [
        "Switch from MyCRM in 7 days"
    ]  # history kept, never overwritten
    assert stored.observations[0]["content_sha256"] != stored.observations[1]["content_sha256"]
    assert {o["raw_sha256"] for o in stored.observations} <= A.cited_digests(engine)
    assert A.load_ads(engine, ["unrelated.example"]) == []


def test_ad_observations_are_append_only(engine: Engine, tmp_path: Path) -> None:
    ad_id, adv, domain = unique()
    A.import_ads(engine, RawStore(tmp_path), payload(record(ad_id, adv, domain)), operator="A")
    with engine.connect() as conn:
        obs = conn.execute(select(AdObservation.id).join(Ad).where(Ad.platform_ad_id == ad_id)
                           ).scalar_one()  # fmt: skip
    for stmt in ("UPDATE ad_observation SET operator = 'x' WHERE id = :id",
                 "DELETE FROM ad_observation WHERE id = :id"):  # fmt: skip
        with pytest.raises(Exception, match="append-only|not allowed|modif"), engine.begin() as c:
            c.execute(text(stmt), {"id": obs})


def test_an_advertiser_cannot_be_reassigned_and_a_refused_file_stores_nothing(
    engine: Engine, tmp_path: Path
) -> None:
    ad_id, adv, domain = unique()
    raw = RawStore(tmp_path)
    A.import_ads(engine, raw, payload(record(ad_id, adv, domain)), operator="A")
    fine, fine_adv, _ = unique()  # valid on its own ...
    moved, _, _ = unique()  # ... but the file also gives adv's ads to another company
    data = payload(record(fine, fine_adv, domain), record(moved, adv, "globex.example"))
    with pytest.raises(A.AdImportError, match="already linked to another company"):
        A.import_ads(engine, raw, data, operator="A")
    with engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(Ad).where(
            Ad.platform_ad_id.in_([fine, moved]))).scalar_one()  # fmt: skip
    assert count == 0  # the whole file or nothing


def test_screenshots_are_kept_and_ocr_corroborates_the_transcription(
    engine: Engine, tmp_path: Path
) -> None:
    ad_id, adv, domain = unique()
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "ad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    rec = record(ad_id, adv, domain)
    rec["screenshots"] = ["ad.png"]
    ocr = FakeOcr("Switch from MyCRM in 7 days. Stop losing leads in spreadsheets. Start a "
                  "free trial, no credit card. Sign up")  # fmt: skip
    raw = RawStore(tmp_path / "raw")
    result = A.import_ads(engine, raw, payload(rec), operator="A", screenshots=shots, ocr=ocr)  # type: ignore[arg-type]
    assert result["verification"] == {"ocr_corroborated": 1}
    (stored,) = A.load_ads(engine, [domain])
    media = stored.observations[0]["media"]
    assert media[0]["name"] == "ad.png" and media[0]["sha256"] in A.cited_digests(engine)
    (shots / "fake.png").write_bytes(b"GIF89a")
    rec["screenshots"] = ["fake.png"]
    with pytest.raises(A.AdImportError, match="PNG, JPEG or WebP"):
        A.import_ads(engine, raw, payload(rec), operator="A", screenshots=shots)
    rec["screenshots"] = ["missing.png"]
    with pytest.raises(A.AdImportError, match="not found"):
        A.import_ads(engine, raw, payload(rec), operator="A", screenshots=shots)
