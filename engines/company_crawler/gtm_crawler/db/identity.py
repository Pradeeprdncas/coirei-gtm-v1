"""Identity resolution: which company a named organization or a profile is.

Two records are the same company only on evidence we hold: links between
pages we fetched. A name never decides it (``identity.normalize_name`` only
makes names comparable). Nothing is merged: the answer is an ``identity_link``
from the record to the company, set by an ``identity_decision`` that names
the stored links it rests on, and undone by changing the link's status.

Named organizations (``resolve_member``): a competitor, investor or partner
named on a page is a name-only ``organization`` row belonging to the company
that named it. It is the company at domain D when the pages that named it
link to D and to no other candidate (``pipeline.resolve_competitor_domains``
finds the candidates).

    one candidate domain          MATCH
    none                          UNKNOWN
    several                       CONFLICTING
    one, but a reviewer rejected  UNKNOWN
    one, differing from the link  CONFLICTING (the link is no longer followed)

Profiles (``resolve_profiles``): a LinkedIn, GitHub, Crunchbase, ... profile
is the company's when its site links to the profile and the profile, fetched
by us, links back to its domain.

    site -> profile and profile -> site   MATCH
    one direction only                    PROBABLE_MATCH
    fetched, no link either way           UNKNOWN (a decision, no link)
    another company claims it too         CONFLICTING, unless exactly one of
                                          the claims is reciprocal: that one
                                          is MATCH, the others CONFLICTING
    several reciprocal profiles of one    CONFLICTING, all of them: a company
    platform for one company              has one, the rest are its people
                                          (github.com/<employee> links to the
                                          employer too); a reviewer picks

Only MATCH is followed when facts are read (``canonical_ids``). A page on
another site is read for facts about a company only if it is that company's
profile (MATCH or PROBABLE_MATCH), or it links to the company's site, or the
company's own site links to that very page (``attributable_pages``); the
company's name on the page or in its URL is not enough. A reviewer's
decision (``review_link``) is never changed by the resolver, so a rejected
link stays rejected however often the same evidence is processed again.
Processing unchanged evidence writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from ..identity import PROFILE_DOMAINS, ExternalId, external_id
from ..metrics import IDENTITY_DECISIONS
from .models import (
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
from .store import ensure_organization

ALGORITHM_VERSION = "ir-v1"
# Signals kept per direction of a profile decision: a footer link is on every page.
MAX_SIGNALS = 5


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class Resolution:
    outcome: str
    # The company, for 'match' and 'probable_match'.
    organization_id: int | None
    decision_id: int
    link_id: int | None = None


def resolve_member(
    conn: Connection,
    member_org_id: int,
    candidates: dict[str, list[dict[str, Any]]],
    *,
    crawl_id: int | None = None,
    actor: str = "pipeline",
) -> Resolution:
    """Decide which company the named organization ``member_org_id`` is.

    ``candidates`` maps each candidate registrable domain to the signals that
    point to it (``{"signal": ..., "link_id": ..., "page_id": ...}``).
    """
    domain = conn.execute(
        select(Organization.registrable_domain)
        .where(Organization.id == member_org_id)
        .with_for_update()
    ).scalar_one()
    if domain is not None:
        # A row with a domain is a company itself; linking it would allow chains.
        raise IdentityError(f"organization {member_org_id} has a domain and cannot be a member")
    links = _links(conn, IdentityLink.member_org_id == member_org_id)
    rejected = {ln.organization_id for ln in links if ln.status == "rejected"}
    link = next((ln for ln in links if ln.status != "rejected"), None)
    if link is not None and (not link.automatic or not candidates):
        # A reviewer's decision stands; and no evidence here is not evidence against.
        return Resolution(link.status, _followed(link), link.decision_id, link.id)

    signals = [s for d in sorted(candidates) for s in candidates[d]]
    contradictions: list[dict[str, Any]] = []
    org_id: int | None = None
    if not candidates:
        outcome, rationale = "unknown", "no page that names it links to a candidate site"
        known: list[int] = []
    elif len(candidates) > 1:
        outcome = "conflicting"
        rationale = "the pages that name it link to several candidate domains"
        contradictions.append({"signal": "several_candidate_domains",
                               "domains": sorted(candidates)})  # fmt: skip
        known = list(conn.execute(
            select(Organization.id)
            .where(Organization.registrable_domain.in_(candidates))
            .order_by(Organization.id)
        ).scalars())  # fmt: skip
    else:
        (candidate,) = candidates
        org_id = ensure_organization(conn, candidate)
        known = [org_id]
        if org_id in rejected:
            outcome, rationale = "unknown", f"a reviewer rejected {candidate} for this name"
            contradictions.append({"signal": "manual_rejection", "organization_id": org_id})
            org_id = None
        elif link is None:
            outcome = "match"
            rationale = f"the pages that name it link to {candidate} and to no other candidate"
        elif link.organization_id == org_id:
            return Resolution(link.status, _followed(link), link.decision_id, link.id)
        else:
            outcome = "conflicting"
            rationale = f"new evidence points to {candidate}, the current link elsewhere"
    if link is not None and outcome == "conflicting":
        contradictions.append({"signal": "differs_from_current_link",
                               "organization_id": link.organization_id})  # fmt: skip
        known = sorted({*known, link.organization_id})
        org_id = link.organization_id

    decision_id, new = _decide(
        conn, member_org_id=member_org_id, organization_id=org_id, outcome=outcome,
        candidates=known, signals=signals, contradictions=contradictions, rationale=rationale,
        crawl_id=crawl_id, actor=actor,
    )  # fmt: skip
    link_id = link.id if link is not None else None
    if outcome == "match":
        link_id = conn.execute(
            pg_insert(IdentityLink)
            .values(organization_id=org_id, member_org_id=member_org_id, status="match",
                    decision_id=decision_id)
            .returning(IdentityLink.id)
        ).scalar_one()  # fmt: skip
    elif link is not None and new:
        conn.execute(
            update(IdentityLink)
            .where(IdentityLink.id == link.id)
            .values(status="conflicting", decision_id=decision_id)
        )
    return Resolution(outcome, org_id if outcome == "match" else None, decision_id, link_id)


def resolve_profiles(conn: Connection, crawl_id: int, actor: str = "pipeline") -> dict[str, int]:
    """Decide which profiles belong to the company of ``crawl_id``, from the
    links its crawl stored. Returns how many profiles ended in each outcome."""
    org_id, domain = conn.execute(
        select(Crawl.organization_id, Organization.registrable_domain)
        .join(Organization, Organization.id == Crawl.organization_id)
        .where(Crawl.id == crawl_id)
    ).one()
    final, target = aliased(Url), aliased(Url)
    page_domain = func.coalesce(final.registrable_domain, Url.registrable_domain)
    rows = conn.execute(
        select(Link.id, Link.page_id, target.url, func.coalesce(final.url, Url.url), page_domain)
        .join(target, target.id == Link.target_url_id)
        .join(Page, Page.id == Link.page_id)
        .join(Fetch, Fetch.id == Page.fetch_id)
        .join(Url, Url.id == Fetch.url_id)
        .outerjoin(final, final.id == Fetch.final_url_id)
        .where(
            Fetch.crawl_id == crawl_id,
            or_(
                # The company's own page links to a profile ...
                and_(page_domain == domain, target.registrable_domain.in_(PROFILE_DOMAINS)),
                # ... or a profile page links to the company's site.
                and_(page_domain.in_(PROFILE_DOMAINS), target.registrable_domain == domain),
            ),
        )
        .order_by(Link.id)
    ).all()  # fmt: skip
    found: dict[ExternalId, dict[str, list[dict[str, Any]]]] = {}
    urls: dict[ExternalId, str] = {}
    for link_id, page_id, target_url, page_url, on_domain in rows:
        own_page = on_domain == domain
        profile_url = target_url if own_page else page_url
        ext = external_id(profile_url)
        if ext is None:
            continue
        urls.setdefault(ext, profile_url)
        direction = "site_links_profile" if own_page else "profile_links_site"
        seen = found.setdefault(ext, {}).setdefault(direction, [])
        if len(seen) < MAX_SIGNALS:
            seen.append({"signal": direction, "link_id": link_id, "page_id": page_id})

    # Profiles we fetched (a search found them) that are linked in neither
    # direction: recorded as UNKNOWN, so it is on file why nothing was read from them.
    fetched = conn.execute(
        select(func.coalesce(final.url, Url.url))
        .select_from(Page)
        .join(Fetch, Fetch.id == Page.fetch_id)
        .join(Url, Url.id == Fetch.url_id)
        .outerjoin(final, final.id == Fetch.final_url_id)
        .where(Fetch.crawl_id == crawl_id, page_domain.in_(PROFILE_DOMAINS))
        .order_by(Page.id)
    ).scalars()  # fmt: skip
    for page_url in fetched:
        ext = external_id(page_url)
        if ext is not None and ext not in found:
            found[ext] = {}
            urls[ext] = page_url

    reciprocal: dict[str, list[str]] = {}
    for ext in sorted(found, key=lambda e: (e.source_type, e.external_id)):
        if len(found[ext]) == 2:
            reciprocal.setdefault(ext.source_type, []).append(ext.external_id)

    counts: dict[str, int] = {}
    for ext in sorted(found, key=lambda e: (e.source_type, e.external_id)):
        directions = found[ext]
        signals = [s for d in sorted(directions) for s in directions[d]]
        rivals = reciprocal.get(ext.source_type, []) if len(directions) == 2 else []
        outcome = _resolve_profile(
            conn,
            org_id,
            ext,
            urls[ext],
            signals,
            crawl_id,
            actor,
            rivals=[r for r in rivals if r != ext.external_id],
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _resolve_profile(
    conn: Connection,
    org_id: int,
    ext: ExternalId,
    profile_url: str,
    signals: list[dict[str, Any]],
    crawl_id: int,
    actor: str,
    rivals: list[str] | None = None,
) -> str:
    """``rivals``: the company's other reciprocal profiles on the same platform."""
    conn.execute(
        pg_insert(ExternalIdentity)
        .values(source_type=ext.source_type, external_id=ext.external_id, profile_url=profile_url)
        .on_conflict_do_nothing(index_elements=["source_type", "external_id"])
    )
    # Locked: two companies claiming one profile are decided one after the other.
    identity_id = conn.execute(
        select(ExternalIdentity.id)
        .where(ExternalIdentity.source_type == ext.source_type,
               ExternalIdentity.external_id == ext.external_id)
        .with_for_update()
    ).scalar_one()  # fmt: skip
    links = _links(conn, IdentityLink.external_identity_id == identity_id)
    own = next((ln for ln in links if ln.organization_id == org_id), None)
    if own is not None and (not own.automatic or own.status == "rejected"):
        return str(own.status)  # a reviewer's decision stands
    others = [ln for ln in links if ln.organization_id != org_id and ln.status != "rejected"]
    if not signals:
        if own is not None:
            return str(own.status)  # no evidence here is not evidence against
        _decide(
            conn, external_identity_id=identity_id, organization_id=org_id, outcome="unknown",
            candidates=[org_id], signals=[], contradictions=[],
            rationale="fetched, but the site does not link to it and it does not link to the site",
            crawl_id=crawl_id, actor=actor,
        )  # fmt: skip
        return "unknown"

    reciprocal = _reciprocal(signals)
    outcome = "match" if reciprocal else "probable_match"
    rationale = ("the site links to the profile and the profile links back" if reciprocal
                 else "linked in one direction only")  # fmt: skip
    contradictions = [{"signal": "claimed_by_other_organization",
                       "organization_id": ln.organization_id, "status": ln.status}
                      for ln in others]  # fmt: skip
    # A claim counts as verified if it is reciprocal (even while 'conflicting')
    # or a reviewer confirmed it.
    verified_elsewhere = any(_reciprocal(ln.signals) or ln.status == "match" for ln in others)
    if others and (verified_elsewhere or not reciprocal):
        outcome, rationale = "conflicting", "another organization claims this profile"
    if rivals:
        outcome = "conflicting"
        rationale = "several profiles on this platform link to the site and are linked by it"
        contradictions.append({"signal": "several_reciprocal_profiles", "external_ids": rivals})
    decision_id, new = _decide(
        conn, external_identity_id=identity_id, organization_id=org_id, outcome=outcome,
        candidates=sorted({org_id, *(ln.organization_id for ln in others)}),
        signals=signals, contradictions=contradictions, rationale=rationale,
        crawl_id=crawl_id, actor=actor,
    )  # fmt: skip
    # The other claims first (at most one 'match' per profile at any moment).
    # A reciprocal claim is not weakened by a one-way one.
    for ln in others:
        if (
            ln.automatic
            and ln.status != "conflicting"
            and (reciprocal or not _reciprocal(ln.signals))
        ):
            other_decision, _ = _decide(
                conn, external_identity_id=identity_id, organization_id=ln.organization_id,
                outcome="conflicting", candidates=sorted({org_id, ln.organization_id}),
                signals=ln.signals, rationale="another organization claims this profile",
                contradictions=[{"signal": "claimed_by_other_organization",
                                 "organization_id": org_id, "status": outcome}],
                crawl_id=crawl_id, actor=actor,
            )  # fmt: skip
            conn.execute(
                update(IdentityLink)
                .where(IdentityLink.id == ln.id)
                .values(status="conflicting", decision_id=other_decision)
            )
    if own is None:
        conn.execute(
            pg_insert(IdentityLink).values(
                organization_id=org_id, external_identity_id=identity_id, status=outcome,
                decision_id=decision_id,
            )
        )  # fmt: skip
    elif new:
        conn.execute(
            update(IdentityLink)
            .where(IdentityLink.id == own.id)
            .values(status=outcome, decision_id=decision_id)
        )
    return outcome


