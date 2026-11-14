import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import Company, FieldAudit, FieldClaim, PricePlan, Product, User
from app.schemas.api import PRODUCT_EDITABLE, PRODUCT_KINDS, ProductCreate, ProductPatch, VerifyProduct, ok
from app.services.common import (
    company_profile_message, field_meta_set, field_status, field_statuses, message, owned_company,
    owned_product, safe_name,
)

router = APIRouter(tags=['products'])

PRODUCT_STATUS_FIELDS = ('name', 'website_url', 'category', 'product_type', 'description',
                         'key_features', 'main_use_case', 'business_model',
                         'positioning', 'value_proposition', 'target_market', 'icp')


def product_dict(p: Product):
    keys=('id','company_id','parent_id','name','website_url','candidate_kind','category',
          'product_type','description','key_features','main_use_case','business_model','pricing_status',
          'positioning','value_proposition','target_market','icp','field_meta','status','revision','verified_at')
    return {key:getattr(p,key) for key in keys}


def blockers(p: Product) -> list[str]:
    reasons=[]
    if p.candidate_kind != 'standalone_product':
        reasons.append('not_standalone_product')
    if not safe_name(p.name):
        reasons.append('invalid_product_name')
    for field in ('description','key_features','main_use_case'):
        if getattr(p,field) in (None,'',[],{}):
            reasons.append('missing_'+field)
        elif (p.field_meta or {}).get(field,{}).get('review') != 'accepted':
            reasons.append('review_'+field)
    return reasons


@router.post('/companies/{company_id}/products', status_code=201)
async def add_product(company_id:int, body:ProductCreate, db:AsyncSession=Depends(get_db),
                      user:User=Depends(current_user)):
    company=await owned_company(db,company_id,user.id)
    if not safe_name(body.name):
        raise HTTPException(422,detail={'code':'INVALID_PRODUCT_NAME','message':'Use a real product name'})
    existing=await db.scalar(select(Product).where(Product.company_id==company_id,
                                                     Product.name==body.name.strip()))
    if existing:
        return ok({'product':product_dict(existing),'existing':True})
    row=Product(company_id=company.id,name=body.name.strip(),website_url=body.website_url,
        candidate_kind=body.candidate_kind,
        field_meta=field_meta_set({},'name',status='user_provided',review='accepted',confidence=100))
    db.add(row);await db.flush()
    await message(db,company_id,'assistant','product_added',{'product_id':row.id,'name':row.name})
    await db.commit()
    return ok({'product':product_dict(row),'existing':False})


@router.get('/companies/{company_id}/products')
async def products(company_id:int, db:AsyncSession=Depends(get_db), user:User=Depends(current_user)):
    company=await owned_company(db,company_id,user.id)
    rows=(await db.scalars(select(Product).where(Product.company_id==company.id)
                           .order_by(Product.id))).all()
    return ok({'company_id':company_id,'company_name':company.name,'total_products':len(rows),
               'products':[dict(product_dict(p), verification={'can_verify':not blockers(p),
                            'blocking_reasons':blockers(p)},
                            field_status=field_statuses(p, p.field_meta, PRODUCT_STATUS_FIELDS))
                           for p in rows]})


@router.get('/products/{product_id}')
async def detail(product_id:int, db:AsyncSession=Depends(get_db), user:User=Depends(current_user)):
    product,company=await owned_product(db,product_id,user.id)
    plans=(await db.scalars(select(PricePlan).where(PricePlan.product_id==product.id).order_by(PricePlan.id))).all()
    claims=(await db.scalars(select(FieldClaim).where(FieldClaim.product_id==product.id)
                             .order_by(FieldClaim.id))).all()
    status = field_statuses(product, product.field_meta, PRODUCT_STATUS_FIELDS)
    status['pricing'] = field_status(product.field_meta, 'pricing', plans)
    return ok(dict(product_dict(product),
        pricing=[{'id':p.id,'name':p.name,'price_amount':float(p.price_amount) if p.price_amount is not None else None,
                  'currency':p.currency,'unit':p.unit,'period':p.period,'is_custom':p.is_custom,
                  'quote':p.quote,'source_id':p.source_id} for p in plans],
        evidence=[{'id':c.id,'field':c.field_key,'value':c.value,'source_id':c.source_id,
                   'quote':c.quote,'status':c.status} for c in claims],
        field_status=status,
        verification={'can_verify':not blockers(product),'blocking_reasons':blockers(product)}))


