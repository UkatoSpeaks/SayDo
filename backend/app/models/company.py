from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.document import Document


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    nse_symbol: Mapped[str | None] = mapped_column(String(32), unique=True)
    bse_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    sector: Mapped[str | None] = mapped_column(String(128))

    documents: Mapped[list["Document"]] = relationship(
        back_populates="company", passive_deletes=True
    )
