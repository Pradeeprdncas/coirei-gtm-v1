import secrets

from fastapi import (
    FastAPI, Header, Depends, HTTPException
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models.entities import Project
from app.models.competitors import CompetitorJob


app = FastAPI(
    title="Coirei Competitor Discovery Service",
    version="0.1.0",
)


def internal_auth(
    x_internal_token: str | None = Header(
        default=None,
        alias="X-Internal-Token",
    ),
):
    expected = settings().competitor_internal_token

    if (
        len(expected) < 32
        or not x_internal_token
        or not secrets.compare_digest(
            expected,
            x_internal_token,
        )
    ):
        raise HTTPException(
            401, detail="Unauthorized internal request"
        )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post(
    "/internal/projects/{project_id}/discover",
    status_code=202,
    dependencies=[Depends(internal_auth)],
)
async def enqueue_discovery(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    project = await db.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
    )

    if not project:
        raise HTTPException(
            404, detail="Project not found"
        )

    # Reuse an unfinished discovery job.
    existing = await db.scalar(
        select(CompetitorJob)
        .where(
            CompetitorJob.project_id == project_id,
            CompetitorJob.status.in_(
                ["queued", "running"]
            ),
        )
        .order_by(CompetitorJob.id.desc())
        .limit(1)
    )

    if existing:
        return {
            "job_id": existing.id,
            "project_id": project_id,
            "status": existing.status,
            "reused": True,
        }

    job = CompetitorJob(
        project_id=project_id,
        status="queued",
        stage="queued",
    )

    db.add(job)
    await db.flush()

    result = {
        "job_id": job.id,
        "project_id": project_id,
        "status": "queued",
        "reused": False,
    }

    await db.commit()

    return result