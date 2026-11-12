import hashlib
from pathlib import Path

from sqlalchemy import select
from app.core.config import settings
from app.core.db import Session
from app.models.entities import Project
from app.models.competitors import (
    CompetitorJob,
    CompetitorCandidate,
    CompetitorSource,
    CompetitorChunk,
    now,
)
from app.services.rag.core import (
    chunk_text,
    embed_texts,
)

from app.competitors.query_generator import (
    generate_queries,
)
from app.competitors.candidates import (
    search_all,
    registered_domain,
)
from app.competitors.scraper import (
    crawl_competitor,
)
from app.competitors.judge import (
    extract_competitor_profile,
    score_competitor,
)


async def update_job(job_id: int, **values):
    async with Session() as db:
        job = await db.get(CompetitorJob, job_id)

        if not job:
            return

        for key, value in values.items():
            setattr(job, key, value)

        job.updated_at = now()
        await db.commit()


async def save_sources(
    candidate_id: int,
    pages: list,
) -> list[dict]:
    result = []

    async with Session() as db:
        candidate = await db.get(
            CompetitorCandidate, candidate_id
        )

        if not candidate:
            return []

        for page in pages:
            # Include title/H1 as part of the stored
            # evidence text so quotes can be checked.
            text = "\n".join([
                page.title or "",
                page.h1 or "",
                page.text or "",
            ]).strip()[:60000]

            if len(text) < 100:
                continue

            digest = hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()

            source = await db.scalar(
                select(CompetitorSource).where(
                    CompetitorSource.candidate_id == (
                        candidate_id
                    ),
                    CompetitorSource.url == page.url,
                    CompetitorSource.content_hash == digest,
                )
            )

            if source is None:
                source = CompetitorSource(
                    company_id=candidate.company_id,
                    product_id=candidate.product_id,
                    project_id=candidate.project_id,
                    candidate_id=candidate.id,
                    url=page.url,
                    page_type=page.page_type,
                    content_hash=digest,
                    text=text,
                    structured=page.structured,
                    status="ok",
                )

                db.add(source)
                await db.flush()

                # Same small embedding model as onboarding.
                chunks = chunk_text(text)[:30]

                try:
                    vectors = await embed_texts(chunks)
                except Exception:
                    # FTS remains available with NULL vectors.
                    vectors = [None] * len(chunks)

                for idx, (chunk, vector) in enumerate(
                    zip(chunks, vectors)
                ):
                    db.add(
                        CompetitorChunk(
                            project_id=candidate.project_id,
                            candidate_id=candidate.id,
                            source_id=source.id,
                            idx=idx,
                            text=chunk,
                            page_type=page.page_type,
                            embedding=vector,
                        )
                    )

                folder = (
                    settings().data_dir
                    / "competitors"
                    / str(candidate.project_id)
                    / str(candidate.id)
                )

                folder.mkdir(
                    parents=True, exist_ok=True
                )

                filename = (
                    f"{source.id}_{digest[:12]}.txt"
                )

                (folder / filename).write_text(
                    text, encoding="utf-8"
                )

            result.append({
                "source_id": source.id,
                "url": source.url,
                "page_type": source.page_type,
                "text": source.text,
            })

        await db.commit()

    return result


async def candidate_sources(
    candidate_id: int,
) -> list[dict]:
    """
    Latest saved content per URL; avoids mixing
    multiple historical versions in one LLM call.
    """
    async with Session() as db:
        rows = (
            await db.scalars(
                select(CompetitorSource)
                .where(
                    CompetitorSource.candidate_id
                    == candidate_id,
                    CompetitorSource.status == "ok",
                )
                .order_by(CompetitorSource.id.desc())
            )
        ).all()

    seen = set()
    result = []

    for source in rows:
        if source.url in seen:
            continue

        seen.add(source.url)

        result.append({
            "source_id": source.id,
            "url": source.url,
            "page_type": source.page_type,
            "text": source.text,
        })

    # Homepage, product and pricing should appear
    # before lower-priority pages in model context.
    order = {
        "home": 0,
        "product": 1,
        "features": 2,
        "pricing": 3,
        "about": 4,
    }

    result.sort(
        key=lambda x: (
            order.get(x["page_type"], 10),
            x["source_id"],
        )
    )

    return result[:4]


async def _load_intelligence_context(company_id: int) -> dict:
    """Phase 3B: load intelligence data to enrich competitor queries."""
    try:
        from app.models.entities import Company
        from app.models.intelligence import IntelligenceLink
        async with Session() as db:
            link = await db.get(IntelligenceLink, company_id)
            if not link:
                return {}
            company = await db.get(Company, company_id)
            if not company:
                return {}

        context = {}
        if company.industry:
            context["industry"] = company.industry
        vp = company.value_proposition or {}
        if vp.get("selling_points"):
            context["selling_points"] = vp["selling_points"]
        if vp.get("key_messages"):
            context["key_messages"] = vp["key_messages"]
        if vp.get("differentiators"):
            context["differentiators"] = vp["differentiators"]
        if vp.get("customer_evidence"):
            customers = []
            for ev in vp["customer_evidence"]:
                if isinstance(ev, dict) and ev.get("type") == "customer":
                    val = ev.get("value")
                    if isinstance(val, list):
                        customers.extend(str(v)[:100] for v in val[:5])
                    elif isinstance(val, str):
                        customers.append(val[:100])
            if customers:
                context["named_customers"] = customers[:5]
        ms = company.market_segment or {}
        if ms.get("target_segments"):
            context["target_segments"] = ms["target_segments"]
        if ms.get("target_industries"):
            context["target_industries"] = ms["target_industries"]
        return context
    except Exception:
        return {}


