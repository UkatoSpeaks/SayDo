"""Claim extraction: extraction_runs, claim provenance columns, per-chunk resume marker.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATUSES = ("running", "completed", "paused_quota", "failed")


def upgrade() -> None:
    run_status = postgresql.ENUM(*RUN_STATUSES, name="extraction_run_status")
    run_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*RUN_STATUSES, name="extraction_run_status", create_type=False),
            server_default="running",
            nullable=False,
        ),
        sa.Column("chunks_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunks_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunks_processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_created", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_rejected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_extraction_runs_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_runs"),
    )
    op.create_index(
        "ix_extraction_runs_document_started", "extraction_runs", ["document_id", "started_at"]
    )
    op.create_index(
        "uq_extraction_runs_one_running",
        "extraction_runs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.add_column("claims", sa.Column("extraction_run_id", sa.UUID(), nullable=True))
    op.add_column("claims", sa.Column("model", sa.String(length=128), nullable=True))
    op.add_column("claims", sa.Column("prompt_version", sa.String(length=32), nullable=True))
    op.add_column(
        "claims",
        sa.Column("timeframe_is_range", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "claims", sa.Column("mention_count", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("claims", sa.Column("validation_notes", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_claims_extraction_run_id_extraction_runs",
        "claims",
        "extraction_runs",
        ["extraction_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_claims_extraction_run_id", "claims", ["extraction_run_id"])
    op.create_check_constraint("ck_claims_mention_count_positive", "claims", "mention_count >= 1")

    op.add_column(
        "chunks", sa.Column("claims_extracted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("chunks", "claims_extracted_at")

    op.drop_constraint("ck_claims_mention_count_positive", "claims", type_="check")
    op.drop_index("ix_claims_extraction_run_id", table_name="claims")
    op.drop_constraint("fk_claims_extraction_run_id_extraction_runs", "claims", type_="foreignkey")
    for column in (
        "validation_notes",
        "mention_count",
        "timeframe_is_range",
        "prompt_version",
        "model",
        "extraction_run_id",
    ):
        op.drop_column("claims", column)

    op.drop_index("uq_extraction_runs_one_running", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_document_started", table_name="extraction_runs")
    op.drop_table("extraction_runs")
    postgresql.ENUM(name="extraction_run_status").drop(op.get_bind(), checkfirst=True)