def review_link(conn: Connection, link_id: int, *, accept: bool, actor: str, rationale: str) -> int:
    """A reviewer confirms (``accept``) or rejects a link. Returns the decision id.

    The automatic decision is superseded, not erased, and the resolver leaves
    the link alone from now on. Nothing else needs repair: facts stay on the
    rows they were recorded on and links are followed when they are read.
    """
    if not rationale.strip() or not actor.strip():
        raise IdentityError("a review needs an actor and a rationale")
    link = conn.execute(
        select(IdentityLink.__table__).where(IdentityLink.id == link_id).with_for_update()
    ).one_or_none()
    if link is None:
        raise IdentityError(f"no identity link {link_id}")
    status = "match" if accept else "rejected"
    decision_id = conn.execute(
        pg_insert(IdentityDecision)
        .values(member_org_id=link.member_org_id, external_identity_id=link.external_identity_id,
                organization_id=link.organization_id, outcome=status,
                candidates=[link.organization_id], rationale=rationale,
                algorithm_version=ALGORITHM_VERSION, actor=actor, automatic=False,
                supersedes_id=link.decision_id)
        .returning(IdentityDecision.id)
    ).scalar_one()  # fmt: skip
    conn.execute(
        update(IdentityLink)
        .where(IdentityLink.id == link_id)
        .values(status=status, decision_id=decision_id)
    )
    IDENTITY_DECISIONS.labels(status, "manual").inc()
    return int(decision_id)


