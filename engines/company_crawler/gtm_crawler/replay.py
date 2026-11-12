"""Replay: the current parser and extractors over stored crawls (spec sections 24, 25, 29).

A replay re-parses every page of a crawl from its stored raw artifact and runs
the current extractors; nothing is fetched. It is compared with the crawl's
current (latest) extraction run:

* dry run (``write=False``): computed in memory, nothing is written (products
  are keyed by slug, no ``product`` rows). Field states are not compared:
  assessing needs recorded observations.
* write (``write=True``): a new extraction run (``trigger='replay'``, with the
  current versions) supersedes the old one, the organization is re-assessed,
  and the new run is compared with the previous one, as are the field states
  (latest ``field_assessment`` rows) before and after. Old observations stay
  queryable; they are superseded, never deleted.

Observations are matched by identity: page URL, field, product slug, qualifier,
verbatim and normalized value, evidence locator (type, locator, character
span) and related organization (type, basis, name). Same identity with a
different kind, extractor, extraction certainty or as-of date is "changed".
An extractor version alone is not a change (only a difference in output is);
versions are reported separately from ``extraction.versions`` (NULL before
migration 0014: unknown).

Pages whose raw artifact is missing or pruned are reported in
``unreadable_pages``; their observations show up as removed.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, aliased

from .assess import assess_organization
from .config import Settings
from .db.evidence import EvidenceStore, ObservationDraft
from .db.models import (
    Crawl,
    Evidence,
    Extraction,
    Fetch,
    FieldAssessment,
    Observation,
    Organization,
    Page,
    Product,
    Relationship,
    Url,
)
from .extract import plan_extraction, record_extraction
from .rawstore import make_rawstore
from .tracing import span
from .versions import current_versions

log = logging.getLogger(__name__)

REPLAYABLE = ("completed", "partial")
IDENTITY = ("url", "field", "product", "qualifier", "value_verbatim", "value_normalized",
            "locator", "related")  # fmt: skip
COMPARED = ("kind", "extractor", "extraction_certainty", "as_of")


class ReplayError(Exception):
    """A crawl that cannot be replayed (unknown, or not finished)."""


def replay_crawl(
    engine: Engine, settings: Settings, crawl_id: int, *, write: bool
) -> dict[str, Any]:
    """Replay one crawl and compare it with its current extraction run."""
    with engine.connect() as conn:
        crawl = conn.execute(
            select(Crawl.status, Crawl.organization_id).where(Crawl.id == crawl_id)
        ).one_or_none()
        if crawl is None:
            raise ReplayError(f"no crawl {crawl_id}")
        if crawl.status not in REPLAYABLE:
            raise ReplayError(f"crawl {crawl_id} is {crawl.status}: nothing complete to replay")
        previous = conn.execute(
            select(Extraction.id, Extraction.versions)
            .where(Extraction.crawl_id == crawl_id)
            .order_by(Extraction.id.desc())
            .limit(1)
        ).one_or_none()  # fmt: skip
    org_id = crawl.organization_id
    before = stored_observations(engine, previous.id) if previous else []
    store = EvidenceStore(engine, make_rawstore(settings.rawstore), settings.sources)
    out: dict[str, Any] = {"crawl_id": crawl_id, "organization_id": org_id,
                           "mode": "write" if write else "dry_run",
                           "baseline_extraction_id": previous.id if previous else None}  # fmt: skip
    with span("replay", crawl_id=crawl_id, stage="extract", organization_id=org_id):
        plan = plan_extraction(store, crawl_id, write_products=write, llm_model=settings.llm.model)
        if write:
            states_before = field_states(engine, org_id)
            stats = record_extraction(store, settings, crawl_id, plan, "replay")
            out["extraction_id"] = stats["extraction_id"]
            out["assessment"] = assess_organization(engine, org_id, settings.assessment)
            after = stored_observations(engine, stats["extraction_id"])
            versions = _versions(engine, stats["extraction_id"])
        else:
            slugs = {pid: slug for slug, pid in plan.products.items()}
            after = [_draft(url, d, slugs) for _, url, drafts in plan.pages for d in drafts]
            versions = current_versions(settings)
    out["versions"] = _version_diff(previous.versions if previous else None, versions)
    out["pages"] = len(plan.pages)
    out["unreadable_pages"] = plan.unreadable
    out["rejected_drafts"] = plan.rejected
    out["observations"] = diff_observations(before, after)
    out["field_states"] = (
        _transitions(states_before, field_states(engine, org_id)) if write else None
    )
    o = out["observations"]
    out["changed"] = bool(o["added"] or o["removed"] or o["changed"] or out["field_states"])
    log.info("replay.completed", extra={"crawl_id": crawl_id, "mode": out["mode"],
                                        "changed": out["changed"]})  # fmt: skip
    return out


def replay_many(
    engine: Engine,
    settings: Settings,
    crawl_ids: list[int] | None = None,
    *,
    organization_id: int | None = None,
    write: bool,
) -> dict[str, Any]:
    """Replay ``crawl_ids``, or every finished crawl (of ``organization_id``, if given).

    A crawl that fails is recorded with its error and the batch goes on."""
    if crawl_ids is None:
        query = select(Crawl.id).where(Crawl.status.in_(REPLAYABLE)).order_by(Crawl.id)
        if organization_id is not None:
            query = query.where(Crawl.organization_id == organization_id)
        with engine.connect() as conn:
            crawl_ids = list(conn.execute(query).scalars())
    results: list[dict[str, Any]] = []
    for crawl_id in crawl_ids:
        try:
            results.append(replay_crawl(engine, settings, crawl_id, write=write))
        except Exception as exc:
            log.warning("replay.failed", extra={"crawl_id": crawl_id, "error": str(exc)})
            results.append({"crawl_id": crawl_id, "error": f"{type(exc).__name__}: {exc}"})
    ok = [r for r in results if "error" not in r]
    totals = {
        "crawls": len(results), "failed": len(results) - len(ok),
        "changed_crawls": sum(r["changed"] for r in ok),
        **{k: sum(len(r["observations"][k]) for r in ok) for k in ("added", "removed", "changed")},
        "field_state_transitions": sum(len(r["field_states"] or []) for r in ok),
        "unreadable_pages": sum(len(r["unreadable_pages"]) for r in ok),
        "rejected_drafts": sum(len(r["rejected_drafts"]) for r in ok),
    }  # fmt: skip
    return {"mode": "write" if write else "dry_run", "totals": totals,
            "changed": totals["changed_crawls"] > 0, "crawls": results}  # fmt: skip


def diff_observations(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Added, removed and changed observations (see the module docstring), per field too."""
    old, new = _group(before), _group(after)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for key in sorted(old.keys() | new.keys()):
        was, now = sorted(old.get(key, []), key=_sort), sorted(new.get(key, []), key=_sort)
        for b, a in zip(was, now, strict=False):
            if _compared(b) != _compared(a):
                changed.append({"before": b, "after": a})
        added += now[len(was) :]
        removed += was[len(now) :]
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for name, items in (("before", before), ("after", after), ("added", added),
                        ("removed", removed)):  # fmt: skip
        for o in items:
            counts[o["field"]][name] += 1
    for c in changed:
        counts[c["after"]["field"]]["changed"] += 1
    per_field = {f: {k: counts[f][k] for k in ("before", "after", "added", "removed", "changed")}
                 for f in sorted(counts)}  # fmt: skip
    return {"before": len(before), "after": len(after), "added": added, "removed": removed,
            "changed": changed, "per_field": per_field}  # fmt: skip


