"""MVP in-process background runner. A durable queue is a later production upgrade."""
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.db import Session
from app.models.entities import Company, ExtractionJob, Product, Source
from app.services.common import domain, message, source_from_text
from app.services.scrape.core import crawl
from app.services.rag.core import index_source
from app.services.extract.core import catalog, derive_company, extract_products, extract_pricing, extract_company_structured
from app.services.llm import start_llm_audit, finish_llm_audit

log = logging.getLogger(__name__)

_MAX_STEPS = 200


def _add_step(job: ExtractionJob, kind: str, **data):
    """Append a timestamped entry to the job's growing progress feed (see
    GET /companies/{id}/status's `steps`). Capped so a very large crawl can't
    grow this JSONB column without bound."""
    entry = {'kind': kind, 'at': datetime.now(timezone.utc).isoformat(), **data}
    job.steps = [*(job.steps or []), entry][-_MAX_STEPS:]


async def process_job(company_id: int, job_id: int, urls: list[str]):
    llm_token = start_llm_audit()
    async def set_progress(url: str, done: int, total: int):
        async with Session() as progress_db:
            j = await progress_db.get(ExtractionJob, job_id)
            if j:
                j.current_url, j.pages_done, j.pages_total = url, done, total
                _add_step(j, 'browsing', url=url, status=f'Opening page {done}/{total}')
                await progress_db.commit()
    try:
        async with Session() as db:
            job = await db.get(ExtractionJob, job_id)
            company = await db.get(Company, company_id)
            if not job or not company:
                return
            job.status, job.stage, company.status = 'running', 'scraping', 'researching'
            _add_step(job, 'thinking', status='Starting company analysis')
            await db.commit()
        for url in urls[:2]:
            try:
                pages, warnings = await crawl(url, progress=set_progress)
            except Exception as exc:
                pages, warnings = [], [{'url': url, 'reason': str(exc)[:200]}]
            async with Session() as db:
                job = await db.get(ExtractionJob, job_id)
                job.warnings = [*(job.warnings or []), *warnings]
                job.pages_done = max(job.pages_done, len(pages))
                for page in pages:
                    await source_from_text(db, company_id, 'website', page.url, page.text,
                        page_type=page.page_type, http_status=200, robots_allowed=True,
                        structured={**page.structured, 'nav':
                            [{'url': link, 'label': text} for link, text in page.links[:80]]},
                        status=page.status)
                _add_step(job, 'browsing', url=url,
                         status=f'Read {len(pages)} page(s)' if pages else 'No pages found')
                await db.commit()
        async with Session() as db:
            job = await db.get(ExtractionJob, job_id)
            company = await db.get(Company, company_id)
            job.stage, job.current_url = 'indexing', None
            _add_step(job, 'thinking', status='Saving source evidence and preparing retrieval')
            await db.commit()
            all_sources = (await db.scalars(select(Source).where(Source.company_id == company_id,
                        Source.status == 'ok').order_by(Source.id))).all()
            # Keep old source versions, but never use a different website's facts
            # as evidence for the current company's identity or products.
            sources = [s for s in all_sources if s.kind != 'website' or
                       not company.domain or domain(s.url) == company.domain]
            if len(sources) != len(all_sources):
                job.warnings = [*(job.warnings or []), {
                    'reason': 'cross_domain_sources_excluded',
                    'count': len(all_sources) - len(sources)}]
            fts_count, vector_degraded = 0, False
            for source in sources:
                stats = await index_source(db, source)
                fts_count += stats['chunks']
                vector_degraded |= stats['status'] == 'fts_only'
            if vector_degraded:
                job.warnings = [*(job.warnings or []), {'reason': 'embedding_degraded_fts_only'}]
            await db.commit()
            job.stage = 'extracting'
            _add_step(job, 'thinking', status='Extracting company, products, pricing and features')
            await db.commit()
            web_sources = [x for x in sources if x.kind in {'website', 'document'}]
            await extract_company_structured(db, company, web_sources)
            products = await catalog(db, company, web_sources)
            await db.commit()
            products = (await db.scalars(select(Product).where(Product.company_id == company_id)
                                        .order_by(Product.id))).all()
            await extract_products(db, company, products, web_sources)
            await extract_pricing(db, company, products, [s for s in web_sources if s.kind == 'website'])
            await derive_company(db, company, web_sources)
            await db.commit()
            products = (await db.scalars(select(Product).where(Product.company_id == company_id))).all()
            llm_issues = finish_llm_audit(llm_token)
            llm_token = None
            if llm_issues:
                job.warnings = [*(job.warnings or []), {
                    'reason': 'llm_unavailable',
                    'detail': llm_issues[0][:220],
                    'note': 'Scraping succeeded, but AI extraction was degraded. Check GET /health/llm and Ollama configuration.'
                }]
            # Deliberately does NOT decide ready_to_verify/needs_context or post the
            # research_attention/verification_ready message here: that decision
            # waits for the intelligence deep-crawl too (if one was started for
            # this company), which this in-process job has no way to know is done.
            # GET .../status makes that call on every poll — see
            # maybe_post_readiness_decision() in app/services/common.py.
            job.status, job.stage, job.completed_at = 'completed', 'completed', datetime.now(timezone.utc)
            job.current_url = None
            _add_step(job, 'thinking', status='Company analysis finished')
            await db.commit()
    except Exception as exc:
        if llm_token is not None:
            finish_llm_audit(llm_token)
        log.exception('Job %s failed', job_id)
        async with Session() as db:
            job = await db.get(ExtractionJob, job_id)
            company = await db.get(Company, company_id)
            if job:
                job.status, job.stage, job.error = 'failed', 'failed', str(exc)[:500]
                job.completed_at = datetime.now(timezone.utc)
                _add_step(job, 'failed', status=f'Research failed: {str(exc)[:200]}')
            if company:
                company.status = 'needs_context'
                await message(db, company_id, 'assistant', 'error', {'text': 'Research failed. Please retry or add a document.',
                                                                      'reason': str(exc)[:200]})
            await db.commit()
