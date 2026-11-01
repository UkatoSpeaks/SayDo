"""Claim type "impact": the size of a one-off charge, headwind or component effect on a
metric ("50 bps impact from the labour code"). Such claims are not targets and are kept
out of threads.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = ("numeric", "directional", "qualitative")


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block on older Postgres versions.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE claim_type ADD VALUE IF NOT EXISTS 'impact'")


def downgrade() -> None:
    # Postgres cannot drop an enum value: rebuild the type without it. Impact claims
    # become qualitative, so they stay out of numeric verification.
    op.execute("UPDATE claims SET claim_type = 'qualitative' WHERE claim_type = 'impact'")
    op.execute("ALTER TYPE claim_type RENAME TO claim_type_old")
    values = ", ".join(f"'{v}'" for v in OLD_VALUES)
    op.execute(f"CREATE TYPE claim_type AS ENUM ({values})")
    op.execute(
        "ALTER TABLE claims ALTER COLUMN claim_type TYPE claim_type "
        "USING claim_type::text::claim_type"
    )
    op.execute("DROP TYPE claim_type_old")
