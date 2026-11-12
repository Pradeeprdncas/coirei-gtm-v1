"""End-to-end run: company site, extraction, competitor domain resolution and crawls."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from gtm_crawler.assess import load_observations
from gtm_crawler.cli import main
from gtm_crawler.config import ExternalSettings, Settings
from gtm_crawler.db.identity import review_link
from gtm_crawler.db.models import (
    Crawl,
    Extraction,
    IdentityDecision,
    IdentityLink,
    Link,
    Observation,
    Organization,
    Url,
)
from gtm_crawler.dossier import build_dossier
from gtm_crawler.pipeline import reextract, resolve_competitor_domains, run_pipeline

from .conftest import TEST_DATABASE_URL
from .test_crawl import SITE, FakeSite, html, settings_for

pytestmark = pytest.mark.db

ROUTES: dict[str, Any] = {
    f"{SITE}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    f"{SITE}/": ("text/html", html(
        "Acme", '<a href="/vs/globex">Acme vs Globex</a><a href="/compare/hooli">Hooli</a>'
        # Globex is stated here first; its comparison page (with the link) must still count.
        '<p>We compete with Initech and Globex.</p>',
        '<meta property="og:site_name" content="Acme">',
    )),
    # Links to the competitor's site: resolvable.
    f"{SITE}/vs/globex": ("text/html", html(
        "vs", '<h1>Acme vs Globex</h1><p>See <a href="https://www.globex.com/">Globex</a>.</p>'
        '<a href="https://www.g2.com/products/globex">Globex on G2</a>')),
    # Two different candidate domains: ambiguous, left unresolved.
    f"{SITE}/compare/hooli": ("text/html", html(
        "vs", '<h1>Compare Acme and Hooli</h1><a href="https://hooli.com/">Hooli</a>'
        '<a href="https://hooli.xyz/">Hooli</a>')),
    "https://www.globex.com/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
    "https://www.globex.com/": ("text/html", html(
        "Globex", '<p>Globex was founded in 1990.</p><a href="/about">About</a>'
        + "".join(f'<a href="/blog/{i}">Post {i}</a>' for i in range(10)),
        '<meta property="og:site_name" content="Globex">',
    )),
    "https://www.globex.com/about": ("text/html", html(
        "About", "<p>Globex is headquartered in Springfield, Oregon.</p>")),
    **{f"https://www.globex.com/blog/{i}": ("text/html", html("Post", f"<p>Post {i}.</p>"))
       for i in range(10)},
}  # fmt: skip


def settings(tmp_path: Path, **external: Any) -> Settings:
    options = {"max_competitor_crawls": 5, "competitor_max_pages": 3, **external}
    return replace(settings_for(tmp_path), external=ExternalSettings(**options))


def run(engine: Engine, s: Settings) -> tuple[dict[str, Any], FakeSite]:
    site = FakeSite(ROUTES)
    result = asyncio.run(run_pipeline(f"{SITE}/", s, engine, transport=httpx.MockTransport(site)))
    return result, site


def test_competitors_are_resolved_and_crawled_within_bounds(engine: Engine, tmp_path: Path) -> None:
    result, site = run(engine, settings(tmp_path))
    assert result["status"] == "completed"
    acme, crawl_id = result["organization_id"], result["crawl_id"]
    by_name = {c["name"]: c for c in result["competitors"]}
    assert set(by_name) == {"Globex", "Hooli", "Initech"}
    assert by_name["Hooli"]["unresolved"] == "ambiguous" and "crawl_id" not in by_name["Hooli"]
    assert by_name["Initech"]["unresolved"] == "no_link"

    globex = by_name["Globex"]
    assert (globex["domain"], globex["basis"]) == ("globex.com", "link_on_evidence_page")
    assert globex["status"] == "partial"  # stopped by competitor_max_pages
    fetched = [u for u in site.requested if "globex.com" in u and not u.endswith((".txt", ".xml"))]
    assert len(fetched) == 3 and "https://www.globex.com/about" in fetched  # high-value first
    assert not any(h in u for u in site.requested for h in ("hooli.com", "hooli.xyz"))

    with engine.connect() as conn:
        named = conn.execute(
            select(Organization.id, Organization.registrable_domain).where(
                Organization.name == "Globex", Organization.named_by_org_id == acme
            )
        ).one()
        crawl_org = conn.execute(
            select(Crawl.organization_id).where(Crawl.id == globex["crawl_id"])
        ).scalar_one()
        facts = conn.execute(
            select(Observation.field, Observation.value_verbatim, Observation.is_first_party)
            .where(Observation.crawl_id == globex["crawl_id"])
        ).all()  # fmt: skip
        link = conn.execute(
            select(IdentityLink.organization_id, IdentityLink.status, IdentityLink.decision_id)
            .where(IdentityLink.member_org_id == named.id)
        ).one()  # fmt: skip
        decisions = {
            d.member_org_id: d for d in conn.execute(
                select(IdentityDecision.__table__).where(IdentityDecision.crawl_id == crawl_id)
            )
        }  # fmt: skip
        links = dict(conn.execute(
            select(Link.id, Url.url).join(Url, Url.id == Link.target_url_id).where(
                Link.id.in_([s["link_id"] for d in decisions.values() for s in d.signals]))
        ).all())  # fmt: skip
    # The name on Acme's page stays what it is; it is linked to the crawled company.
    assert named.registrable_domain is None and named.id == globex["named_organization_id"]
    assert tuple(link) == (crawl_org, "match", globex["identity"]["decision_id"])
    assert globex["organization_id"] == crawl_org
    assert ("founded_year", "1990", True) in facts
    assert ("headquarters", "Springfield, Oregon", True) in facts

    # Every competitor has a decision, resolved or not, that names its evidence.
    by_outcome = {by_name[n]["identity"]["outcome"]: by_name[n] for n in by_name}
    assert set(by_outcome) == {"match", "conflicting", "unknown"}
    match = decisions[named.id]
    assert [links[s["link_id"]] for s in match.signals] == ["https://www.globex.com/"]
    assert match.signals[0]["matched"] == "anchor_text"
    conflict = decisions[by_outcome["conflicting"]["named_organization_id"]]
    assert conflict.contradictions[0]["domains"] == ["hooli.com", "hooli.xyz"]
    assert decisions[by_outcome["unknown"]["named_organization_id"]].signals == []

    # The dossier shows the competitor with the company's own facts.
    (rival,) = [c for c in result["dossier"]["competitors"] if c["name"] == "Globex"]
    assert (rival["organization_id"], rival["domain"], rival["assessed"]) == (
        crawl_org, "globex.com", True)  # fmt: skip
    assert rival["identity"] == {"status": "match", "decision_id": link.decision_id,
                                 "named_organization_id": named.id}  # fmt: skip
    assert rival["fields"]["founded_year"]["value"]
    unresolved = {c["name"]: c for c in result["dossier"]["competitors"] if c["name"] != "Globex"}
    assert all(c["identity"] is None and not c["assessed"] and c["domain"] is None
               for c in unresolved.values())  # fmt: skip

    # A rejected link is no longer followed, and running again does not restore it.
    with engine.begin() as conn:
        link_id = conn.execute(
            select(IdentityLink.id).where(IdentityLink.member_org_id == named.id)
        ).scalar_one()
        review_link(conn, link_id, accept=False, actor="test", rationale="wrong Globex")
    again = {c["name"]: c for c in resolve_competitor_domains(engine, crawl_id, ())}
    assert again["Globex"]["unresolved"] == "rejected" and again["Globex"]["seed"] is None
    (rival,) = [c for c in build_dossier(engine, acme)["competitors"] if c["name"] == "Globex"]
    assert rival["identity"] is None and rival["domain"] is None and not rival["assessed"]


def test_two_companies_naming_one_competitor_share_the_company_not_the_row(
    engine: Engine, tmp_path: Path
) -> None:
    other = "https://umbrella.example"
    routes: dict[str, Any] = {
        **ROUTES,
        f"{other}/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
        f"{other}/": ("text/html", html(
            "Umbrella", '<p>We compete with Globex.</p><a href="https://globex.com/">Globex</a>',
            '<meta property="og:site_name" content="Umbrella">')),
    }  # fmt: skip
    s = settings(tmp_path)
    out = []
    for root in (SITE, other):
        site = FakeSite(routes)
        out.append(asyncio.run(run_pipeline(f"{root}/", s, engine,
                                            transport=httpx.MockTransport(site))))  # fmt: skip
    first, second = ({c["name"]: c for c in o["competitors"]}["Globex"] for o in out)
    with engine.connect() as conn:
        company = conn.execute(
            select(Organization.id).where(Organization.registrable_domain == "globex.com")
        ).scalar_one()
    # One company (already known when Umbrella named it), a name row per namer.
    # (Acme's own link may have been rejected by the test above: that is Acme's.)
    assert second["organization_id"] == company and second["identity"]["outcome"] == "match"
    assert first["named_organization_id"] != second["named_organization_id"]
    (rival,) = [c for c in out[1]["dossier"]["competitors"] if c["name"] == "Globex"]
    assert rival["assessed"] and rival["domain"] == "globex.com"
    assert rival["organization_id"] == second["organization_id"]


def test_competitor_crawls_can_be_turned_off(engine: Engine, tmp_path: Path) -> None:
    result, site = run(engine, settings(tmp_path, max_competitor_crawls=0))
    assert result["competitors"] == []
    assert not any("globex.com" in u for u in site.requested)


def test_first_party_pass_decides_what_to_search(engine: Engine, tmp_path: Path) -> None:
    result, _ = run(engine, settings(tmp_path, max_competitor_crawls=0))
    ext = result["stats"]["external"]
    # The site names its competitors, so they are not searched for; its valuation is open.
    assert "competitor" not in ext["gaps"] and "valuation" in ext["gaps"]
    assert ext["search_error"] == "disabled"  # no API key in tests
    # Nothing was fetched after the first-party pass, so extraction ran once.
    with engine.connect() as conn:
        runs = conn.execute(
            select(Extraction.id).where(Extraction.crawl_id == result["crawl_id"])
        ).scalars().all()  # fmt: skip
    assert runs == [result["extraction"]["extraction_id"]]


def test_reextraction_supersedes_without_fetching(engine: Engine, tmp_path: Path) -> None:
    s = settings(tmp_path, max_competitor_crawls=0)
    result, site = run(engine, s)
    org = result["organization_id"]
    before = load_observations(engine, org)
    fetched = len(site.requested)

    again = reextract(engine, s, result["crawl_id"])
    assert again["extraction"]["extraction_id"] > result["extraction"]["extraction_id"]
    assert len(site.requested) == fetched
    after = load_observations(engine, org)
    # Same statements, counted once: only the latest run is current.
    # (Other test crawls of the same organization stay as they were.)
    old, new = {o.id for o in before}, {o.id for o in after}
    assert len(old - new) == len(new - old) > 0
    assert again["assessment"]["states"] == result["assessment"]["states"]
    dossier = build_dossier(engine, org)
    assert sorted(c["name"] for c in dossier["competitors"]) == ["Globex", "Hooli", "Initech"]


def test_reextract_command(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    result, _ = run(engine, settings(tmp_path, max_competitor_crawls=0))
    config = tmp_path / "config.toml"
    config.write_text(f'[rawstore]\nroot = "{(tmp_path / "raw").as_posix()}"\n')
    monkeypatch.setenv("GTM__DATABASE__URL", TEST_DATABASE_URL)
    # Nothing is fetched, so no user-agent contact is needed.
    assert main(["--reextract", str(result["crawl_id"]), "--config", str(config)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["extraction"]["extraction_id"] > result["extraction"]["extraction_id"]
    assert out["dossier"]["company"]["organization_id"] == result["organization_id"]
    assert main(["--reextract", "999999999", "--config", str(config)]) == 2
