"""Semantic search over a company's embedded chunks (pgvector cosine distance)."""

import uuid

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UpstreamServiceError
from app.models import Chunk, Claim, Document
from app.schemas.chunk import SearchRequest, SearchResult
from app.services.embeddings import EmbeddingError, EmbeddingProvider

# Candidates the HNSW index looks at per query (pgvector default: 40).
HNSW_EF_SEARCH = 100


async def search_chunks(
    session: AsyncSession,
    provider: EmbeddingProvider,
    company_id: uuid.UUID,
    request: SearchRequest,
) -> list[SearchResult]:
    try:
        [query_vector] = await provider.embed([request.query], "query")
    except EmbeddingError as exc:
        raise UpstreamServiceError(str(exc), code="embedding_failed") from exc

    distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
    stmt = (
        select(Chunk, Document.period_label, distance)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.company_id == company_id, Chunk.embedding.is_not(None))
    )
    if request.section is not None:
        stmt = stmt.where(Chunk.section == request.section)
    if request.speaker_role is not None:
        stmt = stmt.where(Chunk.speaker_role == request.speaker_role)
    if request.from_period is not None or request.to_period is not None:
        low, high = request.period_range
        # Quarter ordinal; full-year documents (quarter NULL) count as Q4 of their year.
        ordinal = (
            cast(Document.fiscal_year, Integer) * 4
            + cast(func.coalesce(Document.fiscal_quarter, 4), Integer)
            - 1
        )
        if request.from_period is not None:
            stmt = stmt.where(ordinal >= low)
        if request.to_period is not None:
            stmt = stmt.where(ordinal <= high)
    stmt = stmt.order_by(distance, Chunk.id).limit(request.top_k)

    # With filters, a plain HNSW scan can return fewer than top_k rows; iterative
    # scans (pgvector >= 0.8) keep searching until enough rows pass the filters.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH:d}"))
    rows = (await session.execute(stmt)).all()
    threads = await _threads_by_chunk(session, [chunk.id for chunk, _, _ in rows])
    return [
        SearchResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            score=1.0 - float(dist),
            text=chunk.text,
            speaker=chunk.speaker,
            speaker_role=chunk.speaker_role,
            section=chunk.section,
            period_label=label,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            thread_id=threads.get(chunk.id),
        )
        for chunk, label, dist in rows
    ]


async def _threads_by_chunk(
    session: AsyncSession, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """One claim thread per chunk (the first claim's, by page), for linking a passage to
    the ledger."""
    if not chunk_ids:
        return {}
    rows = await session.execute(
        select(Claim.chunk_id, Claim.thread_id)
        .where(Claim.chunk_id.in_(chunk_ids), Claim.thread_id.is_not(None))
        .order_by(Claim.chunk_id, Claim.page_number, Claim.id)
    )
    out: dict[uuid.UUID, uuid.UUID] = {}
    for chunk_id, thread_id in rows.tuples():
        if chunk_id is not None and thread_id is not None:
            out.setdefault(chunk_id, thread_id)
    return out