def stored_observations(engine: Engine, extraction_id: int) -> list[dict[str, Any]]:
    """The observations of one extraction run, in the comparison form."""
    final = aliased(Url)
    with Session(engine) as session:
        rows = session.execute(
            select(Observation, Evidence.locator_type, Evidence.locator, Evidence.char_start,
                   Evidence.char_end, func.coalesce(final.url, Url.url).label("page_url"),
                   Product.slug, Relationship.type.label("rel_type"),
                   Relationship.basis.label("rel_basis"), Organization.name.label("rel_name"))
            .join(Evidence, Evidence.id == Observation.evidence_id)
            .join(Page, Page.id == Evidence.page_id)
            .join(Fetch, Fetch.id == Page.fetch_id)
            .join(Url, Url.id == Fetch.url_id)
            .outerjoin(final, final.id == Fetch.final_url_id)
            .outerjoin(Product, Product.id == Observation.product_id)
            .outerjoin(Relationship, Relationship.observation_id == Observation.id)
            .outerjoin(Organization, Organization.id == Relationship.object_org_id)
            .where(Observation.extraction_id == extraction_id)
            .order_by(Observation.id)
        ).all()  # fmt: skip
    out = []
    for r in rows:
        o = r.Observation
        related = f"{r.rel_type}:{r.rel_basis}:{r.rel_name.lower()}" if r.rel_type else None
        out.append(_form(r.page_url, o.field, r.slug, o.qualifier, o.value_verbatim,
                         o.value_normalized, (r.locator_type, r.locator, r.char_start,
                                              r.char_end), related, o.kind, o.extractor,
                         o.extractor_version, float(o.extraction_certainty), o.as_of))  # fmt: skip
    return out


