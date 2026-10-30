import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app.api.deps import SessionDep, StorageDep
from app.core.errors import InvalidInputError
from app.models import Document, DocumentPage
from app.schemas.common import ErrorResponse
from app.schemas.document import DocumentPageRead, DocumentPagesResponse, DocumentRead
from app.services.documents import delete_document, get_document_or_404

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGES_PER_REQUEST = 50
DEFAULT_PAGES_PER_REQUEST = 20


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
