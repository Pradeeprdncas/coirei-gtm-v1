import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.entities import Company, Conversation, Message, Product, Source, FieldClaim
from app.core.config import settings


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url if '://' in url else 'https://' + url)
    return parsed.hostname.lower().removeprefix('www.') if parsed.hostname else None


async def owned_company(db: AsyncSession, company_id: int, user_id: int) -> Company:
    company = await db.scalar(select(Company).where(Company.id == company_id, Company.owner_id == user_id))
    if not company:
        raise HTTPException(404, detail={'code': 'NOT_FOUND', 'message': 'Company not found'})
    return company


async def owned_product(db: AsyncSession, product_id: int, user_id: int, *, lock: bool = False) -> tuple[Product, Company]:
    query = select(Product, Company).join(Company, Company.id == Product.company_id).where(
        Product.id == product_id, Company.owner_id == user_id)
    if lock:
        query = query.with_for_update(of=Product)
    row = (await db.execute(query)).first()
    if not row:
        raise HTTPException(404, detail={'code': 'NOT_FOUND', 'message': 'Product not found'})
    return row[0], row[1]


async def onboarding_conversation(db: AsyncSession, company_id: int) -> Conversation:
    row = await db.scalar(select(Conversation).where(Conversation.company_id == company_id,
                                                     Conversation.kind == 'onboarding'))
    if row:
        return row
    row = Conversation(company_id=company_id, title='Onboarding', kind='onboarding')
    db.add(row)
    await db.flush()
    return row


async def message(db: AsyncSession, company_id: int, role: str, typ: str, content: dict, client_id: str | None = None):
    conversation = await onboarding_conversation(db, company_id)
    msg = Message(conversation_id=conversation.id, role=role, type=typ, content=content,
                  client_message_id=client_id)
    db.add(msg)
    await db.flush()
    return msg


async def source_from_text(db: AsyncSession, company_id: int, kind: str, url: str, text: str, *,
                           page_type: str = 'other', http_status: int | None = None,
                           robots_allowed: bool | None = None, structured: dict | None = None,
                           raw: dict | None = None, status: str = 'ok', error: str | None = None) -> Source:
    digest = sha(text)
    old = await db.scalar(select(Source).where(Source.company_id == company_id, Source.url == url,
                                                Source.content_hash == digest))
    if old:
        return old
    source = Source(company_id=company_id, kind=kind, url=url, content_hash=digest, text=text,
                    page_type=page_type, http_status=http_status, robots_allowed=robots_allowed,
                    structured=structured or {}, raw=raw or {}, status=status, error=error)
    db.add(source)
    await db.flush()
    folder = settings().data_dir / 'sources' / str(company_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{source.id}_{digest[:12]}.txt').write_text(text, encoding='utf-8')
    return source


async def add_claim(db: AsyncSession, company_id: int, product_id: int | None, field: str,
                    value, source: Source | None, quote: str | None, status: str = 'evidence'):
    if source is not None and quote and status not in ('intelligence',):
        if quote.casefold() not in source.text.casefold():
            if status != 'structured' or quote.casefold() not in __import__('json').dumps(source.structured or {}).casefold():
                return False
    if source is None and status == 'evidence':
        return False
    db.add(FieldClaim(company_id=company_id, product_id=product_id, field_key=field,
                      value={'value': value}, source_id=source.id if source else None,
                      quote=quote, source_kind=source.kind if source else 'ai', status=status))
    return True


def field_meta_set(meta: dict | None, field: str, *, status='found_on_site', review='pending',
                   source_id=None, quote=None, confidence=75) -> dict:
    result = dict(meta or {})
    result[field] = {'status': status, 'review': review, 'confidence': confidence,
                     'source_id': source_id, 'quote': quote}
    return result


def safe_name(name: str) -> bool:
    name = (name or '').strip()
    return bool(name and not name.isdigit() and len(name) <= 255 and not re.match(r'^(faq|pricing|plans|features)$', name, re.I))


def enough(company: Company, products: list[Product]) -> bool:
    return bool(company.name and any(safe_name(p.name) and p.description and p.key_features
                                     for p in products if p.candidate_kind in {'standalone_product','suite'}))
