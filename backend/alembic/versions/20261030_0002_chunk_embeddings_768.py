"""Chunks: vector(768) embeddings for gemini-embedding-001, HNSW index, chunk_index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 768
PREVIOUS_EMBEDDING_DIM = 1024


def upgrade() -> None:
    # No embeddings exist yet, so the column is dropped and re-added. Chunks made
    # before this revision have no chunk_index; they are cleared and get rebuilt by
    # POST /documents/{id}/process.
    op.execute("DELETE FROM chunks")
    op.drop_column("chunks", "embedding")
    op.add_column("chunks", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("chunks", sa.Column("chunk_index", sa.Integer(), nullable=False))
    op.create_unique_constraint(
        "uq_chunks_document_id_chunk_index", "chunks", ["document_id", "chunk_index"]
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_chunks_company_section_role", "chunks", ["company_id", "section", "speaker_role"]
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_company_section_role", table_name="chunks")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_constraint("uq_chunks_document_id_chunk_index", "chunks", type_="unique")
    op.drop_column("chunks", "chunk_index")
    op.drop_column("chunks", "embedding")
    op.add_column("chunks", sa.Column("embedding", Vector(PREVIOUS_EMBEDDING_DIM), nullable=True))