def attributable_pages(
    conn: Connection, crawl_id: int
) -> tuple[set[ExternalId], set[str], set[int]]:
    """What on other sites may be read for facts about ``crawl_id``'s company:
    its profiles (MATCH or PROBABLE_MATCH), the URLs its own pages link to,
    and the ids of fetched pages elsewhere that link to its site."""
    org_id, domain = conn.execute(
        select(Crawl.organization_id, Organization.registrable_domain)
        .join(Organization, Organization.id == Crawl.organization_id)
        .where(Crawl.id == crawl_id)
    ).one()
    profiles = {
        ExternalId(source_type, ext_id)
        for source_type, ext_id in conn.execute(
            select(ExternalIdentity.source_type, ExternalIdentity.external_id)
            .join(IdentityLink, IdentityLink.external_identity_id == ExternalIdentity.id)
            .where(IdentityLink.organization_id == org_id,
                   IdentityLink.status.in_(("match", "probable_match")))
        )
    }  # fmt: skip
    final, target = aliased(Url), aliased(Url)
    linked = set(conn.execute(
        select(target.url)
        .select_from(Link)
        .join(target, target.id == Link.target_url_id)
        .join(Page, Page.id == Link.page_id)
        .join(Fetch, Fetch.id == Page.fetch_id)
        .join(Url, Url.id == Fetch.url_id)
        .outerjoin(final, final.id == Fetch.final_url_id)
        .where(Fetch.crawl_id == crawl_id,
               func.coalesce(final.registrable_domain, Url.registrable_domain) == domain,
               target.registrable_domain != domain)
    ).scalars())  # fmt: skip
    linking = set(conn.execute(
        select(Link.page_id)
        .join(target, target.id == Link.target_url_id)
        .join(Page, Page.id == Link.page_id)
        .join(Fetch, Fetch.id == Page.fetch_id)
        .join(Url, Url.id == Fetch.url_id)
        .outerjoin(final, final.id == Fetch.final_url_id)
        .where(Fetch.crawl_id == crawl_id,
               func.coalesce(final.registrable_domain, Url.registrable_domain) != domain,
               target.registrable_domain == domain)
    ).scalars())  # fmt: skip
    return profiles, linked, linking


