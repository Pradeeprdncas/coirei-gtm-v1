"""
/api/v1/onboarding/* — Conversation-flow API layer.

Wraps the intelligence pipeline into the frontend UX sequence:
  1. create_company_profile   → company + crawl
  2. jobs/{id}/events (SSE)   → real-time crawl progress
  3. research-outcome         → synthesised profile state + action buttons
  4. documents/upload         → store business-context docs
  5. context/update           → save q1/q2/q3 answers + re-sync
  6. gap-questions/start      → start iterative gap Q&A
  7. gap-questions/answers    → submit one answer, get next question
  8. gap-questions (GET)      → reload current open question
  9. verification             → readiness gate
  10. products/extract        → intelligence-synced products
  11. products/confirm        → force-accept + force-verify selected products
  12. finalize                → mark company complete + create project
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import (
    Company, FieldAudit, GapAnswer, GapQuestion, Product, Project, Source, User,
)
from app.models.intelligence import IntelligenceLink
from app.schemas.api import ok
from app.services.common import (
    domain, enough, field_meta_set, message, onboarding_conversation,
    owned_company, safe_name, source_from_text,
)
from app.services.documents import extract_document
from app.services.intelligence.client import crawler_request
from app.services.intelligence.sync import apply_sync, load_business_profile

router = APIRouter(prefix="/api/v1/onboarding", tags=["conversation"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile_revision(company: Company) -> str:
    return f"rev-{company.revision}"


def _icp_summary(company: Company) -> str:
    icp = company.icp or {}
    return (
        icp.get("target_audience")
        or icp.get("icp_hypotheses", [None])[0]
        or ""
    ) or "Unknown"


def _bm_summary(company: Company) -> str:
    bm = company.business_model or {}
    return (
        bm.get("revenue_model")
        or bm.get("summary")
        or bm.get("customer_type")
        or ""
    ) or "Unknown"


def _market_summary(company: Company) -> str:
    ms = company.market_segment or {}
    segs = ms.get("target_segments")
    if isinstance(segs, list) and segs:
        return ", ".join(str(s) for s in segs[:3])
    if isinstance(segs, str) and segs:
        return segs
    return ms.get("business_types") or company.industry or "Unknown"


def _conversation_missing_fields(company: Company, products: list[Product]) -> list[str]:
    """Simplified field gap names for the conversation UI."""
    gaps: list[str] = []
    if not company.icp:
        gaps.append("target_customer")
    if not (company.business_model or {}).get("revenue_model"):
        gaps.append("pricing_or_revenue_model")
    if not products:
        gaps.append("products")
    else:
        standalone = [p for p in products if p.candidate_kind == "standalone_product"]
        if not standalone:
            gaps.append("products")
        else:
            for p in standalone[:2]:
                if not p.description:
                    gaps.append(f"product_description:{p.name}")
    return gaps


async def _auto_sync(db: AsyncSession, company: Company, user: User) -> None:
    """Run intelligence sync once if the company has no products or description yet."""
    has_products = bool(await db.scalar(
        select(Product.id).where(Product.company_id == company.id).limit(1)
    ))
    if has_products and company.description:
        return

    try:
        link, report = await load_business_profile(db, company)
    except HTTPException:
        return

    # Re-fetch company to get the latest revision after a potential concurrent update.
    fresh = await db.get(Company, company.id)
    if fresh is None:
        return
    try:
        await apply_sync(
            db, fresh, user, link, report,
            expected_revision=fresh.revision,
            include_inferred=True,
            import_products=True,
        )
    except HTTPException:
        # STALE_REVISION or domain mismatch — ignore, data is already synced.
        pass


def _product_dict_mini(p: Product) -> dict:
    bm = p.business_model or {}
    plans = bm.get("plan_name") or []
    prices = bm.get("price") or []
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "category": p.category,
        "candidate_kind": p.candidate_kind,
        "key_features": p.key_features or [],
        "main_use_case": p.main_use_case,
        "status": p.status,
        "revision": p.revision,
        "default_selected": p.candidate_kind == "standalone_product",
        "pricing_and_plans": [
            {"name": pl, "price": pr, "billing_period": "monthly"}
            for pl, pr in zip(plans[:6], prices[:6])
        ] if plans or prices else [],
        "verification_status": (
            "source_supported" if (p.field_meta or {}).get("description", {}).get("status") == "found_on_site"
            else "user_provided" if (p.field_meta or {}).get("description", {}).get("status") == "user_provided"
            else "unverified"
        ),
    }


def _first_open_question(questions: list[GapQuestion]) -> dict | None:
    for q in questions:
        if q.status == "open":
            return {
                "id": q.id,
                "field": q.field_key,
                "question": q.question,
                "suggestions": [
                    s["text"] if isinstance(s, dict) else s
                    for s in (q.suggestions or [])
                ][:4],
                "allow_custom_answer": True,
            }
    return None


# ---------------------------------------------------------------------------
# 1. Create company profile + start crawl
# ---------------------------------------------------------------------------

class CreateProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    website_url: str = Field(min_length=4, max_length=2048)


@router.post("/create_company_profile", status_code=200)
async def create_company_profile(
    body: CreateProfileInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    from app.services.scrape.core import public_url as _public_url
    from uuid import uuid4

    website = body.website_url.strip()
    if "://" not in website:
        website = "https://" + website
    if not await _public_url(website):
        raise HTTPException(422, detail={"code": "INVALID_URL", "message": "Provide a reachable public website"})

    d = domain(website)
    stmt = select(Company).where(Company.owner_id == user.id)
    stmt = stmt.where(Company.domain == d) if d else stmt.where(
        Company.domain.is_(None), Company.name.ilike(body.name.strip()))
    company = await db.scalar(stmt)

    if company is None:
        company = Company(
            owner_id=user.id,
            name=body.name.strip(),
            website_url=website,
            domain=d,
        )
        db.add(company)
        await db.flush()
        await onboarding_conversation(db, company.id)
        await message(db, company.id, "assistant", "welcome",
                      {"text": "Welcome! I'm researching your website now.", "next_action": "watch_status"})
        await db.commit()
    else:
        if not company.website_url and website:
            company.website_url = website
            company.domain = d
            await db.commit()

    # Start or reuse intelligence crawl.
    link = await db.get(IntelligenceLink, company.id)
    already_crawled = False
    if link is None:
        idempotency_key = f"conv-onboarding-{company.id}-rev-{company.revision}"
        try:
            result = await crawler_request("POST", "/v1/crawls", payload={
                "root_url": company.website_url,
                "idempotency_key": idempotency_key,
            })
            link = IntelligenceLink(
                company_id=company.id,
                crawler_company_id=int(result["company_id"]),
                crawler_job_id=int(result["crawl_id"]),
            )
            db.add(link)
            await db.commit()
        except Exception as exc:
            raise HTTPException(502, detail={
                "code": "CRAWLER_UNAVAILABLE",
                "message": f"Could not start research: {str(exc)[:120]}",
            }) from exc
    else:
        already_crawled = True

    return ok({
        "company_id": company.id,
        "job_id": company.id,
        "company_name": company.name,
        "website_url": company.website_url,
        "status": "crawling" if not already_crawled else "ready",
        "stream_url": f"/api/v1/onboarding/jobs/{company.id}/events",
        "outcome_url": f"/api/v1/onboarding/research-outcome?company_id={company.id}",
    })


# ---------------------------------------------------------------------------
# 2. SSE: live crawl progress stream
# ---------------------------------------------------------------------------

@router.get("/jobs/{company_id}/events")
async def crawl_events(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_company(db, company_id, user.id)
    link = await db.get(IntelligenceLink, company_id)
    if link is None:
        raise HTTPException(404, detail={"code": "NOT_STARTED", "message": "Crawl not started"})

    crawler_job_id = link.crawler_job_id
    company_id_val = company_id

    async def _stream():
        STAGE_MESSAGES = {
            "QUEUED": ("thinking", "Preparing to research your website"),
            "RUNNING": ("searching", "Discovering relevant company pages"),
            "COMPLETED": ("complete", "Research complete"),
            "FAILED": ("error", "Research encountered an error"),
        }
        seconds = 0
        while True:
            try:
                job = await crawler_request("GET", f"/v1/crawls/{crawler_job_id}")
            except Exception:
                job = {}

            status = str(job.get("status") or "RUNNING").upper()
            stage = str(job.get("stage") or "").lower()

            event_type, msg_text = STAGE_MESSAGES.get(status, ("thinking", "Analyzing your website"))

            if status == "RUNNING":
                # Emit richer events based on the crawler stage.
                if "browse" in stage or "crawl" in stage or "page" in stage:
                    event_type = "browsing"
                    current_url = job.get("current_url") or ""
                    payload = {
                        "stage": "browsing",
                        "url": current_url,
                        "step": seconds // 3,
                        "status": f"Reading {'pricing evidence' if 'pric' in current_url else 'page evidence'}",
                        "company_id": company_id_val,
                        "job_id": company_id_val,
                    }
                elif "search" in stage or "discover" in stage:
                    event_type = "searching"
                    payload = {
                        "stage": "searching",
                        "message": "Discovering relevant company pages",
                        "searchResults": [],
                        "company_id": company_id_val,
                        "job_id": company_id_val,
                    }
                else:
                    event_type = "thinking"
                    payload = {
                        "stage": "thinking",
                        "message": msg_text,
                        "seconds": seconds,
                        "company_id": company_id_val,
                        "job_id": company_id_val,
                    }

            elif status == "COMPLETED":
                payload = {
                    "stage": "complete",
                    "company_id": company_id_val,
                    "job_id": company_id_val,
                    "analysis_ready": True,
                    "warnings": [],
                }
                yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
                return

            elif status == "FAILED":
                payload = {
                    "stage": "error",
                    "company_id": company_id_val,
                    "job_id": company_id_val,
                    "error": job.get("error_summary") or "Research failed",
                }
                yield f"event: error\ndata: {json.dumps(payload)}\n\n"
                return

            else:
                payload = {
                    "stage": "thinking",
                    "message": msg_text,
                    "seconds": seconds,
                    "company_id": company_id_val,
                    "job_id": company_id_val,
                }

            yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

            await asyncio.sleep(3)
            seconds += 3
            # Safety cap: stop after 10 minutes.
            if seconds >= 600:
                yield f"event: error\ndata: {json.dumps({'stage': 'timeout', 'message': 'Research timed out'})}\n\n"
                return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 3. Research outcome
# ---------------------------------------------------------------------------

@router.get("/research-outcome")
async def research_outcome(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)

    # Auto-sync intelligence into V1 models if not done yet.
    await _auto_sync(db, company, user)

    # Reload company after potential sync.
    await db.refresh(company)

    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id).order_by(Product.id)
    )).all()

    link = await db.get(IntelligenceLink, company_id)
    crawl_status = "not_started"
    if link:
        try:
            job = await crawler_request("GET", f"/v1/crawls/{link.crawler_job_id}")
            crawl_status = str(job.get("status") or "").upper()
        except Exception:
            crawl_status = "unknown"

    missing = _conversation_missing_fields(company, list(products))
    is_enough = enough(company, list(products))

    # Existing open gap questions (first unanswered).
    open_questions = (await db.scalars(
        select(GapQuestion)
        .where(GapQuestion.company_id == company_id, GapQuestion.status == "open")
        .order_by(GapQuestion.id)
    )).all()
    next_q = _first_open_question(list(open_questions))

    what_i_know = [
        {
            "label": "Your primary customer",
            "value": _icp_summary(company),
            "path": "company.icp",
            "evidence": [],
        },
        {
            "label": "Business model",
            "value": _bm_summary(company),
            "path": "company.business_model",
            "evidence": [],
        },
        {
            "label": "Current acquisition channels",
            "value": "Unknown",
            "path": "context.q3",
            "evidence": [],
        },
        {
            "label": "Target market",
            "value": _market_summary(company),
            "path": "company.market_segment",
            "evidence": [],
        },
    ]

    if crawl_status == "RUNNING":
        status = "crawling"
        title = "Researching your website…"
        msg = "Your website is being analysed. This usually takes 2–4 minutes."
    elif not is_enough and missing:
        status = "needs_context"
        title = f"{company.name} needs a bit more context"
        msg = (
            "The website was reached, but some key GTM details could not be extracted. "
            "Add business context or continue with the supported website data."
        )
    else:
        status = "ready"
        title = f"{company.name} is ready"
        msg = "Enough data was gathered to proceed with product verification."

    suggested_docs = ["Business plan", "Pricing / business model", "Customer research", "Sales deck"]

    return ok({
        "company_id": company_id,
        "job_status": crawl_status.lower() if crawl_status not in ("", "unknown") else "completed_with_errors",
        "status": status,
        "enough_data": is_enough,
        "title": title,
        "message": msg,
        "what_i_already_know": what_i_know,
        "missing_fields": missing,
        "next_gap_question": next_q,
        "suggested_documents": suggested_docs,
        "actions": [
            {
                "id": "add_business_context",
                "label": "Add Business Context",
                "method": "POST",
                "url": "/api/v1/onboarding/documents/upload",
                "follow_up": {
                    "method": "POST",
                    "url": "/api/v1/onboarding/context/update",
                    "instruction": "Send returned document IDs in attached_file_ids to attach and reprocess them.",
                },
            },
            {
                "id": "continue_with_website_data",
                "label": "Continue with Website Data",
                "method": "POST",
                "url": "/api/v1/onboarding/gap-questions/start",
                "company_id": company_id,
            },
        ],
        "onboarding_readiness": {
            "ready": is_enough,
            "missing_required": missing,
            "optional_unknown": [],
        },
        "profile_revision": _profile_revision(company),
        "products": [_product_dict_mini(p) for p in products],
    })


# ---------------------------------------------------------------------------
# 4. Document upload
# ---------------------------------------------------------------------------

@router.post("/documents/upload")
async def upload_documents(
    company_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)

    if len(files) > 5:
        raise HTTPException(422, detail={"code": "TOO_MANY_FILES", "message": "At most 5 files per upload"})

    uploaded = []
    for item in files:
        if not item.filename:
            continue
        payload = await item.read(5_000_001)
        if len(payload) > 5_000_000:
            raise HTTPException(413, detail={"code": "FILE_TOO_LARGE", "message": f"{item.filename} exceeds 5 MB"})
        try:
            text = extract_document(item.filename, payload)
        except Exception as exc:
            raise HTTPException(422, detail={"code": "DOCUMENT_PARSE_FAILED", "message": str(exc)[:160]}) from exc
        if not text.strip():
            raise HTTPException(422, detail={"code": "EMPTY_DOCUMENT", "message": f"{item.filename} has no extractable text"})

        raw_hash = hashlib.sha256(payload).hexdigest()
        src = await source_from_text(
            db, company.id, "document",
            f"document://company/{company.id}/{raw_hash}",
            text,
            page_type="document",
            raw={"filename": Path(item.filename).name[:180], "size_bytes": len(payload)},
        )
        await db.commit()

        summary_snippet = text[:300].replace("\n", " ")
        uploaded.append({
            "id": src.id,
            "name": item.filename,
            "size": f"{len(payload) / 1024:.1f} KB",
            "extension": item.filename.rsplit(".", 1)[-1].upper() if "." in item.filename else "TXT",
            "status": "parsed",
            "extracted_summary": summary_snippet,
        })

    return ok({"uploaded_files": uploaded})


# ---------------------------------------------------------------------------
# 5. Context update (q1/q2/q3 + doc IDs + re-sync)
# ---------------------------------------------------------------------------

class ContextUpdateInput(BaseModel):
    company_id: int
    answers: dict[str, str] = Field(default_factory=dict)
    typed_context: str | None = None
    attached_file_ids: list[int] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    profile_revision: str | None = None


@router.post("/context/update")
async def context_update(
    body: ContextUpdateInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, body.company_id, user.id)

    changed_answers: dict[str, str] = {}

    # q1 → ICP / target audience
    if body.answers.get("q1"):
        text = body.answers["q1"].strip()
        icp = dict(company.icp or {})
        icp["target_audience"] = text[:500]
        company.icp = icp
        company.field_meta = field_meta_set(
            company.field_meta, "icp",
            status="user_provided", review="accepted", confidence=100,
        )
        changed_answers["q1"] = text

    # q2 → business model revenue_model
    if body.answers.get("q2"):
        text = body.answers["q2"].strip()
        bm = dict(company.business_model or {})
        bm["revenue_model"] = text[:500]
        company.business_model = bm
        company.field_meta = field_meta_set(
            company.field_meta, "business_model",
            status="user_provided", review="accepted", confidence=100,
        )
        changed_answers["q2"] = text

    # q3 → save as a text source (no DB column for acquisition channels)
    if body.answers.get("q3"):
        text = body.answers["q3"].strip()
        q3_hash = hashlib.sha256(text.encode()).hexdigest()
        await source_from_text(
            db, company.id, "prompt",
            f"user-prompt://company/{company.id}/q3/{q3_hash}",
            f"Acquisition channels: {text}",
            page_type="prompt",
        )
        changed_answers["q3"] = text

    if body.typed_context:
        tc = body.typed_context.strip()
        tc_hash = hashlib.sha256(tc.encode()).hexdigest()
        await source_from_text(
            db, company.id, "prompt",
            f"user-prompt://company/{company.id}/typed/{tc_hash}",
            tc[:12000],
            page_type="prompt",
        )

    company.revision += 1
    await db.commit()

    # Re-sync intelligence with new context.
    sync_ran = False
    if body.attached_file_ids or body.typed_context or body.answers:
        try:
            link, report = await load_business_profile(db, company)
            await db.refresh(company)
            await apply_sync(
                db, company, user, link, report,
                expected_revision=company.revision,
                include_inferred=True,
                import_products=True,
            )
            sync_ran = True
        except HTTPException:
            pass

    return ok({
        "success": True,
        "context_status": "full_profile" if sync_ran else "partial_profile",
        "verification_status": "user_provided",
        "message": "Business context saved" + ("; intelligence re-synced." if sync_ran else "."),
        "processing_status": "complete",
        "enrichment_job": None,
        "update_summary": {
            "changed_answers": changed_answers,
            "attached_document_ids": body.attached_file_ids,
        },
        "profile_revision": _profile_revision(company),
    })


# ---------------------------------------------------------------------------
# 6 + 8. Gap questions start + GET current question
# ---------------------------------------------------------------------------

async def _ensure_gap_questions(db: AsyncSession, company_id: int, company: Company, user: User) -> list[GapQuestion]:
    """Ensure gap questions exist for all current missing fields; return open ones."""
    from app.routers.onboarding import missing_fields as _missing_fields, _gap_passages, _intel_aware_question
    from app.services.gap_suggestions import merge_suggestions
    from app.services.llm import json_completion

    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id)
    )).all()
    products_by_id = {p.id: p for p in products}
    needed = _missing_fields(company, list(products))

    # Close questions for fields that are no longer missing.
    existing = (await db.scalars(
        select(GapQuestion)
        .where(GapQuestion.company_id == company_id, GapQuestion.status == "open")
        .order_by(GapQuestion.id)
        .with_for_update()
    )).all()
    existing_by_key: dict[str, GapQuestion] = {}
    for q in existing:
        if q.field_key not in needed or q.field_key in existing_by_key:
            q.status = "resolved"
        else:
            existing_by_key[q.field_key] = q

    # Create new questions for missing fields not yet covered.
    from app.routers.onboarding import _intel_context
    intel_ctx = _intel_context(company, list(products))

    for field_key in needed:
        if field_key in existing_by_key:
            continue
        product = None
        if field_key.startswith("product."):
            product_id = int(field_key.split(".")[1])
            product = products_by_id.get(product_id)
        product_name = product.name if product else "this product"
        question_text = _intel_aware_question(field_key, product_name, intel_ctx)

        passages = await _gap_passages(db, company_id, field_key, product)
        ai: dict = {}
        if passages:
            evidence = "\n".join(
                f'SOURCE_ID={p["source_id"]} PAGE_TYPE={p["page_type"]} TEXT={p["text"][:650]}'
                for p in passages[:7]
            )
            intel_summary = ""
            if intel_ctx:
                import json as _json
                intel_summary = (
                    f"Company intelligence (already known):\n"
                    f"{_json.dumps(intel_ctx, default=str)[:800]}\n\n"
                )
            ai = await json_completion(
                (
                    "Return JSON only. Generate up to three specific actionable suggestions "
                    "for the missing field. Reference the company's products and use cases. "
                    "Every suggestion MUST cite an exact quote from the evidence with its source_id."
                ),
                (
                    f"{intel_summary}Field={field_key}\nQuestion={question_text}\n"
                    f"Evidence:\n{evidence}\n"
                    'Return {"suggestions":[{"text":"...","quote":"...","source_id":123}]}'
                ),
            )

        suggestions = merge_suggestions(field_key, passages, ai)

        new_q = GapQuestion(
            company_id=company_id,
            field_key=field_key,
            question=question_text,
            suggestions=suggestions,
        )
        db.add(new_q)
        await db.flush()
        existing_by_key[field_key] = new_q

    await db.commit()

    return (await db.scalars(
        select(GapQuestion)
        .where(GapQuestion.company_id == company_id, GapQuestion.status == "open")
        .order_by(GapQuestion.id)
    )).all()


@router.post("/gap-questions/start")
async def gap_questions_start(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)

    # Auto-sync first so products + fields are populated.
    await _auto_sync(db, company, user)
    await db.refresh(company)

    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id)
    )).all()
    missing = _conversation_missing_fields(company, list(products))
    is_enough = enough(company, list(products))

    if is_enough and not missing:
        return ok({
            "status": "ready",
            "enough_data": True,
            "missing_fields": [],
            "next_question": None,
            "profile_revision": _profile_revision(company),
            "processing_status": "complete",
        })

    open_questions = list(await _ensure_gap_questions(db, company_id, company, user))
    next_q = _first_open_question(open_questions)

    return ok({
        "status": "needs_context",
        "enough_data": is_enough,
        "missing_fields": missing,
        "next_question": next_q,
        "profile_revision": _profile_revision(company),
        "processing_status": "complete",
    })


@router.get("/gap-questions")
async def get_current_gap_question(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)
    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id)
    )).all()
    is_enough = enough(company, list(products))
    missing = _conversation_missing_fields(company, list(products))

    open_questions = (await db.scalars(
        select(GapQuestion)
        .where(GapQuestion.company_id == company_id, GapQuestion.status == "open")
        .order_by(GapQuestion.id)
    )).all()
    next_q = _first_open_question(list(open_questions))

    return ok({
        "enough_data": is_enough,
        "missing_fields": missing,
        "next_question": next_q,
        "profile_revision": _profile_revision(company),
    })


# ---------------------------------------------------------------------------
# 7. Submit gap answer → get next question
# ---------------------------------------------------------------------------

class GapAnswerInput(BaseModel):
    company_id: int
    question_id: int
    text: str = Field(min_length=1, max_length=5000)
    profile_revision: str | None = None


@router.post("/gap-questions/answers")
async def submit_gap_answer(
    body: GapAnswerInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, body.company_id, user.id)

    q = await db.scalar(
        select(GapQuestion).where(
            GapQuestion.id == body.question_id,
            GapQuestion.company_id == body.company_id,
        ).with_for_update()
    )
    if not q or q.status != "open":
        raise HTTPException(409, detail={"code": "INVALID_GAP_QUESTION", "message": "Question not found or already answered"})

    text = body.text.strip()
    chosen = next((s for s in (q.suggestions or []) if isinstance(s, dict) and s.get("text") == text), None)

    # Apply the answer to the company / product model.
    if q.field_key.startswith("company."):
        field = q.field_key.split(".", 1)[1]
        if field in {"business_model", "icp", "market_segment", "value_proposition"}:
            payload = {"summary": text} if field != "icp" else {"target_audience": text}
            existing = getattr(company, field) or {}
            merged = {**existing, **payload}
            setattr(company, field, merged)
            company.field_meta = field_meta_set(
                company.field_meta, field,
                status="user_provided", review="accepted", confidence=100,
            )
        elif field == "products":
            for pname in [x.strip() for x in re.split(r"[,;\n]", text)[:10]]:
                if not safe_name(pname):
                    continue
                prev = await db.scalar(
                    select(Product).where(Product.company_id == body.company_id, Product.name == pname)
                )
                if not prev:
                    db.add(Product(
                        company_id=body.company_id, name=pname,
                        candidate_kind="standalone_product",
                        field_meta=field_meta_set({}, "name", status="user_provided", review="accepted", confidence=100),
                    ))

    elif q.field_key.startswith("product."):
        _, ident, field = q.field_key.split(".", 2)
        p = await db.scalar(
            select(Product).where(Product.id == int(ident), Product.company_id == body.company_id)
        )
        if p and field in {"description", "main_use_case", "key_features"}:
            setattr(p, field, [v.strip() for v in text.split(";") if v.strip()] if field == "key_features" else text)
            p.field_meta = field_meta_set(p.field_meta, field, status="user_provided", review="accepted", confidence=100)
            p.revision += 1

    db.add(GapAnswer(
        question_id=q.id,
        actor_id=user.id,
        raw_text=text,
        normalized_value={"value": text, "source_id": chosen.get("source_id") if chosen else None},
    ))
    q.status = "answered"
    company.revision += 1
    await db.flush()

    # Reload products to recompute state.
    products = (await db.scalars(
        select(Product).where(Product.company_id == body.company_id)
    )).all()
    is_enough = enough(company, list(products))
    missing = _conversation_missing_fields(company, list(products))

    await db.commit()

    # Find next open question.
    open_questions = (await db.scalars(
        select(GapQuestion)
        .where(GapQuestion.company_id == body.company_id, GapQuestion.status == "open")
        .order_by(GapQuestion.id)
    )).all()
    next_q = _first_open_question(list(open_questions))

    return ok({
        "success": True,
        "accepted_answer": {
            "question_id": body.question_id,
            "field": q.field_key,
            "text": text,
        },
        "profile_revision": _profile_revision(company),
        "enough_data": is_enough,
        "missing_fields": missing,
        "next_question": next_q,
        "processing_status": "complete",
    })


# ---------------------------------------------------------------------------
# 9. Verification gate
# ---------------------------------------------------------------------------

@router.get("/verification")
async def verification(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)
    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id)
    )).all()
    is_enough = enough(company, list(products))
    missing = _conversation_missing_fields(company, list(products))
    answered = await db.scalar(
        select(GapAnswer.id).limit(1).where(
            GapAnswer.question_id.in_(
                select(GapQuestion.id).where(GapQuestion.company_id == company_id)
            )
        )
    )

    return ok({
        "verification": {
            "company_id": company_id,
            "enough_data": is_enough,
            "verification_status": (
                "user_provided" if answered
                else "source_supported" if company.description
                else "unverified"
            ),
            "missing_fields": missing,
            "selected_product_count": sum(
                1 for p in products if p.candidate_kind == "standalone_product"
            ),
            "answered_question_count": len(
                (await db.scalars(
                    select(GapAnswer.id).where(
                        GapAnswer.question_id.in_(
                            select(GapQuestion.id).where(GapQuestion.company_id == company_id)
                        )
                    )
                )).all()
            ),
            "can_continue_to_products": True,
            "next_action": "continue_to_products",
            "message": (
                "Enough supported company and product data is available."
                if is_enough else
                "Some information is still missing, but you can continue."
            ),
        }
    })


# ---------------------------------------------------------------------------
# 10. Products extract (auto-sync + return all products)
# ---------------------------------------------------------------------------

@router.post("/products/extract")
async def products_extract(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)

    # Always attempt sync to pick up latest intelligence data.
    await _auto_sync(db, company, user)
    await db.refresh(company)

    products = (await db.scalars(
        select(Product).where(Product.company_id == company_id).order_by(Product.id)
    )).all()

    return ok({
        "success": True,
        "company_id": company_id,
        "products": [_product_dict_mini(p) for p in products],
    })


# ---------------------------------------------------------------------------
# 11. Products confirm (force-accept + force-verify)
# ---------------------------------------------------------------------------

class ProductsConfirmInput(BaseModel):
    company_id: int
    selected_product_ids: list[int] = Field(min_length=1, max_length=30)


@router.post("/products/confirm")
async def products_confirm(
    body: ProductsConfirmInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, body.company_id, user.id)

    products = (await db.scalars(
        select(Product).where(
            Product.id.in_(body.selected_product_ids),
            Product.company_id == body.company_id,
        ).with_for_update()
    )).all()

    if len(products) != len(body.selected_product_ids):
        raise HTTPException(404, detail={"code": "PRODUCT_NOT_FOUND", "message": "One or more products not found"})

    confirmed: list[int] = []
    for p in products:
        meta = dict(p.field_meta or {})

        # Ensure it's classified as a standalone product.
        if p.candidate_kind != "standalone_product":
            p.candidate_kind = "standalone_product"
            db.add(FieldAudit(
                company_id=company.id, product_id=p.id, actor_id=user.id,
                field_key="candidate_kind",
                before={"value": p.candidate_kind}, after={"value": "standalone_product"},
            ))

        # Auto-accept all non-empty evidence fields so the user doesn't
        # need to individually approve each one (conversation UX assumption).
        for field in ("description", "key_features", "main_use_case", "category", "positioning"):
            value = getattr(p, field, None)
            if value not in (None, "", [], {}):
                existing = meta.get(field, {})
                if isinstance(existing, dict) and existing.get("review") != "accepted":
                    meta = field_meta_set(
                        meta, field,
                        status=existing.get("status", "found_on_site"),
                        review="accepted",
                        confidence=existing.get("confidence", 70),
                        source_id=existing.get("source_id"),
                        quote=existing.get("quote"),
                    )

        # For fields that are empty but required, insert placeholder values
        # so the product can be verified in the conversation flow.
        if not p.description:
            p.description = f"{p.name} — description to be completed."
            meta = field_meta_set(meta, "description", status="user_provided", review="accepted", confidence=50)
        if not p.key_features:
            p.key_features = []
            meta = field_meta_set(meta, "key_features", status="user_provided", review="accepted", confidence=50)
        if not p.main_use_case:
            p.main_use_case = f"Core use case for {p.name}."
            meta = field_meta_set(meta, "main_use_case", status="user_provided", review="accepted", confidence=50)

        p.field_meta = meta
        p.status = "verified"
        p.verified_at = datetime.now(timezone.utc)
        p.revision += 1
        confirmed.append(p.id)

    await message(db, body.company_id, "assistant", "products_confirmed",
                  {"confirmed_product_ids": confirmed})
    await db.commit()

    return ok({
        "success": True,
        "message": f"{len(confirmed)} product(s) confirmed successfully.",
        "selected_count": len(confirmed),
        "confirmed_product_ids": confirmed,
    })


# ---------------------------------------------------------------------------
# 12. Finalize → mark complete + create project + redirect
# ---------------------------------------------------------------------------

class FinalizeInput(BaseModel):
    company_id: int
    project_name: str | None = None
    website_url: str | None = None
    selected_product_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)
    questionnaire_answers: dict = Field(default_factory=dict)


@router.post("/finalize", status_code=201)
async def finalize(
    body: FinalizeInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    company = await owned_company(db, body.company_id, user.id)

    # Find verified standalone products to create projects for.
    if body.selected_product_ids:
        products = (await db.scalars(
            select(Product).where(
                Product.id.in_(body.selected_product_ids),
                Product.company_id == body.company_id,
            )
        )).all()
    else:
        products = (await db.scalars(
            select(Product).where(
                Product.company_id == body.company_id,
                Product.candidate_kind == "standalone_product",
                Product.status == "verified",
            )
        )).all()

    if not products:
        raise HTTPException(409, detail={
            "code": "NO_VERIFIED_PRODUCTS",
            "message": "Confirm at least one product before finalising",
        })

    # Mark company complete.
    company.status = "complete"
    company.revision += 1
    await db.commit()

    created_projects: list[dict] = []
    for product in products:
        snapshot = {
            "company": {
                "id": company.id,
                "name": company.name,
                "country": company.country,
                "market_segment": company.market_segment,
                "business_model": company.business_model,
                "icp": company.icp,
                "website_url": company.website_url,
            },
            "product": {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "category": product.category,
                "product_type": product.product_type,
                "key_features": product.key_features,
                "main_use_case": product.main_use_case,
                "business_model": product.business_model,
                "positioning": product.positioning,
                "value_proposition": product.value_proposition,
                "target_market": product.target_market,
                "icp": product.icp,
                "website_url": product.website_url,
            },
            "verified_revision": product.revision,
        }
        name = body.project_name or f"{product.name} GTM Project"

        stmt = (
            pg_insert(Project)
            .values(
                company_id=body.company_id,
                product_id=product.id,
                name=name,
                status="ready",
                snapshot=snapshot,
            )
            .on_conflict_do_nothing(constraint="uq_project_product")
            .returning(Project.id)
        )
        await db.scalar(stmt)

        project = await db.scalar(
            select(Project).where(
                Project.company_id == body.company_id,
                Project.product_id == product.id,
            )
        )
        if project:
            created_projects.append({
                "id": project.id,
                "name": project.name,
                "slug": f"{product.name.replace(' ', '-')}-{project.id}",
                "redirect_url": f"/p/{project.id}/home",
                "status": project.status,
            })

    await db.commit()

    primary = created_projects[0] if created_projects else None

    return ok({
        "success": True,
        "project": {
            **(primary or {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "all_projects": created_projects,
    })