def _clean_pricing(value) -> list[dict]:
    """Validate a user-submitted pricing list (replaces every PricePlan row for
    the product). Lenient: only `name` is required; everything else defaults
    sensibly, so the frontend can send exactly the fields it has."""
    if not isinstance(value, list):
        raise HTTPException(422, detail={'code': 'INVALID_FIELD', 'message': 'pricing must be a list'})
    cleaned = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or not str(item.get('name', '')).strip():
            raise HTTPException(422, detail={'code': 'INVALID_PRICING_ITEM',
                                             'message': f'pricing[{i}] needs a non-empty "name"'})
        amount = item.get('price_amount')
        if amount is not None:
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                raise HTTPException(422, detail={'code': 'INVALID_PRICING_ITEM',
                                                 'message': f'pricing[{i}].price_amount is not a number'})
        currency = item.get('currency')
        if currency is not None and not re.fullmatch(r'[A-Za-z]{3}', str(currency)):
            raise HTTPException(422, detail={'code': 'INVALID_PRICING_ITEM',
                                             'message': f'pricing[{i}].currency must be a 3-letter code'})
        cleaned.append({
            'name': str(item['name']).strip()[:120],
            'kind': str(item.get('kind') or 'base')[:40],
            'price_amount': amount,
            'currency': str(currency).upper() if currency else None,
            'unit': (str(item['unit'])[:50] if item.get('unit') else None),
            'period': (str(item['period'])[:50] if item.get('period') else None),
            'is_custom': bool(item.get('is_custom', False)),
            'quote': item.get('quote'),
        })
    return cleaned


