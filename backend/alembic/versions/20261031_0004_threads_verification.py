"""Claim threads, verification runs and thread-level verifications.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS: dict[str, tuple[str, ...]] = {
    "change_type": (
        "new",
        "reiterated",
        "raised",
        "lowered",
        "narrowed",
        "widened",
        "delayed",
        "pulled_forward",
    ),
    "verification_method": ("deterministic", "llm"),
    "verification_run_kind": ("link", "verify"),
    "verification_run_status": ("running", "completed", "failed"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _counter(name: str) -> sa.Column[sa.Integer]:
    return sa.Column(name, sa.Integer(), server_default="0", nullable=False)


def _uuid_array(name: str) -> sa.Column[postgresql.ARRAY[sa.Uuid[object]]]:
    return sa.Column(name, postgresql.ARRAY(sa.Uuid()), server_default="{}", nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "claim_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("metric_normalized", sa.String(128), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("target_fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("target_fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("first_seen_period", sa.String(16), nullable=False),
        sa.Column("last_seen_period", sa.String(16), nullable=False),
        sa.Column("mention_count", sa.Integer(), nullable=False),
        sa.Column("is_dropped", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim_threads"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_claim_threads_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name="ck_claim_threads_target_fiscal_quarter_range",
        ),
        sa.CheckConstraint("mention_count >= 1", name="ck_claim_threads_mention_count_positive"),
    )
    op.create_index("ix_claim_threads_company_id", "claim_threads", ["company_id"])
    op.create_index(
        "ix_claim_threads_company_metric", "claim_threads", ["company_id", "metric_normalized"]
    )

    op.create_table(
        "verification_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("kind", _enum("verification_run_kind"), nullable=False),
        sa.Column("verifier_version", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column(
            "status", _enum("verification_run_status"), server_default="running", nullable=False
        ),
        sa.Column("as_of_period", sa.String(16), nullable=True),
        _counter("threads_total"),
        _counter("threads_created"),
        _counter("threads_verified"),
        _counter("llm_calls"),
        _counter("embedding_calls"),
        _counter("tokens_in"),
        _counter("tokens_out"),
        sa.Column(
            "status_counts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_verification_runs"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_verification_runs_company_id_companies",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_verification_runs_company_started", "verification_runs", ["company_id", "started_at"]
    )
    op.create_index(
        "uq_verification_runs_one_running",
        "verification_runs",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.add_column("claims", sa.Column("thread_id", sa.Uuid(), nullable=True))
    op.add_column("claims", sa.Column("change_type", _enum("change_type"), nullable=True))
    op.create_foreign_key(
        "fk_claims_thread_id_claim_threads",
        "claims",
        "claim_threads",
        ["thread_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_claims_thread_id", "claims", ["thread_id"])

    # "dropped" is already a verification_status value (revision 0001).
    op.add_column("verifications", sa.Column("thread_id", sa.Uuid(), nullable=True))
    op.add_column("verifications", sa.Column("verification_run_id", sa.Uuid(), nullable=True))
    op.add_column("verifications", sa.Column("method", _enum("verification_method"), nullable=True))
    op.add_column("verifications", sa.Column("as_of_period", sa.String(16), nullable=True))
    op.add_column("verifications", sa.Column("verifier_version", sa.String(32), nullable=True))
    op.add_column("verifications", sa.Column("gap_pct", sa.Numeric(), nullable=True))
    op.add_column(
        "verifications",
        sa.Column("near_miss", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("verifications", _uuid_array("evidence_chunk_ids"))
    op.add_column("verifications", _uuid_array("explanation_chunk_ids"))
    op.add_column("verifications", sa.Column("explanation_summary", sa.Text(), nullable=True))
    op.add_column(
        "verifications",
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "verifications",
        sa.Column("is_latest", sa.Boolean(), server_default="true", nullable=False),
    )
    op.create_foreign_key(
        "fk_verifications_thread_id_claim_threads",
        "verifications",
        "claim_threads",
        ["thread_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_verifications_verification_run_id_verification_runs",
        "verifications",
        "verification_runs",
        ["verification_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_verifications_thread_id", "verifications", ["thread_id"])
    op.create_index(
        "ix_verifications_verification_run_id", "verifications", ["verification_run_id"]
    )
    op.create_index(
        "uq_verifications_one_latest_per_thread",
        "verifications",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )


def downgrade() -> None:
    op.drop_index("uq_verifications_one_latest_per_thread", table_name="verifications")
    op.drop_index("ix_verifications_verification_run_id", table_name="verifications")
    op.drop_index("ix_verifications_thread_id", table_name="verifications")
    op.drop_constraint(
        "fk_verifications_verification_run_id_verification_runs",
        "verifications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_verifications_thread_id_claim_threads", "verifications", type_="foreignkey"
    )
    # Thread-level verifications have no meaning without threads.
    op.execute("DELETE FROM verifications WHERE thread_id IS NOT NULL")
    for column in (
        "is_latest",
        "details",
        "explanation_summary",
        "explanation_chunk_ids",
        "evidence_chunk_ids",
        "near_miss",
        "gap_pct",
        "verifier_version",
        "as_of_period",
        "method",
        "verification_run_id",
        "thread_id",
    ):
        op.drop_column("verifications", column)

    op.drop_index("ix_claims_thread_id", table_name="claims")
    op.drop_constraint("fk_claims_thread_id_claim_threads", "claims", type_="foreignkey")
    op.drop_column("claims", "change_type")
    op.drop_column("claims", "thread_id")

    op.drop_index("uq_verification_runs_one_running", table_name="verification_runs")
    op.drop_index("ix_verification_runs_company_started", table_name="verification_runs")
    op.drop_table("verification_runs")

    op.drop_index("ix_claim_threads_company_metric", table_name="claim_threads")
    op.drop_index("ix_claim_threads_company_id", table_name="claim_threads")
    op.drop_table("claim_threads")

    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
