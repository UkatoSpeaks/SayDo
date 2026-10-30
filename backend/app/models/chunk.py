import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import ChunkSection, SpeakerRole

# mistral-embed output dimension.
EMBEDDING_DIM = 1024


class Chunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(255))
    speaker_role: Mapped[SpeakerRole] = mapped_column(
        pg_enum(SpeakerRole, "speaker_role"),
        nullable=False,
        default=SpeakerRole.UNKNOWN,
        server_default=SpeakerRole.UNKNOWN.value,
    )
    section: Mapped[ChunkSection] = mapped_column(
        pg_enum(ChunkSection, "chunk_section"),
        nullable=False,
        default=ChunkSection.OTHER,
        server_default=ChunkSection.OTHER.value,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
