"""Identity resolution: linked on evidence, never on a name, and reversible."""

from typing import Any

import pytest
from sqlalchemy import Connection, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from gtm_crawler.db.evidence import _org_by_name
from gtm_crawler.db.identity import (
    ALGORITHM_VERSION,
    IdentityError,
    attributable_pages,
    canonical_ids,
    resolve_member,
    resolve_profiles,
    review_link,
)
from gtm_crawler.db.models import (
    Crawl,
    ExternalIdentity,
    Fetch,
    IdentityDecision,
    IdentityLink,
    Link,
    Organization,
    Page,
    Url,
)
from gtm_crawler.db.store import ensure_organization
from gtm_crawler.identity import ExternalId
from gtm_crawler.urls import registrable_domain, url_host

pytestmark = pytest.mark.db

LINKEDIN = "https://www.linkedin.com/company/acme"


@pytest.fixture(autouse=True)
def _no_identities(conn: Connection) -> None:
    """Other tests commit crawls; these count rows. Rolled back with the test."""
    conn.execute(text("TRUNCATE identity_link, identity_decision, external_identity CASCADE"))


def _insert(conn: Connection, model: Any, **values: Any) -> int:
    return int(conn.execute(insert(model).values(**values).returning(model.id)).scalar_one())


def _url(conn: Connection, url: str) -> int:
    existing = conn.execute(select(Url.id).where(Url.url == url)).scalar()
    if existing is not None:
        return int(existing)
    host = url_host(url)
    return _insert(conn, Url, url=url, host=host, registrable_domain=registrable_domain(host))


def _crawl(conn: Connection, domain: str) -> tuple[int, int]:
    org = ensure_organization(conn, domain)
    return org, _insert(conn, Crawl, organization_id=org, seed_url=f"https://{domain}/", config={})


def _page(conn: Connection, crawl: int, url: str, *links: str) -> list[int]:
    """A fetched page of ``crawl`` at ``url`` linking to ``links``. Returns the link ids."""
    fetch = _insert(conn, Fetch, crawl_id=crawl, url_id=_url(conn, url), method="http",
                    status_code=200)  # fmt: skip
    page = _insert(conn, Page, fetch_id=fetch)
    return [_insert(conn, Link, page_id=page, target_url_id=_url(conn, target), is_external=True)
            for target in links]  # fmt: skip


def _signal(link_id: int = 1) -> dict[str, Any]:
    return {"signal": "evidence_page_link", "link_id": link_id, "page_id": 1,
            "matched": "anchor_text"}  # fmt: skip


def _count(conn: Connection, model: Any) -> int:
    return int(conn.execute(select(func.count()).select_from(model)).scalar_one())


def _decisions(conn: Connection, **where: Any) -> list[Any]:
    stmt = select(IdentityDecision.__table__).order_by(IdentityDecision.id)
    for column, value in where.items():
        stmt = stmt.where(getattr(IdentityDecision, column) == value)
    return list(conn.execute(stmt).all())


def _link(conn: Connection, **where: Any) -> Any:
    stmt = select(IdentityLink.__table__)
    for column, value in where.items():
        stmt = stmt.where(getattr(IdentityLink, column) == value)
    return conn.execute(stmt).one()


# --- names --------------------------------------------------------------------------


