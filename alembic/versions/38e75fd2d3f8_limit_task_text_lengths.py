"""limit task text lengths

Revision ID: 38e75fd2d3f8
Revises: 9c3363dee7fb
Create Date: 2026-11-04 01:02:23.890012

Written by hand: Alembic does not notice a new length on a column that had none.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "38e75fd2d3f8"
down_revision: str | Sequence[str] | None = "9c3363dee7fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "task",
        "title",
        existing_type=sa.String(),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.alter_column(
        "task",
        "description",
        existing_type=sa.String(),
        type_=sa.String(length=2000),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "task",
        "description",
        existing_type=sa.String(length=2000),
        type_=sa.String(),
        existing_nullable=True,
    )
    op.alter_column(
        "task",
        "title",
        existing_type=sa.String(length=200),
        type_=sa.String(),
        existing_nullable=False,
    )
