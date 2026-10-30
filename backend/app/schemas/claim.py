import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from app.models import (
    ChunkSection,
    ClaimType,
    Direction,
    DocType,
    ExtractionRunStatus,
    SpeakerRole,
)


class ExtractionRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    model: str
    prompt_version: str
    status: ExtractionRunStatus
    chunks_total: int
    chunks_skipped: int
    chunks_processed: int
    claims_created: int
    claims_rejected: int
    tokens_in: int
    tokens_out: int
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    document_id: uuid.UUID
    chunk_id: uuid.UUID | None
    extraction_run_id: uuid.UUID | None
    period_label: str
    quote: str
    speaker: str | None
    page_number: int
    metric_raw: str
    metric_normalized: str | None
    claim_type: ClaimType
    target_low: Decimal | None
    target_high: Decimal | None
    unit: str | None
    direction: Direction
    timeframe_raw: str | None
    target_fiscal_year: int | None
    target_fiscal_quarter: int | None
    timeframe_is_range: bool
    extraction_confidence: float
    mention_count: int
    validation_notes: str | None
    model: str | None
    prompt_version: str | None
    created_at: datetime

    @field_validator("target_low", "target_high", mode="after")
    @classmethod
    def _plain_decimal(cls, value: Decimal | None) -> Decimal | None:
        # asyncpg decodes numeric 40000 as Decimal("4E+4"); serialise it as "40000".
        return None if value is None else Decimal(format(value, "f"))


class ClaimListResponse(BaseModel):
    company_id: uuid.UUID
    total: int
    limit: int
    offset: int
    claims: list[ClaimRead]


class ClaimSourceChunk(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chunk_index: int
    text: str
    speaker: str | None
    speaker_role: SpeakerRole
    section: ChunkSection
    page_start: int
    page_end: int


class ClaimSourceDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: DocType
    period_label: str
    fiscal_year: int
    fiscal_quarter: int | None
    original_filename: str


class ClaimDetail(ClaimRead):
    document: ClaimSourceDocument
    chunk: ClaimSourceChunk | None
