"""Competitor ads: manual import, storage, evidence and the report section.

Design and source limits: ``docs/ad-intelligence-design.md``. In short: no
permitted automated source exists for commercial ads shown in India (Meta's
Ad Library API returns non-EU ads only when they are about social issues,
elections or politics; Meta forbids automated collection without written
permission; Google offers no public API outside the EEA). So an operator
looks at the public Ad Library / Ads Transparency Center pages and records
what they see in an import file (``gtm-market --import-ads``):

* ``import_ads`` validates the file, keeps each record and screenshot in the
  raw store, OCRs screenshots to corroborate the transcription, links the
  advertiser profile to the company the operator names (an operator's
  ``identity_decision``, never a name match) and appends one
  ``ad_observation`` per ad. Re-importing the same file adds nothing.
* ``capture_checklist`` (``gtm-market --ad-checklist COMPANY_ID``) lists a
  crawled company's competitors with Ad Library / Ads Transparency Center
  search links for the operator to open by hand, and what is captured so far.
* ``load_ads`` reads the observations of the competitors of a research run
  (known competitors, and those of the crawled company's dossier);
  ``ad_evidence`` turns the latest capture of each ad into evidence (what the
  advertiser *says*, dimension ``messaging`` only) and lists the landing pages
  on the advertiser's own site to fetch.
* ``ad_section`` reports observations, counts derived from them and LOW
  hypotheses. ``performance_claims`` finds statements that infer spend, reach,
  targeting or performance from ads: public creatives say none of that.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import UrlSettings
from ..db.models import (
    Ad,
    AdObservation,
    ExternalIdentity,
    IdentityDecision,
    IdentityLink,
    Organization,
)
from ..db.store import ensure_organization
from ..parse import ParsedPage
from ..policy import ad_library
from ..rawstore import RawStoreBackend
from ..urls import InvalidUrl, normalize_url, registrable_domain, url_host
from .assessment import confidence
from .extract import dimensions_of, subject_terms
from .models import (
    Confidence,
    Dimension,
    EvidenceKind,
    EvidenceRecord,
    Finding,
    Locator,
    RetrievalStatus,
    Section,
    SourceRecord,
    SourceType,
    Strict,
)
from .ocr import Ocr

ADAPTER = "manual_import"
ADAPTER_VERSION = "ads-import-1"
Platform = Literal["meta", "google"]
SOURCE_TYPES: dict[str, str] = {"meta": "meta_ad_library_page", "google": "google_atc_advertiser"}

# What each way of collecting ads may be used for, and what it yields (design section 0,
# sources retrieved 2026-11-06). Only manual import is implemented; the others are
# listed so a report states why they are not used.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "manual_import": {
        "implemented": True, "platforms": ("meta", "google"), "geographies": "any",
        "limitation": "Ads were looked up and transcribed by an operator from the public Ad "
                      "Library / Ads Transparency Center pages: a sample, not every ad an "
                      "advertiser runs.",
    },
    "meta_api": {
        "implemented": False, "platforms": ("meta",), "geographies": "EU/UK (all ads); "
        "elsewhere political and issue ads only",
        "limitation": "Meta's Ad Library API returns ads that reached no EU country only "
                      "when they are about social issues, elections or politics.",
    },
    "google_atc_dataset": {
        "implemented": False, "platforms": ("google",), "geographies": "EEA and Turkey",
        "limitation": "Google's Ads Transparency Center dataset covers the EEA and Turkey "
                      "and has no ad text.",
    },
}  # fmt: skip


class FieldState(StrEnum):
    PRESENT = "present"
    ABSENT_ON_SOURCE = "absent_on_source"  # the source shows the field, empty
    NOT_COLLECTED = "not_collected"  # this capture did not record it
    INACCESSIBLE = "inaccessible"  # the source has it, the operator could not see it
    REDACTED = "redacted"  # the source shows it withheld or removed


# Fields of an ad capture. Card fields hold one text per card (a carousel has several).
CARD_FIELDS = ("body", "headline", "description", "caption")
TEXT_FIELDS = (*CARD_FIELDS, "cta")
FIELDS = (
    "advertiser_display_name", *TEXT_FIELDS, "format", "destination_url", "platforms",
    "languages", "platform_start", "platform_stop", "regions", "disclosed_reach",
)  # fmt: skip
# Fields that become evidence. ``disclosed_reach`` is quoted as the platform states it
# and never used for anything else.
EVIDENCE_FIELDS = (*TEXT_FIELDS, "format", "destination_url", "platform_start", "platform_stop",
                   "disclosed_reach")  # fmt: skip
FORMATS = ("image", "video", "carousel", "text")
LABELS = {"body": "ad text", "headline": "headline", "description": "description",
          "caption": "caption", "cta": "call to action", "format": "format",
          "destination_url": "destination", "platform_start": "start as shown by the platform",
          "platform_stop": "end as shown by the platform",
          "disclosed_reach": "reach as disclosed by the platform"}  # fmt: skip
_ID = {"meta": (re.compile(r"^\d{5,25}$"), re.compile(r"^\d{3,25}$")),
       "google": (re.compile(r"^CR\d{6,30}$"), re.compile(r"^AR\d{6,30}$"))}  # fmt: skip
_SCREENSHOT_NAME = re.compile(r"^[\w][\w.-]{0,127}$")
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
MAX_FILE_BYTES = 5_000_000
MAX_SCREENSHOT_BYTES = 10_000_000
OCR_MATCH = 0.8  # share of a transcribed text's words the screenshot's OCR must contain


class AdImportError(ValueError):
    pass


class FieldValue(Strict):
    state: FieldState
    verbatim: str | list[str] | None = None
    normalized: str | None = Field(None, max_length=2048)

    @field_validator("verbatim")
    @classmethod
    def _texts(cls, v: str | list[str] | None) -> str | list[str] | None:
        texts = [v] if isinstance(v, str) else v or []
        if len(texts) > 20 or any(not t.strip() or len(t) > 2000 for t in texts):
            raise ValueError("verbatim: 1-20 non-empty texts of at most 2000 characters")
        return v

    @model_validator(mode="after")
    def _state(self) -> FieldValue:
        if (self.state is FieldState.PRESENT) != (self.verbatim is not None):
            raise ValueError("verbatim is given exactly when the state is 'present'")
        return self

    def texts(self) -> list[str]:
        v = self.verbatim
        return [v] if isinstance(v, str) else list(v or [])


class Advertiser(Strict):
    advertiser_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    profile_url: str = Field(max_length=2048)
    # The operator's statement of whose ads these are: the company's own domain.
    competitor_domain: str = Field(min_length=3, max_length=253)


class ImportedAd(Strict):
    platform: Platform
    platform_ad_id: str = Field(min_length=1, max_length=40)
    advertiser: Advertiser
    observed_at: datetime  # when the operator saw it
    fields: dict[str, FieldValue]
    screenshots: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _check(self) -> ImportedAd:
        ad_rx, adv_rx = _ID[self.platform]
        if not ad_rx.match(self.platform_ad_id):
            raise ValueError(f"{self.platform} ad id {self.platform_ad_id!r} is not in the "
                             "platform's format (Meta: the Library ID; Google: CR...)")  # fmt: skip
        if not adv_rx.match(self.advertiser.advertiser_id):
            raise ValueError(
                f"{self.platform} advertiser id {self.advertiser.advertiser_id!r} "
                "is not in the platform's format (Meta: Page ID; Google: AR...)"
            )
        url = self.advertiser.profile_url
        host = urlsplit_host(url)
        if (
            not url.startswith("https://")
            or not ad_library(url)
            or ((host == "adstransparency.google.com") != (self.platform == "google"))
        ):
            raise ValueError(f"profile_url {url!r} is not a {self.platform} ad library page")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at needs a time zone")
        if self.observed_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("observed_at is in the future")
        unknown = set(self.fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        if not any(self.fields.get(f, _NOT).state is FieldState.PRESENT for f in TEXT_FIELDS):
            raise ValueError("an ad needs at least one text field (body, headline, ...)")
        if any(not _SCREENSHOT_NAME.match(s) or ".." in s for s in self.screenshots):
            raise ValueError("screenshots are file names in the screenshot folder, no paths")
        return self


class ImportFile(Strict):
    ads: list[ImportedAd] = Field(min_length=1, max_length=500)


_NOT = FieldValue(state=FieldState.NOT_COLLECTED)


def urlsplit_host(url: str) -> str:
    try:
        return url_host(url).lower()
    except (InvalidUrl, ValueError):
        return ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _normalized(ad: ImportedAd, urls: UrlSettings) -> dict[str, dict[str, Any]]:
    """Every field (missing ones not_collected) with its checked normalized value."""
    out: dict[str, dict[str, Any]] = {}
    for name in FIELDS:
        v = ad.fields.get(name)
        if v is None and name == "advertiser_display_name":
            v = FieldValue(state=FieldState.PRESENT, verbatim=ad.advertiser.name)
        v = v or _NOT
        norm = v.normalized
        if v.state is FieldState.PRESENT:
            text = v.texts()[0]
            if name == "format":
                norm = text.strip().lower()
                if norm not in FORMATS or len(v.texts()) != 1:
                    raise AdImportError(f"ad {ad.platform_ad_id}: format must be one of "
                                        f"{', '.join(FORMATS)}, not {v.verbatim!r}")  # fmt: skip
            elif name == "destination_url":
                try:
                    norm = normalize_url(text, urls)  # drops utm_*, gclid, fbclid, ...
                except InvalidUrl as exc:
                    raise AdImportError(f"ad {ad.platform_ad_id}: destination_url: {exc}") from exc
            elif name in ("platform_start", "platform_stop") and norm is not None:
                try:
                    norm = datetime.fromisoformat(norm).date().isoformat()
                except ValueError as exc:
                    raise AdImportError(f"ad {ad.platform_ad_id}: {name}.normalized is not an "
                                        f"ISO date: {norm!r}") from exc  # fmt: skip
            elif norm is not None:
                raise AdImportError(f"ad {ad.platform_ad_id}: {name} takes no normalized value")
        out[name] = {"state": v.state.value, "verbatim": v.verbatim, "normalized": norm}
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def corroborated(texts: list[str], ocr_text: str) -> bool:
    """Whether the screenshots' OCR text contains (OCR_MATCH of the words of) every
    transcribed text: the transcription is then not only the operator's word."""
    seen = set(_words(ocr_text))
    if not seen or not texts:
        return False
    for t in texts:
        words = _words(t)
        if words and sum(w in seen for w in words) / len(words) < OCR_MATCH:
            return False
    return True