def field_states(engine: Engine, organization_id: int) -> dict[tuple[str, str | None], list[Any]]:
    """The organization's latest assessment: (field, product slug) -> sorted (state, value)."""
    with engine.connect() as conn:
        latest = select(func.max(FieldAssessment.computed_at)).where(
            FieldAssessment.organization_id == organization_id
        ).scalar_subquery()  # fmt: skip
        rows = conn.execute(
            select(FieldAssessment.field, Product.slug, FieldAssessment.state,
                   FieldAssessment.value)
            .outerjoin(Product, Product.id == FieldAssessment.product_id)
            .where(FieldAssessment.organization_id == organization_id,
                   FieldAssessment.computed_at == latest)
        ).all()  # fmt: skip
    out: dict[tuple[str, str | None], list[Any]] = defaultdict(list)
    for field, slug, state, value in rows:
        out[(field, slug)].append({"state": state, "value": value})
    return {k: sorted(v, key=_sort) for k, v in out.items()}


def _transitions(
    before: dict[tuple[str, str | None], list[Any]], after: dict[tuple[str, str | None], list[Any]]
) -> list[dict[str, Any]]:
    return [
        {"field": field, "product": slug,
         "from": "/".join(sorted({s["state"] for s in before.get((field, slug), [])})) or None,
         "to": "/".join(sorted({s["state"] for s in after.get((field, slug), [])})) or None,
         "before": before.get((field, slug), []), "after": after.get((field, slug), [])}
        for field, slug in sorted(before.keys() | after.keys(), key=lambda k: (k[0], k[1] or ""))
        if before.get((field, slug), []) != after.get((field, slug), [])
    ]  # fmt: skip


def _versions(engine: Engine, extraction_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        versions: dict[str, Any] | None = conn.execute(
            select(Extraction.versions).where(Extraction.id == extraction_id)
        ).scalar_one()
    return versions


def _version_diff(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """Old and new versions; ``changed`` is None when the old ones are unknown (pre-0014)."""
    changed = None
    if old is not None and new is not None:
        keys = sorted(old.keys() | new.keys())
        changed = {k: {"before": old.get(k), "after": new.get(k)}
                   for k in keys if old.get(k) != new.get(k)}  # fmt: skip
    return {"before": old, "after": new, "changed": changed}


def _draft(url: str, d: ObservationDraft, slugs: dict[int, str]) -> dict[str, Any]:
    q = d.quote
    related = (f"{d.related.type}:{d.related.basis}:{d.related.name.lower()}"
               if d.related else None)  # fmt: skip
    return _form(url, d.field, slugs.get(d.product_id) if d.product_id is not None else None,
                 d.qualifier, d.value_verbatim, d.value_normalized,
                 (q.locator_type, q.locator, q.char_start, q.char_end), related, d.kind,
                 d.extractor, d.extractor_version, d.extraction_certainty, d.as_of)  # fmt: skip


def _form(url: str, field: str, product: str | None, qualifier: str | None, verbatim: str,
          normalized: Any, locator: tuple[Any, ...], related: str | None, kind: str,
          extractor: str, extractor_version: str, certainty: float,
          as_of: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "url": url, "field": field, "product": product, "qualifier": qualifier,
        "value_verbatim": verbatim,
        # Round-tripped through JSON, as JSONB stores it.
        "value_normalized": json.loads(json.dumps(normalized, default=str)),
        "locator": {"type": locator[0], "locator": locator[1], "char_start": locator[2],
                    "char_end": locator[3]},
        "related": related, "kind": kind, "extractor": extractor,
        "extractor_version": extractor_version,
        "extraction_certainty": f"{certainty:.2f}",  # stored as NUMERIC(3, 2)
        "as_of": as_of.isoformat() if as_of is not None else None,
    }  # fmt: skip


def _group(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in items:
        out[json.dumps([o[k] for k in IDENTITY], sort_keys=True)].append(o)
    return out


def _compared(o: dict[str, Any]) -> list[Any]:
    return [o[k] for k in COMPARED]


def _sort(o: Any) -> str:
    return json.dumps(o, sort_keys=True, default=str)
