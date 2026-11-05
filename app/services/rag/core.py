"""One small ONNX embedding model and hybrid pgvector+Postgres FTS (RRF)."""
import asyncio
import re
from functools import lru_cache
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.entities import Chunk, Source


@lru_cache(maxsize=1)
def embedder():
    from fastembed import TextEmbedding
    return TextEmbedding('BAAI/bge-small-en-v1.5')


async def embed_texts(texts: list[str]) -> list[list[float]]:
    def run():
        return [v.tolist() for v in embedder().embed(texts, batch_size=16)]
    return await asyncio.to_thread(run)


def chunk_text(text: str, size: int = 1000, overlap: int = 120) -> list[str]:
    """Split on boundaries when possible. 1000 chars fits 512-token encoder context better."""
    lines = re.split(r'\n\s*\n|(?<=[.!?])\s+(?=[A-Z])', text or '')
    out: list[str] = []
    buffer = ''
    for line in lines:
        line = line.strip()
        if not line:
            continue
        while len(line) > size:
            if buffer:
                out.append(buffer); buffer = ''
            out.append(line[:size])
            line = line[size - overlap:]
        if len(buffer) + len(line) + 1 > size and buffer:
            out.append(buffer)
            buffer = buffer[-overlap:] + '\n' if overlap else ''
        buffer += line + '\n'
    if buffer.strip():
        out.append(buffer.strip())
    return out


async def index_source(db: AsyncSession, source: Source) -> dict:
    existing = await db.scalar(select(Chunk.id).where(Chunk.source_id == source.id).limit(1))
    if existing:
        return {'chunks': 0, 'status': 'unchanged'}
    if source.status != 'ok' or len(source.text.strip()) < 40:
        return {'chunks': 0, 'status': 'skipped'}
    chunks = chunk_text(source.text)
    try:
        vectors = await embed_texts(chunks)
        status = 'embedded'
    except Exception:  # FTS-only is an explicit degraded mode, no synthetic vectors
        vectors = [None] * len(chunks)
        status = 'fts_only'
    for i, (text, vector) in enumerate(zip(chunks, vectors)):
        db.add(Chunk(company_id=source.company_id, product_id=source.product_id, source_id=source.id,
                     idx=i, text=text, page_type=source.page_type, embedding=vector))
    await db.flush()
    return {'chunks': len(chunks), 'status': status}


async def retrieve(db: AsyncSession, company_id: int, query: str, *, product_id: int | None = None,
                   page_types: tuple[str,...] | None = None, source_ids: list[int] | None = None, k: int = 7) -> list[dict]:
    """RRF rank union from independent FTS and cosine top-20 lists."""
    conditions = [Chunk.company_id == company_id]
    if product_id is not None:
        conditions.append((Chunk.product_id == product_id) | (Chunk.product_id.is_(None)))
    if page_types:
        conditions.append(Chunk.page_type.in_(page_types))
    if source_ids is not None:
        if not source_ids:
            return []
        conditions.append(Chunk.source_id.in_(source_ids))
    tsquery = func.plainto_tsquery('english', query)
    lex = (await db.execute(select(Chunk.id, Chunk.source_id, Chunk.text, Chunk.page_type)
                            .where(*conditions, Chunk.tsv.op('@@')(tsquery))
                            .order_by(func.ts_rank_cd(Chunk.tsv, tsquery).desc()).limit(20))).all()
    vec = []
    try:
        embedded = (await embed_texts([query]))[0]
        distance = Chunk.embedding.cosine_distance(embedded)
        vec = (await db.execute(select(Chunk.id, Chunk.source_id, Chunk.text, Chunk.page_type)
                .where(*conditions, Chunk.embedding.is_not(None), distance < 0.58)
                .order_by(distance).limit(20))).all()
    except Exception:
        pass
    scores: dict[int, float] = {}
    item_by_id: dict[int, object] = {}
    for items in (lex, vec):
        for rank, item in enumerate(items):
            scores[item.id] = scores.get(item.id, 0) + 1 / (60 + rank + 1)
            item_by_id[item.id] = item
    ranked = sorted(scores, key=lambda key: scores[key], reverse=True)[:k]
    # No arbitrary fallback chunks: irrelevant docs can poison gap questions and pricing.
    return [{'chunk_id': idx, 'source_id': item_by_id[idx].source_id,
             'text': item_by_id[idx].text, 'page_type': item_by_id[idx].page_type,
             'score': round(scores.get(idx,0), 6)} for idx in ranked]
