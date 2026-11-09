import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from app.core.db import Session
from app.models.competitors import CompetitorJob, now
from app.competitors.discovery import (
    process_discovery,
)


async def recover_stale_jobs():
    cutoff = now() - timedelta(minutes=20)

    async with Session() as db:
        # A crashed worker may have left jobs running.
        await db.execute(
            update(CompetitorJob)
            .where(
                CompetitorJob.status == "running",
                CompetitorJob.updated_at < cutoff,
                CompetitorJob.attempts < 2,
            )
            .values(
                status="queued",
                stage="recovering",
                updated_at=now(),
            )
        )

        await db.execute(
            update(CompetitorJob)
            .where(
                CompetitorJob.status == "running",
                CompetitorJob.updated_at < cutoff,
                CompetitorJob.attempts >= 2,
            )
            .values(
                status="failed",
                stage="failed",
                error="Worker stopped before completion",
                finished_at=now(),
            )
        )

        await db.commit()


async def claim_job():
    async with Session() as db:
        query = (
            select(CompetitorJob)
            .where(CompetitorJob.status == "queued")
            .order_by(CompetitorJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )

        job = await db.scalar(query)

        if not job:
            return None

        job.status = "running"
        job.stage = "starting"
        job.attempts += 1
        job.updated_at = now()

        job_id = job.id

        await db.commit()

        return job_id


async def run_worker():
    print("Coirei competitor worker started")

    while True:
        await recover_stale_jobs()

        job_id = await claim_job()

        if job_id is None:
            await asyncio.sleep(3)
            continue

        try:
            await process_discovery(job_id)

        except Exception as exc:
            async with Session() as db:
                job = await db.get(
                    CompetitorJob, job_id
                )

                if job:
                    job.status = "failed"
                    job.stage = "failed"
                    job.error = (
                        f"{type(exc).__name__}: "
                        f"{str(exc)[:200]}"
                    )
                    job.finished_at = now()
                    await db.commit()


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("Worker stopped")