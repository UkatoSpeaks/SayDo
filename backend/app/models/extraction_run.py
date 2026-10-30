import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import ExtractionRunStatus


class ExtractionRun(UUIDPrimaryKeyMixin, Base):
    """One claim-extraction pass over a document. Counters cover this run only."""

    __tablename__ = "extraction_runs"
    __table_args__ = (
        Index("ix_extraction_runs_document_started", "document_id", "started_at"),
        # At most one running extraction per document.
        Index(
            "uq_extraction_runs_one_running",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[ExtractionRunStatus] = mapped_column(
        pg_enum(ExtractionRunStatus, "extraction_run_status"),
        nullable=False,
        default=ExtractionRunStatus.RUNNING,
        server_default=ExtractionRunStatus.RUNNING.value,
    )
    # Management chunks still pending when the run started.
    chunks_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    chunks_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    chunks_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claims_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claims_rejected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
