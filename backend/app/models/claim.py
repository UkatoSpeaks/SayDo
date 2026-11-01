import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import ChangeType, ClaimType, Direction


class Claim(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name="target_fiscal_quarter_range",
        ),
        CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="extraction_confidence_range",
        ),
        CheckConstraint("mention_count >= 1", name="mention_count_positive"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"), index=True
    )
    metric_raw: Mapped[str] = mapped_column(Text, nullable=False)
    metric_normalized: Mapped[str | None] = mapped_column(String(128), index=True)
    claim_type: Mapped[ClaimType] = mapped_column(pg_enum(ClaimType, "claim_type"), nullable=False)
    target_low: Mapped[Decimal | None] = mapped_column(Numeric)
    target_high: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[Direction] = mapped_column(
        pg_enum(Direction, "direction"),
        nullable=False,
        default=Direction.NONE,
        server_default=Direction.NONE.value,
    )
    timeframe_raw: Mapped[str | None] = mapped_column(Text)
    target_fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    target_fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(255))
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # True when the target spans several periods ("next two years"); the fiscal
    # year/quarter is then the last one.
    timeframe_is_range: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # How many times the claim was made in the document (near-duplicates merged).
    mention_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # What part of the company the claim is about: "company", "services" or
    # "segment:<name>" (services/scope.py). Claims with different scopes never share a thread.
    scope: Mapped[str] = mapped_column(
        String(64), nullable=False, default="company", server_default="company"
    )
    validation_notes: Mapped[str | None] = mapped_column(Text)
    # Set by linking (services/linking.py).
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("claim_threads.id", ondelete="SET NULL"), index=True
    )
    change_type: Mapped[ChangeType | None] = mapped_column(pg_enum(ChangeType, "change_type"))
