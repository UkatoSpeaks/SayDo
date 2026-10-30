import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from app.api.deps import EmbedderDep, SessionDep, SessionmakerDep, StorageDep, TrackerDep
from app.core.errors import ConflictError, InvalidInputError
from app.models import Chunk, ChunkSection, Document, DocumentPage, SpeakerRole
from app.schemas.chunk import ChunkListResponse, ChunkRead
from app.schemas.common import ErrorResponse
from app.schemas.document import DocumentPageRead, DocumentPagesResponse, DocumentRead
from app.services.documents import delete_document, get_document_or_404
from app.services.ingestion import is_parsed, process_document

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGES_PER_REQUEST = 50
DEFAULT_PAGES_PER_REQUEST = 20
MAX_CHUNKS_PER_REQUEST = 200
DEFAULT_CHUNKS_PER_REQUEST = 50


@router.get(
    "/{document_id}", response_model=DocumentRead, responses={404: {"model": ErrorResponse}}
)
async def get_document(document_id: uuid.UUID, session: SessionDep) -> Document:
    return await get_document_or_404(session, document_id)


@router.get(
    "/{document_id}/pages",
    response_model=DocumentPagesResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def get_document_pages(
    document_id: uuid.UUID,
    session: SessionDep,
    start: Annotated[int, Query(ge=1, description="First page (1-based, inclusive)")] = 1,
    end: Annotated[
        int | None,
        Query(ge=1, description=f"Last page (inclusive); at most {MAX_PAGES_PER_REQUEST} pages"),
    ] = None,
) -> DocumentPagesResponse:
    document = await get_document_or_404(session, document_id)
    if end is None:
        end = start + DEFAULT_PAGES_PER_REQUEST - 1
    if end < start:
        raise InvalidInputError("'end' must be greater than or equal to 'start'.")
    if end - start + 1 > MAX_PAGES_PER_REQUEST:
        raise InvalidInputError(f"At most {MAX_PAGES_PER_REQUEST} pages can be requested at once.")

    page_count = document.page_count or 0
    end = min(end, max(page_count, start))
    pages = await session.scalars(
        select(DocumentPage)
        .where(
            DocumentPage.document_id == document_id,
            DocumentPage.page_number.between(start, end),
        )
        .order_by(DocumentPage.page_number)
    )
    return DocumentPagesResponse(
        document_id=document.id,
        page_count=page_count,
        start=start,
        end=end,
        pages=[DocumentPageRead.model_validate(p) for p in pages],
    )


@router.post(
    "/{document_id}/process",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Not parsed yet, or already processing"},
    },
)
async def process(
    document_id: uuid.UUID,
    session: SessionDep,
    sessionmaker: SessionmakerDep,
    embedder: EmbedderDep,
    tracker: TrackerDep,
    background_tasks: BackgroundTasks,
    rechunk: Annotated[
        bool, Query(description="Rebuild chunks even if they exist (drops their embeddings)")
    ] = False,
) -> Document:
    """Chunk and embed the document in the background.

    Existing chunks are kept unless ``rechunk=true``, so a retry after an
    embedding failure only embeds the chunks that are missing a vector.
    """
    document = await get_document_or_404(session, document_id)
    if not is_parsed(document):
        raise ConflictError(
            f"Document has not been parsed (status '{document.status.value}').",
            code="document_not_parsed",
        )
    if not tracker.try_start(document_id):
        raise ConflictError("Document is already being processed.", code="processing_in_progress")
    background_tasks.add_task(
        process_document, sessionmaker, embedder, document_id, rechunk=rechunk, tracker=tracker
    )
    return document


@router.get(
    "/{document_id}/chunks",
    response_model=ChunkListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_chunks(
    document_id: uuid.UUID,
    session: SessionDep,
    section: ChunkSection | None = None,
    speaker_role: SpeakerRole | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CHUNKS_PER_REQUEST)] = DEFAULT_CHUNKS_PER_REQUEST,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChunkListResponse:
    await get_document_or_404(session, document_id)
    filters = [Chunk.document_id == document_id]
    if section is not None:
        filters.append(Chunk.section == section)
    if speaker_role is not None:
        filters.append(Chunk.speaker_role == speaker_role)

    total = await session.scalar(select(func.count()).select_from(Chunk).where(*filters))
    rows = await session.execute(
        select(Chunk, Chunk.embedding.is_not(None).label("has_embedding"))
        .options(defer(Chunk.embedding))
        .where(*filters)
        .order_by(Chunk.chunk_index)
        .limit(limit)
        .offset(offset)
    )
    chunks = [
        ChunkRead(
            id=chunk.id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            speaker=chunk.speaker,
            speaker_role=chunk.speaker_role,
            section=chunk.section,
            text=chunk.text,
            token_count=chunk.token_count,
            has_embedding=has_embedding,
        )
        for chunk, has_embedding in rows
    ]
    return ChunkListResponse(
        document_id=document_id, total=total or 0, limit=limit, offset=offset, chunks=chunks
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
async def remove_document(
    document_id: uuid.UUID, session: SessionDep, storage: StorageDep
) -> Response:
    document = await get_document_or_404(session, document_id)
    await delete_document(session, storage, document)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
