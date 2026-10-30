import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import DocType, DocumentStatus

if TYPE_CHECKING:
    from app.models.company import Company


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter_range"
        ),
        Index("ix_documents_company_period", "company_id", "fiscal_year", "fiscal_quarter"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doc_type: Mapped[DocType] = mapped_column(pg_enum(DocType, "doc_type"), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    period_label: Mapped[str] = mapped_column(String(16), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.UPLOADED,
        server_default=DocumentStatus.UPLOADED.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    company: Mapped["Company"] = relationship(back_populates="documents")
    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentPage.page_number",
    )


class DocumentPage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    document: Mapped[Document] = relationship(back_populates="pages")
