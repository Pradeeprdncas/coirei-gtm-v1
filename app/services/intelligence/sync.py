"""Safe, explicit import of a ZIP crawler business profile into V1.

- V1 is authoritative; never overwrite accepted fields.
- Model-inferred values require an explicit import opt-in and remain pending review.
- SUPPORTED crawler values can upgrade weaker ai_suggested V1 values (Phase 4B).
- Products are unverified candidates with rich detail from the crawler.
- Evidence trail: Source + FieldClaim rows are created for provenance (Phase 2).
- No writes to the ZIP crawler database; no copies of its private data.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Company, FieldAudit, FieldClaim, Product, Source, User
from app.models.intelligence import IntelligenceLink
from app.services.common import domain, message, safe_name, source_from_text, add_claim
from app.services.intelligence.client import crawler_request
from app.services.intelligence.mapping import (
    SUPPORTED,
    Candidate, company_candidates, customer_evidence, brand_fields, missing_hints,
    product_candidates,
)


def _err(status: int, code: str, msg: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={
        "code": code, "message": msg, "details": details,
    })


def _should_skip(company: Company, field: str, candidate: Candidate) -> str | None:
    """Phase 4B: smart skip logic — SUPPORTED crawler values can upgrade weaker V1 values."""
    meta = (company.field_meta or {}).get(field, {})
    if isinstance(meta, dict) and meta.get("review") == "accepted":
        return "user_accepted"

    current_value = getattr(company, field, None)
    if current_value in (None, "", [], {}):
        return None

    existing_status = meta.get("status", "") if isinstance(meta, dict) else ""
    existing_via = meta.get("via", "") if isinstance(meta, dict) else ""

    if candidate.evidence_state in SUPPORTED and existing_status == "ai_suggested":
        return None

    if candidate.evidence_state in SUPPORTED and existing_via == "intelligence_sync":
        existing_evidence = meta.get("evidence_state", "")
        if existing_evidence == "MODEL_INFERRED":
            return None

    return "already_populated"


def _field_metadata(candidate: Candidate, crawler_company_id: int, source_id: int | None = None) -> dict:
    conf = candidate.confidence
    if conf is not None:
        confidence = round(conf * 100)
    elif candidate.evidence_state in SUPPORTED:
        confidence = 70
    else:
        confidence = 45
    return {
        "status": "model_inferred" if candidate.evidence_state == "MODEL_INFERRED"
                  else "found_on_site",
        "review": "pending",
        "confidence": confidence,
        "source_id": source_id,
        "quote": candidate.quote,
        "source_urls": list(candidate.source_urls),
        "evidence_state": candidate.evidence_state,
        "origin": candidate.origin,
        "crawler_company_id": crawler_company_id,
        "via": "intelligence_sync",
    }


def _product_meta(candidate: dict, crawler_company_id: int) -> dict:
    conf = candidate.get("confidence")
    if conf is not None:
        confidence = round(conf * 100)
    elif str(candidate.get("evidence_state", "")).upper() in SUPPORTED:
        confidence = 70
    else:
        confidence = 45
    info = {
        "status": "model_inferred" if candidate.get("evidence_state") == "MODEL_INFERRED"
                  else "found_on_site",
        "review": "pending",
        "confidence": confidence,
        "source_id": None,
        "quote": candidate.get("quote"),
        "source_urls": candidate.get("source_urls", []),
        "evidence_state": candidate.get("evidence_state"),
        "crawler_company_id": crawler_company_id,
        "via": "intelligence_sync",
    }
    result = {"name": dict(info)}
    for field in ("description", "category", "key_features", "main_use_case",
                  "positioning", "business_model", "value_proposition", "target_market", "icp"):
        if candidate.get(field) not in (None, "", [], {}):
            result[field] = dict(info)
    return result


async def _create_evidence_source(
    db: AsyncSession, company_id: int, url: str, quote: str | None,
) -> Source | None:
    """Phase 2: create a Source row so intelligence data joins the RAG index."""
    if not url or not url.startswith(("https://", "http://")):
        return None
    text = quote or f"Intelligence evidence from {url}"
    return await source_from_text(
        db, company_id, "intelligence", url, text,
        page_type="intelligence", status="ok",
    )


async def _create_evidence_trail(
    db: AsyncSession, company_id: int, product_id: int | None,
    field: str, value: Any, candidate: Candidate,
) -> int | None:
    """Phase 2: create Source + FieldClaim for a synced field."""
    source_id = None
    if candidate.source_urls:
        source = await _create_evidence_source(
            db, company_id, candidate.source_urls[0], candidate.quote,
        )
        if source:
            source_id = source.id
            await add_claim(
                db, company_id, product_id, field, value, source,
                candidate.quote, status="intelligence",
            )
    return source_id


async def load_business_profile(
    db: AsyncSession, company: Company,
) -> tuple[IntelligenceLink, dict]:
    """Owner checks must happen in the calling route before this function."""
    link = await db.get(IntelligenceLink, company.id)
    if link is None:
        raise _err(404, "INTELLIGENCE_NOT_STARTED", "Start a company crawl first")

    report = await crawler_request(
        "GET", f"/v1/companies/{link.crawler_company_id}/business-profile"
    )

    if not isinstance(report.get("sections"), list):
        raise _err(502, "INVALID_INTELLIGENCE_REPORT", "Crawler returned no sections")
    if str(report.get("company_id")) != str(link.crawler_company_id):
        raise _err(502, "CRAWLER_COMPANY_MISMATCH", "Crawler returned another company")
    crawl = report.get("crawl") or {}
    if not isinstance(crawl, dict) or crawl.get("status") != "COMPLETED":
        raise _err(409, "REPORT_NOT_READY", "Crawler profile is not complete")
    report_domain = domain("https://" + str(report.get("domain") or ""))
    v1_domain = company.domain or domain(company.website_url)
    if not report_domain or not v1_domain or report_domain != v1_domain:
        raise _err(409, "REPORT_DOMAIN_MISMATCH", "Crawler profile domain differs from V1 company")
    return link, report


def build_preview(
    company: Company, link: IntelligenceLink, report: dict,
    *, include_inferred: bool, existing_products: list[Product],
) -> dict:
    candidates = company_candidates(report, include_inferred=include_inferred)
    fields = []
    for name, cand in candidates.items():
        reason = _should_skip(company, name, cand)
        fields.append({
            **cand.to_dict(),
            "action": "skip" if reason else ("upgrade" if _is_upgrade(company, name, cand) else "set_pending_review"),
            "reason": reason,
        })

    products = []
    for item in product_candidates(report, include_inferred=include_inferred):
        name = item["name"]
        existing_match = _fuzzy_product_match(name, existing_products)
        if existing_match:
            if existing_match.status == "verified":
                action, reason = "skip", "already_verified"
            else:
                would_enrich = []
                for f in ("description", "category", "key_features", "main_use_case"):
                    if item.get(f) not in (None, "", [], {}) and getattr(existing_match, f) in (None, "", [], {}):
                        would_enrich.append(f)
                vp = existing_match.value_proposition or {}
                if item.get("benefits") and not vp.get("benefits"):
                    would_enrich.append("value_proposition.benefits")
                if item.get("problems_solved") and not vp.get("problems_solved"):
                    would_enrich.append("value_proposition.problems_solved")
                if item.get("target_users") and not existing_match.target_market:
                    would_enrich.append("target_market")
                if would_enrich:
                    action, reason = "enrich", None
                    item = {**item, "existing_product_id": existing_match.id, "would_enrich": would_enrich}
                else:
                    action, reason = "skip", "already_complete"
        elif not safe_name(name):
            action, reason = "skip", "invalid_product_name"
        else:
            action, reason = "create_unverified_candidate", None
        products.append({**item, "action": action, "reason": reason})

    evidence = customer_evidence(report, include_inferred=include_inferred)
    brand = brand_fields(report, include_inferred=include_inferred)

    return {
        "company_id": company.id,
        "crawler_company_id": link.crawler_company_id,
        "revision": company.revision,
        "profile_status": report.get("profile_status"),
        "include_inferred": include_inferred,
        "fields": fields,
        "products": products,
        "customer_evidence": evidence,
        "brand_fields": brand,
        "missing_public_fields": missing_hints(report),
        "review_required": True,
    }


def _is_upgrade(company: Company, field: str, candidate: Candidate) -> bool:
    """Check if the candidate would upgrade a weaker existing value."""
    meta = (company.field_meta or {}).get(field, {})
    if not isinstance(meta, dict):
        return False
    current_value = getattr(company, field, None)
    if current_value in (None, "", [], {}):
        return False
    existing_status = meta.get("status", "")
    return (candidate.evidence_state in SUPPORTED and existing_status == "ai_suggested")


def _fuzzy_product_match(name: str, existing_products: list[Product]) -> Product | None:
    """Phase 1B: fuzzy matching for product deduplication."""
    normalized = _normalize_product_name(name)
    for p in existing_products:
        if p.name.casefold() == name.casefold():
            return p
        if _normalize_product_name(p.name) == normalized:
            return p
    return None


def _normalize_product_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", name.casefold())


async def apply_sync(
    db: AsyncSession, company: Company, user: User,
    link: IntelligenceLink, report: dict,
    *, expected_revision: int, include_inferred: bool, import_products: bool,
) -> dict:
    """Caller owns company. Lock it, validate revision, then write atomically."""
    locked = await db.scalar(
        select(Company).where(Company.id == company.id, Company.owner_id == user.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        raise _err(404, "NOT_FOUND", "Company not found")
    if locked.revision != expected_revision:
        raise _err(409, "STALE_REVISION", "Reload company profile and sync preview",
                   revision=locked.revision)
    if locked.domain and domain("https://" + str(report.get("domain", ""))) != locked.domain:
        raise _err(409, "REPORT_DOMAIN_MISMATCH", "Company domain changed after preview")

    candidates = company_candidates(report, include_inferred=include_inferred)
    updated = []
    upgraded = []
    skipped_fields = []
    meta = dict(locked.field_meta or {})

    length_limits = {
        "industry": 255, "headquarters": 255, "country": 100, "size_band": 100,
    }

    for key, cand in candidates.items():
        reason = _should_skip(locked, key, cand)
        if reason:
            skipped_fields.append({"field": key, "reason": reason})
            continue
        if key in length_limits and isinstance(cand.value, str) and len(cand.value) > length_limits[key]:
            skipped_fields.append({"field": key, "reason": "value_too_long"})
            continue

        is_upgrade = _is_upgrade(locked, key, cand)
        previous = getattr(locked, key)

        if key == "socials" and isinstance(previous, dict) and isinstance(cand.value, dict):
            merged = {**previous, **cand.value}
            setattr(locked, key, merged)
        else:
            setattr(locked, key, cand.value)

        source_id = await _create_evidence_trail(
            db, locked.id, None, key, cand.value, cand,
        )

        meta[key] = _field_metadata(cand, link.crawler_company_id, source_id)
        db.add(FieldAudit(
            company_id=locked.id, actor_id=user.id, field_key=key,
            before={"value": previous},
            after={"value": cand.value, "via": "intelligence_sync",
                   "review": "pending", "upgraded": is_upgrade},
        ))
        updated.append(key)
        if is_upgrade:
            upgraded.append(key)

    # --- Sync customer evidence and brand fields into value_proposition ---
    evidence = customer_evidence(report, include_inferred=include_inferred)
    brand = brand_fields(report, include_inferred=include_inferred)
    vp = dict(locked.value_proposition or {})
    vp_changed = False
    if evidence and "customer_evidence" not in vp:
        vp["customer_evidence"] = evidence
        vp_changed = True
    if brand:
        for bk, bv in brand.items():
            if bk not in vp:
                vp[bk] = bv
                vp_changed = True
    if vp_changed and meta.get("value_proposition", {}).get("review") != "accepted":
        locked.value_proposition = vp
        if "value_proposition" not in updated:
            updated.append("value_proposition")

    if updated:
        locked.field_meta = meta
        locked.revision += 1

    # --- Product sync ---
    created_products = []
    enriched_products = []
    skipped_products = []
    if import_products:
        existing_products = (await db.scalars(
            select(Product).where(Product.company_id == locked.id)
        )).all()

        for candidate in product_candidates(report, include_inferred=include_inferred):
            name = candidate["name"]
            existing = _fuzzy_product_match(name, list(existing_products))

            if existing and existing.status == "verified":
                skipped_products.append({"name": name, "reason": "already_verified"})
                continue

            if not safe_name(name):
                skipped_products.append({"name": name, "reason": "invalid_product_name"})
                continue

            if existing:
                product = existing
                enriched = await _enrich_existing_product(db, product, candidate, link.crawler_company_id)
                if enriched:
                    enriched_products.append({"id": product.id, "name": product.name,
                                              "enriched_fields": enriched})
            else:
                product = Product(
                    company_id=locked.id,
                    name=name,
                    candidate_kind="unknown",
                    status="detected",
                    description=candidate.get("description"),
                    category=candidate.get("category"),
                    key_features=candidate.get("key_features") or [],
                    main_use_case=candidate.get("main_use_case"),
                    positioning=candidate.get("target_users"),
                    field_meta=_product_meta(candidate, link.crawler_company_id),
                )

                if candidate.get("benefits") or candidate.get("problems_solved"):
                    product.value_proposition = {
                        k: v for k, v in [
                            ("benefits", candidate.get("benefits")),
                            ("problems_solved", candidate.get("problems_solved")),
                        ] if v
                    }
                if candidate.get("target_users"):
                    product.target_market = {"target_users": candidate["target_users"]}
                if candidate.get("pricing_model"):
                    product.business_model = {"pricing_model": candidate["pricing_model"]}
                    product.pricing_status = "published_needs_review"
                if candidate.get("prices"):
                    product.pricing_status = "published_needs_review"

                db.add(product)
                await db.flush()

                if candidate.get("source_urls"):
                    source = await _create_evidence_source(
                        db, locked.id, candidate["source_urls"][0], candidate.get("quote"),
                    )
                    if source:
                        await add_claim(db, locked.id, product.id, "name", name, source,
                                        candidate.get("quote"), status="intelligence")

                created_products.append({
                    "id": product.id, "name": product.name,
                    "candidate_kind": product.candidate_kind, "status": product.status,
                    "fields_populated": [f for f in ("description", "category", "key_features",
                                                     "main_use_case", "positioning", "value_proposition",
                                                     "target_market", "business_model")
                                         if getattr(product, f) not in (None, "", [], {})],
                })

            existing_products = list(existing_products) + ([product] if not existing else [])

    changed = bool(updated or created_products or enriched_products)
    if changed:
        await message(db, locked.id, "assistant", "intelligence_synced", {
            "updated_fields": updated,
            "upgraded_fields": upgraded,
            "created_products": created_products,
            "enriched_products": enriched_products,
            "inferred_included": include_inferred,
            "text": "Website intelligence imported as pending-review candidates.",
            "next_action": "review_company_and_products",
        })
    await db.commit()
    return {
        "company_id": locked.id,
        "revision": locked.revision,
        "changed": changed,
        "updated_fields": updated,
        "upgraded_fields": upgraded,
        "skipped_fields": skipped_fields,
        "created_products": created_products,
        "enriched_products": enriched_products,
        "skipped_products": skipped_products,
        "review_required": True,
        "next_action": "review_company_and_products",
        "profile_url": f"/companies/{locked.id}/profile",
        "products_url": f"/companies/{locked.id}/products",
        "gap_questions_url": f"/companies/{locked.id}/gap-questions",
        "intelligence_report_url": f"/companies/{locked.id}/intelligence/report",
    }


async def _enrich_existing_product(
    db: AsyncSession, product: Product, candidate: dict, crawler_company_id: int,
) -> list[str]:
    """Phase 1B: enrich an existing unverified product with crawler data it lacks."""
    enriched = []
    meta = dict(product.field_meta or {})
    enrichable = {
        "description": candidate.get("description"),
        "category": candidate.get("category"),
        "key_features": candidate.get("key_features"),
        "main_use_case": candidate.get("main_use_case"),
    }
    for field, value in enrichable.items():
        if value in (None, "", [], {}):
            continue
        current = getattr(product, field)
        if current not in (None, "", [], {}):
            if not (meta.get(field, {}).get("status") == "ai_suggested"
                    and str(candidate.get("evidence_state", "")).upper() in SUPPORTED):
                continue
        setattr(product, field, value)
        meta[field] = {
            "status": "model_inferred" if candidate.get("evidence_state") == "MODEL_INFERRED"
                      else "found_on_site",
            "review": "pending",
            "confidence": round(candidate["confidence"] * 100) if candidate.get("confidence") is not None else 70,
            "source_id": None,
            "quote": candidate.get("quote"),
            "source_urls": candidate.get("source_urls", []),
            "evidence_state": candidate.get("evidence_state"),
            "crawler_company_id": crawler_company_id,
            "via": "intelligence_sync",
        }
        enriched.append(field)

    if candidate.get("benefits") or candidate.get("problems_solved"):
        vp = dict(product.value_proposition or {})
        changed = False
        if candidate.get("benefits") and "benefits" not in vp:
            vp["benefits"] = candidate["benefits"]
            changed = True
        if candidate.get("problems_solved") and "problems_solved" not in vp:
            vp["problems_solved"] = candidate["problems_solved"]
            changed = True
        if changed:
            product.value_proposition = vp
            enriched.append("value_proposition")

    if candidate.get("target_users") and not product.target_market:
        product.target_market = {"target_users": candidate["target_users"]}
        enriched.append("target_market")

    if enriched:
        product.field_meta = meta
    return enriched