def _screenshot(folder: Path | None, name: str) -> bytes:
    if folder is None:
        raise AdImportError(f"screenshot {name!r} named but no screenshot folder given")
    path = folder / name
    if path.parent.resolve() != folder.resolve() or not path.is_file():
        raise AdImportError(f"screenshot {name!r} not found in {folder}")
    data = path.read_bytes()
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if len(data) > MAX_SCREENSHOT_BYTES or not (data.startswith(_IMAGE_MAGIC) or is_webp):
        raise AdImportError(f"screenshot {name!r}: PNG, JPEG or WebP of at most "
                            f"{MAX_SCREENSHOT_BYTES // 1_000_000} MB")  # fmt: skip
    return data


def parse_import(data: bytes) -> ImportFile:
    if len(data) > MAX_FILE_BYTES:
        raise AdImportError(f"import file over {MAX_FILE_BYTES // 1_000_000} MB")
    try:
        return ImportFile.model_validate_json(data)
    except ValidationError as exc:
        raise AdImportError(f"invalid import file: {exc}") from exc


def import_ads(
    engine: Engine,
    raw: RawStoreBackend,
    data: bytes,
    *,
    operator: str,
    screenshots: Path | None = None,
    ocr: Ocr | None = None,
    urls: UrlSettings | None = None,
) -> dict[str, Any]:
    """Store one import file (all of it or, on any error, none of it). Returns counts."""
    operator = operator.strip()
    if not operator:
        raise AdImportError("an import needs the operator's name")
    parsed = parse_import(data)
    import_id = f"imp_{_sha(data)[:16]}"  # the same file again is the same import
    urls = urls or UrlSettings()
    rows: list[tuple[ImportedAd, dict[str, Any]]] = []
    verification: Counter[str] = Counter()
    for ad in parsed.ads:
        fields = _normalized(ad, urls)
        media: list[dict[str, Any]] = []
        ocr_text = ""
        for name in ad.screenshots:
            body = _screenshot(screenshots, name)
            text = ocr.read_bytes(body) if ocr is not None and ocr.available else ""
            ocr_text += "\n" + text
            media.append({"kind": "image", "name": name, "sha256": raw.put(body),
                          "ocr_text": text or None})  # fmt: skip
        texts = [t for f in TEXT_FIELDS for t in FieldValue.model_validate(fields[f]).texts()]
        level = "ocr_corroborated" if corroborated(texts, ocr_text) else "operator_transcribed"
        verification[level] += 1
        record = ad.model_dump(mode="json")
        content = {f: fields[f]["verbatim"] for f in (*TEXT_FIELDS, "format", "destination_url")}
        content["media"] = sorted(m["sha256"] for m in media)
        rows.append((ad, {
            "observed_at": ad.observed_at, "method": ADAPTER, "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION, "import_id": import_id, "operator": operator,
            "raw_sha256": raw.put(_canonical(record)),
            "content_sha256": _sha(_canonical(content)), "fields": fields, "media": media,
            "verification": level,
        }))  # fmt: skip
    added = 0
    with engine.begin() as conn:
        for ad, obs in rows:
            profile = _profile(conn, ad, operator, import_id)
            conn.execute(
                pg_insert(Ad)
                .values(platform=ad.platform, platform_ad_id=ad.platform_ad_id,
                        external_identity_id=profile)
                .on_conflict_do_nothing(index_elements=["platform", "platform_ad_id"])
            )  # fmt: skip
            ad_id, owner = conn.execute(
                select(Ad.id, Ad.external_identity_id).where(
                    Ad.platform == ad.platform, Ad.platform_ad_id == ad.platform_ad_id)
            ).one()  # fmt: skip
            if owner != profile:
                raise AdImportError(f"{ad.platform} ad {ad.platform_ad_id} is already stored "
                                    "under another advertiser")  # fmt: skip
            new = conn.execute(
                pg_insert(AdObservation).values(ad_id=ad_id, **obs)
                .on_conflict_do_nothing(index_elements=["ad_id", "import_id"])
                .returning(AdObservation.id)
            ).scalar_one_or_none()  # fmt: skip
            added += new is not None
    return {
        "import_id": import_id,
        "ads": len(rows),
        "observations_added": added,
        "already_imported": len(rows) - added,
        "verification": dict(verification),
    }


