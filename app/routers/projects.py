from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import (
    Company, Product, Project, User
)
from app.schemas.api import ok
from app.services.common import owned_company

router = APIRouter(tags=["projects"])


class CreateProjects(BaseModel):
    # Omit or send [] to select all standalone products.
    product_ids: list[int] = Field(
        default_factory=list,
        max_length=30,
    )


def project_data(project: Project):
    return {
        "id": project.id,
        "company_id": project.company_id,
        "product_id": project.product_id,
        "name": project.name,
        "status": project.status,
        "snapshot": project.snapshot,
    }


async def owned_project(
    db: AsyncSession,
    project_id: int,
    user_id: int,
):
    row = (
        await db.execute(
            select(Project, Company)
            .join(
                Company,
                Company.id == Project.company_id,
            )
            .where(
                Project.id == project_id,
                Company.owner_id == user_id,
            )
        )
    ).first()

    if not row:
        raise HTTPException(
            404,
            detail={
                "code": "PROJECT_NOT_FOUND",
                "message": "Project not found",
            },
        )

    return row[0], row[1]


@router.post(
    "/companies/{company_id}/projects",
    status_code=201,
)
async def create_projects(
    company_id: int,
    body: CreateProjects,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(
        db, company_id, user.id
    )

    # Serialize project creation for this company.
    await db.execute(
        select(Company.id)
        .where(Company.id == company_id)
        .with_for_update()
    )

    if company.status != "complete":
        raise HTTPException(
            409,
            detail={
                "code": "ONBOARDING_INCOMPLETE",
                "message": "Complete onboarding first",
            },
        )

    requested = list(dict.fromkeys(body.product_ids))

    query = select(Product).where(
        Product.company_id == company_id
    )

    if requested:
        query = query.where(Product.id.in_(requested))
    else:
        query = query.where(
            Product.candidate_kind == "standalone_product",
            Product.status == "verified",
        )

    products = (await db.scalars(query)).all()

    if requested and len(products) != len(requested):
        raise HTTPException(
            404,
            detail={
                "code": "PRODUCT_NOT_FOUND",
                "message": "One or more products not found",
            },
        )

    if not products:
        raise HTTPException(
            409,
            detail={
                "code": "NO_PRODUCTS",
                "message": "No eligible products",
            },
        )

    invalid = [
        p.id for p in products
        if (
            p.status != "verified"
            or p.candidate_kind != "standalone_product"
        )
    ]

    if invalid:
        raise HTTPException(
            409,
            detail={
                "code": "PRODUCT_NOT_VERIFIED",
                "message": "Verify standalone products first",
                "details": {"product_ids": invalid},
            },
        )

    created = []
    existing = []

    for product in products:
        snapshot = {
            "company": {
                "id": company.id,
                "name": company.name,
                "country": company.country,
                "market_segment": company.market_segment,
                "business_model": company.business_model,
                "icp": company.icp,
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
            },
            "verified_revision": product.revision,
        }

        statement = (
            insert(Project)
            .values(
                company_id=company_id,
                product_id=product.id,
                name=f"{product.name} GTM Project",
                status="ready",
                snapshot=snapshot,
            )
            .on_conflict_do_nothing(
                constraint="uq_project_product"
            )
            .returning(Project.id)
        )

        new_id = await db.scalar(statement)

        project = await db.scalar(
            select(Project).where(
                Project.company_id == company_id,
                Project.product_id == product.id,
            )
        )

        if new_id is not None:
            created.append(project.id)
        else:
            existing.append(project.id)

    await db.commit()

    result = (
        await db.scalars(
            select(Project)
            .where(Project.company_id == company_id)
            .order_by(Project.id)
        )
    ).all()

    # Return requested/eligible products only.
    eligible_ids = {p.id for p in products}
    result = [
        p for p in result
        if p.product_id in eligible_ids
    ]

    return ok({
        "projects": [project_data(p) for p in result],
        "created_project_ids": created,
        "existing_project_ids": existing,
        "primary_project_id": (
            result[0].id if result else None
        ),
        "redirect_url": (
            f"/p/{result[0].id}/home"
            if result else None
        ),
    })


@router.get("/companies/{company_id}/projects")
async def list_projects(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await owned_company(db, company_id, user.id)

    projects = (
        await db.scalars(
            select(Project)
            .where(Project.company_id == company_id)
            .order_by(Project.id)
        )
    ).all()

    return ok({
        "company_id": company_id,
        "projects": [
            project_data(p) for p in projects
        ],
    })


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    project, _ = await owned_project(
        db, project_id, user.id
    )

    return ok({"project": project_data(project)})