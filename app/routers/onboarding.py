import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.security import current_user
from app.models.entities import Company, Conversation, ExtractionJob, FieldAudit, GapAnswer, GapQuestion, Message, Product, Source, User
from app.models.intelligence import IntelligenceLink
from app.schemas.api import ChatInput, GapAnswers, ok
from app.services.common import domain, enough, message, onboarding_conversation, owned_company, source_from_text, field_meta_set, safe_name
from app.services.documents import extract_document
from app.services.intelligence.client import crawler_request
from app.services.llm import json_completion
from app.services.pipeline import process_job
from app.services.rag.core import retrieve
from app.services.gap_suggestions import (
    merge_suggestions,
    page_types_for,
    question_template,
)


def _intel_context(company: Company, products: list[Product]) -> dict:
    """Distill already-synced intelligence into a compact dict for prompt injection."""
    ctx: dict = {}
    if company.description:
        ctx["description"] = company.description[:300]
    if company.industry:
        ctx["industry"] = company.industry
    vp = company.value_proposition or {}
    if vp.get("selling_points"):
        ctx["selling_points"] = vp["selling_points"][:3]
    if vp.get("product_positioning") or vp.get("summary"):
        ctx["positioning"] = (vp.get("product_positioning") or vp.get("summary"))[:200]
    ms = company.market_segment or {}
    if ms.get("main_use_case"):
        ctx["use_cases"] = ms["main_use_case"][:5]
    if ms.get("target_segments"):
        ctx["target_segments"] = ms["target_segments"]
    bm = company.business_model or {}
    if bm.get("customer_type"):
        ctx["customer_type"] = bm["customer_type"]
    if bm.get("revenue_model"):
        ctx["revenue_model"] = bm["revenue_model"]
    if bm.get("price"):
        ctx["published_prices"] = bm["price"][:4]
    if bm.get("plan_name"):
        ctx["plan_names"] = bm["plan_name"][:4]
    icp = company.icp or {}
    if icp.get("icp_hypotheses"):
        ctx["icp_hypotheses"] = icp["icp_hypotheses"][:2]
    if company.socials:
        ctx["has_contact_info"] = True
    for p in products:
        if p.candidate_kind in ("standalone_product", "suite") and p.description:
            ctx.setdefault("known_products", []).append(
                {"name": p.name, "description": p.description[:120]}
            )
    return ctx


def _intel_aware_question(field_key: str, product_name: str, ctx: dict) -> str:
    """Return a richer question that references what we already know."""
    kind = field_key.rsplit(".", 1)[-1]
    company_name = ""
    if ctx.get("description"):
        # extract implied company type from description
        company_name = ""

    if kind == "icp":
        if ctx.get("use_cases"):
            uses = ", ".join(str(u) for u in ctx["use_cases"][:3])
            return f"Based on your use cases ({uses}), who specifically is your ideal customer — their role, company size, and industry?"
        if ctx.get("selling_points"):
            sp = ctx["selling_points"][0]
            return f"Your platform {sp[:80]}. Who specifically is the ideal customer you built this for?"
        return "Who is your ideal customer — their role, company size, and the specific problem they hire you to solve?"

    if kind == "business_model":
        if ctx.get("plan_names") or ctx.get("published_prices"):
            plans = ", ".join(str(p) for p in (ctx.get("plan_names") or []))
            prices = ", ".join(str(p) for p in (ctx.get("published_prices") or []))
            return f"We found plan names ({plans}) and prices ({prices}) on your site. What is the full pricing model — subscription tiers, usage-based fees, or enterprise contracts?"
        return "How do you charge customers — subscription, usage-based, one-time, or enterprise contract?"

    if kind == "key_features":
        if ctx.get("use_cases"):
            uses = ctx["use_cases"][0]
            return f"{product_name} supports use cases like '{uses}'. What are its 3–5 key features that deliver these outcomes?"
        return f"What are the 3–5 key capabilities that make {product_name} valuable to customers?"

    if kind == "main_use_case":
        if ctx.get("description"):
            return f"You described your platform as: '{ctx['description'][:120]}'. What is the single most important use case customers rely on {product_name} for?"
        return f"What is the primary job customers hire {product_name} to do?"

    if kind == "description":
        if ctx.get("industry"):
            return f"In the {ctx['industry']} space, how would you describe {product_name} in 1–2 sentences for a first-time visitor?"
        return f"How would you describe {product_name} in 1–2 sentences for someone who has never heard of it?"

    return question_template(field_key, product_name)