def _profile(conn: Any, ad: ImportedAd, operator: str, import_id: str) -> int:
    """The advertiser's profile row, linked (once, by the operator's decision) to the
    company at ``competitor_domain``. A link to another company is refused."""
    adv = ad.advertiser
    source_type = SOURCE_TYPES[ad.platform]
    conn.execute(
        pg_insert(ExternalIdentity)
        .values(source_type=source_type, external_id=adv.advertiser_id, profile_url=adv.profile_url)
        .on_conflict_do_nothing(index_elements=["source_type", "external_id"])
    )
    profile: int = conn.execute(
        select(ExternalIdentity.id).where(ExternalIdentity.source_type == source_type,
                                          ExternalIdentity.external_id == adv.advertiser_id)
    ).scalar_one()  # fmt: skip
    domain = registrable_domain(adv.competitor_domain.strip().lower().removeprefix("www."))
    org = ensure_organization(conn, domain)
    links = conn.execute(
        select(IdentityLink.organization_id, IdentityLink.status).where(
            IdentityLink.external_identity_id == profile
        )
    ).all()
    for org_id, status in links:
        if status == "match" and org_id != org:
            raise AdImportError(f"advertiser {adv.advertiser_id} is already linked to another "
                                f"company than {domain}")  # fmt: skip
        if org_id == org:
            if status != "match":
                raise AdImportError(
                    f"advertiser {adv.advertiser_id} -> {domain} is "
                    f"'{status}': a reviewer decides it (review_link)"
                )
            return profile
    decision = conn.execute(
        pg_insert(IdentityDecision)
        .values(external_identity_id=profile, organization_id=org, outcome="match",
                candidates=[org], signals=[{"signal": "operator_assertion",
                                            "import_id": import_id}],
                rationale=f"{operator} imported this advertiser's ads as {domain}'s",
                algorithm_version=ADAPTER_VERSION, actor=operator, automatic=False)
        .returning(IdentityDecision.id)
    ).scalar_one()  # fmt: skip
    conn.execute(
        pg_insert(IdentityLink).values(
            organization_id=org, external_identity_id=profile, status="match", decision_id=decision
        )
    )
    return profile