@router.patch('/products/{product_id}')
async def patch_product(product_id:int, body:ProductPatch, db:AsyncSession=Depends(get_db),
                        user:User=Depends(current_user)):
    product,company=await owned_product(db,product_id,user.id,lock=True)
    if product.revision!=body.revision:
        raise HTTPException(409,detail={'code':'STALE_REVISION','message':'Reload product before editing',
                                        'details':{'revision':product.revision}})
    invalid=(set(body.fields) | set(body.accept_fields)) - PRODUCT_EDITABLE
    if invalid:
        raise HTTPException(422,detail={'code':'UNKNOWN_FIELD','message':str(sorted(invalid))})
    if 'candidate_kind' in body.fields and body.fields['candidate_kind'] not in PRODUCT_KINDS:
        raise HTTPException(422,detail={'code':'INVALID_KIND','message':'Unknown product classification'})
    if 'name' in body.fields and not safe_name(str(body.fields['name'])):
        raise HTTPException(422,detail={'code':'INVALID_NAME','message':'Invalid product name'})
    if 'key_features' in body.fields and not (isinstance(body.fields['key_features'],list) and
               all(isinstance(x,str) for x in body.fields['key_features'])):
        raise HTTPException(422,detail={'code':'INVALID_FEATURES','message':'key_features must be a string list'})
    for field in ('business_model','icp','target_market','value_proposition'):
        if field in body.fields and not isinstance(body.fields[field],dict):
            raise HTTPException(422,detail={'code':'INVALID_FIELD','message':f'{field} must be an object'})
    new_pricing = _clean_pricing(body.fields['pricing']) if 'pricing' in body.fields else None
    if 'pricing' in body.accept_fields:
        raise HTTPException(422, detail={'code': 'UNKNOWN_FIELD',
                                         'message': 'pricing has no review state to accept; just set it in fields'})
    for field in body.accept_fields:
        value=body.fields.get(field,getattr(product,field))
        if value in (None,'',[],{}):
            raise HTTPException(409,detail={'code':'EMPTY_FIELD','message':f'{field} cannot be accepted empty'})
    meta=dict(product.field_meta or {})
    dirty=False
    if new_pricing is not None:
        existing_plans=(await db.scalars(select(PricePlan).where(PricePlan.product_id==product.id))).all()
        before=[{'name':p.name,'price_amount':float(p.price_amount) if p.price_amount is not None else None,
                 'currency':p.currency} for p in existing_plans]
        await db.execute(delete(PricePlan).where(PricePlan.product_id==product.id))
        for item in new_pricing:
            db.add(PricePlan(company_id=company.id, product_id=product.id, **item))
        product.pricing_status='published_needs_review' if new_pricing else 'not_found'
        after=[{**item,'price_amount':float(item['price_amount']) if item['price_amount'] is not None else None}
               for item in new_pricing]
        db.add(FieldAudit(company_id=company.id,product_id=product.id,actor_id=user.id,
                          field_key='pricing',before={'value':before},after={'value':after}))
        meta=field_meta_set(meta,'pricing',status='user_provided',review='accepted',confidence=100)
        dirty=True
    for field,value in body.fields.items():
        if field=='pricing':
            continue
        old=getattr(product,field)
        if old==value:
            continue
        setattr(product,field,value)
        db.add(FieldAudit(company_id=company.id,product_id=product.id,actor_id=user.id,
                          field_key=field,before={'value':old},after={'value':value}))
        # Existing FieldClaim rows stay intact; a manual correction wins in the UI.
        meta=field_meta_set(meta,field,status='user_provided',review='accepted',confidence=100)
        dirty=True
    for field in body.accept_fields:
        existing=meta.get(field,{})
        if existing.get('review')!='accepted':
            meta=field_meta_set(meta,field,status=existing.get('status','user_provided'),
                                review='accepted',confidence=existing.get('confidence',75),
                                source_id=existing.get('source_id'),quote=existing.get('quote'))
            db.add(FieldAudit(company_id=company.id,product_id=product.id,actor_id=user.id,
                        field_key=field,before={'review':existing.get('review')},after={'review':'accepted'}))
            dirty=True
    if dirty:
        product.revision+=1
        product.field_meta=meta
        if product.status=='verified':
            product.status='detected'; product.verified_at=None
        await message(db,company.id,'assistant','product_edited',
                      {'product_id':product.id,'name':product.name,'changed_fields':list(body.fields),
                       'accepted_fields':body.accept_fields})
    await db.commit()
    return ok({'product':product_dict(product),'changed':dirty,
               'verification':{'can_verify':not blockers(product),
                               'blocking_reasons':blockers(product)}})

