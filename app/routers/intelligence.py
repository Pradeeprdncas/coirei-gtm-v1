
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import User
from app.models.intelligence import IntelligenceLink
from app.schemas.api import ok
from app.services.common import owned_company
from app.services.intelligence.client import crawler_request


router = APIRouter(
    prefix="/companies",
    tags=["intelligence"],
)


class CrawlInput(BaseModel):
    rerun: bool = False


@router.post(
    "/{company_id}/intelligence/crawl",
    status_code=202,
)
async def start_intelligence_crawl(
    company_id: int,
    body: CrawlInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(
        db, company_id, user.id
    )

    if not company.website_url:
        raise HTTPException(
            409,
            detail={
                "code": "WEBSITE_REQUIRED",
                "message": "Add a company website first",
            },
        )

    # Deterministic key for retries.
    # Manual rerun gets a new key.
    idempotency_key = (
        f"onboarding-{company_id}-rev-{company.revision}"
        if not body.rerun
        else f"onboarding-{company_id}-{uuid4().hex}"
    )

    result = await crawler_request(
        "POST",
        "/v1/crawls",
        payload={
            "root_url": company.website_url,
            "idempotency_key": idempotency_key,
        },
    )

    # These are the actual response fields
    # used by the imported crawler API.
    crawler_company_id = int(result["company_id"])
    crawler_job_id = int(result["crawl_id"])

    link = await db.get(
        IntelligenceLink,
        company_id,
    )

    if link is None:
        link = IntelligenceLink(
            company_id=company_id,
            crawler_company_id=crawler_company_id,
            crawler_job_id=crawler_job_id,
        )
        db.add(link)
    else:
        link.crawler_company_id = crawler_company_id
        link.crawler_job_id = crawler_job_id

    await db.commit()

    return ok({
        "company_id": company_id,
        "crawler_job_id": crawler_job_id,
        "crawler_status": result["status"],
        "reused": not result.get("created", True),
        "status_url": (
            f"/companies/{company_id}/intelligence/status"
        ),
        "report_url": (
            f"/companies/{company_id}/intelligence/report"
        ),
    })


@router.get(
    "/{company_id}/intelligence/status",
)
async def intelligence_status(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_company(db, company_id, user.id)

    link = await db.get(
        IntelligenceLink, company_id
    )

    if link is None:
        raise HTTPException(
            404,
            detail={
                "code": "INTELLIGENCE_NOT_STARTED",
                "message": "Start a crawler job first",
            },
        )

    job = await crawler_request(
        "GET",
        f"/v1/crawls/{link.crawler_job_id}",
    )

    return ok({
        "company_id": company_id,
        "crawl": job,
    })


@router.get(
    "/{company_id}/intelligence/live-screenshot/{fetch_id}",
)
async def intelligence_live_screenshot(
    company_id: int,
    fetch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Proxies the GTM crawler's page screenshot so the frontend never needs the
    crawler's own service token — this endpoint is reachable with the user's
    normal session, same as every other /companies/{company_id}/... route."""
    await owned_company(db, company_id, user.id)

    link = await db.get(IntelligenceLink, company_id)

    if link is None:
        raise HTTPException(
            404,
            detail={
                "code": "INTELLIGENCE_NOT_STARTED",
                "message": "Start a crawler job first",
            },
        )

    cfg = settings()
    url = (
        f"{cfg.crawler_base_url.rstrip('/')}/v1/crawls/"
        f"{link.crawler_job_id}/live/{fetch_id}.png"
    )

    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            upstream = await client.get(
                url,
                headers={"Authorization": f"Bearer {cfg.crawler_service_token}"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "CRAWLER_UNAVAILABLE",
                "message": "Cannot connect to crawler service",
            },
        ) from exc

    if upstream.status_code == 404:
        raise HTTPException(
            404,
            detail={"code": "SCREENSHOT_NOT_FOUND", "message": "No such page in this crawl"},
        )
    if upstream.status_code != 200:
        raise HTTPException(
            502,
            detail={"code": "CRAWLER_UPSTREAM_ERROR", "message": "Crawler request failed"},
        )

    return Response(
        upstream.content,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/{company_id}/intelligence/report",
)
async def intelligence_report(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_company(db, company_id, user.id)

    link = await db.get(
        IntelligenceLink, company_id
    )

    if link is None:
        raise HTTPException(
            404,
            detail={
                "code": "INTELLIGENCE_NOT_STARTED",
                "message": "Start a crawler job first",
            },
        )

    job = await crawler_request(
        "GET",
        f"/v1/crawls/{link.crawler_job_id}",
    )

    if job["status"] not in {"COMPLETED"}:
        raise HTTPException(
            409,
            detail={
                "code": "REPORT_NOT_READY",
                "message": (
                    "The current crawler job has not "
                    "completed successfully"
                ),
                "details": {
                    "crawl_status": job["status"],
                    "crawl_stage": job.get("stage"),
                    "error": job.get("error_summary"),
                },
            },
        )

    report = await crawler_request(
        "GET",
        (
            f"/v1/companies/{link.crawler_company_id}"
            "/business-profile"
        ),
    )

    return ok({
        "company_id": company_id,
        "crawler_company_id": link.crawler_company_id,
        "report": report,
    })
