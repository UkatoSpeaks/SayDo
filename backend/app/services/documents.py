"""Document ingestion and lookup helpers shared by the API routes."""

import logging
import uuid
from pathlib import PurePath

from fastapi import UploadFile
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.models import Company, DocType, Document, DocumentStatus
from app.services.fiscal import parse_period_label, period_label
from app.services.pdf_parser import parse_document
from app.services.storage import PdfStorage

logger = logging.getLogger(__name__)

_MAX_FILENAME_LEN = 512


async def get_company_or_404(session: AsyncSession, company_id: uuid.UUID) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFoundError(f"Company {company_id} not found.", code="company_not_found")
    return company


async def get_document_or_404(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await session.get(Document, document_id)
    if document is None:
        raise NotFoundError(f"Document {document_id} not found.", code="document_not_found")
    return document


def resolve_period(doc_type: DocType, label: str) -> tuple[int, int | None]:
    """Parse and validate a period label against the document type."""
    try:
        fiscal_year, fiscal_quarter = parse_period_label(label)
    except ValueError as exc:
        raise InvalidInputError(str(exc), code="invalid_period_label") from exc

    if doc_type is DocType.ANNUAL_REPORT and fiscal_quarter is not None:
        raise InvalidInputError(
            "Annual reports must use a full-year period label such as 'FY26'.",
            code="invalid_period_label",
        )
    if doc_type is not DocType.ANNUAL_REPORT and fiscal_quarter is None:
        raise InvalidInputError(
            f"A {doc_type.value} needs a quarterly period label such as 'Q2FY26'.",
            code="invalid_period_label",
        )
    return fiscal_year, fiscal_quarter


def _clean_filename(filename: str | None) -> str:
    name = PurePath(filename or "").name.strip() or "upload.pdf"
    return name[-_MAX_FILENAME_LEN:]


async def _file_still_referenced(session: AsyncSession, relative_path: str) -> bool:
    result = await session.scalar(select(exists().where(Document.file_path == relative_path)))
    return bool(result)


async def ingest_document(
    session: AsyncSession,
    storage: PdfStorage,
    company: Company,
    upload: UploadFile,
    doc_type: DocType,
    raw_period_label: str,
) -> Document:
    """Store an uploaded PDF, create its ``documents`` row and parse it synchronously."""
    fiscal_year, fiscal_quarter = resolve_period(doc_type, raw_period_label)

    staged = await storage.stage(upload)
    try:
        duplicate_id = await session.scalar(
            select(Document.id).where(Document.file_hash == staged.sha256)
        )
        if duplicate_id is not None:
            raise ConflictError(
                f"This file has already been uploaded as document {duplicate_id}.",
                code="duplicate_document",
            )
        relative_path = storage.commit(staged, company.id)
    except BaseException:
        storage.discard(staged)
        raise

    document = Document(
        company_id=company.id,
        doc_type=doc_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_label=period_label(fiscal_year, fiscal_quarter),
        original_filename=_clean_filename(upload.filename),
        file_path=relative_path,
        file_hash=staged.sha256,
        status=DocumentStatus.UPLOADED,
    )
    session.add(document)
    try:
        await session.flush()
        await parse_document(session, document, storage.resolve(relative_path))
        await session.commit()
    except BaseException as exc:
        await session.rollback()
        # A concurrent upload of the same file may own this path now; keep it if so.
        if not await _file_still_referenced(session, relative_path):
            storage.delete(relative_path)
        if isinstance(exc, IntegrityError):
            raise ConflictError(
                "This file has already been uploaded.", code="duplicate_document"
            ) from exc
        raise

    await session.refresh(document)
    return document


async def delete_document(session: AsyncSession, storage: PdfStorage, document: Document) -> None:
    relative_path = document.file_path
    await session.delete(document)
    await session.commit()
    storage.delete(relative_path)
    logger.info("Deleted document %s", document.id)
