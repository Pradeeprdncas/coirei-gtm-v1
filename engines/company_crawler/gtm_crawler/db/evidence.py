"""Recording observations with their evidence, and tracing them back.

``EvidenceStore.record`` is the only way observations are written. It
re-parses the page's raw document and refuses, before anything is stored:

* a quote that is not exactly at the location it claims on the page it cites;
* a *stated* value that is not word-for-word in its quote (a source cannot
  "state" something it does not say), or that cuts a qualifier off the quote
  ("500 customers" out of "more than 500 customers" or "500+ customers");
* a normalized value without an explicit qualifier, so "more than 500
  customers" can never be stored as a bare 500.

Source tier and type, confidence, publication and retrieval times are filled
in from the page and its fetch, not supplied by the extractor.

``verify`` re-parses the raw document an evidence row points to and checks
the quote is still exactly there, so every citation can be re-checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from ..config import ParseSettings, SourceSettings, UrlSettings, _build
from ..evidence import Quote, Source, classify_source, observation_confidence, resolve
from ..identity import normalize_name
from ..metrics import EVIDENCE_CREATED, OBSERVATIONS_CREATED
from ..parse import ParsedPage, parse_page
from ..rawstore import RawStoreBackend, RawStoreError
from .models import (
    OBSERVATION_KINDS,
    RELATIONSHIP_TYPES,
    Artifact,
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
    RobotsSnapshot,
    Url,
)

# A stated value right after one of these (or right before "+") has lost its qualifier.
_QUALIFIER_BEFORE = re.compile(
    r"(?<!\w)(?:more than|greater than|over|above|up to|nearly|almost|about|around|approximately|"
    r"approx\.|roughly|some|at least|at most|less than|fewer than|under|below|close to|"
    r"in excess of|upwards of|starting at|starting from|starts? at|as low as|~|>|<|≈)\s*$",
    re.IGNORECASE,
)
# A stated value whose sentence, before the value, says it is not so, or not so
# yet: "we do not serve 500 customers", "we plan to open in Berlin".
# ponytail: word list on the text before the value in its sentence; a hedge
# after the value ("500 customers is our goal") is not seen. Add a parser if
# rejected or missed statements show up in the corpus.
_NOT_ASSERTED = re.compile(
    r"(?<!\w)(?:(?i:not|no longer|never|\w+n['’]t|cannot|plans? to|planning to|planned|"
    r"exploring|explore|considering|expects? to|expected to|aims? to|aiming to|hopes? to|"
    r"hoping to|intends? to|goal of|target of|targeting|projected|forecast|on track to|"
    r"soon|upcoming)|will|would|could|might)(?!\w)"
)
_SENTENCE_END = re.compile(r"[.!?;]\s")
QUALIFIERS = frozenset({"eq", "gt", "gte", "lt", "lte", "approx", "range"})


class EvidenceError(ValueError):
    """An observation that would not be supported by its own evidence."""


@dataclass(frozen=True)
class RelatedOrg:
    """Another organization an observation links the company to (an investor, a partner).

    Stored as a ``relationship`` row citing the observation. ``name`` must
    appear in the quote; organizations known only by name are matched by name.
    """

    type: str
    name: str
    basis: str


@dataclass(frozen=True)
class ObservationDraft:
    field: str
    value_verbatim: str
    quote: Quote
    extractor: str
    extractor_version: str
    extraction_certainty: float
    value_normalized: dict[str, Any] | None = None
    qualifier: str | None = None
    as_of: date | None = None
    kind: str = "stated"
    product_id: int | None = None
    # Overrides the URL-based classification, e.g. a verified partner announcement.
    source: Source | None = None
    related: RelatedOrg | None = None


class EvidenceStore:
    def __init__(self, engine: Engine, rawstore: RawStoreBackend, sources: SourceSettings) -> None:
        self.engine = engine
        self.rawstore = rawstore
        self.sources = sources

    def start_extraction(
        self, crawl_id: int, trigger: str = "crawl", versions: dict[str, Any] | None = None
    ) -> int:
        """A new extraction run over ``crawl_id``; it supersedes the crawl's earlier runs.

        ``versions`` (``versions.current_versions``) and ``trigger`` ('crawl' or
        'replay') record what produced the run (spec sections 24, 29)."""
        with self.engine.begin() as conn:
            return conn.execute(
                insert(Extraction)
                .values(crawl_id=crawl_id, trigger=trigger, versions=versions)
                .returning(Extraction.id)
            ).scalar_one()

    def record(
        self, extraction_id: int, organization_id: int, page_id: int, drafts: list[ObservationDraft]
    ) -> list[int]:
        """Store each draft as evidence + observation, all or nothing. Returns observation ids."""
        row, page = self.load_page(page_id)
        for draft in drafts:
            check_draft(draft, page)
        with self.engine.begin() as conn:
            crawl_id = conn.execute(
                select(Extraction.crawl_id).where(Extraction.id == extraction_id)
            ).scalar_one()
            subject = conn.execute(
                select(Organization.registrable_domain).where(Organization.id == organization_id)
            ).scalar_one()
            ids = []
            for draft in drafts:
                source = draft.source or classify_source(row.page_url, subject, self.sources)
                q = draft.quote
                evidence_id = conn.execute(
                    insert(Evidence)
                    .values(page_id=page_id, quote=q.text, locator_type=q.locator_type,
                            locator=q.locator, char_start=q.char_start, char_end=q.char_end)
                    .returning(Evidence.id)
                ).scalar_one()  # fmt: skip
                observation_id = conn.execute(
                        insert(Observation)
                        .values(
                            crawl_id=crawl_id, extraction_id=extraction_id,
                            organization_id=organization_id,
                            product_id=draft.product_id, field=draft.field,
                            value_verbatim=draft.value_verbatim,
                            value_normalized=draft.value_normalized, qualifier=draft.qualifier,
                            as_of=draft.as_of, kind=draft.kind, evidence_id=evidence_id,
                            source_tier=source.tier, source_type=source.type,
                            is_first_party=source.first_party, extractor=draft.extractor,
                            extractor_version=draft.extractor_version,
                            extraction_certainty=_score(draft.extraction_certainty),
                            confidence=_score(
                                observation_confidence(
                                    draft.extraction_certainty, source, draft.kind
                                )
                            ),
                            published_at=row.published_at, retrieved_at=row.fetched_at,
                        )
                        .returning(Observation.id)
                    ).scalar_one()  # fmt: skip
                ids.append(observation_id)
                if draft.related is not None:
                    r = draft.related
                    conn.execute(
                        insert(Relationship).values(
                            subject_org_id=organization_id,
                            object_org_id=_org_by_name(conn, r.name, organization_id),
                            type=r.type, basis=r.basis, observation_id=observation_id,
                        )
                    )  # fmt: skip
        EVIDENCE_CREATED.inc(len(ids))
        OBSERVATIONS_CREATED.inc(len(ids))
        return ids

    def ensure_product(self, organization_id: int, slug: str, name: str, basis: str) -> int:
        """The product row for (organization, slug), created on first sighting."""
        with self.engine.begin() as conn:
            conn.execute(
                pg_insert(Product)
                .values(organization_id=organization_id, slug=slug, name=name,
                        identification_basis=basis)
                .on_conflict_do_nothing(index_elements=["organization_id", "slug"])
            )  # fmt: skip
            return int(
                conn.execute(
                    select(Product.id).where(
                        Product.organization_id == organization_id, Product.slug == slug
                    )
                ).scalar_one()
            )

    def trace(self, observation_id: int) -> dict[str, Any]:
        """The full chain behind one observation (spec sections 15, 25): value,
        evidence span, page, URL, retrieval event and raw artifact, with the
        parser and extractor versions that produced them."""
        final = aliased(Url)
        with Session(self.engine) as session:
            row = session.execute(
                select(
                    Observation, Evidence, Artifact, Fetch.id.label("fetch_id"), Fetch.method,
                    Fetch.fetched_at, Fetch.status_code, Fetch.content_type, Fetch.raw_sha256,
                    Page.parser_version, Url.url, final.url.label("final_url"),
                    Crawl.seed_url, Crawl.started_at.label("crawl_started_at"),
                )
                .join(Evidence, Evidence.id == Observation.evidence_id)
                .join(Page, Page.id == Evidence.page_id)
                .join(Fetch, Fetch.id == Page.fetch_id)
                .outerjoin(Artifact, Artifact.sha256 == Fetch.raw_sha256)
                .join(Url, Url.id == Fetch.url_id)
                .outerjoin(final, final.id == Fetch.final_url_id)
                .join(Crawl, Crawl.id == Observation.crawl_id)
                .where(Observation.id == observation_id)
            ).one()  # fmt: skip
        o, e, a = row.Observation, row.Evidence, row.Artifact
        return {
            "observation_id": o.id, "organization_id": o.organization_id,
            "product_id": o.product_id, "field": o.field, "kind": o.kind,
            "value_verbatim": o.value_verbatim, "value_normalized": o.value_normalized,
            "qualifier": o.qualifier, "as_of": o.as_of,
            "source": {"tier": o.source_tier, "type": o.source_type,
                       "first_party": o.is_first_party},
            "extractor": f"{o.extractor}@{o.extractor_version}",
            "extraction_certainty": o.extraction_certainty, "confidence": o.confidence,
            "evidence": {"id": e.id, "quote": e.quote, "locator_type": e.locator_type,
                         "locator": e.locator, "char_start": e.char_start,
                         "char_end": e.char_end},
            "extraction_id": o.extraction_id,
            "page_id": e.page_id, "parser_version": row.parser_version,
            "url": row.final_url or row.url, "requested_url": row.url,
            "fetch": {"id": row.fetch_id, "method": row.method, "raw_sha256": row.raw_sha256,
                      "fetched_at": row.fetched_at, "status": row.status_code,
                      "content_type": row.content_type},
            "artifact": None if a is None else {
                "sha256": a.sha256, "size_bytes": a.size_bytes, "content_type": a.content_type,
                "version": a.artifact_version, "stored_at": a.created_at,
                "pruned_at": a.pruned_at},
            "published_at": o.published_at, "retrieved_at": o.retrieved_at,
            "observed_at": o.created_at,
            "crawl": {"id": o.crawl_id, "seed_url": row.seed_url,
                      "started_at": row.crawl_started_at},
        }  # fmt: skip

    def field_lineage(
        self, organization_id: int, field: str, product_id: int | None = None
    ) -> dict[str, Any]:
        """Why the system states what it states for one field: every item of
        its latest assessment (one for a scalar field, one per value for a list
        field) with the full ``trace`` of every observation it cites.

        A field never assessed, or assessed UNKNOWN, has no observations and
        says so; lineage never invents a source.
        """
        scope = (FieldAssessment.organization_id == organization_id,
                 FieldAssessment.field == field,
                 FieldAssessment.product_id.is_(None) if product_id is None
                 else FieldAssessment.product_id == product_id)  # fmt: skip
        with Session(self.engine) as session:
            latest = select(func.max(FieldAssessment.computed_at)).where(*scope).scalar_subquery()
            rows = session.execute(
                select(FieldAssessment)
                .where(*scope, FieldAssessment.computed_at == latest)
                .order_by(FieldAssessment.id)
            ).scalars().all()  # fmt: skip
        items = [
            {"assessment_id": fa.id, "state": fa.state, "value": fa.value,
             "confidence": fa.confidence, "method_version": fa.method_version,
             "computed_at": fa.computed_at,
             "observations": [self.trace(i) for i in fa.observation_ids]}
            for fa in rows
        ] or [{"assessment_id": None, "state": "UNKNOWN", "observations": []}]  # fmt: skip
        return {"organization_id": organization_id, "product_id": product_id,
                "field": field, "items": items}  # fmt: skip

    def verify(self, evidence_id: int) -> str | None:
        """Re-derive the quote from the raw document. None if it matches, else the reason."""
        with Session(self.engine) as session:
            e = session.execute(select(Evidence).where(Evidence.id == evidence_id)).scalar_one()
        try:
            _, page = self.load_page(e.page_id)
        except EvidenceError as exc:
            return str(exc)
        found = resolve(page, Quote(e.locator_type, e.locator, e.quote, e.char_start, e.char_end))
        if found is None:
            return f"locator not found: {e.locator}"
        if found != e.quote:
            return f"quote differs: {found[:200]!r}"
        return None

    def load_page(self, page_id: int) -> tuple[Any, ParsedPage]:
        """The page's fetch row and its raw document parsed exactly as the crawl parsed it.

        Raises ``EvidenceError`` if the raw document is missing or corrupt.
        """
        final = aliased(Url)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    Page.published_at, Fetch.fetched_at, Fetch.raw_sha256, Fetch.headers,
                    func.coalesce(final.url, Url.url).label("page_url"), Crawl.config,
                )
                .join(Fetch, Fetch.id == Page.fetch_id)
                .join(Url, Url.id == Fetch.url_id)
                .outerjoin(final, final.id == Fetch.final_url_id)
                .join(Crawl, Crawl.id == Fetch.crawl_id)
                .where(Page.id == page_id)
            ).one()  # fmt: skip
        if row.raw_sha256 is None:
            raise EvidenceError(f"page {page_id}: no raw document")
        try:
            body = self.rawstore.get(row.raw_sha256)
        except RawStoreError as exc:
            raise EvidenceError(f"page {page_id}: raw document unavailable: {exc}") from exc
        # XPaths and offsets depend on the parse settings, so use the crawl's own.
        parse = _build(ParseSettings, row.config.get("parse", {}), "parse.")
        urls = _build(UrlSettings, row.config.get("urls", {}), "urls.")
        content_type = (row.headers or {}).get("content-type")
        return row, parse_page(body, row.page_url, content_type, parse, urls)

    def evidence_raw_digests(self) -> set[str]:
        """Raw documents that evidence cites, and the robots.txt files crawls obeyed:
        the keep set for ``RawStore.prune``."""
        cited = (
            select(Fetch.raw_sha256)
            .join(Page, Page.fetch_id == Fetch.id)
            .join(Evidence, Evidence.page_id == Page.id)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(cited.union(select(RobotsSnapshot.raw_sha256))).scalars()
            return {digest for digest in rows if digest is not None}


def _org_by_name(conn: Connection, name: str, named_by: int) -> int:
    """``named_by``'s row for the organization it calls ``name``, created on first
    mention. Spelled otherwise (case, spacing, punctuation, a trailing legal
    form: "SproutSocial", "Sprout Social, Inc."), it is the same mention.

    The row is that company's mention of a name, nothing more: the same name
    on another company's pages is another row, because a name alone does not
    say two companies are the same. Which company a mention is, is decided
    from evidence and stored as an ``identity_link`` (``db/identity.py``).
    """
    # ponytail: scans the company's named rows per mention; fine for hundreds of
    # names, store the key in an indexed column if one company names thousands.
    key = _name_key(name)
    named = conn.execute(select(Organization.id, Organization.name)
                         .where(Organization.named_by_org_id == named_by)
                         .order_by(Organization.id))  # fmt: skip
    for org_id, other in named:
        if key and _name_key(other) == key:
            return int(org_id)
    scope = (Organization.named_by_org_id == named_by,
             func.lower(Organization.name) == func.lower(name))  # fmt: skip
    conn.execute(
        pg_insert(Organization)
        .values(name=name, named_by_org_id=named_by)
        .on_conflict_do_nothing(
            index_elements=[Organization.named_by_org_id, func.lower(Organization.name)],
            index_where=Organization.named_by_org_id.is_not(None),
        )
    )
    return int(conn.execute(select(Organization.id).where(*scope)).scalar_one())


def _name_key(name: str) -> str:
    return normalize_name(name).replace(" ", "")


def check_draft(draft: ObservationDraft, page: ParsedPage) -> None:
    q = draft.quote
    if draft.kind not in OBSERVATION_KINDS:
        raise EvidenceError(f"{draft.field}: unknown kind {draft.kind!r}")
    if not q.text.strip():
        raise EvidenceError(f"{draft.field}: empty quote")
    if resolve(page, q) != q.text:
        raise EvidenceError(f"{draft.field}: quote not at {q.locator}: {q.text[:100]!r}")
    if not draft.value_verbatim.strip():
        raise EvidenceError(f"{draft.field}: empty value")
    if draft.kind == "stated" and draft.value_verbatim not in q.text:
        raise EvidenceError(
            f"{draft.field}: stated value {draft.value_verbatim!r} is not in its quote"
        )
    if draft.kind == "stated" and _drops_qualifier(draft.value_verbatim, q.text):
        raise EvidenceError(
            f"{draft.field}: stated value {draft.value_verbatim!r} drops a qualifier in its quote"
        )
    if draft.kind == "stated" and _not_asserted(draft.value_verbatim, q.text):
        raise EvidenceError(
            f"{draft.field}: stated value {draft.value_verbatim!r} is negated or not yet true "
            "in its quote"
        )
    if draft.value_normalized is not None:
        qualifier = draft.value_normalized.get("qualifier")
        if qualifier not in QUALIFIERS:
            raise EvidenceError(
                f"{draft.field}: normalized value needs a qualifier in {sorted(QUALIFIERS)}"
            )
    if draft.related is not None:
        if draft.related.type not in RELATIONSHIP_TYPES:
            raise EvidenceError(f"{draft.field}: unknown relationship {draft.related.type!r}")
        if not draft.related.name.strip() or draft.related.name not in q.text:
            raise EvidenceError(f"{draft.field}: related organization is not named in the quote")
    if not 0 <= draft.extraction_certainty <= 1:
        raise EvidenceError(f"{draft.field}: extraction_certainty must be between 0 and 1")


def _drops_qualifier(value: str, quote: str) -> bool:
    """True if every occurrence of ``value`` in ``quote`` is qualified outside the value."""
    starts = [m.start() for m in re.finditer(re.escape(value), quote)]
    return bool(starts) and all(
        _QUALIFIER_BEFORE.search(quote[:start]) is not None
        or quote[start + len(value) : start + len(value) + 1] == "+"
        for start in starts
    )


def _not_asserted(value: str, quote: str) -> bool:
    """True if every occurrence of ``value`` in ``quote`` follows, in its own
    sentence, a negation or a word that makes it a plan rather than a fact."""

    def hedged(start: int) -> bool:
        before = quote[:start]
        ends = [m.end() for m in _SENTENCE_END.finditer(before)]
        return _NOT_ASSERTED.search(before[ends[-1] if ends else 0 :]) is not None

    starts = [m.start() for m in re.finditer(re.escape(value), quote)]
    return bool(starts) and all(hedged(start) for start in starts)


def _score(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))
