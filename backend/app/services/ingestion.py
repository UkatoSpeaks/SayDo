"""Chunking + embedding pipeline for parsed documents.

    parsed -> chunk_document -> chunked -> embed_document -> embedded

``embed_document`` commits after every batch, so a crash or API failure keeps
the vectors already stored and the next run only embeds what is missing.
"""

import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models import Chunk, Claim, DocType, Document, DocumentPage, DocumentStatus
from app.services.chunker import ChunkDraft, chunk_pages, chunk_transcript, embedding_text
from app.services.embeddings import EmbeddingError, EmbeddingProvider
from app.services.transcript_cleaner import clean_pages
from app.services.transcript_parser import parse_transcript

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 1000

# Statuses at which chunking has finished (or a later step has).
_CHUNKED_OR_LATER = frozenset(
    {DocumentStatus.CHUNKED, DocumentStatus.EMBEDDED, DocumentStatus.EXTRACTED}
)


class IngestionError(Exception):
    """A document cannot be processed; the message is safe to store and show."""


class ProcessingTracker:
    """Documents being processed by this process, so one isn't processed twice at once.

    In-process only: with several API workers, two could still overlap (the
    result is still consistent, just wasted work).
    """

    def __init__(self) -> None:
        self._active: set[uuid.UUID] = set()

    def try_start(self, document_id: uuid.UUID) -> bool:
        if document_id in self._active:
            return False
        self._active.add(document_id)
        return True

    def finish(self, document_id: uuid.UUID) -> None:
        self._active.discard(document_id)

    def is_active(self, document_id: uuid.UUID) -> bool:
        return document_id in self._active


def is_parsed(document: Document) -> bool:
    """True once text extraction succeeded (also for a later failure, e.g. embedding)."""
    if document.status is DocumentStatus.UPLOADED:
        return False
    if document.status is DocumentStatus.FAILED:
        return document.page_count is not None
    return True


def safe_error_message(exc: BaseException) -> str:
    """Error text for ``documents.error_message``: no secrets, bounded length."""
    if isinstance(exc, IngestionError | EmbeddingError):
        message = str(exc)
    else:
        message = f"{type(exc).__name__}: {exc}"
    key = get_settings().gemini_api_key
    if key is not None and key.get_secret_value():
        message = message.replace(key.get_secret_value(), "[redacted]")
    return message[:MAX_ERROR_MESSAGE_CHARS]


async def _load_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).options(selectinload(Document.company)).where(Document.id == document_id)
    )
    if document is None:
        raise IngestionError(f"Document {document_id} not found.")
    return document


def build_chunks(doc_type: DocType, page_texts: list[str]) -> list[ChunkDraft]:
    cleaned = clean_pages(page_texts).pages
    if doc_type is DocType.TRANSCRIPT:
        parsed = parse_transcript(cleaned)
        if any(turn.speaker is not None for turn in parsed.turns):
            return chunk_transcript(parsed)
        logger.warning("No speaker turns found; falling back to fixed-size chunking")
    return chunk_pages(cleaned)


async def _has_chunks(session: AsyncSession, document_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    return bool(count)


async def chunk_document(session: AsyncSession, document_id: uuid.UUID) -> int:
    """(Re)build the chunks of a parsed document and mark it ``chunked``.

    Idempotent: existing chunks (and their embeddings) are replaced, and so are
    the document's claims, which point at those chunks. Commits.
    Returns the number of chunks.
    """
    document = await _load_document(session, document_id)
    if not is_parsed(document):
        raise IngestionError(
            f"Document must be parsed before chunking (status is '{document.status.value}')."
        )

    pages = await session.scalars(
        select(DocumentPage.text)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    )
    drafts = build_chunks(document.doc_type, list(pages))
    if not drafts:
        raise IngestionError("No text to chunk; the PDF may be scanned and need OCR.")

    await session.execute(delete(Claim).where(Claim.document_id == document_id))
    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    session.add_all(
        Chunk(
            document_id=document.id,
            company_id=document.company_id,
            chunk_index=index,
            page_start=draft.page_start,
            page_end=draft.page_end,
            speaker=draft.speaker[:255] if draft.speaker else None,
            speaker_role=draft.speaker_role,
            section=draft.section,
            text=draft.text,
            token_count=draft.token_count,
        )
        for index, draft in enumerate(drafts)
    )
    if document.status is DocumentStatus.FAILED:
        document.error_message = None
    document.status = DocumentStatus.CHUNKED
    await session.commit()
    logger.info("Chunked document %s into %d chunks", document_id, len(drafts))
    return len(drafts)


async def embed_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    provider: EmbeddingProvider,
    *,
    batch_size: int | None = None,
) -> int:
    """Embed chunks that have no embedding yet, committing after each batch.

    Marks the document ``embedded`` once every chunk has a vector. Returns the
    number of chunks embedded by this call.
    """
    document = await _load_document(session, document_id)
    if not await _has_chunks(session, document_id):
        raise IngestionError("Document has no chunks; chunk it before embedding.")

    company_name = document.company.name
    size = min(batch_size or provider.max_batch_size, provider.max_batch_size)
    embedded = 0
    while True:
        batch = list(
            await session.scalars(
                select(Chunk)
                .where(Chunk.document_id == document_id, Chunk.embedding.is_(None))
                .order_by(Chunk.chunk_index)
                .limit(size)
            )
        )
        if not batch:
            break
        texts = [
            embedding_text(
                chunk.text,
                company=company_name,
                period_label=document.period_label,
                doc_type=document.doc_type,
                speaker=chunk.speaker,
                speaker_role=chunk.speaker_role,
                section=chunk.section,
            )
            for chunk in batch
        ]
        vectors = await provider.embed(texts, "document")
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.embedding = vector
        await session.commit()
        embedded += len(batch)
        logger.info("Document %s: embedded %d chunks so far", document_id, embedded)

    if document.status is not DocumentStatus.EXTRACTED:
        if document.status is DocumentStatus.FAILED:
            document.error_message = None
        document.status = DocumentStatus.EMBEDDED
    await session.commit()
    return embedded


async def process_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: EmbeddingProvider,
    document_id: uuid.UUID,
    *,
    rechunk: bool = False,
    tracker: ProcessingTracker | None = None,
) -> None:
    """Chunk (unless already chunked) and embed a document. Never raises.

    Chunks are kept when they already exist, so a retry after an embedding
    failure resumes instead of starting over; pass ``rechunk=True`` to rebuild.
    On failure the document is marked ``failed`` with the reason.
    """
    try:
        async with sessionmaker() as session:
            document = await _load_document(session, document_id)
            already_chunked = document.status in _CHUNKED_OR_LATER or (
                document.status is DocumentStatus.FAILED and await _has_chunks(session, document_id)
            )
            if rechunk or not already_chunked:
                await chunk_document(session, document_id)
            await embed_document(session, document_id, provider)
    except Exception as exc:
        message = safe_error_message(exc)
        logger.error("Processing document %s failed: %s", document_id, message)
        await _mark_failed(sessionmaker, document_id, message)
    finally:
        if tracker is not None:
            tracker.finish(document_id)


async def _mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: uuid.UUID, message: str
) -> None:
    try:
        async with sessionmaker() as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            document.status = DocumentStatus.FAILED
            document.error_message = message
            await session.commit()
    except Exception:
        logger.error("Could not mark document %s as failed", document_id)
