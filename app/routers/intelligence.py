
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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
