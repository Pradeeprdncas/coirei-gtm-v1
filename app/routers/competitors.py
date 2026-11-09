import httpx

from fastapi import (
    APIRouter, Depends, HTTPException
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import current_user

from app.models.entities import User, Product
from app.models.competitors import (
    CompetitorJob,
    CompetitorCandidate,
    CompetitorSource,
)

from app.schemas.api import ok
from app.routers.projects import owned_project


router = APIRouter(tags=["competitors"])


def candidate_data(c):
    return {
        "id": c.id,
        "company_id": c.company_id,
        "product_id": c.product_id,
        "project_id": c.project_id,
        "name": c.name,
        "domain": c.domain,
        "website_url": c.website_url,
        "status": c.status,
        "match_percent": c.match_percent,
        "evidence_coverage": c.evidence_coverage,
        "profile": c.profile,
        "score_details": c.score_details,
        "explanation": c.explanation,
        "discovered_via": c.discovered_via,
    }


@router.post(
    "/projects/{project_id}/competitors/discover",
    status_code=202,
)
async def start_discovery(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    project, company = await owned_project(
        db, project_id, user.id
    )

    product = await db.get(
        Product, project.product_id
    )

    if (
        not product
        or product.status != "verified"
        or product.candidate_kind != "standalone_product"
    ):
        raise HTTPException(
            409,
            detail={
                "code": "PRODUCT_NOT_VERIFIED",
                "message": (
                    "Verify this standalone product "
                    "before competitor discovery"
                ),
            },
        )

    cfg = settings()

    if len(cfg.competitor_internal_token) < 32:
        raise HTTPException(
            503,
            detail={
                "code": "SERVICE_NOT_CONFIGURED",
                "message": (
                    "Competitor internal token missing"
                ),
            },
        )

    try:
        async with httpx.AsyncClient(
            timeout=12,
            trust_env=False,
        ) as client:
            response = await client.post(
                (
                    cfg.competitor_internal_url.rstrip("/")
                    + f"/internal/projects/{project_id}"
                    + "/discover"
                ),
                headers={
                    "X-Internal-Token": (
                        cfg.competitor_internal_token
                    )
                },
            )

            response.raise_for_status()
            data = response.json()

    except (
        httpx.HTTPError,
        ValueError,
    ) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "DISCOVERY_UNAVAILABLE",
                "message": (
                    "Competitor discovery service "
                    "is unavailable"
                ),
            },
        ) from exc

    return ok({
        **data,
        "status_url": (
            f"/projects/{project_id}"
            "/competitors/status"
        ),
        "results_url": (
            f"/projects/{project_id}/competitors"
        ),
    })


@router.get(
    "/projects/{project_id}/competitors/status"
)
async def discovery_status(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_project(
        db, project_id, user.id
    )

    job = await db.scalar(
        select(CompetitorJob)
        .where(
            CompetitorJob.project_id == project_id
        )
        .order_by(CompetitorJob.id.desc())
        .limit(1)
    )

    if not job:
        return ok({
            "project_id": project_id,
            "status": "not_started",
        })

    return ok({
        "job_id": job.id,
        "project_id": project_id,
        "status": job.status,
        "stage": job.stage,
        "queries": job.queries,
        "search_hits": job.search_hits,
        "candidates_total": job.candidates_total,
        "candidates_done": job.candidates_done,
        "confirmed_count": job.confirmed_count,
        "warnings": job.warnings,
        "error": job.error,
        "finished_at": job.finished_at,
    })


@router.get("/projects/{project_id}/competitors")
async def list_competitors(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_project(
        db, project_id, user.id
    )

    candidates = (
        await db.scalars(
            select(CompetitorCandidate)
            .where(
                CompetitorCandidate.project_id
                == project_id
            )
            .order_by(
                CompetitorCandidate.match_percent
                .desc()
                .nullslast(),
                CompetitorCandidate.id,
            )
        )
    ).all()

    return ok({
        "project_id": project_id,
        "total": len(candidates),
        "competitors": [
            candidate_data(c) for c in candidates
        ],
    })


@router.get(
    "/projects/{project_id}/competitors/{candidate_id}"
)
async def competitor_detail(
    project_id: int,
    candidate_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_project(
        db, project_id, user.id
    )

    candidate = await db.scalar(
        select(CompetitorCandidate).where(
            CompetitorCandidate.id == candidate_id,
            CompetitorCandidate.project_id
            == project_id,
        )
    )

    if not candidate:
        raise HTTPException(
            404,
            detail={
                "code": "COMPETITOR_NOT_FOUND",
                "message": "Competitor not found",
            },
        )

    sources = (
        await db.scalars(
            select(CompetitorSource)
            .where(
                CompetitorSource.candidate_id
                == candidate_id
            )
            .order_by(CompetitorSource.id.desc())
        )
    ).all()

    return ok({
        "competitor": candidate_data(candidate),
        "sources": [
            {
                "id": s.id,
                "url": s.url,
                "page_type": s.page_type,
                "status": s.status,
                "fetched_at": s.fetched_at,
                "text_preview": s.text[:500],
            }
            for s in sources
        ],
    })