async def process_discovery(job_id: int):
    async with Session() as db:
        job = await db.get(
            CompetitorJob, job_id
        )

        if not job:
            raise ValueError("Discovery job missing")

        project = await db.get(
            Project, job.project_id
        )

        if not project:
            raise ValueError("Project missing")

        snapshot = project.snapshot or {}

        company_id = project.company_id
        product_id = project.product_id
        project_id = project.id

    if not snapshot.get("product"):
        raise ValueError(
            "Project is missing its verified snapshot"
        )

    intelligence = await _load_intelligence_context(
        company_id
    )
    if intelligence:
        snapshot = {**snapshot, "intelligence": intelligence}

    # Phase 1: generate queries.
    await update_job(
        job_id,
        status="running",
        stage="generating_queries",
    )

    queries = await generate_queries(
        snapshot,
        count=min(
            settings().competitor_max_queries,
            5,
        ),
    )

    await update_job(
        job_id,
        queries=queries,
        stage="searching",
    )

    own_domains = set()

    company_website = (
        snapshot.get("company", {})
        .get("website_url")
    )

    product_website = (
        snapshot.get("product", {})
        .get("website_url")
    )

    for value in (company_website, product_website):
        if value:
            d = registered_domain(value)
            if d:
                own_domains.add(d)

    # Also exclude this company's main domain.
    async with Session() as db:
        from app.models.entities import Company, Product

        company = await db.get(Company, company_id)
        product = await db.get(Product, product_id)

        for item in (
            company.website_url if company else None,
            product.website_url if product else None,
        ):
            if item:
                d = registered_domain(item)
                if d:
                    own_domains.add(d)

    # Phase 2: search and deduplicate.
    candidates, hits, search_errors = await search_all(
        queries,
        own_domains,
        max_candidates=min(
            settings().competitor_max_candidates,
            20,
        ),
    )

    if len(search_errors) == len(queries):
        raise RuntimeError(
            "All search requests failed; check search API key"
        )

    await update_job(
        job_id,
        stage="scraping",
        search_hits=hits,
        candidates_total=len(candidates),
        warnings=[
            {"type": "search", "message": x}
            for x in search_errors
        ],
    )

    confirmed = 0
    completed = 0

    for item in candidates:
        async with Session() as db:
            candidate = await db.scalar(
                select(CompetitorCandidate).where(
                    CompetitorCandidate.project_id
                    == project_id,
                    CompetitorCandidate.domain
                    == item["domain"],
                )
            )

            if candidate is None:
                candidate = CompetitorCandidate(
                    company_id=company_id,
                    product_id=product_id,
                    project_id=project_id,
                    name=item["name"],
                    domain=item["domain"],
                    website_url=item["website_url"],
                    discovered_via=item["hits"],
                    status="pending",
                )
                db.add(candidate)
            else:
                candidate.discovered_via = item["hits"]
                candidate.status = "pending"

            await db.flush()
            candidate_id = candidate.id
            await db.commit()

        try:
            # Phase 3: scrape the candidate's OWN site.
            pages, scrape_warnings = (
                await crawl_competitor(
                    item["website_url"],
                    max_pages=min(
                        settings().competitor_max_pages,
                        6,
                    ),
                )
            )

            if not pages:
                async with Session() as db:
                    candidate = await db.get(
                        CompetitorCandidate,
                        candidate_id,
                    )
                    candidate.status = "failed"
                    candidate.explanation = (
                        "No readable first-party pages"
                    )
                    await db.commit()
                continue

            await save_sources(candidate_id, pages)

            sources = await candidate_sources(
                candidate_id
            )

            await update_job(
                job_id,
                stage="extracting_and_scoring",
            )

            # Phase 4: extract with field-level evidence.
            profile = await extract_competitor_profile(
                sources
            )

            # Phase 5: LLM product-to-product matching.
            assessment = await score_competitor(
                snapshot,
                profile,
                sources,
            )

            async with Session() as db:
                candidate = await db.get(
                    CompetitorCandidate,
                    candidate_id,
                )

                extracted_name = (
                    profile.get("company_name")
                    or profile.get("product_name")
                )

                if extracted_name:
                    candidate.name = (
                        extracted_name["value"][:255]
                    )

                candidate.profile = profile

                candidate.match_percent = (
                    assessment["match_percent"]
                )

                candidate.evidence_coverage = (
                    assessment["evidence_coverage"]
                )

                candidate.score_details = (
                    assessment["score_details"]
                )

                candidate.explanation = (
                    assessment["explanation"]
                )

                candidate.status = (
                    assessment["status"]
                )

                await db.commit()

            if assessment["status"] == "confirmed":
                confirmed += 1

        except Exception as exc:
            async with Session() as db:
                candidate = await db.get(
                    CompetitorCandidate,
                    candidate_id,
                )

                if candidate:
                    candidate.status = "failed"
                    candidate.explanation = (
                        f"Discovery failed: "
                        f"{type(exc).__name__}: "
                        f"{str(exc)[:200]}"
                    )
                    await db.commit()

        finally:
            completed += 1

            await update_job(
                job_id,
                candidates_done=completed,
                confirmed_count=confirmed,
                stage="processing_candidates",
            )

    await update_job(
        job_id,
        status="completed",
        stage="completed",
        finished_at=now(),
        candidates_done=completed,
        confirmed_count=confirmed,
    )