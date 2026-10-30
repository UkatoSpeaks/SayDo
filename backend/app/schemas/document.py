import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import DocType, DocumentStatus


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    doc_type: DocType
    fiscal_year: int
    fiscal_quarter: int | None
    period_label: str
    original_filename: str
    file_hash: str
    page_count: int | None
    status: DocumentStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentPageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    text: str
    char_count: int


class DocumentPagesResponse(BaseModel):
    document_id: uuid.UUID
    page_count: int
    start: int
    end: int
    pages: list[DocumentPageRead]
