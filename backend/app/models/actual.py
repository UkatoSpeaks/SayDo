import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Actual(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "actuals"
    __table_args__ = (
        # NULLS NOT DISTINCT so annual figures (quarter NULL) are unique too (PG15+).
        UniqueConstraint(
            "company_id",
            "metric_normalized",
            "fiscal_year",
            "fiscal_quarter",
            name="uq_actuals_company_metric_period",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter_range"
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    metric_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_note: Mapped[str | None] = mapped_column(Text)
