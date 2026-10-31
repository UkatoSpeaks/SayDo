import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import VerificationRunKind, VerificationRunStatus


def _counter() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, default=0, server_default="0")


class VerificationRun(UUIDPrimaryKeyMixin, Base):
    """One link (threads only) or verify (link + verify) pass over a company."""

    __tablename__ = "verification_runs"
    __table_args__ = (
        Index("ix_verification_runs_company_started", "company_id", "started_at"),
        # At most one running link/verify job per company.
        Index(
            "uq_verification_runs_one_running",
            "company_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[VerificationRunKind] = mapped_column(
        pg_enum(VerificationRunKind, "verification_run_kind"), nullable=False
    )
    verifier_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[VerificationRunStatus] = mapped_column(
        pg_enum(VerificationRunStatus, "verification_run_status"),
        nullable=False,
        default=VerificationRunStatus.RUNNING,
        server_default=VerificationRunStatus.RUNNING.value,
    )
    as_of_period: Mapped[str | None] = mapped_column(String(16))
    threads_total: Mapped[int] = _counter()
    threads_created: Mapped[int] = _counter()
    threads_verified: Mapped[int] = _counter()
    llm_calls: Mapped[int] = _counter()
    embedding_calls: Mapped[int] = _counter()
    tokens_in: Mapped[int] = _counter()
    tokens_out: Mapped[int] = _counter()
    # {"delivered": 3, "missed": 1, ...} for the verifications this run wrote.
    status_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
