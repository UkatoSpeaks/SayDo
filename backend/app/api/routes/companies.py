import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    EmbedderDep,
    LLMDep,
    SessionDep,
    SessionmakerDep,
    SettingsDep,
    StorageDep,
    TrackerDep,
)
from app.core.errors import ConflictError
from app.models import Company, DocType, Document, DocumentStatus
from app.schemas.chunk import SearchRequest, SearchResponse
from app.schemas.common import ErrorResponse
from app.schemas.company import CompanyCreate, CompanyListItem, CompanyRead
from app.schemas.document import DocumentRead
from app.services.documents import get_company_or_404, ingest_document
from app.services.extraction import process_then_extract
from app.services.search import search_chunks
from app.services.threads import company_summaries

router = APIRouter(prefix="/companies", tags=["companies"])


@router.post(
    "",
    response_model=CompanyRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_company(payload: CompanyCreate, session: SessionDep) -> Company:
    identifiers = []
    if payload.nse_symbol:
        identifiers.append(Company.nse_symbol == payload.nse_symbol)
    if payload.bse_code:
        identifiers.append(Company.bse_code == payload.bse_code)
    if identifiers:
        existing = await session.scalar(select(Company).where(or_(*identifiers)))
        if existing is not None:
            raise ConflictError(
                f"A company with this NSE symbol or BSE code already exists ({existing.id}).",
                code="company_exists",
            )

    company = Company(**payload.model_dump())
    session.add(company)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "A company with this NSE symbol or BSE code already exists.", code="company_exists"
        ) from exc
    await session.refresh(company)
    return company


@router.get("", response_model=list[CompanyListItem])
async def list_companies(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CompanyListItem]:
    """Companies with their Say-Do ratio against latest targets, the number of promises
    still open and the latest period with data."""
    result = await session.scalars(
        select(Company).order_by(Company.name, Company.id).limit(limit).offset(offset)
    )
    return await company_summaries(session, list(result))


@router.get("/{company_id}", response_model=CompanyRead, responses={404: {"model": ErrorResponse}})
async def get_company(company_id: uuid.UUID, session: SessionDep) -> Company:
    return await get_company_or_404(session, company_id)


@router.post(
    "/{company_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Duplicate file"},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse, "description": "Not a PDF"},
        422: {"model": ErrorResponse},
    },
)
async def upload_document(
    company_id: uuid.UUID,
    session: SessionDep,
    storage: StorageDep,
    sessionmaker: SessionmakerDep,
    embedder: EmbedderDep,
    llm: LLMDep,
    settings: SettingsDep,
    tracker: TrackerDep,
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File(description="PDF file")],
    doc_type: Annotated[DocType, Form()],
    period_label: Annotated[str, Form(max_length=32, examples=["Q2FY26"])],
) -> Document:
    """Upload a PDF. It is parsed before responding; transcripts are then chunked
    and embedded in the background (poll ``GET /documents/{id}`` for the status).
    Claims are extracted afterwards only when ``AUTO_EXTRACT`` is on; otherwise
    call ``POST /documents/{id}/extract``."""
    company = await get_company_or_404(session, company_id)
    document = await ingest_document(session, storage, company, file, doc_type, period_label)
    if (
        document.doc_type is DocType.TRANSCRIPT
        and document.status is DocumentStatus.PARSED
        and tracker.try_start(document.id)
    ):
        background_tasks.add_task(
            process_then_extract,
            sessionmaker,
            embedder,
            llm,
            settings,
            document.id,
            tracker=tracker,
        )
    return document


@router.get(
    "/{company_id}/documents",
    response_model=list[DocumentRead],
    responses={404: {"model": ErrorResponse}},
)
async def list_company_documents(
    company_id: uuid.UUID,
    session: SessionDep,
    doc_type: DocType | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> list[Document]:
    await get_company_or_404(session, company_id)

    query = select(Document).where(Document.company_id == company_id)
    if doc_type is not None:
        query = query.where(Document.doc_type == doc_type)
    # A full-year document (quarter NULL) sorts after Q4 of the same fiscal year.
    if order == "asc":
        query = query.order_by(
            Document.fiscal_year.asc(),
            Document.fiscal_quarter.asc().nulls_last(),
            Document.created_at.asc(),
        )
    else:
        query = query.order_by(
            Document.fiscal_year.desc(),
            Document.fiscal_quarter.desc().nulls_first(),
            Document.created_at.desc(),
        )
    return list(await session.scalars(query))


@router.post(
    "/{company_id}/search",
    response_model=SearchResponse,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def search(
    company_id: uuid.UUID, payload: SearchRequest, session: SessionDep, embedder: EmbedderDep
) -> SearchResponse:
    """Semantic search over the company's embedded chunks, most similar first."""
    await get_company_or_404(session, company_id)
    results = await search_chunks(session, embedder, company_id, payload)
    return SearchResponse(query=payload.query, results=results)