def canonical_ids(conn: Connection, org_ids: list[int]) -> dict[int, Any]:
    """For each named organization in ``org_ids`` with a MATCH link: that link
    (``organization_id`` is the company, ``decision_id`` why)."""
    rows = conn.execute(
        select(
            IdentityLink.member_org_id, IdentityLink.organization_id, IdentityLink.decision_id
        ).where(IdentityLink.member_org_id.in_(org_ids), IdentityLink.status == "match")
    ).all()
    return {row.member_org_id: row for row in rows}


def _followed(link: Any) -> int | None:
    return int(link.organization_id) if link.status == "match" else None


def _links(conn: Connection, where: Any) -> list[Any]:
    """Links with their decision's ``automatic`` flag and ``signals``, oldest first."""
    return list(conn.execute(
        select(IdentityLink.id, IdentityLink.organization_id, IdentityLink.status,
               IdentityLink.decision_id, IdentityDecision.automatic, IdentityDecision.signals)
        .join(IdentityDecision, IdentityDecision.id == IdentityLink.decision_id)
        .where(where)
        .order_by(IdentityLink.id)
    ).all())  # fmt: skip


def _reciprocal(signals: list[dict[str, Any]]) -> bool:
    return {s.get("signal") for s in signals} >= {"site_links_profile", "profile_links_site"}