# --- reading ------------------------------------------------------------------------


@dataclass
class AdView:
    """One stored ad and its observations, oldest first."""

    platform: str
    platform_ad_id: str
    advertiser_id: str
    profile_url: str
    domain: str
    observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def url(self) -> str:
        if self.platform == "meta":
            return f"https://www.facebook.com/ads/library/?id={self.platform_ad_id}"
        return (f"https://adstransparency.google.com/advertiser/{self.advertiser_id}/creative/"
                f"{self.platform_ad_id}")  # fmt: skip


def load_ads(engine: Engine, domains: Iterable[str]) -> list[AdView]:
    """The ads of the companies at ``domains`` (followed through 'match' links only)."""
    wanted = sorted({registrable_domain(d.lower()) for d in domains})
    if not wanted:
        return []
    query = (
        select(Ad.id, Ad.platform, Ad.platform_ad_id, ExternalIdentity.external_id,
               ExternalIdentity.profile_url, Organization.registrable_domain,
               AdObservation.__table__)
        .join(ExternalIdentity, ExternalIdentity.id == Ad.external_identity_id)
        .join(IdentityLink, (IdentityLink.external_identity_id == ExternalIdentity.id)
              & (IdentityLink.status == "match"))
        .join(Organization, Organization.id == IdentityLink.organization_id)
        .join(AdObservation, AdObservation.ad_id == Ad.id)
        .where(Organization.registrable_domain.in_(wanted),
               ExternalIdentity.source_type.in_(tuple(SOURCE_TYPES.values())))
        .order_by(Ad.id, AdObservation.observed_at, AdObservation.id)
    )  # fmt: skip
    views: dict[int, AdView] = {}
    with engine.connect() as conn:
        for row in conn.execute(query).mappings():
            view = views.setdefault(row["id"], AdView(
                row["platform"], row["platform_ad_id"], row["external_id"],
                row["profile_url"], row["registrable_domain"]))  # fmt: skip
            view.observations.append({k: row[k] for k in (
                "observed_at", "import_id", "operator", "raw_sha256", "content_sha256",
                "fields", "media", "verification")})  # fmt: skip
    return list(views.values())


