import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.entities import Company, Conversation, ExtractionJob, Message, Product, Source, FieldClaim
from app.models.intelligence import IntelligenceLink
from app.core.config import settings
from app.services.intelligence.client import crawler_request

_RUNNING_GTM_STATES = {'CREATED', 'QUEUED', 'RUNNING', 'FINALIZING', 'PAUSED'}


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


def field_status(meta: dict | None, field: str, value) -> str:
    """Human-facing verification status for one field, derived from its
    field_meta entry: 'verified' / 'extracted' / 'needs_confirmation' /
    'inferred' / 'not_found'. One shared mapping so the label means the same
    thing on every endpoint that shows field-level detail (company profile,
    product detail, product list)."""
    empty = value in (None, '', [], {})
    entry = (meta or {}).get(field)
    if empty:
        return 'not_found'
    if not entry:
        return 'extracted'  # a value with no tracked history (e.g. seeded) — assume found, not verified
    if entry.get('review') == 'accepted':
        return 'verified'
    if entry.get('status') == 'ai_suggested':
        return 'inferred'
    if entry.get('status') in ('found_on_site', 'user_provided') and (entry.get('confidence') or 0) >= 70:
        return 'extracted'
    return 'needs_confirmation'


def field_statuses(obj, meta: dict | None, fields: tuple[str, ...]) -> dict[str, str]:
    """field_status() for a batch of fields read off `obj` by attribute name."""
    return {f: field_status(meta, f, getattr(obj, f, None)) for f in fields}


def safe_name(name: str) -> bool:
    name = (name or '').strip()
    return bool(name and not name.isdigit() and len(name) <= 255 and not re.match(r'^(faq|pricing|plans|features)$', name, re.I))


def enough(company: Company, products: list[Product]) -> bool:
    return bool(company.name and any(safe_name(p.name) and p.description and p.key_features
                                     for p in products if p.candidate_kind in {'standalone_product','suite'}))


def extraction_gaps(company: Company, products: list[Product]) -> list[str]:
    """Real, snake_case reasons enough() is False — what research_attention should
    list in missing_data. Distinct from missing_business_fields(): this reflects
    raw extraction completeness (does any product have a description and key
    features yet), not the later business-profile verification checklist."""
    gaps: list[str] = []
    classified = [p for p in products if p.candidate_kind in ('standalone_product', 'suite')]
    if not classified:
        gaps.append('product_classification')
    else:
        complete = any(safe_name(p.name) and p.description and p.key_features for p in classified)
        if not complete:
            for p in classified[:5]:
                if not safe_name(p.name):
                    gaps.append('product_name')
                if not p.description:
                    gaps.append('product_description')
                if not p.key_features:
                    gaps.append('product_key_features')
    if not company.business_model:
        gaps.append('business_model')
    if not company.value_proposition:
        gaps.append('value_proposition')
    if not company.icp:
        gaps.append('icp')
    return list(dict.fromkeys(gaps))[:6]


# (key, human label) for each business-profile dimension that still needs real
# data before verification is meaningful. Single source of truth for both the
# machine-readable `missing_data` keys and their display labels.
def missing_business_fields(company: Company, products: list[Product]) -> list[tuple[str, str]]:
    standalone = [p for p in products if p.candidate_kind in ('standalone_product', 'suite')]
    items: list[tuple[str, str]] = []
    if not company.business_model:
        items.append(('business_model', 'Business Model'))
    if not company.value_proposition:
        items.append(('value_proposition', 'Value Proposition'))
    if standalone and any(not p.positioning for p in standalone):
        items.append(('market_positioning', 'Current Market Positioning'))
    if standalone and any(p.pricing_status == 'not_found' for p in standalone):
        items.append(('pricing_and_plans', 'Pricing & Plans'))
    if not company.icp:
        items.append(('icp', 'Ideal Customer Profile (ICP)'))
    if not company.market_segment:
        items.append(('target_market_segments', 'Target Market Segments'))
    return items


def _summarize_field(value: Any, max_len: int = 220) -> str:
    """A short readable string for a JSONB business-profile field, whatever shape
    it was stored in (plain string, {'summary': ...}, {'target_audience': ...},
    a list, or an arbitrary dict from intelligence sync)."""
    if isinstance(value, dict):
        for key in ('summary', 'target_audience', 'text', 'value', 'description'):
            if value.get(key):
                return str(value[key])[:max_len]
        parts = []
        for v in value.values():
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                parts.extend(str(x) for x in v if isinstance(x, str) and x.strip())
        return '; '.join(parts)[:max_len] if parts else ''
    if isinstance(value, list):
        return ', '.join(str(v) for v in value[:5] if v)[:max_len]
    return str(value)[:max_len] if value else ''


def already_known_summary(company: Company, products: list[Product]) -> list[dict]:
    """What the conversation should tell the user we already know — real
    extracted values, not just "we have a business model" with no data."""
    items: list[dict] = []

    def add(label: str, value: Any):
        text = _summarize_field(value)
        if text:
            items.append({'label': label, 'value': text})

    add('Company name', company.name)
    add('Industry', company.industry)
    add('Business model', company.business_model)
    add('Value proposition', company.value_proposition)
    add('Ideal customer profile', company.icp)
    add('Target market', company.market_segment)

    classified = [p for p in products if p.candidate_kind in ('standalone_product', 'suite')]
    if classified:
        items.append({'label': 'Products', 'value': ', '.join(p.name for p in classified[:8])})

    return items


