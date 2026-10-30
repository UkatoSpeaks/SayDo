"""Initial schema: companies, documents, pages, chunks, claims, actuals, verifications.

Revision ID: 0001
Revises:
Create Date: 2026-10-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024

ENUMS: dict[str, tuple[str, ...]] = {
    "doc_type": ("transcript", "results", "presentation", "annual_report"),
    "document_status": ("uploaded", "parsed", "chunked", "embedded", "extracted", "failed"),
    "speaker_role": ("management", "analyst", "moderator", "unknown"),
    "chunk_section": ("prepared_remarks", "qna", "other"),
    "claim_type": ("numeric", "directional", "qualitative"),
    "direction": ("increase", "decrease", "maintain", "none"),
    "verification_status": ("delivered", "missed", "in_progress", "dropped", "unverifiable"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _id() -> sa.Column[sa.Uuid[object]]:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def _ts(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "companies",
        _id(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("nse_symbol", sa.String(32), nullable=True),
        sa.Column("bse_code", sa.String(16), nullable=True),
        sa.Column("sector", sa.String(128), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
        sa.UniqueConstraint("nse_symbol", name="uq_companies_nse_symbol"),
        sa.UniqueConstraint("bse_code", name="uq_companies_bse_code"),
    )

    op.create_table(
        "documents",
        _id(),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("doc_type", _enum("doc_type"), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("period_label", sa.String(16), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", _enum("document_status"), server_default="uploaded", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_documents_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("file_hash", name="uq_documents_file_hash"),
        sa.CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4",
            name="ck_documents_fiscal_quarter_range",
        ),
    )
    op.create_index("ix_documents_company_id", "documents", ["company_id"])
    op.create_index(
        "ix_documents_company_period", "documents", ["company_id", "fiscal_year", "fiscal_quarter"]
    )

    op.create_table(
        "document_pages",
        _id(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_pages"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_pages_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "document_id", "page_number", name="uq_document_pages_document_id_page_number"
        ),
        sa.CheckConstraint("page_number >= 1", name="ck_document_pages_page_number_positive"),
    )

    op.create_table(
        "chunks",
        _id(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(255), nullable=True),
        sa.Column("speaker_role", _enum("speaker_role"), server_default="unknown", nullable=False),
        sa.Column("section", _enum("chunk_section"), server_default="other", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_chunks_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_chunks_company_id_companies",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_chunks_company_id", "chunks", ["company_id"])
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])

    op.create_table(
        "claims",
        _id(),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("metric_raw", sa.Text(), nullable=False),
        sa.Column("metric_normalized", sa.String(128), nullable=True),
        sa.Column("claim_type", _enum("claim_type"), nullable=False),
        sa.Column("target_low", sa.Numeric(), nullable=True),
        sa.Column("target_high", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("direction", _enum("direction"), server_default="none", nullable=False),
        sa.Column("timeframe_raw", sa.Text(), nullable=True),
        sa.Column("target_fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("target_fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("speaker", sa.String(255), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_claims"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_claims_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_claims_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunks.id"], name="fk_claims_chunk_id_chunks", ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name="ck_claims_target_fiscal_quarter_range",
        ),
        sa.CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="ck_claims_extraction_confidence_range",
        ),
    )
    op.create_index("ix_claims_company_id", "claims", ["company_id"])
    op.create_index("ix_claims_document_id", "claims", ["document_id"])
    op.create_index("ix_claims_chunk_id", "claims", ["chunk_id"])
    op.create_index("ix_claims_metric_normalized", "claims", ["metric_normalized"])

    op.create_table(
        "actuals",
        _id(),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("metric_normalized", sa.String(128), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_actuals"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_actuals_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["documents.id"],
            name="fk_actuals_source_document_id_documents",
            ondelete="SET NULL",
        ),
        # NULLS NOT DISTINCT so annual actuals (quarter NULL) are also unique (PG15+).
        sa.UniqueConstraint(
            "company_id",
            "metric_normalized",
            "fiscal_year",
            "fiscal_quarter",
            name="uq_actuals_company_metric_period",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4",
            name="ck_actuals_fiscal_quarter_range",
        ),
    )

    op.create_table(
        "verifications",
        _id(),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("actual_id", sa.Uuid(), nullable=True),
        sa.Column("status", _enum("verification_status"), nullable=False),
        sa.Column("computed_gap", sa.Numeric(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=False),
        _ts("checked_at"),
        sa.PrimaryKeyConstraint("id", name="pk_verifications"),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name="fk_verifications_claim_id_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actual_id"],
            ["actuals.id"],
            name="fk_verifications_actual_id_actuals",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_verifications_claim_id", "verifications", ["claim_id"])
    op.create_index("ix_verifications_actual_id", "verifications", ["actual_id"])


def downgrade() -> None:
    for table in (
        "verifications",
        "actuals",
        "claims",
        "chunks",
        "document_pages",
        "documents",
        "companies",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS vector")