def test_a_name_belongs_to_the_organization_that_used_it(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    initech = ensure_organization(conn, "initech.com")
    # A company that happens to be called Mercury is already known by its domain.
    known = _insert(conn, Organization, name="Mercury", registrable_domain="mercury.com")

    first = _org_by_name(conn, "Mercury", acme)
    assert _org_by_name(conn, "Mercury", acme) == first  # idempotent
    assert _org_by_name(conn, "MERCURY", acme) == first  # whatever the capitalization
    other = _org_by_name(conn, "Mercury", initech)
    # Same name, three rows: nothing says they are one company.
    assert len({first, other, known}) == 3
    rows = conn.execute(
        select(Organization.registrable_domain, Organization.named_by_org_id)
        .where(Organization.id.in_([first, other]))
        .order_by(Organization.id)
    ).all()
    assert [tuple(r) for r in rows] == [(None, acme), (None, initech)]


def test_a_name_written_another_way_is_the_same_mention(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    first = _org_by_name(conn, "Sprout Social", acme)
    # Spacing, punctuation and a legal form do not make another company.
    for variant in ("SproutSocial", "Sprout-Social", "Sprout Social, Inc."):
        assert _org_by_name(conn, variant, acme) == first
    assert _org_by_name(conn, "Sprout Social Labs", acme) != first  # another word is another name
    assert conn.execute(select(Organization.name).where(Organization.id == first)).scalar() == (
        "Sprout Social"  # the first spelling seen
    )


def test_same_name_with_different_domains_is_not_merged(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    initech = ensure_organization(conn, "initech.com")
    bank = _org_by_name(conn, "Mercury", acme)
    insurer = _org_by_name(conn, "Mercury", initech)

    a = resolve_member(conn, bank, {"mercury.com": [_signal(1)]})
    b = resolve_member(conn, insurer, {"mercuryinsurance.com": [_signal(2)]})
    assert (a.outcome, b.outcome) == ("match", "match")
    assert a.organization_id != b.organization_id
    linked = canonical_ids(conn, [bank, insurer])
    assert linked[bank].organization_id == a.organization_id
    assert linked[insurer].organization_id == b.organization_id


# --- named organizations ------------------------------------------------------------


def test_match_is_a_link_with_an_explained_decision(conn: Connection) -> None:
    acme, crawl = _crawl(conn, "acme.com")
    globex = _org_by_name(conn, "Globex", acme)
    signal = _signal(7)

    r = resolve_member(conn, globex, {"globex.com": [signal]}, crawl_id=crawl, actor="worker-1")
    company = conn.execute(
        select(Organization.id).where(Organization.registrable_domain == "globex.com")
    ).scalar_one()
    assert (r.outcome, r.organization_id) == ("match", company)

    (decision,) = _decisions(conn, member_org_id=globex)
    assert decision.id == r.decision_id
    assert (decision.outcome, decision.organization_id, decision.candidates) == (
        "match", company, [company])  # fmt: skip
    assert decision.signals == [signal] and decision.contradictions == []
    assert (decision.algorithm_version, decision.actor, decision.automatic, decision.crawl_id) == (
        ALGORITHM_VERSION, "worker-1", True, crawl)  # fmt: skip
    assert "globex.com" in decision.rationale

    link = _link(conn, member_org_id=globex)
    assert (link.id, link.organization_id, link.status, link.decision_id) == (
        r.link_id, company, "match", r.decision_id)  # fmt: skip
    # Nothing was merged: the named row is as it was.
    named = conn.execute(
        select(Organization.name, Organization.registrable_domain).where(Organization.id == globex)
    ).one()
    assert tuple(named) == ("Globex", None)


def test_reprocessing_the_same_evidence_writes_nothing(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    cases = {
        "Globex": {"globex.com": [_signal(1)]},
        "Hooli": {"hooli.com": [_signal(2)], "hooli.xyz": [_signal(3)]},
        "Initech": {},
    }
    first = {n: resolve_member(conn, _org_by_name(conn, n, acme), c) for n, c in cases.items()}
    before = (_count(conn, IdentityDecision), _count(conn, IdentityLink),
              _count(conn, Organization))  # fmt: skip
    again = {n: resolve_member(conn, _org_by_name(conn, n, acme), c) for n, c in cases.items()}
    assert again == first
    assert before == (_count(conn, IdentityDecision), _count(conn, IdentityLink),
                      _count(conn, Organization))  # fmt: skip
    assert {n: r.outcome for n, r in first.items()} == {
        "Globex": "match", "Hooli": "conflicting", "Initech": "unknown"}  # fmt: skip


def test_several_candidates_conflict_and_no_evidence_is_unknown(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    hooli_com = ensure_organization(conn, "hooli.com")
    hooli = _org_by_name(conn, "Hooli", acme)
    initech = _org_by_name(conn, "Initech", acme)

    r = resolve_member(conn, hooli, {"hooli.com": [_signal(1)], "hooli.xyz": [_signal(2)]})
    assert (r.outcome, r.organization_id, r.link_id) == ("conflicting", None, None)
    (decision,) = _decisions(conn, member_org_id=hooli)
    assert decision.contradictions == [
        {"signal": "several_candidate_domains", "domains": ["hooli.com", "hooli.xyz"]}
    ]
    assert decision.candidates == [hooli_com] and len(decision.signals) == 2
    # A candidate that is only a domain so far did not become an organization.
    assert conn.execute(
        select(Organization.id).where(Organization.registrable_domain == "hooli.xyz")
    ).first() is None  # fmt: skip

    r = resolve_member(conn, initech, {})
    assert (r.outcome, r.organization_id, r.link_id) == ("unknown", None, None)
    assert canonical_ids(conn, [hooli, initech]) == {}
    assert _count(conn, IdentityLink) == 0


def test_a_rejected_link_stays_rejected(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    globex = _org_by_name(conn, "Globex", acme)
    evidence = {"globex.com": [_signal(1)]}
    first = resolve_member(conn, globex, evidence)
    assert first.link_id is not None and globex in canonical_ids(conn, [globex])

    decision_id = review_link(conn, first.link_id, accept=False, actor="jane",
                              rationale="globex.com is a different Globex")  # fmt: skip
    assert canonical_ids(conn, [globex]) == {}

    # The same evidence again does not bring the link back.
    again = resolve_member(conn, globex, evidence)
    assert (again.outcome, again.organization_id) == ("unknown", None)
    assert _link(conn, member_org_id=globex).status == "rejected"
    assert resolve_member(conn, globex, evidence) == again  # and stays quiet about it

    automatic, manual, after = _decisions(conn, member_org_id=globex)
    assert (manual.id, manual.outcome, manual.automatic, manual.actor, manual.supersedes_id) == (
        decision_id, "rejected", False, "jane", automatic.id)  # fmt: skip
    assert after.contradictions[0]["signal"] == "manual_rejection"
    # Another candidate can still be matched afterwards.
    other = resolve_member(conn, globex, {"globex.io": [_signal(2)]})
    assert other.outcome == "match" and canonical_ids(conn, [globex])


def test_contradicting_evidence_stops_a_link_until_reviewed(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    globex = _org_by_name(conn, "Globex", acme)
    first = resolve_member(conn, globex, {"globex.com": [_signal(1)]})
    assert first.link_id is not None

    # No evidence in a later crawl is not evidence against.
    assert resolve_member(conn, globex, {}) == first

    r = resolve_member(conn, globex, {"globex.io": [_signal(2)]})
    assert (r.outcome, r.organization_id, r.link_id) == ("conflicting", None, first.link_id)
    assert canonical_ids(conn, [globex]) == {}  # no longer followed
    last = _decisions(conn, member_org_id=globex)[-1]
    assert last.contradictions == [
        {"signal": "differs_from_current_link", "organization_id": first.organization_id}
    ]
    assert len(last.candidates) == 2 and last.supersedes_id == first.decision_id

    review_link(conn, first.link_id, accept=True, actor="jane", rationale="checked both sites")
    assert canonical_ids(conn, [globex])[globex].organization_id == first.organization_id
    # A reviewer's decision is not reopened by the resolver.
    kept = resolve_member(conn, globex, {"globex.io": [_signal(2)]})
    assert (kept.outcome, kept.organization_id) == ("match", first.organization_id)


def test_links_cannot_chain_or_loop(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    globex_com = ensure_organization(conn, "globex.com")
    globex = _org_by_name(conn, "Globex", acme)
    r = resolve_member(conn, globex, {"globex.com": [_signal(1)]})

    # A company (a row with a domain) is never the member of a link ...
    with pytest.raises(IdentityError):
        resolve_member(conn, globex_com, {"acme.com": [_signal(2)]})
    # ... so no link can lead onward from a link's target, and none to itself.
    for values in (
        dict(organization_id=globex, member_org_id=globex),
        dict(organization_id=acme, member_org_id=globex),  # a second open link
    ):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(insert(IdentityLink).values(status="match", decision_id=r.decision_id,
                                                     **values))  # fmt: skip


def test_decisions_are_append_only_and_reviews_need_a_reason(conn: Connection) -> None:
    acme = ensure_organization(conn, "acme.com")
    r = resolve_member(conn, _org_by_name(conn, "Globex", acme), {"globex.com": [_signal(1)]})
    assert r.link_id is not None
    for stmt in ("UPDATE identity_decision SET outcome = 'unknown' WHERE id = :id",
                 "DELETE FROM identity_decision WHERE id = :id"):  # fmt: skip
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text(stmt).bindparams(id=r.decision_id))
    with pytest.raises(IdentityError):
        review_link(conn, r.link_id, accept=False, actor="jane", rationale="  ")
    with pytest.raises(IdentityError):
        review_link(conn, 10**9, accept=False, actor="jane", rationale="x")


# --- profiles -----------------------------------------------------------------------


def _profile_link(conn: Connection, org: int) -> Any:
    return conn.execute(
        select(IdentityLink.__table__)
        .join(ExternalIdentity, ExternalIdentity.id == IdentityLink.external_identity_id)
        .where(IdentityLink.organization_id == org, ExternalIdentity.source_type == "linkedin")
    ).one()


def test_reciprocal_links_match_a_profile(conn: Connection) -> None:
    acme, crawl = _crawl(conn, "acme.com")
    (forward, _, _) = _page(
        conn,
        crawl,
        "https://acme.com/",
        LINKEDIN + "/",
        "https://www.linkedin.com/in/jane-doe",  # a person
        "https://github.com/acme/widget",
    )  # a repository
    (backward,) = _page(conn, crawl, LINKEDIN, "https://www.acme.com/")

    assert resolve_profiles(conn, crawl) == {"match": 1}
    identity = conn.execute(select(ExternalIdentity.__table__)).one()
    assert (identity.source_type, identity.external_id) == ("linkedin", "company/acme")
    link = _profile_link(conn, acme)
    assert link.status == "match"
    (decision,) = _decisions(conn, external_identity_id=identity.id)
    assert [(s["signal"], s["link_id"]) for s in decision.signals] == [
        ("profile_links_site", backward), ("site_links_profile", forward)]  # fmt: skip
    assert (decision.organization_id, decision.crawl_id, decision.automatic) == (acme, crawl, True)

    # Idempotent.
    assert resolve_profiles(conn, crawl) == {"match": 1}
    assert (_count(conn, IdentityDecision), _count(conn, IdentityLink)) == (1, 1)


def test_several_reciprocal_profiles_of_one_platform_are_held_for_review(
    conn: Connection,
) -> None:
    """github.com/<employee> links to the employer and is linked from its site,
    like the company's own account: none is taken as the company's unasked."""
    acme, crawl = _crawl(conn, "acme.com")
    org, jane, joe = (f"https://github.com/{name}" for name in ("acme", "jane", "joe"))
    _page(conn, crawl, "https://acme.com/", org, jane, joe, LINKEDIN)
    _page(conn, crawl, org, "https://acme.com/")
    _page(conn, crawl, jane, "https://acme.com/")  # "I work at acme.com"
    _page(conn, crawl, LINKEDIN, "https://acme.com/")

    for _ in range(2):  # the same answer and no new decision on a rerun
        assert resolve_profiles(conn, crawl) == {
            "conflicting": 2, "match": 1, "probable_match": 1}  # fmt: skip
    status = dict(conn.execute(
        select(ExternalIdentity.external_id, IdentityLink.status)
        .join(IdentityLink, IdentityLink.external_identity_id == ExternalIdentity.id)
    ).all())  # fmt: skip
    # The only reciprocal LinkedIn profile is still the company's.
    assert status == {"acme": "conflicting", "jane": "conflicting", "joe": "probable_match",
                      "company/acme": "match"}  # fmt: skip
    assert _count(conn, IdentityDecision) == 4
    held = _decisions(conn, outcome="conflicting")[0]
    assert held.contradictions == [
        {"signal": "several_reciprocal_profiles", "external_ids": ["jane"]}]  # fmt: skip
    assert attributable_pages(conn, crawl)[0] == {
        ExternalId("github", "joe"), ExternalId("linkedin", "company/acme")}  # fmt: skip

    # A reviewer says which one is the company; that stands.
    links = dict(conn.execute(
        select(ExternalIdentity.external_id, IdentityLink.id)
        .join(IdentityLink, IdentityLink.external_identity_id == ExternalIdentity.id)
    ).all())  # fmt: skip
    review_link(conn, links["acme"], accept=True, actor="jane", rationale="the organization")
    review_link(conn, links["jane"], accept=False, actor="jane", rationale="an employee")
    resolve_profiles(conn, crawl)
    assert _profile_status(conn, "acme") == "match"
    assert _profile_status(conn, "jane") == "rejected"


def _profile_status(conn: Connection, external_id: str) -> str:
    return str(conn.execute(
        select(IdentityLink.status)
        .join(ExternalIdentity, ExternalIdentity.id == IdentityLink.external_identity_id)
        .where(ExternalIdentity.external_id == external_id)
    ).scalar_one())  # fmt: skip


def test_one_direction_is_only_probable(conn: Connection) -> None:
    acme, crawl = _crawl(conn, "acme.com")
    _page(conn, crawl, "https://acme.com/", LINKEDIN)
    # Found by search, linking back, but the site does not link to it.
    _page(conn, crawl, "https://www.crunchbase.com/organization/acme", "https://acme.com/")
    # A profile page that does not mention the site at all is nobody's yet.
    _page(conn, crawl, "https://www.g2.com/products/acme-uk", "https://acme.co.uk/")

    for _ in range(2):
        assert resolve_profiles(conn, crawl) == {"probable_match": 2, "unknown": 1}
    assert (_count(conn, ExternalIdentity), _count(conn, IdentityLink)) == (3, 2)
    assert sorted(d.outcome for d in _decisions(conn)) == [
        "probable_match", "probable_match", "unknown"]  # fmt: skip
    profiles, linked, linking = attributable_pages(conn, crawl)
    assert profiles == {ExternalId("linkedin", "company/acme"),
                        ExternalId("crunchbase", "organization/acme")}  # fmt: skip
    assert linked == {LINKEDIN} and len(linking) == 1  # the Crunchbase page


def test_a_one_way_claim_does_not_weaken_a_reciprocal_one(conn: Connection) -> None:
    acme, acme_crawl = _crawl(conn, "acme.com")
    _page(conn, acme_crawl, "https://acme.com/", LINKEDIN)
    _page(conn, acme_crawl, LINKEDIN, "https://acme.com/")
    reseller, reseller_crawl = _crawl(conn, "reseller.com")
    _page(conn, reseller_crawl, "https://reseller.com/partners", LINKEDIN)

    for _ in range(3):  # in either order, however often
        assert resolve_profiles(conn, acme_crawl) == {"match": 1}
        assert resolve_profiles(conn, reseller_crawl) == {"conflicting": 1}
    assert _profile_link(conn, acme).status == "match"
    conflict = _decisions(conn, organization_id=reseller)[-1]
    assert conflict.contradictions == [
        {"signal": "claimed_by_other_organization", "organization_id": acme, "status": "match"}
    ]
    # Acme's match, the reseller's conflict, and Acme's match again now that it
    # is contested. The reruns after that wrote nothing.
    assert [(d.organization_id, d.outcome) for d in _decisions(conn)] == [
        (acme, "match"), (reseller, "conflicting"), (acme, "match")]  # fmt: skip


def test_one_way_claim_first_then_the_owner(conn: Connection) -> None:
    reseller, reseller_crawl = _crawl(conn, "reseller.com")
    _page(conn, reseller_crawl, "https://reseller.com/partners", LINKEDIN)
    assert resolve_profiles(conn, reseller_crawl) == {"probable_match": 1}

    acme, acme_crawl = _crawl(conn, "acme.com")
    _page(conn, acme_crawl, "https://acme.com/", LINKEDIN)
    _page(conn, acme_crawl, LINKEDIN, "https://acme.com/")
    assert resolve_profiles(conn, acme_crawl) == {"match": 1}
    assert _profile_link(conn, reseller).status == "conflicting"
    assert _profile_link(conn, acme).status == "match"


def test_two_companies_claiming_one_profile_conflict(conn: Connection) -> None:
    acme, acme_crawl = _crawl(conn, "acme.com")
    other, other_crawl = _crawl(conn, "acme.io")
    for crawl, site in ((acme_crawl, "https://acme.com/"), (other_crawl, "https://acme.io/")):
        _page(conn, crawl, site, LINKEDIN)
        _page(conn, crawl, LINKEDIN, site)

    assert resolve_profiles(conn, acme_crawl) == {"match": 1}
    assert resolve_profiles(conn, other_crawl) == {"conflicting": 1}
    # Both verified, different domains: neither is followed until someone looks.
    for _ in range(2):
        assert _profile_link(conn, acme).status == "conflicting"
        assert _profile_link(conn, other).status == "conflicting"
        assert resolve_profiles(conn, acme_crawl) == {"conflicting": 1}
        assert resolve_profiles(conn, other_crawl) == {"conflicting": 1}
    decisions = len(_decisions(conn))

    # A reviewer settles it; the resolver keeps both answers.
    review_link(conn, _profile_link(conn, acme).id, accept=True, actor="jane", rationale="rebrand")
    review_link(conn, _profile_link(conn, other).id, accept=False, actor="jane",
                rationale="parked domain")  # fmt: skip
    assert resolve_profiles(conn, acme_crawl) == {"match": 1}
    assert resolve_profiles(conn, other_crawl) == {"rejected": 1}
    assert len(_decisions(conn)) == decisions + 2


def test_only_one_verified_owner_per_profile(conn: Connection) -> None:
    acme, crawl = _crawl(conn, "acme.com")
    other = ensure_organization(conn, "acme.io")
    _page(conn, crawl, "https://acme.com/", LINKEDIN)
    _page(conn, crawl, LINKEDIN, "https://acme.com/")
    resolve_profiles(conn, crawl)
    link = _profile_link(conn, acme)
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(insert(IdentityLink).values(
            organization_id=other, external_identity_id=link.external_identity_id,
            status="match", decision_id=link.decision_id))  # fmt: skip
