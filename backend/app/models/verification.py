import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import VerificationStatus


class Verification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "verifications"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actual_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actuals.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"), nullable=False
    )
    computed_gap: Mapped[Decimal | None] = mapped_column(Numeric)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