from typing import Any

router = APIRouter(prefix='/companies', tags=['onboarding'])
@router.post('/{company_id}/context', status_code=202)
async def add_context(company_id: int, background: BackgroundTasks,
                      website_url: str | None = Form(None),
                      website_urls: list[str] | None = Form(None),
                      prompt: str | None = Form(None),
                      files: list[Any] | None = Form(None),
                      db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    company = await owned_company(db, company_id, user.id)
    running = await db.scalar(select(ExtractionJob.id).where(ExtractionJob.company_id == company_id,
                        ExtractionJob.status.in_(['queued','running'])).limit(1))
    if running:
        raise HTTPException(409, detail={'code': 'RESEARCH_IN_PROGRESS', 'message': 'Research is already running',
                                         'details': {'job_id': running}})
    raw_urls = ([website_url] if website_url else []) + (website_urls or [])
    urls = list(dict.fromkeys([u.strip() if '://' in u else 'https://' + u.strip()
                               for u in raw_urls if u and u.strip() and u.strip().lower() != 'string']))
    
    # Safely filter out strings, None, or empty values sent by the client under 'files'
    valid_files: list[UploadFile] = []
    if files:
        for item in files:
            if isinstance(item, UploadFile) and item.filename:
                valid_files.append(item)

    if not (urls or valid_files or prompt):
        raise HTTPException(422, detail={'code': 'EMPTY_CONTEXT', 'message': 'Provide a website, prompt or document'})
    if urls:
        from app.services.scrape.core import public_url
        for url in urls:
            if not await public_url(url):
                raise HTTPException(422, detail={'code': 'INVALID_URL', 'message': 'Use a public http(s) website'})
        if company.domain and any(domain(u) != company.domain for u in urls):
            raise HTTPException(422, detail={'code': 'DOMAIN_MISMATCH', 'message': 'Website must match company domain'})
        if not company.domain:
            wanted = domain(urls[0])
            if len({domain(u) for u in urls}) > 1:
                raise HTTPException(422, detail={'code': 'MULTIPLE_DOMAINS',
                    'message': 'Use one company domain per workspace'})
            existing_id = await db.scalar(select(Company.id).where(
                Company.owner_id == user.id, Company.domain == wanted, Company.id != company_id))
            if existing_id:
                raise HTTPException(409, detail={'code': 'DOMAIN_ALREADY_LINKED',
                    'message': 'This website belongs to another of your companies. Use its company ID.',
                    'details': {'existing_company_id': existing_id}})
            company.website_url = urls[0]
            company.domain = wanted
    if prompt:
        await source_from_text(db, company_id, 'prompt', f'user-prompt://company/{company_id}/{hashlib.sha256(prompt.encode()).hexdigest()}',
                               prompt[:12000], page_type='prompt')
    uploaded=[]
    for item in valid_files:
        payload = await item.read(5_000_001)
        if len(payload)>5_000_000:
            raise HTTPException(413, detail={'code':'FILE_TOO_LARGE','message':'Each document must be <= 5 MB'})
        if len(uploaded)>=5:
            raise HTTPException(422, detail={'code':'TOO_MANY_FILES','message':'At most 5 files'})
        try:
            text = extract_document(item.filename or 'document.txt', payload)
        except Exception as exc:
            raise HTTPException(422, detail={'code':'DOCUMENT_PARSE_FAILED','message': str(exc)[:160]}) from exc
        if not text.strip():
            raise HTTPException(422, detail={'code':'EMPTY_DOCUMENT','message':'Document has no extractable text; OCR is not supported'})
        raw_hash = hashlib.sha256(payload).hexdigest()
        src = await source_from_text(db, company_id,'document',
                     f'document://company/{company_id}/{raw_hash}', text, page_type='document',
                     raw={'filename':Path(item.filename or 'document').name[:180], 'size_bytes':len(payload)})
        uploaded.append({'source_id': src.id, 'filename': item.filename})
    job = ExtractionJob(company_id=company_id)
    db.add(job)
    await db.flush()
    company.status = 'researching'
    await message(db, company_id, 'user', 'context',
                  {'website_urls': urls, 'prompt': prompt, 'files': uploaded})
    await message(db, company_id, 'assistant', 'research_started',
                  {'text': 'Research started', 'job_id': job.id, 'next_action': 'watch_status'})
    await db.commit()
    background.add_task(process_job, company_id, job.id, urls)
    return ok({'company_id': company_id, 'job_id': job.id, 'status': 'queued',
               'conversation_url': f'/companies/{company_id}/conversation',
               'status_url': f'/companies/{company_id}/status'})

RUNNING_MAIN_STATES = {'queued', 'running'}
RUNNING_GTM_STATES = {'CREATED', 'QUEUED', 'RUNNING', 'FINALIZING', 'PAUSED'}
# How long the "live" pick stays on one page before a newer fetch replaces it,
# so the frontend's live view doesn't flicker on every 5s poll.
_LIVE_PICK_STABLE_SECONDS = 10.0
_live_pick_cache: dict[int, tuple[int, float]] = {}


def _stable_live_pick(company_id: int, pages: list[dict]) -> dict | None:
    if not pages:
        _live_pick_cache.pop(company_id, None)
        return None
    newest = pages[0]
    cached = _live_pick_cache.get(company_id)
    now = time.monotonic()
    if cached:
        cached_fetch_id, picked_at = cached
        if now - picked_at < _LIVE_PICK_STABLE_SECONDS:
            match = next((p for p in pages if p.get('fetch_id') == cached_fetch_id), None)
            if match:
                return match
    _live_pick_cache[company_id] = (newest.get('fetch_id'), now)
    return newest


async def _intelligence_snapshot(company_id: int, db: AsyncSession) -> dict | None:
    """Best-effort status of the GTM crawler's deep-crawl job (started via
    POST .../intelligence/crawl), if one exists for this company. Never raises:
    a crawler hiccup degrades this section instead of failing the whole poll."""
    link = await db.get(IntelligenceLink, company_id)
    if link is None:
        return None
    try:
        job = await crawler_request('GET', f'/v1/crawls/{link.crawler_job_id}')
        live = await crawler_request('GET', f'/v1/crawls/{link.crawler_job_id}/live?limit=5')
    except HTTPException as exc:
        detail = exc.detail.get('message') if isinstance(exc.detail, dict) else str(exc.detail)
        return {'crawler_job_id': link.crawler_job_id, 'status': 'UNAVAILABLE', 'stage': None,
                'url_report': [], 'live_browser_url': None, 'error_summary': detail}
    pages = live.get('pages') or []
    url_report = [{
        'url': p.get('url'), 'page_type': p.get('page_type'), 'fetched_at': p.get('fetched_at'),
        'screenshot_url': (f'/companies/{company_id}/intelligence/live-screenshot/{p["fetch_id"]}'
                           if p.get('fetch_id') is not None else None),  # fmt: skip
    } for p in pages]
    pick = _stable_live_pick(company_id, pages)
    live_browser_url = (f'/companies/{company_id}/intelligence/live-screenshot/{pick["fetch_id"]}'
                        if pick and pick.get('fetch_id') is not None else None)  # fmt: skip
    return {
        'crawler_job_id': link.crawler_job_id, 'status': job.get('status'), 'stage': job.get('stage'),
        'site_crawls': job.get('site_crawls'), 'result': job.get('result'),
        'error_summary': job.get('error_summary'), 'url_report': url_report,
        'live_browser_url': live_browser_url,
    }


def _merge_status(main_status: str, gtm_status: str | None) -> str:
    """One of running / success / failed / not_started, combining the main
    onboarding pipeline (add_context) and the GTM deep-crawl (intelligence/crawl).
    Either pipeline still going keeps it "running"; either one failing is "failed";
    otherwise "success" once at least one has completed."""
    if main_status in RUNNING_MAIN_STATES or gtm_status in RUNNING_GTM_STATES:
        return 'running'
    if main_status == 'failed' or gtm_status == 'FAILED':
        return 'failed'
    if main_status == 'completed' or gtm_status == 'COMPLETED':
        return 'success'
    if main_status == 'not_started' and gtm_status in (None, 'CANCELLED', 'UNAVAILABLE'):
        return 'not_started'
    return 'running'


@router.get('/{company_id}/status')
async def status(company_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    company = await owned_company(db, company_id, user.id)
    job = await db.scalar(select(ExtractionJob).where(ExtractionJob.company_id == company_id)
                          .order_by(ExtractionJob.id.desc()).limit(1))
    intelligence = await _intelligence_snapshot(company_id, db)

    main_status = job.status if job else 'not_started'
    overall_status = _merge_status(main_status, intelligence.get('status') if intelligence else None)

    messages: list[str] = []
    if job and job.current_url:
        messages.append(f'Crawling {job.current_url}')
    elif job and job.stage == 'indexing':
        messages.append('Indexing extracted pages')
    elif job and job.stage == 'extracting':
        messages.append('Extracting company and product details')
    if job and job.status == 'completed':
        product_count = await db.scalar(
            select(func.count()).select_from(Product).where(Product.company_id == company_id))
        messages.append(f'{product_count} product(s) found' if product_count else 'Research complete')
    if job and job.status == 'failed':
        messages.append(f'Research failed: {job.error}' if job.error else 'Research failed')
    if intelligence:
        gtm_status = intelligence['status']
        if gtm_status in RUNNING_GTM_STATES:
            messages.append(f'Deep crawl: {intelligence.get("stage") or "running"}')
        elif gtm_status == 'COMPLETED':
            messages.append('Deep crawl complete')
        elif gtm_status == 'FAILED':
            messages.append('Deep crawl failed')
        elif gtm_status == 'CANCELLED':
            messages.append('Deep crawl cancelled')
        elif gtm_status == 'UNAVAILABLE':
            messages.append('Deep crawl status temporarily unavailable')

    return ok({'company_id': company_id, 'company_status': company.status,
               'job_id': job.id if job else None, 'status': main_status,
               'stage': job.stage if job else 'created', 'current_url':job.current_url if job else None,
               'pages_done':job.pages_done if job else 0,'pages_total': job.pages_total if job else 0,
               'warnings': job.warnings if job else [], 'error': job.error if job else None,
               'overall_status': overall_status, 'messages': messages,
               'intelligence': intelligence,
               'live_browser_url': intelligence.get('live_browser_url') if intelligence else None,
               'url_report': intelligence.get('url_report', []) if intelligence else [],
               'next_action': 'verify_products' if company.status in {'ready_to_verify','complete'} else
                              'add_business_context' if company.status=='needs_context' else 'watch_status'})


@router.get('/{company_id}/conversation')
async def conversation(company_id: int, before: int | None = None, limit: int = 100,
                       db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await owned_company(db, company_id, user.id)
    chat = await onboarding_conversation(db, company_id)
    await db.commit()
    query = select(Message).where(Message.conversation_id == chat.id)
    if before is not None:
        query = query.where(Message.id < before)
    items = (await db.scalars(query.order_by(Message.id.desc()).limit(min(max(limit,1),100)))).all()
    items.reverse()
    return ok({'conversation_id':chat.id,'company_id':company_id,
               'messages':[{'id':m.id,'role':m.role,'type':m.type,'content':m.content,
                            'created_at':m.created_at} for m in items],
               'next_before': items[0].id if len(items)==min(max(limit,1),100) else None})


@router.post('/{company_id}/messages')
async def chat_message(company_id: int, body: ChatInput, db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    await owned_company(db, company_id, user.id)
    convo = await onboarding_conversation(db, company_id)
    if body.client_message_id:
        old = await db.scalar(select(Message).where(Message.conversation_id==convo.id,
                           Message.client_message_id==body.client_message_id))
        if old:
            possible=(await db.scalars(select(Message).where(Message.conversation_id==convo.id)
                        .order_by(Message.id.desc()).limit(20))).all()
            reply=next((m for m in possible if (m.content or {}).get('reply_to')==old.id),None)
            return ok({'idempotent_replay':True,'message_id':old.id,
                       'assistant_message_id': reply.id if reply else None,
                       'reply':reply.content if reply else None})
    question = body.message.strip()
    user_message = await message(db, company_id, 'user', 'text', {'text':question}, body.client_message_id)
    await db.commit()  # Never lose user message if provider fails
    recent=(await db.scalars(select(Message).where(Message.conversation_id==convo.id,
                  Message.type=='text').order_by(Message.id.desc()).limit(12))).all()
    history='\n'.join(f'{m.role}: {str((m.content or {}).get("text",""))[:300]}'
                      for m in reversed(recent))[-2600:]
    passages = await retrieve(db,company_id,question,k=5)
    context = '\n'.join(f'SOURCE_ID={p["source_id"]}\n{p["text"][:750]}' for p in passages)
    answer = await json_completion('You answer questions about the current company using ONLY supplied evidence. '
        'If it is not in evidence, say it is unknown. Return {"answer":"...","source_ids":[...]}.',
        f'Conversation history (untrusted):\n{history}\nQuestion={question}\nEvidence:\n{context[:5000]}')
    text = answer.get('answer') or 'I saved your message. I do not have enough source evidence to answer that yet.'
    citations = [int(v) for v in answer.get('source_ids',[]) if str(v).isdigit()] if isinstance(answer.get('source_ids'),list) else []
    allowed = {p['source_id'] for p in passages}
    ai_message = await message(db, company_id,'assistant','text',
                               {'text':text[:4000], 'source_ids':[i for i in citations if i in allowed],
                                'reply_to':user_message.id})
    await db.commit()
    return ok({'user_message_id':user_message.id,'assistant_message_id':ai_message.id,
               'reply':ai_message.content})


def missing_fields(company: Company, products: list[Product]) -> list[str]:
    gaps=[]
    if not company.business_model:
        gaps.append('company.business_model')
    if not company.icp:
        gaps.append('company.icp')
    if not products:
        gaps.append('company.products')
    else:
        for product in products:
            if product.candidate_kind not in {'standalone_product','suite'}:
                continue
            for field in ('description','key_features','main_use_case'):
                if getattr(product,field) in (None,'',[],{}):
                    gaps.append(f'product.{product.id}.{field}')
    return gaps[:4]


QUESTION_TEMPLATES = {
    'company.business_model':'How does your business charge customers (subscriptions, usage, services or other)?',
    'company.icp':'Who is your primary ideal customer, and what problem do you solve for them?',
    'company.products':'What are the names of your sellable products or services?'
}
async def _gap_passages(
    db: AsyncSession,
    company_id: int,
    field_key: str,
    product: Product | None = None,
) -> list[dict]:

    types = page_types_for(field_key)
    name = product.name if product else ""

    search_queries = {
        "business_model": "subscription pricing billing plans",
        "icp": "ideal customer target audience teams businesses",
        "key_features": f"{name} product features capabilities",
        "main_use_case": f"{name} product use cases benefits",
        "description": f"{name} description software product",
        "products": "our products offerings platforms",
    }

    kind = field_key.rsplit(".", 1)[-1]

    search = search_queries.get(
        kind,
        field_key.replace(".", " ").replace("_", " "),
    )

    # First try hybrid RAG.
    found = await retrieve(
        db,
        company_id,
        search,
        product_id=product.id if product else None,
        page_types=types,
        k=7,
    )

    # Source fallback when vector search has poor results.
    sources = (
        await db.scalars(
            select(Source)
            .where(
                Source.company_id == company_id,
                Source.status == "ok",
                Source.page_type.in_(types),
                Source.kind.in_(["website", "document"]),
            )
            .order_by(Source.id.desc())
            .limit(60)
        )
    ).all()

    selected = []
    seen_urls = set()

    for source in sources:
        if not source.text:
            continue

        if source.url in seen_urls:
            continue

        if product:
            if source.product_id not in (None, product.id):
                continue

            if source.page_type == "product":
                visible = (
                    source.url + " " + source.text[:1200]
                ).lower()

                if product.name.lower() not in visible:
                    continue

        seen_urls.add(source.url)
        selected.append(source)

    sources_by_id = {s.id: s for s in selected}

    passages = []
    used_ids = set()

    for chunk in found:
        source_id = chunk["source_id"]
        source = sources_by_id.get(source_id)

        if not source:
            continue

        passages.append({
            **chunk,
            "url": source.url,
            "page_type": source.page_type,
        })

        used_ids.add(source_id)

    # Add real source text, even if the embedding search
    # didn't retrieve the relevant sentence.
    for source in selected[:12]:
        if source.id in used_ids:
            continue

        passages.append({
            "source_id": source.id,
            "url": source.url,
            "page_type": source.page_type,
            "text": source.text[:10000],
            "score": 0,
        })

    return passages


@router.post("/{company_id}/gap-questions")
async def generate_gap(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    company = await owned_company(
        db, company_id, user.id
    )

    products = (
        await db.scalars(
            select(Product).where(
                Product.company_id == company_id
            )
        )
    ).all()

    # Always calculate CURRENT missing fields.
    needed = missing_fields(company, products)

    products_by_id = {p.id: p for p in products}
    intel_ctx = _intel_context(company, list(products))

    # Load old questions, but do not automatically reuse them.
    existing = (
        await db.scalars(
            select(GapQuestion)
            .where(
                GapQuestion.company_id == company_id,
                GapQuestion.status == "open",
            )
            .order_by(GapQuestion.id)
            .with_for_update()
        )
    ).all()

    existing_by_key = {}
    resolved = 0

    for question in existing:

        # Close questions if extraction or user edits
        # already filled the corresponding field.
        if (
            question.field_key not in needed
            or question.field_key in existing_by_key
        ):
            question.status = "resolved"
            resolved += 1

        else:
            existing_by_key[question.field_key] = question

    # Nothing missing. Proceed to verification.
    if not needed:
        await db.commit()

        return ok({
            "questions": [],
            "reused": False,
            "resolved_stale_questions": resolved,
            "next_action": "verify_products",
        })

    result = []
    created_new = False

    for field_key in needed:

        product = None

        if field_key.startswith("product."):
            product_id = int(field_key.split(".")[1])
            product = products_by_id.get(product_id)

        product_name = (
            product.name if product else "this product"
        )

        question_text = _intel_aware_question(
            field_key, product_name, intel_ctx
        )

        old_question = existing_by_key.get(field_key)

        # Even existing questions with empty suggestions
        # must be regenerated.
        if old_question and old_question.suggestions:
            result.append({
                "id": old_question.id,
                "field": field_key,
                "question": question_text,
                "suggestions": old_question.suggestions,
            })
            continue

        passages = await _gap_passages(
            db,
            company_id,
            field_key,
            product,
        )

        ai = {}

        if passages:
            evidence = "\n".join(
                (
                    f'SOURCE_ID={p["source_id"]} '
                    f'PAGE_TYPE={p["page_type"]} '
                    f'TEXT={p["text"][:650]}'
                )
                for p in passages[:7]
            )

            intel_summary = ""
            if intel_ctx:
                intel_summary = (
                    f"Company intelligence (already known, use to write specific suggestions):\n"
                    f"{json.dumps(intel_ctx, default=str)[:800]}\n\n"
                )

            ai = await json_completion(
                (
                    "Return JSON only. Generate up to three specific, "
                    "actionable answer suggestions for the missing field. "
                    "Use the company intelligence context to make suggestions "
                    "concrete — reference the company's own products, use cases, "
                    "and terminology. Every suggestion MUST cite an exact quote "
                    "from the evidence with its source_id. "
                    "Do not invent information not in the evidence."
                ),
                (
                    f"{intel_summary}"
                    f"Field={field_key}\n"
                    f"Question={question_text}\n"
                    f"Evidence:\n{evidence}\n"
                    'Return {"suggestions":['
                    '{"text":"...","quote":"...",'
                    '"source_id":123}]}'
                ),
            )

        # AI answers + evidence-based fallback.
        suggestions = merge_suggestions(
            field_key,
            passages,
            ai,
        )

        if old_question:
            old_question.question = question_text
            old_question.suggestions = suggestions
            question = old_question

        else:
            question = GapQuestion(
                company_id=company_id,
                field_key=field_key,
                question=question_text,
                suggestions=suggestions,
            )

            db.add(question)
            await db.flush()
            created_new = True

        result.append({
            "id": question.id,
            "field": field_key,
            "question": question_text,
            "suggestions": suggestions,
        })

    # Preserve the onboarding conversation timeline.
    if created_new or resolved:
        await message(
            db,
            company_id,
            "assistant",
            "gap_questions",
            {"questions": result},
        )

    await db.commit()

    return ok({
        "questions": result,
        "reused": not created_new,
        "resolved_stale_questions": resolved,
        "allow_custom_input": True,
        "next_action": "answer_gaps",
    })


@router.post('/{company_id}/gap-answers')
async def answer_gaps(company_id:int, body:GapAnswers, db:AsyncSession=Depends(get_db),
                      user:User=Depends(current_user)):
    company=await owned_company(db,company_id,user.id)
    updates=[]
    for answer in body.answers:
        q=await db.scalar(select(GapQuestion).where(GapQuestion.id==answer.question_id,
                                 GapQuestion.company_id==company_id).with_for_update())
        if not q or q.status != 'open':
            raise HTTPException(409,detail={'code':'INVALID_GAP_QUESTION','message':str(answer.question_id)})
        text=answer.answer.strip()
        # If user selected suggestion, keep its cited evidence but record user confirmation.
        chosen=next((s for s in q.suggestions if s['text']==text),None)
        if q.field_key.startswith('company.'):
            field=q.field_key.split('.',1)[1]
            if field in {'business_model','icp','market_segment','value_proposition'}:
                setattr(company,field,{'summary':text} if field != 'icp' else {'target_audience':text})
                company.field_meta=field_meta_set(company.field_meta,field,status='user_provided',
                                                   review='accepted',confidence=100)
            elif field=='products':
                for name in [x.strip() for x in re.split(r'[,;\n]',text)[:10]]:
                    if not safe_name(name):
                        continue
                    previous=await db.scalar(select(Product).where(Product.company_id==company_id,
                                                                   Product.name==name))
                    if not previous:
                        db.add(Product(company_id=company_id,name=name,candidate_kind='standalone_product',
                            field_meta=field_meta_set({},'name',status='user_provided',
                                                      review='accepted',confidence=100)))
        elif q.field_key.startswith('product.'):
            _,ident,field=q.field_key.split('.',2)
            p=await db.scalar(select(Product).where(Product.id==int(ident),Product.company_id==company_id))
            if p and field in {'description','main_use_case','key_features'}:
                setattr(p,field,[v.strip() for v in text.split(';') if v.strip()] if field=='key_features' else text)
                p.field_meta=field_meta_set(p.field_meta,field,status='user_provided',review='accepted',confidence=100)
                p.revision+=1
        db.add(GapAnswer(question_id=q.id,actor_id=user.id,raw_text=text,normalized_value={'value':text,
                            'source_id':chosen.get('source_id') if chosen else None}))
        q.status='answered'
        updates.append({'field':q.field_key,'value':text})
    company.revision+=1
    products=(await db.scalars(select(Product).where(Product.company_id==company_id))).all()
    is_ready=enough(company,products)
    company.status='ready_to_verify' if is_ready else 'needs_context'
    await message(db,company_id,'user','gap_answers',{'answers':updates})
    await message(db,company_id,'assistant','verification_ready' if is_ready else 'research_attention',
                  {'next_action':'verify_products' if is_ready else 'add_business_context'})
    await db.commit()
    return ok({'answered':len(body.answers),'next_action':'verify_products' if is_ready else 'add_business_context'})


@router.post('/{company_id}/continue')
async def continue_with_website(company_id:int, db:AsyncSession=Depends(get_db),
                                user:User=Depends(current_user)):
    company=await owned_company(db,company_id,user.id)
    company.status='ready_to_verify'
    await message(db,company_id,'user','action',{'action':'continue_website_data'})
    await message(db,company_id,'assistant','verification_ready',{'next_action':'verify_products'})
    await db.commit()
    return ok({'company_id':company_id,'next_action':'verify_products',
               'verification_url':f'/workspace/verification?company_id={company_id}'})
