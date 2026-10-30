import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import ChunkSection, SpeakerRole

# gemini-embedding-001 with output_dimensionality=768. Changing it needs a migration.
EMBEDDING_DIM = 768


class Chunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        Index("ix_chunks_company_section_role", "company_id", "section", "speaker_role"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Position of the chunk within its document (0-based).
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
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
    # Set once claim extraction has handled this chunk, so a paused run can resume.
    claims_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
