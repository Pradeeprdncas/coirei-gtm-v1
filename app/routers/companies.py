from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import Company, FieldAudit, FieldClaim, User
from app.schemas.api import COMPANY_EDITABLE, CompanyPatch, CreateCompany, ok
from app.services.common import (
    domain, field_meta_set, field_statuses, message, onboarding_conversation, owned_company,
)

router = APIRouter(prefix='/companies', tags=['companies'])

COMPANY_STATUS_FIELDS = ('name', 'website_url', 'industry', 'size_band', 'employee_count',
                         'headquarters', 'country', 'founded_year', 'description', 'business_model',
                         'market_segment', 'value_proposition', 'icp', 'socials')


def company_dict(c: Company):
    return {key: getattr(c, key) for key in ('id','name','website_url','domain','industry','size_band',
            'employee_count','headquarters','country','founded_year','description','business_model',
            'market_segment','value_proposition','icp','socials','field_meta','status','revision',
            'created_at','updated_at')}


@router.post('')
async def create_company(body: CreateCompany, db: AsyncSession = Depends(get_db),
                         user: User = Depends(current_user)):
    d = domain(body.website_url)
    # One owned company per domain, or one owned no-domain company per name.
    stmt = select(Company).where(Company.owner_id == user.id)
    stmt = stmt.where(Company.domain == d) if d else stmt.where(
        Company.domain.is_(None), Company.name.ilike(body.name.strip()))
    old = await db.scalar(stmt)
    if not old and d:
        # Claim an unlinked signup company by exact owned name instead of creating a duplicate.
        old = await db.scalar(select(Company).where(Company.owner_id == user.id,
                    Company.domain.is_(None), Company.name.ilike(body.name.strip())))
        if old:
            old.domain, old.website_url = d, body.website_url
            await db.commit()
    if old:
        return ok({'company': company_dict(old), 'existing': True})
    company = Company(owner_id=user.id, name=body.name.strip(), website_url=body.website_url, domain=d)
    db.add(company)
    await db.flush()
    await onboarding_conversation(db, company.id)
    await message(db, company.id, 'assistant', 'welcome', {'text': 'Welcome to Coirei onboarding.',
                                                          'next_action': 'add_context'})
    await db.commit()
    return ok({'company': company_dict(company), 'existing': False})


@router.get('')
async def list_companies(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    items = (await db.scalars(select(Company).where(Company.owner_id == user.id).order_by(Company.id))).all()
    return ok({'companies': [company_dict(c) for c in items]})


@router.get('/{company_id}/profile')
async def get_profile(company_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(current_user)):
    company = await owned_company(db, company_id, user.id)
    claims = (await db.scalars(select(FieldClaim).where(FieldClaim.company_id == company_id,
        FieldClaim.product_id.is_(None)).order_by(FieldClaim.id))).all()
    return ok({'company': company_dict(company),
               'field_status': field_statuses(company, company.field_meta, COMPANY_STATUS_FIELDS),
               'claims': [
        {'id': x.id, 'field': x.field_key, 'value': x.value, 'quote': x.quote,
         'source_id': x.source_id, 'status': x.status} for x in claims]})


@router.patch('/{company_id}/profile')
async def update_profile(company_id: int, body: CompanyPatch, db: AsyncSession = Depends(get_db),
                         user: User = Depends(current_user)):
    company = await owned_company(db, company_id, user.id)
    if body.revision != company.revision:
        raise HTTPException(409, detail={'code': 'STALE_REVISION', 'message': 'Reload company profile',
                                         'details': {'revision': company.revision}})
    invalid = (set(body.fields) | set(body.accept_fields)) - COMPANY_EDITABLE
    if invalid:
        raise HTTPException(422, detail={'code': 'UNKNOWN_FIELD', 'message': str(sorted(invalid))})
    meta = dict(company.field_meta or {})
    dirty = False
    for key, value in body.fields.items():
        old = getattr(company, key)
        if old != value:
            setattr(company, key, value)
            db.add(FieldAudit(company_id=company.id, actor_id=user.id, field_key=key,
                              before={'value': old}, after={'value': value}))
            meta = field_meta_set(meta, key, status='user_provided', review='accepted', confidence=100)
            dirty = True
    for key in body.accept_fields:
        if getattr(company, key) in (None, '', [], {}):
            raise HTTPException(409, detail={'code': 'EMPTY_FIELD', 'message': key})
        if meta.get(key, {}).get('review') != 'accepted':
            meta = field_meta_set(meta, key, status=meta.get(key, {}).get('status','found_on_site'),
                                  review='accepted', confidence=meta.get(key,{}).get('confidence',75),
                                  source_id=meta.get(key,{}).get('source_id'),
                                  quote=meta.get(key,{}).get('quote'))
            dirty = True
    if dirty:
        company.revision += 1
        company.field_meta = meta
    await db.commit()
    return ok({'company': company_dict(company), 'changed': dirty})


@router.get('/{company_id}/sources')
async def list_sources(company_id: int, db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    from app.models.entities import Source
    await owned_company(db, company_id, user.id)
    items = (await db.scalars(select(Source).where(Source.company_id == company_id)
                              .order_by(Source.id.desc()).limit(100))).all()
    return ok({'sources': [{'id':x.id,'url':x.url,'kind':x.kind,'page_type':x.page_type,
                            'status':x.status,'http_status':x.http_status,
                            'robots_allowed':x.robots_allowed,'error':x.error,
                            'fetched_at':x.fetched_at,
                            'text_preview':x.text[:250]} for x in items]})
