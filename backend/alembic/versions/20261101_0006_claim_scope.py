"""Scope on claims and actuals: "company" (default), "services" or "segment:<name>".

Claims with different scopes are never linked into one thread; an actual is matched to
claims of its own scope, so the unique key on actuals now includes the scope.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "claims",
        sa.Column("scope", sa.String(64), nullable=False, server_default="company"),
    )
    op.add_column(
        "actuals",
        sa.Column("scope", sa.String(64), nullable=False, server_default="company"),
    )
    op.drop_constraint("uq_actuals_company_metric_period", "actuals", type_="unique")
    op.create_unique_constraint(
        "uq_actuals_company_metric_period",
        "actuals",
        ["company_id", "metric_normalized", "scope", "fiscal_year", "fiscal_quarter"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Only company-level actuals fit the old key.
    op.execute("DELETE FROM actuals WHERE scope <> 'company'")
    op.drop_constraint("uq_actuals_company_metric_period", "actuals", type_="unique")
    op.create_unique_constraint(
        "uq_actuals_company_metric_period",
        "actuals",
        ["company_id", "metric_normalized", "fiscal_year", "fiscal_quarter"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column("actuals", "scope")
    op.drop_column("claims", "scope")