def verification_message(company: Company, products: list[Product]) -> dict:
    """Content for the 'verification_ready' conversation message."""
    items = missing_business_fields(company, products)
    return {
        'title': 'Coirei AI needs to verify some important product information.',
        'what_i_already_know': already_known_summary(company, products),
        'missing_data': [key for key, _ in items],
        'missing_data_labels': [label for _, label in items],
        'message': ('Please click Verify Product Information below to review, update, and '
                    'confirm the extracted details for each product before proceeding with '
                    'your GTM strategy.'),
        'action': {'id': 'verify_product_information', 'label': 'Verify Product Information'},
        'next_action': 'verify_products',
    }


def company_profile_message(company: Company, products: list[Product]) -> dict:
    """Content for the 'company_profile' conversation message, posted once every
    required product has been verified."""
    offerings = [p.name for p in products
                if p.candidate_kind in ('standalone_product', 'suite') and p.status == 'verified']
    size = company.size_band or (f'{company.employee_count} employees' if company.employee_count else None)
    location = ', '.join(x for x in (company.headquarters, company.country) if x) or None
    return {
        'name': company.name,
        'website_url': company.website_url,
        'business_overview': company.description,
        'industry': company.industry,
        'company_size': size,
        'location': location,
        'founded': company.founded_year,
        'offerings': offerings,
        'action': {'id': 'confirm_go_to_workspace', 'label': 'Confirm & Go to Workspace'},
    }


def onboarding_stage(company: Company | None) -> str:
    """The user's current onboarding stage, for login/signup responses.

    'onboarding_complete' (not just 'complete'): the ONBOARDING flow is done
    once a product is verified, but the project as a whole isn't — competitor
    discovery and other post-onboarding steps come after this, so the status
    name leaves room for a later 'project_complete'-type value instead of
    reusing a bare 'complete' that would then be ambiguous.
    """
    if company is None:
        return 'new'
    return {
        'researching': 'researching',
        'needs_context': 'needs_context',
        'ready_to_verify': 'verification',
        'complete': 'onboarding_complete',
    }.get(company.status, 'needs_context')


async def maybe_post_readiness_decision(db: AsyncSession, company_id: int) -> dict | None:
    """Post the research_attention / verification_ready decision once the
    add_context pipeline AND (if one was started) the intelligence deep-crawl
    have both reached a terminal state — never while either is still running,
    and never more than once per research round. Called from GET .../status on
    every poll (there is no push from either background job), so it is the
    single place this decision actually gets made and posted; pipeline.py only
    updates company.status, it never posts this message itself.

    Returns the posted content, or None if nothing was due (still running, or
    already decided this round).
    """
    job = await db.scalar(select(ExtractionJob).where(ExtractionJob.company_id == company_id)
                          .order_by(ExtractionJob.id.desc()).limit(1))
    if not job or job.status != 'completed':
        return None

    link = await db.get(IntelligenceLink, company_id)
    if link is not None:
        try:
            crawl = await crawler_request('GET', f'/v1/crawls/{link.crawler_job_id}')
        except HTTPException:
            return None  # crawler unreachable: wait rather than deciding on stale info
        if crawl.get('status') in _RUNNING_GTM_STATES:
            return None

    conversation = await onboarding_conversation(db, company_id)
    recent = (await db.scalars(select(Message).where(Message.conversation_id == conversation.id)
                               .order_by(Message.id.desc()).limit(15))).all()
    for m in recent:
        if m.type in ('research_attention', 'verification_ready'):
            return None  # already decided this round
        if m.type == 'research_started':
            break

    company = await db.get(Company, company_id)
    products = (await db.scalars(select(Product).where(Product.company_id == company_id))).all()

    # Local import: extract.core imports from this module, so this stays a
    # call-time import to avoid a circular import at module load.
    from app.services.extract.core import backfill_product_narrative_fields
    await backfill_product_narrative_fields(db, company, products)

    llm_issue = any(isinstance(w, dict) and w.get('reason') == 'llm_unavailable' for w in (job.warnings or []))
    ready_for_review = enough(company, products) and not llm_issue
    company.status = 'ready_to_verify' if ready_for_review else 'needs_context'

    if ready_for_review:
        content = verification_message(company, products)
        content['job_id'] = job.id
        content['product_count'] = len(products)
        await message(db, company_id, 'assistant', 'verification_ready', content)
    else:
        content = {
            'title': 'Coirei AI needs a little more context',
            'job_id': job.id,
            'actions': ['add_business_context', 'answer_questions', 'continue_website_data'],
            'description': 'Add documents, answer gap questions, or continue with website data.',
            'next_action': 'needs_context',
            'product_count': len(products),
            'what_i_already_know': already_known_summary(company, products),
            'missing_data': extraction_gaps(company, products),
        }
        await message(db, company_id, 'assistant', 'research_attention', content)

    await db.commit()
    return content