def cited_digests(engine: Engine) -> set[str]:
    """Raw captures and screenshots of stored observations: ``gtm-crawl --prune`` keeps
    them while the observation exists, so every ad quote stays verifiable."""
    out: set[str] = set()
    with engine.connect() as conn:
        for digest, media in conn.execute(select(AdObservation.raw_sha256, AdObservation.media)):
            out.add(digest)
            out.update(m["sha256"] for m in media if m.get("sha256"))
    return out


# --- what to capture ----------------------------------------------------------------

CHECKLIST_NOTE = (
    "Open each link in a browser yourself: COIREI never fetches ad libraries (their terms "
    "forbid or restrict automated collection). Record what you see in an import file "
    "(docs/ad-intelligence-design.md) and run gtm-market --import-ads. Ads count for a "
    "competitor only under its domain; a candidate without a domain needs one first."
)  # fmt: skip


def dossier_competitors(dossier: dict[str, Any]) -> list[dict[str, Any]]:
    """A crawled company's competitors (``role`` competitor, with a domain when resolved)
    and co-listed candidates (``role`` candidate_competitor: never confirmed, no domain)."""
    out = [{"name": c.get("name") or c.get("domain"), "domain": c.get("domain"),
            "role": "competitor"} for c in dossier.get("competitors", [])]  # fmt: skip
    out += [{"name": c["name"], "domain": None, "role": "candidate_competitor"}
            for c in dossier.get("candidate_competitors", []) if c.get("name")]  # fmt: skip
    return [c for c in out if c["name"]]


def search_links(name: str, domain: str | None, country: str = "IN") -> dict[str, str]:
    """Ad Library / Ads Transparency Center search pages for an operator to open."""
    meta = urlencode(
        {
            "active_status": "all",
            "ad_type": "all",
            "country": country,
            "q": name,
            "search_type": "keyword_unordered",
            "media_type": "all",
        }
    )
    links = {"meta": f"https://www.facebook.com/ads/library/?{meta}"}
    if domain:
        google = urlencode({"region": country, "domain": domain})
        links["google"] = f"https://adstransparency.google.com/?{google}"
    return links


def capture_checklist(dossier: dict[str, Any], captured: list[AdView],
                      country: str = "IN") -> dict[str, Any]:  # fmt: skip
    """Per competitor of a crawled company: where to look for its ads and what is
    already captured (ads, platforms, latest capture)."""
    by_domain: dict[str, list[AdView]] = defaultdict(list)
    for v in captured:
        by_domain[v.domain].append(v)
    rows = []
    for c in dossier_competitors(dossier):
        mine = by_domain.get(c["domain"] or "", [])
        latest = max((o["observed_at"] for v in mine for o in v.observations), default=None)
        rows.append({**c, "links": search_links(c["name"], c["domain"], country),
                     "captured": {"ads": len(mine),
                                  "platforms": sorted({v.platform for v in mine}),
                                  "latest": latest.isoformat() if latest else None}})  # fmt: skip
    return {"company": dossier.get("company", {}).get("domain"), "country": country,
            "note": CHECKLIST_NOTE, "competitors": rows}  # fmt: skip


def checklist_markdown(checklist: dict[str, Any]) -> str:
    lines = [f"# Ads to capture: competitors of {checklist['company']} "
             f"({checklist['country']})", "", checklist["note"], ""]  # fmt: skip
    for c in checklist["competitors"]:
        got = c["captured"]
        state = (f"{got['ads']} captured ({', '.join(got['platforms'])}; latest "
                 f"{got['latest'][:10]})" if got["ads"] else "none captured")  # fmt: skip
        role = "" if c["role"] == "competitor" else " (candidate: co-listed, not confirmed)"
        lines.append(f"- **{c['name']}**{role}, {c['domain'] or 'no domain known'}: {state}")
        lines += [f"  - {k}: {u}" for k, u in c["links"].items()]
    return "\n".join(lines) + "\n"


