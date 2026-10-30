import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from app.api.deps import SessionDep
from app.core.errors import InvalidInputError, NotFoundError
from app.models import Chunk, Claim, ClaimType, Document, MetricNormalized
from app.schemas.claim import (
    ClaimDetail,
    ClaimListResponse,
    ClaimRead,
    ClaimSourceChunk,
    ClaimSourceDocument,
)
from app.schemas.common import ErrorResponse
from app.services.documents import get_company_or_404
from app.services.fiscal import parse_period_label

router = APIRouter(tags=["claims"])

MAX_CLAIMS_PER_REQUEST = 200
DEFAULT_CLAIMS_PER_REQUEST = 50


def _period_ordinal(label: str, name: str, *, upper: bool) -> int:
    try:
        fiscal_year, quarter = parse_period_label(label)
    except ValueError as exc:
        raise InvalidInputError(f"{name}: {exc}", code="invalid_period_label") from exc
    return fiscal_year * 4 + (quarter or (4 if upper else 1)) - 1


def _target_year(raw: str) -> int:
    """Accept "FY27", "2027" or "27"."""
    value = raw.strip()
    if value.isdigit():
        year = int(value)
        return year + 2000 if year < 100 else year
    try:
        fiscal_year, quarter = parse_period_label(value)
    except ValueError as exc:
        raise InvalidInputError(f"target_fy: {exc}", code="invalid_period_label") from exc
    if quarter is not None:
        raise InvalidInputError("target_fy must be a fiscal year such as FY27.")
    return fiscal_year


def _read(claim: Claim, document: Document) -> ClaimRead:
    return ClaimRead.model_validate({**_columns(claim), "period_label": document.period_label})


def _columns(claim: Claim) -> dict[str, object]:
    return {
        field: getattr(claim, field) for field in ClaimRead.model_fields if field != "period_label"
    }


@router.get(
    "/companies/{company_id}/claims",
    response_model=ClaimListResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def list_company_claims(
    company_id: uuid.UUID,
    session: SessionDep,
    metric: MetricNormalized | None = None,
    claim_type: ClaimType | None = None,
    period_from: Annotated[
        str | None, Query(description="Earliest call period, e.g. Q1FY25", examples=["Q1FY25"])
    ] = None,
    period_to: Annotated[
        str | None, Query(description="Latest call period, e.g. Q4FY26", examples=["Q4FY26"])
    ] = None,
    target_fy: Annotated[
        str | None, Query(description="Target fiscal year: FY27, 2027 or 27", examples=["FY27"])
    ] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CLAIMS_PER_REQUEST)] = DEFAULT_CLAIMS_PER_REQUEST,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ClaimListResponse:
    """Claims made on the company's calls, oldest call first, then by page."""
    await get_company_or_404(session, company_id)

    ordinal = Document.fiscal_year * 4 + func.coalesce(Document.fiscal_quarter, 4) - 1
    filters = [Claim.company_id == company_id]
    if metric is not None:
        filters.append(Claim.metric_normalized == metric.value)
    if claim_type is not None:
        filters.append(Claim.claim_type == claim_type)
    if period_from is not None:
        filters.append(ordinal >= _period_ordinal(period_from, "period_from", upper=False))
    if period_to is not None:
        filters.append(ordinal <= _period_ordinal(period_to, "period_to", upper=True))
    if (
        period_from is not None
        and period_to is not None
        and _period_ordinal(period_from, "period_from", upper=False)
        > _period_ordinal(period_to, "period_to", upper=True)
    ):
        raise InvalidInputError("period_from must not be after period_to.")
    if target_fy is not None:
        filters.append(Claim.target_fiscal_year == _target_year(target_fy))
    if min_confidence is not None:
        filters.append(Claim.extraction_confidence >= min_confidence)

    base = select(Claim, Document).join(Document, Claim.document_id == Document.id).where(*filters)
    total = await session.scalar(
        select(func.count()).select_from(Claim).join(Document).where(*filters)
    )
    rows = await session.execute(
        base.order_by(ordinal, Document.created_at, Claim.page_number, Claim.created_at, Claim.id)
        .limit(limit)
        .offset(offset)
    )
    return ClaimListResponse(
        company_id=company_id,
        total=total or 0,
        limit=limit,
        offset=offset,
        claims=[_read(claim, document) for claim, document in rows],
    )


@router.get(
    "/claims/{claim_id}", response_model=ClaimDetail, responses={404: {"model": ErrorResponse}}
)
async def get_claim(claim_id: uuid.UUID, session: SessionDep) -> ClaimDetail:
    """A claim with its source chunk text and the call it came from."""
    claim = await session.get(Claim, claim_id)
    if claim is None:
        raise NotFoundError(f"Claim {claim_id} not found.", code="claim_not_found")
    document = await session.get(Document, claim.document_id)
    assert document is not None  # FK with ON DELETE CASCADE
    chunk = None
    if claim.chunk_id is not None:
        chunk = await session.scalar(
            select(Chunk).options(defer(Chunk.embedding)).where(Chunk.id == claim.chunk_id)
        )
    return ClaimDetail.model_validate(
        {
            **_columns(claim),
            "period_label": document.period_label,
            "document": ClaimSourceDocument.model_validate(document),
            "chunk": ClaimSourceChunk.model_validate(chunk) if chunk is not None else None,
        }
    )
