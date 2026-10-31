import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClaimThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One promise tracked across calls (e.g. "FY27 EBITDA margin 19-20%" said in Q1, Q2, Q3).

    Built by ``services/linking.py``; the target fields mirror the latest mention.
    """

    __tablename__ = "claim_threads"
    __table_args__ = (
        CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name="target_fiscal_quarter_range",
        ),
        CheckConstraint("mention_count >= 1", name="mention_count_positive"),
        Index("ix_claim_threads_company_metric", "company_id", "metric_normalized"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_normalized: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    target_fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen_period: Mapped[str] = mapped_column(String(16), nullable=False)
    last_seen_period: Mapped[str] = mapped_column(String(16), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_dropped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