@router.post("/products/{product_id}/verify")
async def verify_product(
    product_id: int,
    body: VerifyProduct,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    product, company = await owned_product(
        db,
        product_id,
        user.id,
        lock=True,
    )

    # Repeated successful verification is idempotent.
    if (
        product.status == "verified"
        and body.revision in (
            product.revision,
            product.revision - 1,
        )
    ):
        return ok({
            "product_id": product.id,
            "status": "verified",
            "revision": product.revision,
            "changed": False,
            "accepted_fields": [],
            "all_products_verified": await all_verified(
                db, company.id
            ),
        })

    # Detect genuinely stale frontend data.
    if body.revision != product.revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_REVISION",
                "message": "Reload product before verifying",
                "details": {
                    "revision": product.revision,
                },
            },
        )

    # Fields explicitly accepted by the user.
    accepted_fields = list(
        dict.fromkeys(body.accept_fields)
    )

    current_blockers = blockers(product)

    # An accepted field may resolve its corresponding
    # review blocker, but it cannot resolve missing data
    # or an invalid product classification.
    resolved_blockers = {
        f"review_{field}"
        for field in accepted_fields
    }

    remaining_blockers = [
        reason
        for reason in current_blockers
        if reason not in resolved_blockers
    ]

    if remaining_blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRODUCT_REVIEW_INCOMPLETE",
                "message": (
                    "Review required product fields "
                    "and classification before verification"
                ),
                "details": {
                    "blocking_reasons": remaining_blockers,
                    "required_accept_fields": [
                        reason.removeprefix("review_")
                        for reason in current_blockers
                        if reason.startswith("review_")
                    ],
                    "revision": product.revision,
                    "next_action": "review_product_fields",
                },
            },
        )

    meta = dict(product.field_meta or {})
    newly_accepted = []

    for field in accepted_fields:

        # Never approve empty values.
        value = getattr(product, field)

        if value in (None, "", [], {}):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EMPTY_FIELD",
                    "message": (
                        f"{field} must be filled "
                        "before verification"
                    ),
                },
            )

        previous = meta.get(field, {})

        if previous.get("review") == "accepted":
            continue

        # Preserve extraction evidence and confidence.
        meta = field_meta_set(
            meta,
            field,
            status=previous.get(
                "status",
                "user_provided",
            ),
            review="accepted",
            confidence=previous.get(
                "confidence",
                75,
            ),
            quote=previous.get("quote"),
            source_id=previous.get("source_id"),
        )

        # Persist the user's acceptance.
        db.add(
            FieldAudit(
                company_id=company.id,
                product_id=product.id,
                actor_id=user.id,
                field_key=field,
                before={
                    "review": previous.get("review"),
                },
                after={
                    "review": "accepted",
                    "via": "verify",
                },
            )
        )

        newly_accepted.append(field)

    product.field_meta = meta

    product.status = "verified"
    product.verified_at = datetime.now(timezone.utc)
    product.revision += 1

    # Store a verification audit record.
    db.add(
        FieldAudit(
            company_id=company.id,
            product_id=product.id,
            actor_id=user.id,
            field_key="verification",
            before={
                "status": "detected",
            },
            after={
                "status": "verified",
            },
        )
    )

    # Store the verification event in onboarding chat.
    await message(
        db,
        company.id,
        "assistant",
        "product_verified",
        {
            "product_id": product.id,
            "name": product.name,
            "text": f"{product.name} verified",
        },
    )

    all_products_verified = await all_verified(db, company.id)

    if all_products_verified:
        all_products = (await db.scalars(
            select(Product).where(Product.company_id == company.id)
        )).all()
        await message(
            db, company.id, "assistant", "company_profile",
            company_profile_message(company, list(all_products)),
        )

    await db.commit()

    return ok({
        "product_id": product.id,
        "status": "verified",
        "revision": product.revision,
        "changed": True,
        "accepted_fields": newly_accepted,
        "all_products_verified": all_products_verified,
    })

async def all_verified(db:AsyncSession,company_id:int)->bool:
    rows=(await db.scalars(select(Product).where(Product.company_id==company_id,
                                 Product.candidate_kind=='standalone_product'))).all()
    return bool(rows) and all(p.status=='verified' for p in rows)

@router.post("/companies/{company_id}/complete")
async def complete(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(
        db, company_id, user.id
    )

    if company.status == "complete":
        return ok({
            "company_id": company_id,
            "status": "onboarding_complete",
            "changed": False
        })

    # At least ONE standalone product must be verified.
    verified_product_id = await db.scalar(
        select(Product.id)
        .where(
            Product.company_id == company_id,
            Product.candidate_kind == "standalone_product",
            Product.status == "verified",
        )
        .limit(1)
    )

    if verified_product_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NO_VERIFIED_PRODUCT",
                "message": (
                    "Verify at least one standalone product "
                    "before completing onboarding."
                ),
            },
        )

    company.status = "complete"

    await message(
        db,
        company_id,
        "assistant",
        "onboarding_complete",
        {
            "text": (
                "Onboarding completed successfully. "
                "You can verify additional products later."
            ),
            "verified_product_id": verified_product_id,
            "next_action": "create_projects",
        },
    )

    await db.commit()

    return ok({
        "company_id": company_id,
        "status": "onboarding_complete",
        "verified_product_id": verified_product_id,
        "next_action": "create_projects",
        "conversation_url": (
            f"/companies/{company_id}/conversation"
        ),
    })