# --- evidence -----------------------------------------------------------------------


@dataclass
class AdEvidence:
    sources: list[SourceRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    # Landing pages on the advertiser's own site: (url, "known_competitor:<name>").
    landing: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def ad_evidence(views: list[AdView], names: dict[str, str]) -> AdEvidence:
    """The latest capture of every ad as evidence. ``names``: competitor name by domain.

    A destination on another domain than the advertiser's is not fetched (it may be a
    click tracker, which counts a paid click, or a site the advertiser does not own)."""
    out = AdEvidence()
    seen: set[str] = set()
    for view in views:
        obs = view.observations[-1]
        fields = {k: FieldValue.model_validate(v) for k, v in obs["fields"].items()}
        advertiser = (fields["advertiser_display_name"].texts() or [view.advertiser_id])[0]
        manual = obs["verification"] == "operator_transcribed"
        src = SourceRecord(
            url=view.url, domain=view.domain, publisher=advertiser,
            source_type=SourceType.AD_LIBRARY, tier=3, title=f"{view.platform} ad "
            f"{view.platform_ad_id}", retrieved_at=obs["observed_at"],
            raw_sha256=obs["raw_sha256"], content_sha256=obs["content_sha256"],
            status=RetrievalStatus.CACHED, discovered_by=f"ad_import:{obs['import_id']}",
            extraction_method=f"manual ad import ({obs['verification']})",
            extraction_version=ADAPTER_VERSION,
            limitations=[CAPABILITIES["manual_import"]["limitation"]]
            + (["Transcribed by an operator; not corroborated by the screenshot's text."]
               if manual else []),
        )  # fmt: skip
        out.sources.append(src)
        for name in EVIDENCE_FIELDS:
            v = fields.get(name, _NOT)
            texts = v.texts()
            for i, text in enumerate(texts):
                out.evidence.append(EvidenceRecord(
                    source_id=src.source_id, excerpt=text,
                    claim=f"{advertiser} ({view.platform} ad {view.platform_ad_id}) "
                          f"{LABELS[name]}: {text}"[:2000],
                    locator=Locator(type="ad", value=name,
                                    row=i if name in CARD_FIELDS else None),
                    dimensions=[Dimension.MESSAGING],
                    extraction_certainty=0.6 if manual else 0.8,
                ))  # fmt: skip
        dest = fields.get("destination_url", _NOT).normalized
        if dest is None:
            continue
        if registrable_domain(urlsplit_host(dest)) != view.domain:
            out.notes.append(f"{advertiser} ad {view.platform_ad_id}: destination {dest} is not "
                             f"on {view.domain}; not fetched")  # fmt: skip
        elif dest not in seen:
            seen.add(dest)
            out.landing.append((dest, f"known_competitor:{names.get(view.domain, advertiser)}"))
    return out


# --- analysis -----------------------------------------------------------------------

OFFERS: dict[str, re.Pattern[str]] = {
    label: re.compile(rx, re.I) for label, rx in {
        "free trial": r"\bfree trial\b|\btry (it )?(for )?free\b",
        "free plan": r"\bfree (plan|forever|tier|version)\b",
        "discount": r"\b\d{1,2}\s?% off\b|\bdiscount\w*\b|\bsave (up to )?\d",
        "demo": r"\b(book|get|request|schedule) a (free |live )?demo\b",
        "no credit card": r"\bno credit card\b",
        "limited time": r"\blimited[- ]time\b|\boffer ends\b",
    }.items()
}  # fmt: skip
_AMOUNT = re.compile(r"(?:[₹$€£]|\b(?:rs\.?|inr|usd|eur|gbp)\s?)\s?\d[\d,]*(?:\.\d+)?", re.I)
# Words of spend, delivery, audience or results: what public creatives never show.
FORBIDDEN = re.compile(
    r"\b(spend\w*|spent|budgets?|impressions?|reach(es|ed)?|ctr|click-through|roas|roi|"
    r"conversions?|convert\w*|perform\w*|effective\w*|success\w*|target(s|ed|ing)?|"
    r"audiences?|winning|best-performing)\b", re.I)  # fmt: skip
MAX_HEADLINES = 3
CAVEAT = ("Ads were looked up and transcribed by an operator: a sample, not every ad an "
          "advertiser runs. Nothing here says what an ad cost, how often it was shown, "
          "whom it was shown to or how it performed.")  # fmt: skip


def _one(e: EvidenceRecord, s: SourceRecord, what: str) -> Finding:
    return Finding(statement=f"“{e.excerpt}” ({s.publisher}, {s.title} {what})",
                   evidence_ids=[e.evidence_id], confidence=confidence([e], {s.source_id: s}),
                   basis=EvidenceKind.DIRECT, independent_domains=1)  # fmt: skip


def _derived(statement: str, evidence: list[EvidenceRecord],
             sources: dict[str, SourceRecord]) -> Finding:  # fmt: skip
    # The engine's confidence rule: one advertiser's own ads are one domain, never
    # independent corroboration.
    return Finding(statement=statement, evidence_ids=[e.evidence_id for e in evidence][:50],
                   confidence=confidence(evidence, sources), basis=EvidenceKind.DERIVED,
                   independent_domains=1)  # fmt: skip


def _hypothesis(statement: str, evidence: list[EvidenceRecord]) -> Finding:
    return Finding(statement=f"Hypothesis: {statement}",
                   evidence_ids=[e.evidence_id for e in evidence][:10],
                   confidence=Confidence.LOW, basis=EvidenceKind.INFERRED,
                   independent_domains=1)  # fmt: skip


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{k} {n}" for k, n in counter.most_common())


def ad_section(
    evidence: list[EvidenceRecord],
    sources: dict[str, SourceRecord],
    pages: dict[str, ParsedPage],
    rivals: Iterable[str] = (),
    notes: Iterable[str] = (),
) -> Section:
    """The ``competitor_ads`` report section: per advertiser what its captured ads say
    (quotes), counts derived from them (formats, CTAs, themes, offers, landing-page
    agreement) and LOW hypotheses. ``rivals``: names to look for in ad text."""
    section = Section(title="Competitor advertising: observed messaging")
    ads: dict[str, dict[str, list[EvidenceRecord]]] = defaultdict(lambda: defaultdict(list))
    for e in evidence:
        s = sources.get(e.source_id)
        if s is not None and s.source_type is SourceType.AD_LIBRARY and e.locator is not None:
            ads[e.source_id][e.locator.value].append(e)
    if not ads:
        section.gaps.append("no captured ads for the known competitors "
                            "(gtm-market --import-ads)")  # fmt: skip
    by_advertiser: dict[str, list[str]] = defaultdict(list)
    for sid in ads:
        by_advertiser[sources[sid].domain].append(sid)
    fetched = {u: s.source_id for s in sources.values() if s.status is RetrievalStatus.FETCHED
               for u in (s.url, s.final_url) if u}  # fmt: skip
    rival_names = [r for r in rivals if r.strip()]
    for domain, sids in sorted(by_advertiser.items()):
        name = sources[sids[0]].publisher or domain
        n = len(sids)
        fmt = [e for sid in sids for e in ads[sid].get("format", [])]
        ctas = [e for sid in sids for e in ads[sid].get("cta", [])]
        tally = Counter((sources[sid].title or "ad").split()[0] for sid in sids)  # platform
        line = f"{name}: {n} captured ad(s) ({_counts(tally)})"
        if fmt:
            line += f"; formats: {_counts(Counter(e.excerpt.strip().lower() for e in fmt))}"
        if ctas:
            line += f"; calls to action: {_counts(Counter(e.excerpt.strip() for e in ctas))}"
        cited = fmt + ctas or [recs[0] for sid in sids for recs in ads[sid].values()]
        section.findings.append(_derived(line, cited, sources))
        heads = [(e, sid) for sid in sids for e in ads[sid].get("headline", [])]
        for e, sid in heads[:MAX_HEADLINES]:
            section.findings.append(_one(e, sources[sid], "headline"))
        texts = {sid: [e for f in TEXT_FIELDS for e in ads[sid].get(f, [])] for sid in sids}
        themes: dict[str, set[str]] = defaultdict(set)
        theme_ev: dict[str, list[EvidenceRecord]] = defaultdict(list)
        offers: dict[str, set[str]] = defaultdict(set)
        offer_ev: list[EvidenceRecord] = []
        named: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for sid, recs in texts.items():
            for e in recs:
                for d in dimensions_of(e.excerpt):
                    themes[d.value.replace("_", " ")].add(sid)
                    theme_ev[d.value.replace("_", " ")].append(e)
                for label, rx in OFFERS.items():
                    if rx.search(e.excerpt):
                        offers[label].add(sid)
                        offer_ev.append(e)
                for rival in rival_names:
                    if rival.casefold() != name.casefold() and re.search(
                            rf"\b{re.escape(rival)}\b", e.excerpt, re.I):  # fmt: skip
                        named[rival].append(e)
        if themes:
            parts = [f"{t} in {len(v)} of {n}" for t, v in
                     sorted(themes.items(), key=lambda kv: (-len(kv[1]), kv[0]))]  # fmt: skip
            section.findings.append(_derived(
                f"{name}'s ad text speaks to: {'; '.join(parts)} captured ad(s) (keyword rules)",
                [e for t in theme_ev for e in theme_ev[t]], sources))  # fmt: skip
        if offers:
            parts = [f"{o} ({len(v)} of {n})" for o, v in sorted(offers.items())]
            section.findings.append(_derived(f"Offers stated in {name}'s ads: {', '.join(parts)}",
                                             offer_ev, sources))  # fmt: skip
        for sid in sids:
            landing = _landing(sid, ads[sid], sources, pages, fetched)
            if landing is not None:
                section.findings.append(landing)
        for rival, recs in sorted(named.items()):
            count = len({e.source_id for e in recs})
            section.findings.append(
                _hypothesis(
                    f"{name} positions itself against {rival} (named in {count} of {n} captured "
                    f"ad(s)); its comparison or migration pages would test this.",
                    recs,
                )
            )
        led = {sid for v in offers.values() for sid in v}
        if n >= 2 and len(led) * 2 >= n:
            section.findings.append(
                _hypothesis(
                    f"{name} leads with entry offers in {len(led)} of {n} captured ads; its "
                    "pricing page shows whether they are standing terms.",
                    offer_ev,
                )
            )
    section.gaps += list(notes)
    if ads:
        section.gaps.append(CAVEAT)
    return section


def _landing(
    sid: str,
    fields: dict[str, list[EvidenceRecord]],
    sources: dict[str, SourceRecord],
    pages: dict[str, ParsedPage],
    fetched: dict[str, str],
) -> Finding | None:
    """How far one ad's words and stated amounts appear on its fetched landing page."""
    dest = fields.get("destination_url", [])
    page_id = fetched.get(_dest_url(dest[0])) if dest else None
    page = pages.get(page_id) if page_id is not None else None
    if page_id is None or page is None:
        return None
    s = sources[sid]
    words = [e for f in ("headline", "body") for e in fields.get(f, [])]
    terms = subject_terms(*(e.excerpt for e in words))
    text = f"{page.title or ''}\n{page.main_text}"
    on_page = set(_words(text))
    found = sorted(t for t in terms if t in on_page)
    amounts = {" ".join(m.group(0).split()) for e in words + fields.get("cta", [])
               for m in _AMOUNT.finditer(e.excerpt)}  # fmt: skip
    flat = " ".join(text.split())
    missing = sorted(a for a in amounts if a not in flat)
    line = (
        f"{s.publisher} {s.title}: {len(found)} of {len(terms)} distinctive words of its "
        f"headline and text appear on its landing page {sources[page_id].url}"
    )
    if amounts:
        line += (
            f"; amounts in the ad not on that page: {', '.join(missing)}"
            if missing
            else "; every amount in the ad also appears on that page"
        )
    return _derived(line, dest + words, sources)


def _dest_url(e: EvidenceRecord) -> str:
    # ponytail: default URL rules; the landing URL was normalized with settings.urls at
    # import. They differ only with a custom urls.strip_params: then no landing finding.
    try:
        return normalize_url(e.excerpt, UrlSettings())
    except InvalidUrl:
        return e.excerpt


def performance_claims(sections: Iterable[tuple[str, Any]], evidence: list[EvidenceRecord],
                       sources: Iterable[SourceRecord]) -> tuple[int, list[str]]:  # fmt: skip
    """(checked, failures): findings citing ad evidence whose own words (quotes of the
    cited excerpts removed) speak of spend, reach, targeting or performance."""
    index = {e.evidence_id: e for e in evidence}
    ad_sources = {s.source_id for s in sources if s.source_type is SourceType.AD_LIBRARY}
    checked, failures = 0, []
    for key, findings in sections:
        for f in findings:
            cited = [index[i] for i in f.evidence_ids if i in index]
            if not any(e.source_id in ad_sources for e in cited):
                continue
            checked += 1
            words = f.statement
            for e in sorted(cited, key=lambda e: -len(e.excerpt)):
                words = words.replace(e.excerpt, " ")
            m = FORBIDDEN.search(words)
            if m:
                failures.append(f"{key}: {m.group(0)!r} in {f.statement[:120]!r}")
    return checked, failures
