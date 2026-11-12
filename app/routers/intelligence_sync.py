"""V1 JWT-protected crawler import endpoints (additive to intelligence.py)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import Product, User
from app.schemas.api import ok
from app.services.common import owned_company
from app.services.intelligence.sync import (
    apply_sync, build_preview, load_business_profile,
)

router = APIRouter(prefix="/companies", tags=["intelligence"])


class ApplyIntelligenceSync(BaseModel):
    revision: int = Field(ge=1)
    include_inferred: bool = False
    import_products: bool = True


@router.get("/{company_id}/intelligence/sync-preview")
async def intelligence_sync_preview(
    company_id: int,
    include_inferred: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)
    link, report = await load_business_profile(db, company)
    existing = (await db.scalars(
        select(Product).where(Product.company_id == company.id)
    )).all()
    return ok(build_preview(company, link, report,
                            include_inferred=include_inferred, existing_products=existing))


@router.post("/{company_id}/intelligence/sync")
async def intelligence_sync(
    company_id: int,
    body: ApplyIntelligenceSync,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(db, company_id, user.id)
    link, report = await load_business_profile(db, company)
    result = await apply_sync(
        db, company, user, link, report,
        expected_revision=body.revision,
        include_inferred=body.include_inferred,
        import_products=body.import_products,
    )
    return ok(result)