def _decide(
    conn: Connection,
    *,
    organization_id: int | None,
    outcome: str,
    candidates: list[int],
    signals: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    rationale: str,
    crawl_id: int | None,
    actor: str,
    member_org_id: int | None = None,
    external_identity_id: int | None = None,
) -> tuple[int, bool]:
    """Record a decision unless the latest one for the same subject already says
    the same on the same evidence. Returns (decision id, whether it is new)."""
    if member_org_id is not None:
        subject = IdentityDecision.member_org_id == member_org_id
    else:
        subject = and_(IdentityDecision.external_identity_id == external_identity_id,
                       IdentityDecision.organization_id == organization_id)  # fmt: skip
    last = conn.execute(
        select(IdentityDecision.__table__)
        .where(subject)
        .order_by(IdentityDecision.id.desc())
        .limit(1)
    ).one_or_none()
    if last is not None and (
        last.outcome, last.organization_id, last.signals, last.contradictions,
        last.algorithm_version, last.automatic,
    ) == (outcome, organization_id, signals, contradictions, ALGORITHM_VERSION, True):  # fmt: skip
        return int(last.id), False
    decision_id = conn.execute(
        pg_insert(IdentityDecision)
        .values(member_org_id=member_org_id, external_identity_id=external_identity_id,
                organization_id=organization_id, outcome=outcome, candidates=candidates,
                signals=signals, contradictions=contradictions, rationale=rationale,
                algorithm_version=ALGORITHM_VERSION, actor=actor, automatic=True,
                crawl_id=crawl_id, supersedes_id=last.id if last is not None else None)
        .returning(IdentityDecision.id)
    ).scalar_one()  # fmt: skip
    IDENTITY_DECISIONS.labels(outcome, "automatic").inc()
    return int(decision_id), True
