import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import VerificationMethod, VerificationStatus


class Verification(UUIDPrimaryKeyMixin, Base):
    """The outcome of a promise (thread) as of a period. Older rows are kept as history;
    exactly one per thread has ``is_latest``."""

    __tablename__ = "verifications"
    __table_args__ = (
        Index(
            "uq_verifications_one_latest_per_thread",
            "thread_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )

    # The mention whose target was judged (the thread's latest one).
    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("claim_threads.id", ondelete="CASCADE"), index=True
    )
    verification_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verification_runs.id", ondelete="SET NULL"), index=True
    )
    # Set when the actual used was reported (not derived).
    actual_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actuals.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"), nullable=False
    )
    method: Mapped[VerificationMethod | None] = mapped_column(
        pg_enum(VerificationMethod, "verification_method")
    )
    as_of_period: Mapped[str | None] = mapped_column(String(16))
    verifier_version: Mapped[str | None] = mapped_column(String(32))
    # In the claim's unit; negative = short of the target.
    computed_gap: Mapped[Decimal | None] = mapped_column(Numeric)
    gap_pct: Mapped[Decimal | None] = mapped_column(Numeric)
    near_miss: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    explanation_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    # Only with VERIFY_SUMMARIZE_EXPLANATIONS=true.
    explanation_summary: Mapped[str | None] = mapped_column(Text)
    # Snapshot of what was compared: target used, original target, actual (value, unit,
    # period, derived_from), baseline for directional claims, the LLM's quote.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    is_latest: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
