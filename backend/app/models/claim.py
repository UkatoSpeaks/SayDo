import uuid
from decimal import Decimal

from sqlalchemy import (
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
from app.models.enums import ClaimType, Direction


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
